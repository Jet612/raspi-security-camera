const $ = (selector) => document.querySelector(selector);

const viewer = $("#viewer");
const stream = $("#camera-stream");
const overlay = $("#detection-overlay");
const statusBox = $("#connection-status");
const statusLabel = $("#status-label");
const deviceDot = $("#sidebar-device-dot");
const emptyTitle = $("#empty-title");
const emptyDetail = $("#empty-detail");
const liveBadge = $("#live-badge");
const liveLabel = $("#live-label");
const fpsValue = $("#fps-value");
const streamDetails = $("#stream-details");
const viewerTime = $("#viewer-time");
const fullscreenButton = $("#fullscreen-button");
const detectionBadge = $("#detection-badge");
const detectionBadgeLabel = $("#detection-badge-label");
const activityTitle = $("#activity-title");
const activityPulse = $("#activity-pulse");
const motionValue = $("#motion-value");
const aiValue = $("#ai-value");
const objectsValue = $("#objects-value");
const cameraToggle = $("#camera-toggle");
const aiToggle = $("#ai-toggle");
const motionToggle = $("#motion-toggle");
const motionSensitivity = $("#motion-sensitivity");
const motionSensitivityValue = $("#motion-sensitivity-value");
const cameraDetail = $("#camera-control-detail");
const aiDetail = $("#ai-control-detail");
const motionDetail = $("#motion-control-detail");
const recordButton = $("#record-button");
const recordButtonLabel = $("#record-button-label");
const recordingTimer = $("#recording-timer");
const snapshotButton = $("#snapshot-button");
const recordingsList = $("#recordings-list");
const refreshRecordingsButton = $("#refresh-recordings");
const playbackDialog = $("#playback-dialog");
const playbackStream = $("#playback-stream");
const playbackTitle = $("#playback-title");
const closePlaybackButton = $("#close-playback");
const toast = $("#toast");

let streamRetry;
let toastTimer;
let consecutiveStatusErrors = 0;
let latestStatus = null;
let latestDetections = [];
let sourceResolution = [1920, 1080];
const pendingControls = new Set();

