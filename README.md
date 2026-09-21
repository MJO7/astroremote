# AstroRemote

AstroRemote is a small local web intervalometer for long-exposure astrophotography with a Nikon D3500 and `gphoto2`. The computer owns the USB camera and the complete imaging sequence; an iPhone or iPad is only a remote control and status display. Locking the phone, closing Safari, or briefly losing Wi-Fi does not stop a sequence.

The application uses FastAPI, Uvicorn, `asyncio`, Server-Sent Events, and plain HTML/CSS/JavaScript. There is no database, Node.js, Docker, cloud service, or external frontend dependency.

## Quick start in simulation mode

Simulation mode exercises the real sequence state machine, creates tiny placeholder `.nef` files, and needs no camera or `gphoto2` installation.

```bash
cd astroremote
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x start.sh scripts/release-macos-ptp.sh
./start.sh --simulate
```

Open `http://localhost:8000` on the computer. The startup output also prints a LAN address such as `http://192.168.1.42:8000`; open that address in Safari on a phone or tablet connected to the same Wi-Fi network.

By default, images go below `~/Pictures/AstroRemote`. Change that server-controlled root before starting:

```bash
ASTROREMOTE_OUTPUT_ROOT=/Volumes/AstroDisk/Lights ./start.sh
```

The web UI can select only a relative subfolder beneath this root. Absolute paths and `..` traversal are rejected.

The **Save images to** control offers two modes:

- **This Mac** — the camera capture target is switched to Internal RAM, each image is downloaded into the configured Mac root/session subfolder, and the frame is counted only after the local file exists.
- **Camera memory card** — the target is switched to Memory card and AstroRemote waits for the camera's `FILEADDED` event without downloading a Mac copy. The Nikon assigns its normal `DSC_...` filename.

To use a particular Mac folder, set its absolute path as the server-controlled root and optionally enter a session subfolder in the UI:

```bash
ASTROREMOTE_OUTPUT_ROOT="/Users/yourname/Pictures/Astro Sessions" ./start.sh
```

Safari on an iPhone cannot safely browse the Mac filesystem, so the browser is intentionally limited to subfolders beneath that configured root.

## Install with a physical camera

### macOS

```bash
brew install gphoto2
cd astroremote
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x start.sh scripts/release-macos-ptp.sh
./start.sh
```

### Debian, Ubuntu, or Raspberry Pi OS

```bash
sudo apt update
sudo apt install gphoto2
cd astroremote
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x start.sh
./start.sh
```

The server binds to `0.0.0.0:8000`. Keep the computer and iPhone/iPad on the same trusted Wi-Fi network, then open the printed phone/tablet URL in Safari. Safari’s **Share → Add to Home Screen** makes AstroRemote behave like a lightweight standalone app.

To use a different port:

```bash
ASTROREMOTE_PORT=8080 ./start.sh
```

If the LAN URL is not reachable, allow incoming connections for Python/Uvicorn in the computer firewall and confirm the Wi-Fi network does not isolate wireless clients.

## Camera preparation

1. Put the Nikon in manual exposure mode and switch the lens to manual focus.
2. Disable camera auto-off or set its timeout long enough for the session when possible.
3. Connect the camera directly by USB and turn it on. Avoid an unreliable or unpowered hub.
4. Close Photos, Image Capture, camera-import applications, and other `gphoto2` processes.
5. Start AstroRemote and press the camera refresh button.
6. Confirm that the UI shows the detected model, port, and camera-reported ISO/aperture choices.

Aperture control depends on the attached lens and what libgphoto2 exposes. A manual lens may not expose an aperture control; AstroRemote disables the control rather than inventing values. Long-exposure duration is deliberately independent of the camera’s normal shutter-speed choices.

## macOS USB/PTP ownership

macOS services such as `ptpcamerad`, `icdd`, and `mscamerad-xpc` sometimes claim the camera before `gphoto2`. AstroRemote detects the usual USB-claim error and explains it in the UI. It never asks for, transmits, or stores a sudo password, and the web server must not run as root.

First try closing Photos and Image Capture, unplugging/reconnecting the camera, and refreshing. For occasional manual recovery, run this in a separate Terminal:

```bash
sudo killall -TERM ptpcamerad icdd mscamerad-xpc 2>/dev/null || true
```

Then press Refresh Camera. macOS may restart these services later.

### Optional one-tap helper

