# All-in-one Home Smart Box

A self-contained smart-home appliance built on a Raspberry Pi 5 + Hailo-8 NPU + Pironman 5 Max chassis. Boots straight into a kiosk dashboard and exposes voice control, on-device computer vision, and an LLM assistant — all running locally.

## What's in the box

| Layer | Stack |
|---|---|
| Kiosk display | Pi OS Lite + Xorg + Openbox + Chromium (full-screen, auto-login) |
| Dashboard UI | Single-file HTML, served by nginx on port 3000 |
| System stats | Zero-dep Python HTTP server reading `/proc` |
| Computer vision | YOLO on Hailo-8 NPU via `hailo_apps`, with MJPEG stream + MQTT alerts |
| AI assistant | Regex intent matcher (instant) + `qwen2.5:1.5b` via Ollama (fallback) |
| Voice pipeline | Whisper STT (`tiny`) + Piper TTS, USB mic, custom wake word "Smart Box" |
| Home Assistant | Docker container, subscribes to MQTT detection topics |
| NAS | OpenMediaVault on port 80 (dashboard moved to 3000 to avoid conflict) |

The voice pipeline can drive the kiosk Chromium via Chrome DevTools Protocol on port 9222 — saying *"open Jellyfin"* navigates the on-screen browser.

## Repo layout

```
.
├── dashboard/
│   ├── index.html          # Single-file dashboard (dark theme, live stats, service cards, chat widget)
│   └── pi-stats.py         # /stats JSON endpoint on :5050
├── services/
│   ├── detection_service.py  # Hailo NPU + MQTT + MJPEG (port 8085)
│   ├── camera_stream.py      # Standalone MJPEG (use OR detection, not both)
│   ├── ai_assistant.py       # Intent matcher + Ollama + MQTT + HTTP API on :8086
│   └── voice_pipeline.py     # Wake word + STT + TTS + kiosk navigation via CDP
├── kiosk-extension/        # Chromium extension: floating "back to dashboard" button
│   ├── manifest.json
│   ├── home.css
│   └── home.js
├── nginx/
│   └── dashboard.conf      # Reverse proxy: /, /stats, /ask
├── systemd/
│   ├── pi-stats.service
│   ├── kiosk.service
│   ├── detection.service
│   ├── ai-assistant.service
│   ├── voice-pipeline.service
│   └── getty-autologin.conf
├── scripts/
│   └── kiosk.sh            # X + Chromium launcher with --remote-debugging-port=9222
├── SETUP-GUIDE.md          # Step-by-step deployment
└── README.md
```

## Quick install paths

| File | Goes to |
|---|---|
| `dashboard/index.html` | `/var/www/dashboard/index.html` |
| `dashboard/pi-stats.py` | `/usr/local/bin/pi-stats.py` |
| `scripts/kiosk.sh` | `/usr/local/bin/kiosk.sh` (chmod +x) |
| `kiosk-extension/*` | `/home/pi/kiosk-ext/` |
| `nginx/dashboard.conf` | `/etc/nginx/sites-available/dashboard` (then symlink) |
| `systemd/*.service` | `/etc/systemd/system/` |
| `systemd/getty-autologin.conf` | `/etc/systemd/system/getty@tty1.service.d/autologin.conf` |
| `services/*.py` | `/home/pi/hailo-apps/` |

Then `systemctl daemon-reload && systemctl enable --now pi-stats kiosk detection ai-assistant voice-pipeline`.

See `SETUP-GUIDE.md` for the full walkthrough including auto-login, sudoers, udev rules, and OMV port handling.

## Wake word

Default is **"Smart Box"**. Whisper's mishearings are handled by a three-tier matcher in `voice_pipeline.py` — exact match, known mishearings (`smart fox`, `smart blocks`, etc.), then `smart` + any short word as a fallback.

## Configuration knobs

- **`PI_LAN_IP`** in `services/ai_assistant.py` — your Pi's LAN IP (used for NAVIGATE responses)
- **`MONITORED_CLASSES`** in `services/detection_service.py` — which COCO classes trigger alerts
- **`OLLAMA_MODEL`** in `services/ai_assistant.py` — swap for any model in your Ollama
- **`PIPER_MODEL`** in `services/voice_pipeline.py` — path to your `.onnx` Piper voice

## Hardware

- Raspberry Pi 5 (8 GB) in a Pironman 5 Max chassis (OLED, RGB, fans, power button)
- Hailo-8 NPU (PCIe HAT)
- Pi AI Camera
- USB PnP microphone + 3.5 mm speaker