function showToast(message, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast visible${error ? " error" : ""}`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* Empty responses are allowed. */ }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function setConnection(kind, label) {
  statusBox.className = `status status-${kind}`;
  statusLabel.textContent = label;
}

function connectStream() {
  clearTimeout(streamRetry);
  if (!stream.getAttribute("src")) stream.src = `/stream.mjpg?v=${Date.now()}`;
}

function disconnectStream() {
  clearTimeout(streamRetry);
  stream.removeAttribute("src");
  viewer.classList.remove("has-frame");
}

function markOffline(detail) {
  viewer.classList.remove("has-frame");
  setConnection("offline", "Camera offline");
  emptyTitle.textContent = "Camera unavailable";
  emptyDetail.textContent = detail || "Trying to reconnect…";
}

function titleCase(value) {
  return String(value).replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function detectionColor(category) {
  if (category === "person") return "#ff756f";
  if (category === "animal") return "#ffc469";
  return "#63b7ff";
}

function drawDetections() {
  const rect = viewer.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  overlay.width = Math.round(rect.width * ratio);
  overlay.height = Math.round(rect.height * ratio);
  const context = overlay.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!latestDetections.length || !viewer.classList.contains("has-frame")) return;

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

function updateDetection(detection) {
  if (!detection) return;
  const detections = Array.isArray(detection.detections) ? detection.detections : [];
  latestDetections = detections;

  if (!pendingControls.has("motion")) motionToggle.checked = Boolean(detection.motion?.enabled);
  if (!pendingControls.has("ai")) aiToggle.checked = Boolean(detection.ai?.enabled);
  if (!pendingControls.has("sensitivity") && detection.motion?.sensitivity) {
    motionSensitivity.value = detection.motion.sensitivity;
    motionSensitivityValue.value = detection.motion.sensitivity;
  }
  motionSensitivity.disabled = !detection.motion?.enabled;

  if (!detection.motion?.enabled) {
    motionValue.textContent = "Disabled";
    motionValue.className = "";
    motionDetail.textContent = "Detection is off";
  } else {
    motionValue.textContent = detection.motion.active ? "Detected" : "Clear";
    motionValue.className = detection.motion.active ? "alert" : "";
    motionDetail.textContent = detection.motion.active ? "Movement detected" : "Monitoring frame changes";
  }

  if (!detection.ai?.enabled) {
    aiValue.textContent = "Disabled";
    aiValue.className = "";
    aiDetail.textContent = "Object detection is off";
  } else if (detection.ai.online) {
    const backend = detection.ai.backend === "cpu" ? "CPU" : "AI HAT+";
    aiValue.textContent = backend;
    aiValue.className = "";
    aiValue.title = detection.ai.model || backend;
    aiDetail.textContent = detection.ai.model || `${backend} detector online`;
  } else if (detection.ai.state === "starting") {
    aiValue.textContent = "Starting";
    aiValue.className = "";
    aiDetail.textContent = "Loading detection engine";
  } else {
    aiValue.textContent = "Unavailable";
    aiValue.className = "error";
    aiValue.title = detection.ai.error || "AI unavailable";
    aiDetail.textContent = detection.ai.error || "Detection engine unavailable";
  }

  const counts = new Map();
  for (const item of detections) counts.set(item.label, (counts.get(item.label) || 0) + 1);
  objectsValue.textContent = counts.size
    ? [...counts].map(([label, count]) => `${titleCase(label)}${count > 1 ? ` ×${count}` : ""}`).join(", ")
    : "None";

  const hasActivity = detections.length > 0 || Boolean(detection.motion?.active);
  activityPulse.classList.toggle("alert", hasActivity);
  if (detections.length) {
    detectionBadgeLabel.textContent = `${titleCase(detections[0].label)} detected`;
    detectionBadge.classList.remove("hidden");
    activityTitle.textContent = "Object detected";
  } else if (detection.motion?.active) {
    detectionBadgeLabel.textContent = "Motion detected";
    detectionBadge.classList.remove("hidden");
    activityTitle.textContent = "Motion detected";
  } else {
    detectionBadge.classList.add("hidden");
    activityTitle.textContent = detection.ai?.enabled || detection.motion?.enabled ? "Monitoring" : "Detection paused";
  }
  drawDetections();
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function updateRecording(recording) {
  const active = Boolean(recording?.active);
  recordButton.classList.toggle("recording", active);
  recordButtonLabel.textContent = active ? "Stop recording" : "Start recording";
  recordingTimer.classList.toggle("hidden", !active);
  if (active) recordingTimer.textContent = formatDuration(recording.duration_seconds);
}

function updateCamera(status) {
  latestStatus = status;
  if (!pendingControls.has("camera")) cameraToggle.checked = Boolean(status.enabled);
  snapshotButton.setAttribute("aria-disabled", String(!status.online));
  recordButton.disabled = !status.online && !status.recording?.active;
  fpsValue.textContent = status.online ? `${Number(status.fps).toFixed(1)} fps` : "—";
  const dimensions = String(status.resolution).match(/(\d+)\D+(\d+)/);
  if (dimensions) sourceResolution = [Number(dimensions[1]), Number(dimensions[2])];
  streamDetails.textContent = status.online ? `${status.resolution} · ${Number(status.fps).toFixed(1)} fps` : status.resolution;
  deviceDot.classList.toggle("online", Boolean(status.enabled));

  if (!status.enabled || status.state === "disabled") {
    disconnectStream();
    setConnection("disabled", "Camera off");
    liveBadge.classList.add("disabled");
    liveLabel.textContent = "Off";
    cameraDetail.textContent = "Video capture is off";
    emptyTitle.textContent = "Camera is off";
    emptyDetail.textContent = "Use the Camera switch to turn it on.";
  } else {
    connectStream();
    liveBadge.classList.remove("disabled");
    liveLabel.textContent = "Live";
    cameraDetail.textContent = status.online ? "Video capture is on" : "Camera is starting";
    if (status.online) {
      viewer.classList.add("has-frame");
      setConnection("online", "Camera online");
    } else if (status.state === "starting") {
      viewer.classList.remove("has-frame");
      setConnection("waiting", "Starting camera");
      emptyTitle.textContent = "Starting camera";
      emptyDetail.textContent = "Waiting for the first frame…";
    } else {
      markOffline(status.error || "Trying to reconnect…");
    }
  }
  updateDetection(status.detection);
  updateRecording(status.recording);
}

async function updateStatus() {
  try {
    const status = await api("/api/status");
    consecutiveStatusErrors = 0;
    updateCamera(status);
  } catch (error) {
    consecutiveStatusErrors += 1;
    if (consecutiveStatusErrors > 1) markOffline("The camera service cannot be reached.");
  }
}

async function setToggle(control, path, payload, key) {
  pendingControls.add(key);
  control.disabled = true;
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    showToast(`${titleCase(key)} ${control.checked ? "enabled" : "disabled"}`);
    await updateStatus();
  } catch (error) {
    control.checked = !control.checked;
    showToast(error.message, true);
  } finally {
    control.disabled = false;
    pendingControls.delete(key);
  }
}

cameraToggle.addEventListener("change", () => setToggle(cameraToggle, "/api/camera", { enabled: cameraToggle.checked }, "camera"));
aiToggle.addEventListener("change", () => setToggle(aiToggle, "/api/detection", { ai_enabled: aiToggle.checked }, "ai"));
motionToggle.addEventListener("change", () => setToggle(motionToggle, "/api/detection", { motion_enabled: motionToggle.checked }, "motion"));
motionSensitivity.addEventListener("input", () => { motionSensitivityValue.value = motionSensitivity.value; });
motionSensitivity.addEventListener("change", async () => {
  pendingControls.add("sensitivity");
  motionSensitivity.disabled = true;
  try {
    await api("/api/detection", { method: "POST", body: JSON.stringify({ motion_sensitivity: Number(motionSensitivity.value) }) });
    showToast(`Motion sensitivity set to ${motionSensitivity.value}`);
    await updateStatus();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    pendingControls.delete("sensitivity");
    motionSensitivity.disabled = !motionToggle.checked;
  }
});

recordButton.addEventListener("click", async () => {
  recordButton.disabled = true;
  try {
    const active = Boolean(latestStatus?.recording?.active);
    await api(active ? "/api/recordings/stop" : "/api/recordings/start", { method: "POST" });
    showToast(active ? "Recording saved" : "Recording started");
    await Promise.all([updateStatus(), loadRecordings()]);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    recordButton.disabled = false;
  }
});

snapshotButton.addEventListener("click", (event) => {
  if (!latestStatus?.online) {
    event.preventDefault();
    showToast("Turn the camera on before taking a snapshot", true);
  }
});

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

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
  playbackStream.src = `/api/recordings/${recording.id}/stream.mjpg?v=${Date.now()}`;
  playbackDialog.showModal();
}

function closePlayback() {
  playbackDialog.close();
  playbackStream.removeAttribute("src");
}

async function deleteRecording(recording) {
  if (!window.confirm(`Delete the recording from ${new Date(recording.started_at).toLocaleString()}?`)) return;
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
    detail.textContent = "Start a recording from the live camera controls.";
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
    meta.textContent = `${formatDuration(recording.duration_seconds)} · ${formatBytes(recording.bytes)}${recording.active ? " · Recording now" : ""}`;
    const actions = document.createElement("div");
    actions.className = "recording-actions";
    const play = createButton(recording.active ? "In progress" : "Play", "button button-secondary", () => openPlayback(recording));
    play.disabled = Boolean(recording.active);
    const download = document.createElement("a");
    download.className = "button button-secondary";
    download.href = `/api/recordings/${recording.id}/download`;
    download.textContent = "Download";
    const remove = createButton("×", "button button-danger", () => deleteRecording(recording));
    remove.title = "Delete recording";
    remove.disabled = Boolean(recording.active);
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

refreshRecordingsButton.addEventListener("click", loadRecordings);
closePlaybackButton.addEventListener("click", closePlayback);
playbackDialog.addEventListener("click", (event) => {
  if (event.target === playbackDialog) closePlayback();
});

stream.addEventListener("load", () => viewer.classList.add("has-frame"));
stream.addEventListener("error", () => {
  viewer.classList.remove("has-frame");
  stream.removeAttribute("src");
  if (latestStatus?.enabled) streamRetry = setTimeout(connectStream, 2500);
});

fullscreenButton.addEventListener("click", async () => {
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

function updateClock() {
  viewerTime.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
}

updateStatus();
loadRecordings();
updateClock();
setInterval(updateStatus, 1000);
setInterval(updateClock, 1000);
setInterval(loadRecordings, 10000);
