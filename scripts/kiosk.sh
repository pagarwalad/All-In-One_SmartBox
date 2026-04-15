#!/bin/bash
# Pi Kiosk Launcher
# Launches a minimal X server with Chromium in full-screen kiosk mode.
# --remote-debugging-port=9222 lets the voice pipeline drive navigation
# via Chrome DevTools Protocol.

export DISPLAY=:0
sleep 3

# Disable screen blanking
xset s off 2>/dev/null
xset -dpms 2>/dev/null
xset s noblank 2>/dev/null

xinit /bin/bash -c "openbox & chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --no-sandbox \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --check-for-update-interval=31536000 \
    --load-extension=/home/pi/kiosk-ext \
    --remote-debugging-port=9222 \
    --homepage=http://localhost:3000 \
    http://localhost:3000" -- :0 -nolisten tcp
