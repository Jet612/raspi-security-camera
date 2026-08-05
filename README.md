# Raspberry Pi Security Camera

A password-protected device dashboard for a Raspberry Pi camera. It provides a
live MJPEG feed, recordings, detection controls, Linux system telemetry, and a
guarded device reboot action. Slow viewers and detection never delay capture
because both use only the latest frame.

No programming experience is required for the normal installation. This guide
starts with the beginner setup and keeps developer information near the end.

## Guide

- [Before you begin](#before-you-begin)
- [Quick start](#quick-start)
- [Private remote access with Tailscale](#tailscale-serve)
- [Using the dashboard](#using-the-dashboard)
- [Software updates and forks](#software-updates)
- [Changing the dashboard password](#changing-the-dashboard-password)
- [Troubleshooting](#troubleshooting)
- [Removing the app](#removing-the-app)
- [Advanced installation and configuration](#advanced-installation)

## Features

- Require a login for the dashboard, live feed, snapshots, APIs, health status,
  recording playback, and downloads
- Turn the camera on and off without stopping the dashboard service
- Start and stop recordings, replay them in the browser, download, or delete them
- Turn AI object detection and motion detection on or off independently
- Adjust motion sensitivity from 1-100 while the camera is running
- Automatically use a Hailo AI HAT when available and fall back to CPU detection
- Monitor CPU, temperature, load, memory, storage, uptime, OS, and kernel details
- Show a dashboard notification when the installed Git repository has an update
- Reboot the Raspberry Pi through an explicitly confirmed, narrowly authorized action
- Keep camera frames, AI inference, password verification, and recordings on the Pi

## Before you begin

You will need:

- A Raspberry Pi with a camera connector; a Pi 4 or Pi 5 is recommended
- A supported Raspberry Pi Camera Module and the correct ribbon cable
- A microSD card with a current Raspberry Pi OS installation
- A network connection for the Pi and the phone or computer that will view it
- Access to Terminal on the Pi, either directly or through SSH

If Raspberry Pi OS is not installed yet, use [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
to prepare the microSD card. Raspberry Pi OS Lite and Desktop both work. During
Imager setup, create a user, enter the Wi-Fi details, and optionally enable SSH.

Turn the Pi off and unplug its power before connecting or disconnecting the
camera ribbon cable. The exposed contacts must face the correct direction for
your Pi model. Raspberry Pi's [camera installation guide](https://www.raspberrypi.com/documentation/accessories/camera.html#install-a-raspberry-pi-camera)
has pictures for the different connectors.

## Quick start

### 1. Open Terminal on the Raspberry Pi

On Raspberry Pi OS Desktop, click the Terminal icon. For a headless Pi, connect
with SSH from another computer.

### 2. Paste the install command

Copy this entire line, paste it into Terminal, and press Enter:

```bash
curl -fsSL https://raw.githubusercontent.com/Jet612/raspi-security-camera/main/install.sh | bash
```

The installer downloads the app, installs the camera and detection software,
and configures it to start whenever the Pi boots. The first installation can
take several minutes. It also asks whether you want optional private remote
access through Tailscale Serve.

To select Tailscale Serve automatically instead of waiting for that question,
use this version of the install command:

```bash
curl -fsSL https://raw.githubusercontent.com/Jet612/raspi-security-camera/main/install.sh | bash -s -- --tailscale-serve
```

If Tailscale is not on the Pi yet, the installer downloads it from Tailscale's
official installer. It may print a sign-in link; open that link on any device
and approve the Pi. Your viewing phone or computer also needs Tailscale and must
be signed into the same private Tailscale network.

The installer may ask for two passwords:

- **Raspberry Pi password:** required by `sudo` while installing system
  packages. Nothing appears while this password is typed; that is normal.
- **Dashboard password:** the password used to view the camera. It must contain
  at least 12 characters. Enter it twice. The username is `admin`.

### 3. Open the dashboard

When installation finishes, Terminal prints one of these addresses:

```text
Open:        https://192.168.1.50:8080
Open:        https://camera-name.your-tailnet.ts.net
Username:    admin
```

Open the address that your installer printed. The numeric address with `:8080`
works on the same local network. A `ts.net` address works from devices in your
Tailscale network and does not use port `8080` in the browser address.

The numeric address normally shows a browser privacy warning because the Pi
creates its own self-signed certificate. Compare the SHA-256 fingerprint shown
by the browser with the fingerprint printed by the installer. If they match,
use the browser's **Advanced** or **Continue** option. The Tailscale address uses
a browser-trusted certificate and should not show this warning.

Sign in with username `admin` and the dashboard password created during setup.

> [!IMPORTANT]
> Keep port 8080 off the public internet. Use the dashboard on a trusted local
> network or through a private service such as Tailscale.

## Using the dashboard

### Live camera

- **Camera switch:** turns video capture on or off without shutting down the
  dashboard. Turning it off also safely finishes an active recording.
- **Start recording:** begins saving video on the Pi. The button changes to
  **Stop recording** while recording.
- **Snapshot:** opens the newest camera image in a new browser tab. Save it with
  the browser's normal image-save option.
- **Fullscreen button:** expands the live image. Use Escape or the button again
  to leave fullscreen.

The connection indicator shows whether the camera is online, starting, turned
off, or unavailable. If the camera is unavailable, the dashboard remains open
and keeps trying to reconnect.

### Detection controls

- **AI detection:** looks for security-relevant objects. Without an optional AI
  model, the CPU fallback detects people. A compatible YOLO model adds vehicles
  and animals.
- **AI detection filter:** choose any combination of People, Vehicles, and
  Animals. The selection applies immediately and is saved across restarts.
- **Motion detection:** looks for changes between camera frames.
- **Motion sensitivity:** higher values react to smaller changes. If normal
  lighting changes cause alerts, lower the value. If movement is missed, raise it.

Detection boxes and the current activity appear over the live view. All
detection runs on the Pi; frames are not uploaded to a cloud service.

For human-only AI detection, leave only **People** selected. Turn **Motion
detection** off as well if you want activity alerts exclusively from detected
people; motion detection reacts to any visible image change.

### Recordings

Open the **Recordings** section to see saved clips.

- **Play** watches a completed recording in the dashboard.
- **Download** saves the raw `.mjpeg` file to the viewing device. VLC can play
  this format directly.
- **×** permanently deletes that recording from the Pi.

Recordings use the Pi's microSD card. Check the Storage value in the Device
section occasionally and delete or download old recordings before storage fills.

### Device information and reboot

The **Device** section shows processor use, temperature, memory, storage,
hostname, operating system, kernel, and uptime. **Reboot** asks for confirmation
and then restarts the entire Pi. Live video is unavailable until startup finishes.

Your camera, AI, motion, and sensitivity choices are saved. Recordings and saved
settings remain after a service restart, software update, or device reboot.

## Software updates

The dashboard checks the current branch's configured Git remote about every 15
minutes. When a different commit is available, a **Software update available**
banner appears. Select **Update now**, review the confirmation, and choose
**Install update**. The app only accepts a fast-forward update, then briefly
restarts the camera service and reloads the dashboard.

Updates are never installed without a signed-in user selecting the button. They
also never overwrite local changes. If the banner says the update needs
attention, someone has changed files in the app directory and should review
those changes from Terminal first.

To check or update manually:

```bash
cd ~/raspi-security-camera
./update.sh --check
./update.sh
```

The update source is not hard-coded in the dashboard. It uses the upstream
configured for the currently checked-out branch—normally `origin/main`. A
standard clone of a fork therefore checks and updates from that fork.

### Installing a fork

Fork owners should use both their raw script URL and their repository URL in the
install command. Replace `OWNER` and `REPOSITORY` below:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPOSITORY/main/install.sh | RASPI_CAMERA_REPOSITORY=https://github.com/OWNER/REPOSITORY.git bash
```

That repository becomes `origin`, so future dashboard and `update.sh` checks
continue to use the fork. A maintainer can intentionally configure a different
tracking remote with Git; the updater follows that configured upstream.

To select Tailscale Serve automatically while installing a fork, add the same
installer option:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPOSITORY/main/install.sh | RASPI_CAMERA_REPOSITORY=https://github.com/OWNER/REPOSITORY.git bash -s -- --tailscale-serve
```

## Changing the dashboard password

Run the same one-line install command again. When asked whether to replace the
existing dashboard password, enter `y`, then create the new password. Existing
recordings and settings are kept.

## Troubleshooting

### I lost the dashboard address

Run this on the Pi:

```bash
hostname -I
```

Use the first address shown, for example `https://192.168.1.50:8080`. The phone
or computer must be able to reach the same local network. Home routers can give
the Pi a different address after a reboot; reserving an address in the router
prevents that.

### The browser says the connection is not private

This is expected with the installer-created self-signed certificate. Verify the
certificate fingerprint against the installer output before continuing. If the
warning changes unexpectedly later, stop and verify the Pi's address and
certificate again.

### The dashboard opens but the camera is offline

1. Shut down and unplug the Pi, then check both ends of the ribbon cable.
2. Start the Pi and test the camera from Terminal:

   ```bash
   rpicam-hello --timeout 5s
   ```

3. Restart the camera app:

   ```bash
   sudo systemctl restart raspi-security-camera
   ```

### The dashboard does not open

Check whether the service is running:

```bash
sudo systemctl status raspi-security-camera
```

Press `q` to leave the status screen. If it reports a failure, show the latest
messages with:

```bash
journalctl -u raspi-security-camera -n 50 --no-pager
```

Rerunning the one-line installer safely repairs the service files and keeps
recordings and settings.

### I forgot the dashboard password

Rerun the install command and answer `y` when asked to replace the password.
The old password cannot be displayed because only a protected verifier is stored.

### An update is blocked by local changes

The updater stops so it cannot erase someone's work. If you intentionally
edited the application, commit or remove those changes with Git before trying
again. If you did not edit it, save the output of these commands and ask the
fork maintainer for help:

```bash
cd ~/raspi-security-camera
git status
./update.sh --check
```

### View live service messages

```bash
journalctl -u raspi-security-camera -f
```

Press Ctrl+C to stop following the messages.

## Removing the app

First download any recordings you want to keep. Then stop the service and remove
its system integration:

```bash
sudo systemctl disable --now raspi-security-camera
sudo rm -f /etc/systemd/system/raspi-security-camera.service
sudo rm -f /etc/systemd/system/raspi-security-camera-update.service
sudo rm -f /etc/polkit-1/rules.d/50-raspi-security-camera-reboot.rules
sudo systemctl daemon-reload
```

If this installer configured Tailscale Serve and you no longer want the camera
address shared with your tailnet, turn that proxy off too. This does not uninstall
Tailscale or remove the Pi from your tailnet:

```bash
sudo tailscale serve off
```

The app folder still contains the recordings. To remove the application files
and recordings too, delete the `raspi-security-camera` folder from your home
directory. Protected credentials and saved control settings can be removed with:

```bash
sudo rm -rf /etc/raspi-security-camera /var/lib/raspi-security-camera
```

These final deletion steps cannot be undone.

## Advanced installation

To inspect the code before installing, clone it and run the local installer:

```bash
git clone https://github.com/Jet612/raspi-security-camera.git
cd raspi-security-camera
./install-service.sh
```

The local installer installs Git, curl, `rpicam-apps`, OpenCV, NumPy, OpenSSL,
and the available polkit service package through Raspberry Pi OS. Pass
`--tailscale-serve` to select private
remote access without a question, or `--no-tailscale-serve` to skip the question.
Pass `--skip-dependencies` only when the required packages are already managed
separately. If the Pi has a Hailo AI HAT, install its optional runtime before or
after installing the app:

```bash
sudo apt install hailo-all
```

You can test the camera independently with:

```bash
rpicam-hello --timeout 5s
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

Rerun `./install-service.sh` to change the password. Existing login sessions are
memory-only and are invalidated whenever the service restarts. The installer
preserves an existing loopback-only listener used by a reverse proxy.

### Tailscale Serve

Tailscale Serve provides a browser-trusted address that is available only inside
your private tailnet. The easy installer asks whether to set it up. To enable it
later—or repair its configuration—run:

```bash
cd ~/raspi-security-camera
./install-service.sh --tailscale-serve
```

The installer installs Tailscale when necessary, guides you through signing in,
configures a persistent Serve proxy, and limits the camera backend to localhost.
After enabling it, use the printed Tailscale address instead of the Pi's numeric
LAN address.

Tailscale documents its supported Linux installer and Serve behavior in the
[Linux installation guide](https://tailscale.com/docs/install/linux) and
[Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

For a Tailscale client that you already manage yourself, the equivalent manual
configuration is:

```bash
sudo tailscale serve --bg https+insecure://127.0.0.1:8080
sudo sed -i 's/^CAMERA_HOST=.*/CAMERA_HOST=127.0.0.1/' /etc/raspi-security-camera/environment
sudo systemctl restart raspi-security-camera
```

Use the `https://<device>.<tailnet>.ts.net` URL printed by Tailscale without
port `8080`. Loopback-only mode prevents bypassing Serve through either the LAN
or Tailscale IP address; tailnet access controls and the dashboard password both
remain in effect.

To stop using Serve and restore direct access from the local network:

```bash
sudo tailscale serve off
sudo sed -i 's/^CAMERA_HOST=.*/CAMERA_HOST=0.0.0.0/' /etc/raspi-security-camera/environment
sudo systemctl restart raspi-security-camera
```

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
  reboot actions from the camera service; it does not grant shell or general
  systemd administration privileges.
- Update discovery is read-only. Installing an update starts one fixed,
  sandboxed systemd job that can write only the application checkout, can only
  fast-forward its configured upstream, and may restart only the camera service.

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
| `AI_CATEGORIES` | `person,vehicle,animal` | Initial AI result filters; dashboard choices are persisted |
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

Camera enabled state, AI detection, AI category filters, motion detection, and
motion sensitivity are saved atomically whenever they are changed in the
dashboard. The system service stores them in
`/var/lib/raspi-security-camera/device-settings.json` and restores them before
capture and detection start. Active recordings are intentionally not resumed
after a service or device restart.

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
- `GET /api/update` - configured Git upstream and update availability
- `POST /api/update` with `{"confirm":"update"}` - start a safe fast-forward update
- `GET /api/recordings` - list local recordings
- `POST /api/recordings/start` and `/api/recordings/stop` - recording control
- `GET /api/recordings/<id>/stream.mjpg` - browser playback
- `GET /api/recordings/<id>/download` - download raw MJPEG
- `DELETE /api/recordings/<id>` - delete a recording
- `GET /stream.mjpg` - live stream
- `GET /snapshot.jpg` - latest frame
- `GET /healthz` - camera health for authenticated monitoring