The included helper uses only non-interactive `sudo -n` and is disabled unless explicitly configured. For a dedicated imaging computer, use `sudo visudo -f /etc/sudoers.d/astroremote-ptp` and add the following three narrowly scoped rules, replacing `YOUR_USER` with the account that runs AstroRemote:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/killall -TERM ptpcamerad
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/killall -TERM icdd
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/killall -TERM mscamerad-xpc
```

Check the command locations with `command -v sudo killall`; on standard macOS installations they are `/usr/bin/sudo` and `/usr/bin/killall`. Make the repository helper executable and provide its absolute path:

```bash
chmod +x scripts/release-macos-ptp.sh
ASTROREMOTE_PTP_HELPER="$PWD/scripts/release-macos-ptp.sh" ./start.sh
```

When a USB-claim error occurs, the UI offers a release button. The helper has no general shell interface and receives no browser input. If you do not want passwordless commands, leave `ASTROREMOTE_PTP_HELPER` unset and use the manual Terminal command.

## Reliability behavior

- A single background task owns a single camera lock for the complete sequence. Multiple browsers can watch, but cannot start competing sequences or refresh/configure the camera mid-capture.
- Bulb is closed in a `finally` block. A normal completion, STOP, camera error, or task cancellation all attempt `/main/actions/bulb=0`.
- STOP sets a server-side cancellation event. Exposure and interval timers check it repeatedly; the HTTP request itself does not wait for a long exposure.
- After closing Bulb, AstroRemote waits for the resulting camera event. In Mac mode, a frame is counted only after a new local file exists. In memory-card mode, it is counted only after gphoto2 reports the camera's `FILEADDED` event.
- Filenames use `light_0001.%C`, preserving the camera’s real extension. Existing `light_NNNN.*` files are scanned before the sequence and rechecked before every frame. AstroRemote never enables force-overwrite.
- Raw `gphoto2` output is kept in the Advanced/Debug panel. Nikon `UNKNOWN PTP Property` notifications do not cause failure by themselves; exit status plus the required local file or camera-card event decide success.
- On macOS, a `caffeinate -dims` child process runs only while a sequence is active. It is terminated afterward. On Linux/Raspberry Pi, configure the desktop or system power manager not to suspend during imaging.
- Event streaming is for display only. All state and timing live on the server; reconnecting Safari immediately fetches the current state.

## Browser Live View, zoom, and camera gallery

When idle, the **Camera display** panel can send the controls actually reported by the connected camera:

- **Open Live View** opens a dedicated `/liveview` page and starts one persistent `gphoto2 --stdout --capture-movie` process. JPEG preview frames go directly to the browser without transcoding. AstroRemote retains only the newest frame, so a slow browser drops stale frames instead of accumulating lag.
- Live View includes 1×, 2×, 4×, and 8× browser-side magnification. Drag the blue target, use the direction pad, or press the keyboard arrow keys to move the zoom area. This changes only the display and never interrupts the camera preview stream.
- **Camera Gallery** opens `/playback`, recursively reads the camera card, and presents camera-generated thumbnails in a responsive newest-first grid. Thumbnails are lazy-loaded and cached in bounded memory. Selecting one opens a larger lightbox with previous/next controls.
- Gallery access is deliberately read-only: there are no delete, rename, or write operations, and original NEF/JPEG files remain on the camera card.
- Starting a sequence automatically exits camera-side Live View/playback mode before Bulb control.
- On connection and after every completed/stopped sequence, AstroRemote automatically returns the D3500 to local controls/playback mode.
- Both display buttons are disabled during a sequence so they cannot race with the shutter command.

Nikon firmware ultimately decides which rear-panel operations are allowed while USB is attached. The D3500 reports both `viewfinder` and `applicationmode` through libgphoto2, but if a particular firmware revision still refuses playback, unplugging USB remains the camera-enforced fallback. AstroRemote shows a clear error when either property is not exposed.

While USB remote Live View is active, the physical Live View switch may remain locked because the persistent gphoto2 process owns the PTP session. Use AstroRemote's **Open Live View** and **Camera Gallery** screens instead. Closing or leaving the viewer, starting a sequence, refreshing the camera, or stopping the server safely terminates the persistent process before another camera command begins.

If the process is forcibly killed by the operating system, no application can run its cleanup code. In that exceptional situation, turn the camera off or disconnect USB if the shutter remains open.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `ASTROREMOTE_SIMULATE` | `0` | Set to `1` to use the simulated D3500 |
| `ASTROREMOTE_OUTPUT_ROOT` | `~/Pictures/AstroRemote` | Server-controlled root for downloaded files |
| `ASTROREMOTE_PORT` | `8000` | HTTP listen port used by `start.sh` |
| `ASTROREMOTE_PTP_HELPER` | unset | Absolute path to the optional executable macOS helper |

## API

- `GET /api/camera` — last detected camera and port
- `GET /api/camera/config` — camera-reported ISO, aperture, shutter, and mode properties
- `POST /api/camera/refresh` — redetect and query configuration (rejected while imaging)
- `POST /api/camera/release` — invoke the configured macOS helper, then refresh
- `POST /api/camera/display` — select camera-side `liveview` or `playback` mode while idle
- `GET /liveview` — dedicated full-window browser viewer
- `POST /api/liveview/start` and `POST /api/liveview/stop` — manage the persistent preview session
- `GET /api/liveview/stream` — latest-frame multipart preview stream
- `GET /api/liveview/status` — preview process state and frame number
- `GET /playback` — read-only, scrollable camera-card gallery
- `GET /api/gallery` — recursively list camera-card media
- `GET /api/gallery/thumbnail?path=...` — retrieve a camera-generated preview for an indexed file
- `GET /api/status` — current server-owned sequence state
- `POST /api/sequence/start` — validate and start a background sequence
- `POST /api/sequence/stop` — request immediate safe stop
- `GET /api/logs` and `GET /api/logs?debug=true` — session or raw debug messages
- `GET /api/events` — Server-Sent Events status stream

FastAPI’s interactive schema is available at `http://localhost:8000/docs`.

