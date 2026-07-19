# Raspberry Pi Security Camera

A password-protected device dashboard for a Raspberry Pi camera. It provides a
live MJPEG feed, recordings, detection controls, Linux system telemetry, and a
guarded device reboot action. Slow viewers and detection never delay capture
because both use only the latest frame.

## Features

- Require a login for the dashboard, live feed, snapshots, APIs, health status,
  recording playback, and downloads
- Turn the camera on and off without stopping the dashboard service
- Start and stop recordings, replay them in the browser, download, or delete them
- Turn AI object detection and motion detection on or off independently
- Adjust motion sensitivity from 1-100 while the camera is running
- Automatically use a Hailo AI HAT when available and fall back to CPU detection
- Monitor CPU, temperature, load, memory, storage, uptime, OS, and kernel details
- Reboot the Raspberry Pi through an explicitly confirmed, narrowly authorized action
- Keep camera frames, AI inference, password verification, and recordings on the Pi

## Secure installation

Raspberry Pi OS normally includes `rpicam-apps`. Install the camera, OpenCV, and
TLS tools if needed:

```bash
sudo apt update
sudo apt install rpicam-apps python3-opencv openssl policykit-1
```

If the Pi has a Hailo AI HAT, install its runtime too:

```bash
sudo apt install hailo-all
```

Test the camera, then install the service:

```bash
rpicam-hello --timeout 5s
chmod +x install-service.sh
./install-service.sh
```

The installer prompts for a dashboard password of at least 12 characters. It
stores only a salted scrypt verifier in `/etc/raspi-security-camera/password-hash`,
generates a protected TLS private key and local certificate, installs the
unprivileged systemd service, and prints the HTTPS address and certificate
fingerprint. The installed configuration binds to all interfaces only with that
TLS certificate active; the application default remains loopback-only.

The generated certificate is self-signed. Verify the printed SHA-256 fingerprint
before accepting or importing it in a browser. A certificate from a private CA,
Caddy, or Tailscale HTTPS avoids the browser warning and provides stronger server
identity verification.

Follow service logs with:

```bash
journalctl -u raspi-security-camera -f
```

Rerun `./install-service.sh` to change the password. Existing login sessions are
memory-only and are invalidated whenever the service restarts. The installer
preserves an existing loopback-only listener used by a reverse proxy.

### Tailscale Serve

To use a browser-trusted, tailnet-only URL instead of the generated self-signed
LAN certificate, proxy the local HTTPS service with Tailscale Serve:

```bash
tailscale serve --bg https+insecure://127.0.0.1:8080
sudo sed -i 's/^CAMERA_HOST=.*/CAMERA_HOST=127.0.0.1/' /etc/raspi-security-camera/environment
sudo systemctl restart raspi-security-camera
```

Use the `https://<device>.<tailnet>.ts.net` URL printed by Tailscale without
port `8080`. Loopback-only mode prevents bypassing Serve through either the LAN
or Tailscale IP address; tailnet access controls and the dashboard password both
remain in effect.

## Manual local run

Plain HTTP is accepted only on a loopback address. This is suitable for local
development or an HTTPS reverse proxy:

```bash
install -d -m 700 ~/.config/raspi-security-camera
python3 camera_server.py --hash-password > ~/.config/raspi-security-camera/password-hash
chmod 600 ~/.config/raspi-security-camera/password-hash
CAMERA_PASSWORD_FILE="$HOME/.config/raspi-security-camera/password-hash" python3 camera_server.py
```

Open `http://127.0.0.1:8080`. The server refuses to bind an unencrypted listener
to a non-loopback address. For direct network access, configure
`CAMERA_TLS_CERT` and `CAMERA_TLS_KEY`. For an HTTPS reverse proxy, keep
`CAMERA_HOST=127.0.0.1` and set `CAMERA_TRUST_PROXY_HTTPS=true` so session cookies
remain HTTPS-only.

## Security model

- Every camera frame, recording, API, status response, and static dashboard file
  is checked server-side. Only the login page and its presentation assets are public.
- Passwords are verified with salted scrypt and are never stored in source,
  browser storage, cookies, URLs, or logs.
- Login sessions use random opaque tokens in `HttpOnly`, `SameSite=Strict`
  cookies. Network deployments also use the `Secure` cookie attribute and HSTS.
- State-changing requests require an unpredictable session-bound CSRF token.
- Login attempts are rate limited, sessions expire after 12 hours by default,
  and open MJPEG streams stop when their session expires or is logged out.
- CSP, clickjacking, MIME-sniffing, referrer, cross-origin, and permissions
  headers are applied centrally.
