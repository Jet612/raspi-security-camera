# Raspberry Pi Security Camera

A local device dashboard for a Raspberry Pi camera. It provides a live MJPEG
feed, runtime camera and detection controls, adjustable motion sensitivity, and
local recording playback. Slow viewers and detection never delay camera
capture because both use only the latest frame.

## Features

- Turn the camera on and off without stopping the dashboard service
- Start and stop recordings, replay them in the browser, download, or delete them
- Turn AI object detection and motion detection on or off independently
- Adjust motion sensitivity from 1–100 while the camera is running
- Automatically use a Hailo AI HAT when available and fall back to CPU detection
- Keep camera frames, AI inference, and recordings on the Raspberry Pi

## Quick start

Raspberry Pi OS normally includes `rpicam-apps`. Install the camera and OpenCV
dependencies if needed:

```bash
sudo apt update
sudo apt install rpicam-apps python3-opencv
```

If the Pi has a Hailo AI HAT, install its runtime too:

```bash
sudo apt install hailo-all
```

Test the camera and run the dashboard:

```bash
rpicam-hello --timeout 5s
python3 camera_server.py
```

Open `http://<raspberry-pi-or-tailscale-ip>:8080`. To run it at boot:

```bash
chmod +x install-service.sh
./install-service.sh
```

Follow service logs with:

```bash
journalctl -u raspi-security-camera -f
```

## Automatic AI backend selection

`AI_BACKEND=auto` is the default. At startup the detector checks `/dev/hailo0`
and identifies the device with `hailortcli`:

1. A working Hailo 8/8L and HEF model are preferred.
2. If no Hailo device is present, initialization fails, or inference fails, the
   application switches to CPU detection automatically.
3. A failed Hailo backend is retried later, so reconnecting or fixing the module
   does not require changing the configuration.

The zero-setup CPU fallback uses OpenCV's built-in person detector. For CPU
detection of people, vehicles, and animals, place a compatible COCO YOLOv8 ONNX
model at `models/yolov8n.onnx` or set `AI_CPU_MODEL` to its path. The latest-frame
worker drops analysis frames when the Pi cannot infer at the requested rate, so
the live video remains responsive.

You can force a backend for troubleshooting:

```bash
AI_BACKEND=cpu python3 camera_server.py
AI_BACKEND=hailo python3 camera_server.py
```

Verify Hailo connectivity with:

```bash
hailortcli fw-control identify
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CAMERA_HOST` | `0.0.0.0` | Dashboard listen address |
| `CAMERA_PORT` | `8080` | Dashboard port |
| `CAMERA_WIDTH` | `1920` | Stream width |
| `CAMERA_HEIGHT` | `1080` | Stream height |
| `CAMERA_FPS` | `20` | Capture and recording playback frame rate |
| `CAMERA_QUALITY` | `75` | JPEG quality from 1–100 |
| `CAMERA_SENSOR_MODE` | `2304:1296:10:P` | Camera Module 3 full-field sensor mode |
| `CAMERA_AF_MODE` | `continuous` | Camera autofocus mode |
| `MOTION_ENABLED` | `true` | Initial motion detection state |
| `MOTION_THRESHOLD` | `0.012` | Initial changed-image fraction (the dashboard slider updates it at runtime) |
| `MOTION_HOLD_SECONDS` | `3` | Hold an alert after movement stops |
| `AI_ENABLED` | `true` | Initial AI detection state |
| `AI_BACKEND` | `auto` | `auto`, `hailo`, or `cpu` |
| `AI_MODEL` | automatic | Backward-compatible `.hef` or `.onnx` model override |
| `AI_CPU_MODEL` | `models/yolov8n.onnx` when present | Compatible YOLOv8 ONNX model |
| `AI_INPUT_SIZE` | `640` | CPU YOLO square input size |
| `DETECTION_FPS` | `5` | Maximum analysed frames per second |
| `RECORDINGS_DIR` | `./recordings` | Local recording storage directory |
| `LOG_LEVEL` | `INFO` | Python log level |

For the system service, put overrides in `/etc/default/raspi-security-camera`
and restart it:

```bash
sudo systemctl restart raspi-security-camera
```

`CAMERA_COMMAND` can replace the entire capture command for development or a
custom pipeline. It must continuously emit JPEG images to stdout.

## Recordings

Recordings use raw MJPEG so they can be written directly from the existing
camera stream without a second camera process or FFmpeg. The dashboard replays
them at `CAMERA_FPS`. Downloads use the `.mjpeg` format; VLC and FFmpeg can open
or convert it. Turning the camera off safely stops and saves an active recording.
The installed systemd service grants write access to the default `./recordings`
directory; a custom `RECORDINGS_DIR` also needs a matching `ReadWritePaths` entry
in the service unit.

## HTTP API

- `GET /api/status` — camera, recording, AI, and motion state
- `POST /api/camera` with `{"enabled":true}` — turn capture on/off
- `POST /api/detection` — update `ai_enabled`, `motion_enabled`, and/or `motion_sensitivity`
- `GET /api/recordings` — list local recordings
- `POST /api/recordings/start` and `/api/recordings/stop` — recording control
- `GET /api/recordings/<id>/stream.mjpg` — browser playback
- `GET /api/recordings/<id>/download` — download raw MJPEG
- `DELETE /api/recordings/<id>` — delete a recording
- `GET /stream.mjpg` — live stream
- `GET /snapshot.jpg` — latest frame
- `GET /healthz` — 200 while camera frames are arriving, otherwise 503

## Network access

The server listens on all interfaces by default and does not implement user
authentication. Tailscale encrypts tailnet traffic, but the port may also be
reachable from the local network. Use Tailscale ACLs and the Pi firewall to
limit access, or set `CAMERA_HOST` to a specific Tailscale address.
