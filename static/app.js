const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const page = document.body.dataset.page;

const statusBox = $("#connection-status");
const statusLabel = $("#status-label");
const deviceDot = $("#sidebar-device-dot");
const deviceState = $("#sidebar-device-state");
const logoutButtons = $$(".logout-button");
const toast = $("#toast");

let csrfToken = "";
let toastTimer;
let latestStatus = null;
let consecutiveStatusErrors = 0;
const pendingControls = new Set();

function showToast(message, error = false) {
  if (!toast) return;
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast visible${error ? " error" : ""}`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(path, { cache: "no-store", ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* Empty responses are valid. */ }
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Your session has expired");
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function setConnection(kind, label) {
  if (statusBox) statusBox.className = `status status-${kind}`;
  if (statusBox) {
    statusBox.setAttribute("aria-label", label);
    statusBox.title = label;
  }
  if (statusLabel) statusLabel.textContent = label;
  if (deviceDot) deviceDot.classList.toggle("online", kind === "online");
  if (deviceState) deviceState.textContent = label;
}

function commonStatus(status) {
  if (!status.enabled || status.state === "disabled") {
    setConnection("disabled", "Camera off");
  } else if (status.online) {
    setConnection("online", "Camera online");
  } else if (status.state === "starting") {
    setConnection("waiting", "Starting camera");
  } else {
    setConnection("offline", "Camera offline");
  }
}

function titleCase(value) {
  return String(value).replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(bytes) || 0;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  const precision = index < 2 || amount >= 100 ? 0 : 1;
  return `${amount.toFixed(precision)} ${units[index]}`;
}

function storedBoolean(key, fallback = true) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value === "true";
  } catch (_) {
    return fallback;
  }
}

function storeBoolean(key, value) {
  try { window.localStorage.setItem(key, String(value)); } catch (_) { /* Optional. */ }
}

function visibleInterval(callback, milliseconds) {
  return window.setInterval(() => {
    if (!document.hidden) Promise.resolve(callback()).catch(() => {});
  }, milliseconds);
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    latestStatus = status;
    consecutiveStatusErrors = 0;
    commonStatus(status);
    if (page === "camera") renderCamera(status);
    if (page === "settings") renderSettings(status);
    return status;
  } catch (error) {
    consecutiveStatusErrors += 1;
    if (consecutiveStatusErrors > 1) setConnection("offline", "Service offline");
    throw error;
  }
}

function initializeLogout() {
  for (const button of logoutButtons) {
    button.addEventListener("click", async () => {
      for (const item of logoutButtons) item.disabled = true;
      try {
        await api("/api/logout", { method: "POST" });
      } finally {
        window.location.replace("/login");
      }
    });
  }
}

// Camera page
const viewer = $("#viewer");
const stream = $("#camera-stream");
const overlay = $("#detection-overlay");
const motionOverlay = $("#motion-overlay");
const emptyTitle = $("#empty-title");
const emptyDetail = $("#empty-detail");
const streamDetails = $("#stream-details");
const viewerTime = $("#viewer-time");
const fullscreenButton = $("#fullscreen-button");
const detectionBadge = $("#detection-badge");
const detectionBadgeLabel = $("#detection-badge-label");
const liveStreamValue = $("#live-stream-value");
const captureSourceValue = $("#capture-source-value");
const aiValue = $("#ai-value");
const aiDetail = $("#ai-detail");
const motionValue = $("#motion-value");
const motionDetail = $("#motion-detail");
const recordButton = $("#record-button");
const recordButtonLabel = $("#record-button-label");
const recordingTimer = $("#recording-timer");
const snapshotButton = $("#snapshot-button");
const aiOverlayButton = $("#ai-overlay-toggle");
const motionOverlayButton = $("#motion-overlay-toggle");

let streamRetry;
let latestDetections = [];
let sourceResolution = [1920, 1080];
let showAiOverlay = storedBoolean("sentinel.overlay.ai");
let showMotionOverlay = storedBoolean("sentinel.overlay.motion");

function connectStream() {
  if (!stream) return;
  clearTimeout(streamRetry);
  if (!stream.getAttribute("src")) stream.src = `/stream.mjpg?v=${Date.now()}`;
}

function disconnectStream() {
  if (!stream || !viewer) return;
  clearTimeout(streamRetry);
  stream.removeAttribute("src");
  viewer.classList.remove("has-frame");
}

function setOverlayButton(button, active) {
  if (!button) return;
  button.classList.toggle("active", active);
  button.setAttribute("aria-pressed", String(active));
}

function detectionColor(category) {
  if (category === "person") return "#ff756f";
  if (category === "animal") return "#ffc469";
  return "#63b7ff";
}

function drawDetections() {
  if (!viewer || !overlay) return;
  const rect = viewer.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  overlay.width = Math.round(rect.width * ratio);
  overlay.height = Math.round(rect.height * ratio);
  const context = overlay.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!showAiOverlay || !latestDetections.length || !viewer.classList.contains("has-frame")) return;

  const sourceAspect = sourceResolution[0] / sourceResolution[1];
  const viewerAspect = rect.width / rect.height;
  let imageWidth = rect.width;
  let imageHeight = rect.height;
  let offsetX = 0;
  let offsetY = 0;
  if (viewerAspect > sourceAspect) {
    imageWidth = rect.height * sourceAspect;
    offsetX = (rect.width - imageWidth) / 2;
  } else if (viewerAspect < sourceAspect) {
    imageHeight = rect.width / sourceAspect;
    offsetY = (rect.height - imageHeight) / 2;
  }

  for (const detection of latestDetections) {
    const [x0, y0, x1, y1] = detection.bbox;
    const x = offsetX + x0 * imageWidth;
    const y = offsetY + y0 * imageHeight;
    const width = (x1 - x0) * imageWidth;
    const height = (y1 - y0) * imageHeight;
    const color = detectionColor(detection.category);
    const label = `${titleCase(detection.label)}  ${Math.round(detection.confidence * 100)}%`;
    context.strokeStyle = color;
    context.lineWidth = Math.max(2, rect.width / 650);
    context.strokeRect(x, y, width, height);
    context.font = "600 11px ui-sans-serif, system-ui, sans-serif";
    const labelWidth = context.measureText(label).width + 12;
    const labelY = Math.max(0, y - 23);
    context.fillStyle = color;
    context.fillRect(x, labelY, labelWidth, 23);
    context.fillStyle = "#090b0d";
    context.fillText(label, x + 6, labelY + 16);
  }
}

function renderCameraDetection(detection) {
  if (!detection) return;
  latestDetections = Array.isArray(detection.detections) ? detection.detections : [];
  const motionActive = Boolean(detection.motion?.active);
  motionOverlay?.classList.toggle("hidden", !showMotionOverlay || !motionActive);

  if (!detection.ai?.enabled) {
    aiValue.textContent = "Disabled";
    aiDetail.textContent = "Enable it in Settings";
  } else if (detection.ai.online) {
    aiValue.textContent = detection.ai.backend === "cpu" ? "CPU" : "AI HAT+";
    aiDetail.textContent = detection.ai.model || "Detector online";
  } else if (detection.ai.state === "starting") {
    aiValue.textContent = "Starting";
    aiDetail.textContent = "Loading detector";
  } else {
    aiValue.textContent = "Unavailable";
    aiDetail.textContent = detection.ai.error || "Detector unavailable";
  }

  if (!detection.motion?.enabled) {
    motionValue.textContent = "Disabled";
    motionDetail.textContent = "Enable it in Settings";
  } else {
    motionValue.textContent = motionActive ? "Detected" : "Clear";
    motionDetail.textContent = motionActive ? "Movement detected" : "Monitoring frame changes";
  }

  if (showAiOverlay && latestDetections.length) {
    detectionBadgeLabel.textContent = `${titleCase(latestDetections[0].label)} detected`;
    detectionBadge.classList.remove("hidden");
  } else if (showMotionOverlay && motionActive) {
    detectionBadgeLabel.textContent = "Motion detected";
    detectionBadge.classList.remove("hidden");
  } else {
    detectionBadge?.classList.add("hidden");
  }
  drawDetections();
}

function renderRecording(recording) {
  if (!recordButton) return;
  const active = Boolean(recording?.active);
  recordButton.classList.toggle("recording", active);
  recordButtonLabel.textContent = active ? "Stop recording" : "Start recording";
  recordingTimer.classList.toggle("hidden", !active);
  if (active) recordingTimer.textContent = formatDuration(recording.duration_seconds);
}

function renderCamera(status) {
  if (!viewer) return;
  snapshotButton.setAttribute("aria-disabled", String(!status.online));
  recordButton.disabled = !status.online && !status.recording?.active;
  liveStreamValue.textContent = `${status.live_resolution || status.resolution} · ${Number(status.live_fps || 0).toFixed(1)} fps`;
  captureSourceValue.textContent = `${status.capture_resolution || status.resolution} · Q${status.capture_quality || "—"}`;
  streamDetails.textContent = `${status.live_resolution || status.resolution} · ${Number(status.live_fps || 0).toFixed(1)} fps`;
  const dimensions = String(status.capture_resolution || status.resolution).match(/(\d+)\D+(\d+)/);
  if (dimensions) sourceResolution = [Number(dimensions[1]), Number(dimensions[2])];

  if (!status.enabled || status.state === "disabled") {
    disconnectStream();
    emptyTitle.textContent = "Camera is off";
    emptyDetail.textContent = "Turn camera capture on in Settings.";
  } else {
    connectStream();
    if (status.online) {
      viewer.classList.add("has-frame");
    } else if (status.state === "starting") {
      viewer.classList.remove("has-frame");
      emptyTitle.textContent = "Starting camera";
      emptyDetail.textContent = "Waiting for the first frame…";
    } else {
      viewer.classList.remove("has-frame");
      emptyTitle.textContent = "Camera unavailable";
      emptyDetail.textContent = status.error || "Trying to reconnect…";
    }
  }
  renderCameraDetection(status.detection);
  renderRecording(status.recording);
}

function initializeCameraPage() {
  setOverlayButton(aiOverlayButton, showAiOverlay);
  setOverlayButton(motionOverlayButton, showMotionOverlay);
  aiOverlayButton?.addEventListener("click", () => {
    showAiOverlay = !showAiOverlay;
    storeBoolean("sentinel.overlay.ai", showAiOverlay);
    setOverlayButton(aiOverlayButton, showAiOverlay);
    renderCameraDetection(latestStatus?.detection);
  });
  motionOverlayButton?.addEventListener("click", () => {
    showMotionOverlay = !showMotionOverlay;
    storeBoolean("sentinel.overlay.motion", showMotionOverlay);
    setOverlayButton(motionOverlayButton, showMotionOverlay);
    renderCameraDetection(latestStatus?.detection);
  });

  stream?.addEventListener("load", () => viewer.classList.add("has-frame"));
  stream?.addEventListener("error", () => {
    viewer.classList.remove("has-frame");
    stream.removeAttribute("src");
    if (latestStatus?.enabled) streamRetry = setTimeout(connectStream, 2500);
  });
  fullscreenButton?.addEventListener("click", async () => {
    try {
      if (!document.fullscreenElement) await viewer.requestFullscreen();
      else await document.exitFullscreen();
    } catch (_) { showToast("Fullscreen is unavailable in this browser", true); }
  });
  document.addEventListener("fullscreenchange", () => {
    const active = document.fullscreenElement === viewer;
    fullscreenButton.setAttribute("aria-label", active ? "Exit fullscreen" : "Enter fullscreen");
    fullscreenButton.title = active ? "Exit fullscreen" : "Fullscreen";
    drawDetections();
  });
  window.addEventListener("resize", drawDetections);

  recordButton?.addEventListener("click", async () => {
    recordButton.disabled = true;
    try {
      const active = Boolean(latestStatus?.recording?.active);
      const recording = await api(
        active ? "/api/recordings/stop" : "/api/recordings/start",
        { method: "POST" },
      );
      if (!active) {
        showToast("High-quality recording started");
      } else if (recording.processing) {
        showToast("Recording stopped; preparing MP4");
      } else {
        showToast("Recording saved as MJPEG; rerun the installer to enable MP4");
      }
      await refreshStatus();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      recordButton.disabled = false;
    }
  });
  snapshotButton?.addEventListener("click", (event) => {
    if (!latestStatus?.online) {
      event.preventDefault();
      showToast("Turn the camera on before taking a snapshot", true);
    }
  });

  const updateClock = () => {
    viewerTime.textContent = new Intl.DateTimeFormat(undefined, {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date());
  };
  updateClock();
  visibleInterval(updateClock, 1000);
  visibleInterval(refreshStatus, 1000);
}

// Recordings page
const recordingsList = $("#recordings-list");
const refreshRecordingsButton = $("#refresh-recordings");
const playbackDialog = $("#playback-dialog");
const playbackVideo = $("#playback-video");
const playbackStream = $("#playback-stream");
const playbackTitle = $("#playback-title");
const playbackDetail = $("#playback-detail");
const closePlaybackButton = $("#close-playback");

function createButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function openPlayback(recording) {
  playbackTitle.textContent = new Date(recording.started_at).toLocaleString();
  const isMp4 = recording.format === "mp4";
  playbackVideo.hidden = !isMp4;
  playbackStream.hidden = isMp4;
  if (isMp4) {
    playbackDetail.textContent = "Use the player controls to pause, scrub, or change playback speed.";
    playbackVideo.src = `/api/recordings/${recording.id}/video.mp4`;
    playbackVideo.load();
  } else {
    playbackDetail.textContent = "This legacy MJPEG clip has basic playback. Restart after installing FFmpeg to convert it to MP4.";
    playbackStream.src = `/api/recordings/${recording.id}/stream.mjpg?v=${Date.now()}`;
  }
  playbackDialog.showModal();
}

function closePlayback() {
  playbackVideo.pause();
  playbackVideo.removeAttribute("src");
  playbackVideo.load();
  playbackDialog.close();
  playbackStream.removeAttribute("src");
}

async function deleteRecording(recording) {
  const started = new Date(recording.started_at).toLocaleString();
  if (!window.confirm(`Delete the recording from ${started}?`)) return;
  try {
    await api(`/api/recordings/${recording.id}`, { method: "DELETE" });
    showToast("Recording deleted");
    await loadRecordings();
  } catch (error) { showToast(error.message, true); }
}

function renderRecordings(recordings) {
  recordingsList.replaceChildren();
  if (!recordings.length) {
    const empty = document.createElement("div");
    empty.className = "recordings-empty";
    const title = document.createElement("strong");
    title.textContent = "No recordings yet";
    const detail = document.createElement("span");
    detail.textContent = "Start a recording from the Camera page.";
    empty.append(title, detail);
    recordingsList.append(empty);
    return;
  }

  for (const recording of recordings) {
    const card = document.createElement("article");
    card.className = "recording-card";
    const preview = document.createElement("div");
    preview.className = "recording-preview";
    preview.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="m10 9 5 3-5 3Z"></path></svg>';
    const body = document.createElement("div");
    body.className = "recording-card-body";
    const title = document.createElement("strong");
    title.textContent = new Date(recording.started_at).toLocaleString();
    const meta = document.createElement("span");
    const state = recording.active
      ? "Recording now"
      : recording.processing
        ? "Preparing MP4"
        : recording.format === "mp4" ? "MP4" : "Legacy MJPEG";
    meta.textContent = `${formatDuration(recording.duration_seconds)} · ${formatBytes(recording.bytes)} · ${state}`;
    const actions = document.createElement("div");
    actions.className = "recording-actions";
    const unavailable = Boolean(recording.active || recording.processing);
    const playLabel = recording.active ? "In progress" : recording.processing ? "Preparing" : "Play";
    const play = createButton(playLabel, "button button-secondary", () => openPlayback(recording));
    play.disabled = unavailable;
    const download = document.createElement("a");
    download.className = "button button-secondary";
    if (unavailable) {
      download.setAttribute("aria-disabled", "true");
      download.addEventListener("click", (event) => event.preventDefault());
    } else {
      download.href = `/api/recordings/${recording.id}/download`;
    }
    download.textContent = recording.processing ? "Preparing" : "Download";
    const remove = createButton("Delete", "button button-danger", () => deleteRecording(recording));
    remove.disabled = unavailable;
    actions.append(play, download, remove);
    body.append(title, meta, actions);
    card.append(preview, body);
    recordingsList.append(card);
  }
}

async function loadRecordings() {
  refreshRecordingsButton.disabled = true;
  try {
    const payload = await api("/api/recordings");
    renderRecordings(Array.isArray(payload.recordings) ? payload.recordings : []);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    refreshRecordingsButton.disabled = false;
  }
}

function initializeRecordingsPage() {
  refreshRecordingsButton.addEventListener("click", loadRecordings);
  closePlaybackButton.addEventListener("click", closePlayback);
  playbackVideo.addEventListener("error", () => {
    showToast("This MP4 could not be played in the browser", true);
  });
  playbackDialog.addEventListener("click", (event) => {
    if (event.target === playbackDialog) closePlayback();
  });
  visibleInterval(loadRecordings, 5000);
  visibleInterval(refreshStatus, 5000);
}

// Settings page
const cameraToggle = $("#camera-toggle");
const aiToggle = $("#ai-toggle");
const motionToggle = $("#motion-toggle");
const aiCategoryInputs = $$("input[name='ai-category']");
const motionSensitivity = $("#motion-sensitivity");
const motionSensitivityValue = $("#motion-sensitivity-value");
const cameraControlDetail = $("#camera-control-detail");
const aiControlDetail = $("#ai-control-detail");
const motionControlDetail = $("#motion-control-detail");
const liveQualityValue = $("#live-quality-value");
const captureQualityValue = $("#capture-quality-value");
const settingsAiOverlay = $("#settings-ai-overlay");
const settingsMotionOverlay = $("#settings-motion-overlay");

function renderSettings(status) {
  if (!cameraToggle) return;
  const detection = status.detection || {};
  if (!pendingControls.has("camera")) cameraToggle.checked = Boolean(status.enabled);
  if (!pendingControls.has("ai")) aiToggle.checked = Boolean(detection.ai?.enabled);
  if (!pendingControls.has("motion")) motionToggle.checked = Boolean(detection.motion?.enabled);
  if (!pendingControls.has("categories")) {
    const selected = new Set(detection.ai?.categories || []);
    for (const input of aiCategoryInputs) input.checked = selected.has(input.value);
  }
  if (!pendingControls.has("sensitivity") && detection.motion?.sensitivity) {
    motionSensitivity.value = detection.motion.sensitivity;
    motionSensitivityValue.value = detection.motion.sensitivity;
  }
  motionSensitivity.disabled = !detection.motion?.enabled;
  for (const input of aiCategoryInputs) input.disabled = !detection.ai?.enabled;
  cameraControlDetail.textContent = status.enabled ? "Capture is on" : "Capture is off";
  aiControlDetail.textContent = detection.ai?.enabled
    ? (detection.ai.model || "Object detection is on")
    : "Object detection is off";
  motionControlDetail.textContent = detection.motion?.enabled
    ? "Monitoring frame changes"
    : "Motion detection is off";
  liveQualityValue.textContent = `${status.live_resolution || status.resolution} · Q${status.live_quality || "—"}`;
  captureQualityValue.textContent = `${status.capture_resolution || status.resolution} · Q${status.capture_quality || "—"}`;
}

async function setToggle(control, path, payload, key, label) {
  pendingControls.add(key);
  control.disabled = true;
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    showToast(`${label} ${control.checked ? "enabled" : "disabled"}`);
    await refreshStatus();
  } catch (error) {
    control.checked = !control.checked;
    showToast(error.message, true);
  } finally {
    control.disabled = false;
    pendingControls.delete(key);
  }
}

function initializeSettingsPage() {
  settingsAiOverlay.checked = storedBoolean("sentinel.overlay.ai");
  settingsMotionOverlay.checked = storedBoolean("sentinel.overlay.motion");
  settingsAiOverlay.addEventListener("change", () => {
    storeBoolean("sentinel.overlay.ai", settingsAiOverlay.checked);
    showToast(`AI boxes ${settingsAiOverlay.checked ? "shown" : "hidden"} on the Camera page`);
  });
  settingsMotionOverlay.addEventListener("change", () => {
    storeBoolean("sentinel.overlay.motion", settingsMotionOverlay.checked);
    showToast(`Motion overlay ${settingsMotionOverlay.checked ? "shown" : "hidden"} on the Camera page`);
  });
  cameraToggle.addEventListener("change", () => setToggle(cameraToggle, "/api/camera", { enabled: cameraToggle.checked }, "camera", "Camera"));
  aiToggle.addEventListener("change", () => setToggle(aiToggle, "/api/detection", { ai_enabled: aiToggle.checked }, "ai", "AI detection"));
  motionToggle.addEventListener("change", () => setToggle(motionToggle, "/api/detection", { motion_enabled: motionToggle.checked }, "motion", "Motion detection"));
  for (const input of aiCategoryInputs) {
    input.addEventListener("change", async () => {
      const selected = aiCategoryInputs.filter((item) => item.checked).map((item) => item.value);
      if (!selected.length) {
        input.checked = true;
        showToast("Choose at least one AI detection category", true);
        return;
      }
      pendingControls.add("categories");
      for (const item of aiCategoryInputs) item.disabled = true;
      try {
        await api("/api/detection", { method: "POST", body: JSON.stringify({ ai_categories: selected }) });
        showToast(`AI filter: ${selected.map(titleCase).join(", ")}`);
      } catch (error) {
        showToast(error.message, true);
      } finally {
        pendingControls.delete("categories");
        await refreshStatus();
      }
    });
  }
  motionSensitivity.addEventListener("input", () => {
    motionSensitivityValue.value = motionSensitivity.value;
  });
  motionSensitivity.addEventListener("change", async () => {
    pendingControls.add("sensitivity");
    motionSensitivity.disabled = true;
    try {
      await api("/api/detection", {
        method: "POST",
        body: JSON.stringify({ motion_sensitivity: Number(motionSensitivity.value) }),
      });
      showToast(`Motion sensitivity set to ${motionSensitivity.value}`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      pendingControls.delete("sensitivity");
      await refreshStatus();
    }
  });
  visibleInterval(refreshStatus, 5000);
}

// System page
const refreshSystemButton = $("#refresh-system");
const healthState = $("#health-state");
const systemCpu = $("#system-cpu");
const systemCpuMeter = $("#system-cpu-meter");
const systemLoad = $("#system-load");
const systemTemperature = $("#system-temperature");
const systemTemperatureMeter = $("#system-temperature-meter");
const systemMemory = $("#system-memory");
const systemMemoryMeter = $("#system-memory-meter");
const systemMemoryDetail = $("#system-memory-detail");
const systemStorage = $("#system-storage");
const systemStorageMeter = $("#system-storage-meter");
const systemStorageDetail = $("#system-storage-detail");
const systemHostname = $("#system-hostname");
const systemOs = $("#system-os");
const systemKernel = $("#system-kernel");
const systemUptime = $("#system-uptime");
const rebootButton = $("#reboot-button");
const rebootDialog = $("#reboot-dialog");
const cancelRebootButton = $("#cancel-reboot");
const confirmRebootButton = $("#confirm-reboot");
const updateCard = $("#update-card");
const updateTitle = $("#update-title");
const updateDetail = $("#update-detail");
const updateButton = $("#update-button");
const updateDialog = $("#update-dialog");
const cancelUpdateButton = $("#cancel-update");
const confirmUpdateButton = $("#confirm-update");

function setMeter(element, percent) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  element.style.width = `${value}%`;
  element.parentElement.classList.toggle("warning", value >= 80);
  element.parentElement.classList.toggle("critical", value >= 92);
}

function renderSystem(system) {
  const cpu = system.cpu_percent;
  systemCpu.textContent = cpu == null ? "Sampling" : `${Number(cpu).toFixed(1)}%`;
  setMeter(systemCpuMeter, cpu);
  systemLoad.textContent = `Load ${system.load_average.map((value) => Number(value).toFixed(2)).join(" · ")}`;
  const temperature = system.temperature_c;
  systemTemperature.textContent = temperature == null ? "Unavailable" : `${Number(temperature).toFixed(1)} °C`;
  setMeter(systemTemperatureMeter, temperature == null ? 0 : (temperature / 85) * 100);
  systemMemory.textContent = `${Number(system.memory.used_percent).toFixed(1)}%`;
  setMeter(systemMemoryMeter, system.memory.used_percent);
  systemMemoryDetail.textContent = `${formatBytes(system.memory.used_bytes)} of ${formatBytes(system.memory.total_bytes)}`;
  systemStorage.textContent = `${Number(system.disk.used_percent).toFixed(1)}%`;
  setMeter(systemStorageMeter, system.disk.used_percent);
  systemStorageDetail.textContent = `${formatBytes(system.disk.used_bytes)} of ${formatBytes(system.disk.total_bytes)}`;
  systemHostname.textContent = system.hostname;
  systemOs.textContent = system.os;
  systemOs.title = system.os;
  systemKernel.textContent = `${system.kernel} · ${system.architecture}`;
  systemKernel.title = `${system.kernel} · ${system.architecture}`;
  systemUptime.textContent = system.uptime;
  healthState.textContent = "Live";
  healthState.classList.add("online");
}

async function loadSystem() {
  refreshSystemButton.disabled = true;
  try {
    renderSystem(await api("/api/system"));
  } catch (_) {
    healthState.textContent = "Unavailable";
    healthState.classList.remove("online");
  } finally {
    refreshSystemButton.disabled = false;
  }
}

function renderUpdate(update) {
  const available = Boolean(update?.supported && update.available);
  const blocked = available && !update.can_update;
  updateCard.classList.toggle("available", available && !blocked);
  updateCard.classList.toggle("blocked", blocked);
  if (!update?.supported) {
    updateTitle.textContent = "Updates unavailable";
    updateDetail.textContent = update?.message || "This installation cannot check for updates.";
    updateButton.textContent = "Unavailable";
    updateButton.disabled = true;
  } else if (!available) {
    updateTitle.textContent = "Software is up to date";
    updateDetail.textContent = update.message || "This installation matches its configured repository.";
    updateButton.textContent = "Up to date";
    updateButton.disabled = true;
  } else {
    updateTitle.textContent = blocked ? "Update needs attention" : "Software update available";
    updateDetail.textContent = blocked
      ? update.message
      : `${update.repository} · ${update.current_version} → ${update.latest_version}`;
    updateButton.textContent = blocked ? "Update locally" : "Update now";
    updateButton.disabled = blocked;
  }
}

async function loadUpdate(refresh = false) {
  try {
    const update = await api(`/api/update${refresh ? "?refresh=1" : ""}`);
    renderUpdate(update);
    return update;
  } catch (_) {
    renderUpdate({ supported: false, message: "The update service could not be reached." });
    return null;
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function monitorUpdate() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await wait(2000);
    const update = await loadUpdate(true);
    if (update?.supported && !update.available) {
      window.location.reload();
      return;
    }
  }
  confirmUpdateButton.disabled = false;
  showToast("The update did not finish. Run ./update.sh on the Pi for details.", true);
}

function initializeSystemPage() {
  refreshSystemButton.addEventListener("click", () => Promise.all([loadSystem(), loadUpdate(true)]));
  rebootButton.addEventListener("click", () => rebootDialog.showModal());
  cancelRebootButton.addEventListener("click", () => rebootDialog.close());
  rebootDialog.addEventListener("click", (event) => {
    if (event.target === rebootDialog) rebootDialog.close();
  });
  confirmRebootButton.addEventListener("click", async () => {
    confirmRebootButton.disabled = true;
    try {
      await api("/api/system/reboot", { method: "POST", body: JSON.stringify({ confirm: "reboot" }) });
      rebootDialog.close();
      setConnection("waiting", "Rebooting device");
      showToast("Reboot requested. The dashboard will reconnect after startup.");
    } catch (error) {
      showToast(error.message, true);
      confirmRebootButton.disabled = false;
    }
  });
  updateButton.addEventListener("click", () => updateDialog.showModal());
  cancelUpdateButton.addEventListener("click", () => updateDialog.close());
  updateDialog.addEventListener("click", (event) => {
    if (event.target === updateDialog) updateDialog.close();
  });
  confirmUpdateButton.addEventListener("click", async () => {
    confirmUpdateButton.disabled = true;
    try {
      await api("/api/update", { method: "POST", body: JSON.stringify({ confirm: "update" }) });
      updateDialog.close();
      updateButton.disabled = true;
      updateButton.textContent = "Updating…";
      updateTitle.textContent = "Installing software update";
      updateDetail.textContent = "The dashboard reloads after the camera service restarts.";
      showToast("Update started. Recordings and settings will be kept.");
      void monitorUpdate();
    } catch (error) {
      showToast(error.message, true);
      confirmUpdateButton.disabled = false;
      await loadUpdate(true);
    }
  });
  visibleInterval(loadSystem, 5000);
  visibleInterval(refreshStatus, 5000);
  visibleInterval(() => loadUpdate(true), 15 * 60 * 1000);
}

async function initialize() {
  try {
    const session = await api("/api/session");
    csrfToken = session.csrf_token;
    initializeLogout();
    await refreshStatus();
    if (page === "camera") initializeCameraPage();
    if (page === "recordings") {
      initializeRecordingsPage();
      await loadRecordings();
    }
    if (page === "settings") initializeSettingsPage();
    if (page === "system") {
      initializeSystemPage();
      await Promise.all([loadSystem(), loadUpdate(true)]);
    }
  } catch (error) {
    setConnection("offline", "Service offline");
    showToast(error.message, true);
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void refreshStatus();
});

void initialize();