## Tests

No physical camera is needed:

```bash
cd astroremote
source .venv/bin/activate
pytest -q
```

The suite covers validation, path confinement, both storage targets, filename collision avoidance, camera-card event verification, Live View/playback serialization, subprocess argument invocation, sequence progression, post-download interval timing, STOP during exposure and interval, simulated failures/disconnection, state retrieval, concurrency protection, and Bulb-close attempts after failure.

## Physical Nikon D3500 verification checklist

Start with a short session in an empty output folder:

1. Run `gphoto2 --auto-detect` and confirm the D3500 and a USB port appear.
2. Start AstroRemote normally and verify the same camera appears in the UI.
3. Confirm ISO and aperture choices match `gphoto2 --get-config /main/imgsettings/iso` and the aperture path discovered by `gphoto2 --list-config`.
4. Shoot one 5-second exposure. Confirm the physical shutter opens and closes, a uniquely named NEF appears, and the UI counts the frame only after download.
5. Shoot a 30-second, two-frame sequence with a 5-second interval. Confirm the five seconds begin after the first file finishes downloading.
6. During a 30-second exposure, press STOP. Confirm the shutter closes promptly, any produced image is downloaded, and no second exposure begins.
7. Press STOP during an interval. Confirm the next exposure never starts.
8. Lock the phone or close Safari during a two-frame sequence, then reconnect. Confirm imaging continued and the current status is restored.
9. Pre-create `light_0001.nef`, run another exposure, and confirm AstroRemote chooses `light_0002` or higher without a prompt or overwrite.
10. Review Advanced/Debug output for hardware-specific behavior. Nikon live-view-prohibit property notifications alone are expected and harmless.
11. While idle, test **Open Live View**, all four zoom levels, target dragging, and the direction pad. Then open **Camera Gallery**, scroll through several pages, and open a thumbnail in the lightbox. Confirm starting a sequence exits Live View and still closes Bulb normally.
12. Run one frame in **Camera memory card** mode and confirm no Mac file is created while the NEF appears in Nikon playback.

For the first unattended night, remain near the camera for several frames and verify shutter closure, file integrity, camera battery/external power, storage capacity, and computer power-management behavior.

## Project layout

```text
astroremote/
  app/
    main.py                 FastAPI routes, startup, and SSE
    camera.py               gphoto2 and simulation abstractions
    sequence.py             background state machine and locking
    models.py               API validation and status models
    platform_support.py     macOS keep-awake and optional PTP helper
    static/                 mobile-first web interface and manifest
  scripts/
    release-macos-ptp.sh
  tests/
  requirements.txt
  pytest.ini
  start.sh
```