- The service runs without Linux capabilities and with systemd filesystem,
  kernel, namespace, and privilege-escalation restrictions.
- Reboot uses systemd-logind over D-Bus. The installed polkit rule permits only
  reboot actions for the service account; it does not grant shell or general
  systemd administration privileges.

Authentication protects the application, but it does not replace network access
control. Keep port 8080 off the public internet. Prefer a LAN firewall, Tailscale
ACLs, or both, and use a trusted TLS certificate for access outside a controlled
network.

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
| `CAMERA_HOST` | `127.0.0.1` | Dashboard listen address; non-loopback requires TLS |
| `CAMERA_PORT` | `8080` | Dashboard port |
| `CAMERA_USERNAME` | `admin` | Dashboard login username |
| `CAMERA_PASSWORD_FILE` | `/etc/raspi-security-camera/password-hash` | Protected scrypt verifier file |
| `CAMERA_PASSWORD_HASH` | unset | Development-only inline verifier override |
| `CAMERA_SESSION_SECONDS` | `43200` | Absolute login session lifetime, 300-604800 seconds |
| `CAMERA_TLS_CERT` | unset | PEM TLS certificate for direct HTTPS |
| `CAMERA_TLS_KEY` | unset | PEM TLS private key for direct HTTPS |
| `CAMERA_SETTINGS_FILE` | `./state/device-settings.json` | Persisted dashboard control state |
| `CAMERA_TRUST_PROXY_HTTPS` | `false` | Use HTTPS-only cookies behind a loopback HTTPS proxy |
| `CAMERA_WIDTH` | `1920` | Stream width |
| `CAMERA_HEIGHT` | `1080` | Stream height |
| `CAMERA_FPS` | `20` | Capture and recording playback frame rate |
| `CAMERA_QUALITY` | `75` | JPEG quality from 1-100 |
| `CAMERA_SENSOR_MODE` | `2304:1296:10:P` | Camera Module 3 full-field sensor mode |
| `CAMERA_AF_MODE` | `continuous` | Camera autofocus mode |
| `MOTION_ENABLED` | `true` | Initial motion detection state |
| `MOTION_THRESHOLD` | `0.012` | Initial changed-image fraction |
| `MOTION_HOLD_SECONDS` | `3` | Hold an alert after movement stops |
| `AI_ENABLED` | `true` | Initial AI detection state |
| `AI_BACKEND` | `auto` | `auto`, `hailo`, or `cpu` |
| `AI_MODEL` | automatic | Backward-compatible `.hef` or `.onnx` model override |
| `AI_CPU_MODEL` | `models/yolov8n.onnx` when present | Compatible YOLOv8 ONNX model |
| `AI_INPUT_SIZE` | `640` | CPU YOLO square input size |
| `DETECTION_FPS` | `5` | Maximum analyzed frames per second |
| `RECORDINGS_DIR` | `./recordings` | Local recording storage directory |
| `LOG_LEVEL` | `INFO` | Python log level |

For the system service, put ordinary overrides in
`/etc/default/raspi-security-camera` and restart it:

```bash
sudo systemctl restart raspi-security-camera
```

The installer-managed password and TLS paths live in
`/etc/raspi-security-camera/environment` and intentionally take precedence over
the ordinary overrides.

Camera enabled state, AI detection, motion detection, and motion sensitivity are
saved atomically whenever they are changed in the dashboard. The system service
stores them in `/var/lib/raspi-security-camera/device-settings.json` and restores
them before capture and detection start. Active recordings are intentionally not
resumed after a service or device restart.

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

All endpoints except `POST /api/login` require an authenticated session. POST
and DELETE endpoints also require the session's `X-CSRF-Token` header.

- `POST /api/login` and `POST /api/logout` - session lifecycle
- `GET /api/session` - current user and CSRF token
- `GET /api/status` - camera, recording, AI, and motion state
- `POST /api/camera` with `{"enabled":true}` - turn capture on/off
- `POST /api/detection` - update detection settings
- `GET /api/system` - device resource and OS telemetry
- `POST /api/system/reboot` with `{"confirm":"reboot"}` - reboot the Pi
- `GET /api/recordings` - list local recordings
- `POST /api/recordings/start` and `/api/recordings/stop` - recording control
- `GET /api/recordings/<id>/stream.mjpg` - browser playback
- `GET /api/recordings/<id>/download` - download raw MJPEG
- `DELETE /api/recordings/<id>` - delete a recording
- `GET /stream.mjpg` - live stream
- `GET /snapshot.jpg` - latest frame
- `GET /healthz` - camera health for authenticated monitoring
