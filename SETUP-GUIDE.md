# Pi 5 Kiosk Dashboard — Complete Setup Guide

> **Your setup:** Raspberry Pi 5 · Pi OS Lite (no desktop) · OpenMediaVault · Docker containers (Home Assistant, Camera, Jellyfin)

This guide sets up a full-screen kiosk dashboard that launches automatically when your Pi boots, showing system stats and clickable cards for each Docker service. You can navigate to any service and return to the dashboard easily.

---

## What You'll End Up With

- **Auto-boot kiosk** — Pi boots straight into a full-screen Chromium browser showing your dashboard
- **Live system stats** — CPU, RAM, disk, temperature, uptime, network IP
- **Service cards** — Click to open Home Assistant, Jellyfin, Camera, OMV, Portainer, etc.
- **Back to Dashboard** — A keyboard shortcut (Alt+Home) and a bookmark button to return
- **Configurable** — Add/edit/remove services through the dashboard UI itself
- **Reliable startup** — Uses systemd (not `.bashrc` hacks), so SSH sessions aren't affected

---

## Step 1: Install Required Packages

SSH into your Pi:

```bash
sudo apt update
sudo apt install -y chromium nginx xorg openbox python3
```

**What each does:**
- `chromium` — the kiosk web browser
- `nginx` — serves the dashboard + proxies the stats API
- `xorg` + `openbox` — minimal display server (since Lite has no desktop)
- `python3` — runs the stats API (already installed on most Pi OS images)

---

## Step 2: Deploy the Dashboard Files

### 2a. Create the dashboard directory

```bash
sudo mkdir -p /var/www/dashboard
```

### 2b. Copy the dashboard HTML

Transfer the `index.html` file (provided with this guide) to your Pi, then:

```bash
sudo cp index.html /var/www/dashboard/index.html
```

### 2c. Replace YOUR_PI_IP with your actual IP

Find your Pi's IP:

```bash
hostname -I | awk '{print $1}'
```

Then replace all placeholders:

```bash
sudo sed -i 's/YOUR_PI_IP/192.168.1.100/g' /var/www/dashboard/index.html
```

*(Replace `192.168.1.100` with your actual IP from above.)*

### 2d. Install the stats API

```bash
sudo cp pi-stats.py /usr/local/bin/pi-stats.py
sudo chmod +x /usr/local/bin/pi-stats.py
```

---

## Step 3: Configure Nginx

This sets up Nginx to serve the dashboard on port 80 and proxy the stats API.

### 3a. Create the Nginx config

```bash
sudo nano /etc/nginx/sites-available/dashboard
```

Paste this:

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    root /var/www/dashboard;
    index index.html;

    # Serve the dashboard
    location / {
        try_files $uri $uri/ =404;
    }

    # Proxy the stats API
    location /stats {
        proxy_pass http://127.0.0.1:5050/stats;
        proxy_set_header Host $host;
    }
}
```

### 3b. Enable the config

```bash
# Remove the default site if it exists
sudo rm -f /etc/nginx/sites-enabled/default

# Enable the dashboard site
sudo ln -sf /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/dashboard

# Test and reload
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx
```

Your dashboard is now accessible at `http://localhost` (port 80).

---

## Step 4: Set Up the Stats API as a Service

This ensures the Python stats server starts on boot and restarts if it crashes.

```bash
sudo nano /etc/systemd/system/pi-stats.service
```

Paste:

```ini
[Unit]
Description=Pi Dashboard Stats API
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/pi-stats.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pi-stats
sudo systemctl start pi-stats
```

Verify it works:

```bash
curl http://localhost/stats
```

You should see JSON with CPU, RAM, disk, etc.

---

## Step 5: Configure Auto-Login on TTY1

Since Pi OS Lite has no desktop, you need auto-login on the first virtual terminal.

### Option A: Use raspi-config (easiest)

```bash
sudo raspi-config
```

Navigate to: **System Options → Boot / Auto Login → Console Autologin**

### Option B: Manual systemd override

```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo nano /etc/systemd/system/getty@tty1.service.d/autologin.conf
```

Paste (replace `pi` with your username if different):

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM
```

Then:

```bash
sudo systemctl daemon-reload
```

---

## Step 6: Create the Kiosk Startup Service

This is the reliable way to launch the kiosk — a systemd service that starts X + Chromium after login, without messing up SSH sessions.

### 6a. Create the kiosk launch script

```bash
sudo nano /usr/local/bin/kiosk.sh
```

Paste:

```bash
#!/bin/bash
# Pi Kiosk Launcher
# Launches minimal X server with Chromium in kiosk mode

export DISPLAY=:0

# Wait for the stats API and nginx to be ready
sleep 3

# Start X with openbox, then launch Chromium
xinit /usr/bin/openbox --startup "chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --check-for-update-interval=31536000 \
    --homepage=http://localhost \
    http://localhost" -- :0 vt1 -nolisten tcp
```

Make it executable:

```bash
sudo chmod +x /usr/local/bin/kiosk.sh
```

### 6b. Create the systemd service

```bash
sudo nano /etc/systemd/system/kiosk.service
```

Paste (replace `pi` with your username):

```ini
[Unit]
Description=Pi Kiosk Dashboard
After=network.target nginx.service pi-stats.service
Wants=nginx.service pi-stats.service

[Service]
Type=simple
User=pi
ExecStart=/usr/local/bin/kiosk.sh
Restart=on-failure
RestartSec=5
Environment=HOME=/home/pi

[Install]
WantedBy=multi-user.target
```

### 6c. Enable the kiosk service

```bash
sudo systemctl daemon-reload
sudo systemctl enable kiosk
```

---

## Step 7: Navigating Back to the Dashboard

Since the kiosk runs full-screen Chromium with no address bar, you need a way to get back to the dashboard from inside a container's web UI. Here are three approaches (use whichever works best for you):

### Approach A: Keyboard Shortcut (simplest)

Alt+Home in Chromium goes to the homepage, which we set to `http://localhost`. Just press **Alt+Home** on your keyboard from any page.

If you have a keyboard connected to the Pi, this works immediately with no extra setup.

### Approach B: Floating "Home" Button via Chromium Extension

Create a tiny local extension that adds a floating home button to every page:

```bash
mkdir -p /home/pi/kiosk-ext
```

Create the manifest:

```bash
nano /home/pi/kiosk-ext/manifest.json
```

```json
{
  "manifest_version": 3,
  "name": "Dashboard Home Button",
  "version": "1.0",
  "content_scripts": [{
    "matches": ["<all_urls>"],
    "js": ["home.js"],
    "css": ["home.css"],
    "run_at": "document_idle"
  }]
}
```

Create the CSS:

```bash
nano /home/pi/kiosk-ext/home.css
```

```css
#pi-dash-home {
  position: fixed !important;
  bottom: 20px !important;
  right: 20px !important;
  z-index: 2147483647 !important;
  width: 56px !important;
  height: 56px !important;
  border-radius: 50% !important;
  background: #00e5a0 !important;
  color: #000 !important;
  font-size: 24px !important;
  border: none !important;
  cursor: pointer !important;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4) !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  transition: transform 0.2s !important;
}
#pi-dash-home:hover {
  transform: scale(1.1) !important;
}
```

Create the JS:

```bash
nano /home/pi/kiosk-ext/home.js
```

```javascript
(function() {
  // Don't show on the dashboard itself
  if (location.port === '' || location.port === '80') return;

  const btn = document.createElement('button');
  btn.id = 'pi-dash-home';
  btn.textContent = '⌂';
  btn.title = 'Back to Dashboard';
  btn.addEventListener('click', function() {
    window.location.href = 'http://localhost';
  });
  document.body.appendChild(btn);
})();
```

Then update the kiosk launch script to load the extension. Edit `/usr/local/bin/kiosk.sh` and add this flag to the chromium command:

```
--load-extension=/home/pi/kiosk-ext
```

So the chromium line becomes:

```bash
chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI,ExtensionManifestV2Disabled \
    --check-for-update-interval=31536000 \
    --load-extension=/home/pi/kiosk-ext \
    --homepage=http://localhost \
    http://localhost
```

This puts a green floating ⌂ button on every container page that takes you back to the dashboard. It does NOT appear on the dashboard itself.

### Approach C: Touchscreen Gesture (if you have a touchscreen)

If you're using a touchscreen, you can add swipe-to-go-home via the extension's JS. Replace the `home.js` with a version that detects a two-finger swipe down to navigate home.

---

## Step 8: Update Service URLs in the Dashboard

Edit the dashboard to match your actual container ports:

```bash
sudo nano /var/www/dashboard/index.html
```

Find the `DEFAULT_SERVICES` array near the top of the `<script>` section and update the URLs to match your Docker setup. Common ports:

| Service          | Typical Port |
|------------------|-------------|
| Home Assistant   | 8123        |
| Jellyfin         | 8096        |
| Frigate (camera) | 5000        |
| OpenMediaVault   | 8080 (or 80 if not moved) |
| Portainer        | 9443        |

You can also add/edit/remove services directly from the dashboard UI by clicking the ⚙ gear button.

---

## Step 9: Handle OMV Port Conflict

OMV's own web UI usually listens on port 80. Since we want Nginx on port 80 for the dashboard, you have two options:

### Option A: Move OMV to a different port (recommended)

In the OMV web UI, go to **System → Workbench** and change the port to `8080` (or another port). Then update the dashboard's OMV service URL accordingly.

### Option B: Use a different port for the dashboard

If you'd rather not touch OMV, change Nginx to listen on port 3000 instead:

```nginx
listen 3000 default_server;
listen [::]:3000 default_server;
```

Then update the kiosk script to load `http://localhost:3000` and the Chromium extension's `home.js` to navigate to `http://localhost:3000`.

---

## Step 10: Reboot and Test

```bash
sudo reboot
```

After reboot, you should see:

1. The Pi auto-logs in on TTY1
2. X starts with Chromium in full-screen kiosk mode
3. The dashboard appears with live stats
4. Clicking a service card navigates to that container
5. The green ⌂ button (if using the extension) lets you return to the dashboard

---

## Troubleshooting

### Dashboard shows but stats are all dashes
The stats API might not be running:
```bash
sudo systemctl status pi-stats
curl http://localhost/stats
```

### Black screen on boot
Check if the kiosk service started:
```bash
sudo systemctl status kiosk
journalctl -u kiosk -n 50
```

### Chromium shows "restore session" popup
Add `--disable-session-crashed-bubble` to the chromium flags (already included in the script above). Also remove any crash state:
```bash
rm -rf /home/pi/.config/chromium/Singleton*
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' /home/pi/.config/chromium/Default/Preferences 2>/dev/null
```

### Screen goes blank after a while (screensaver)
Add these to `/usr/local/bin/kiosk.sh` before the `xinit` line:
```bash
xset s off
xset -dpms
xset s noblank
```

Or better, add them to openbox autostart. Create `/home/pi/.config/openbox/autostart`:
```bash
mkdir -p /home/pi/.config/openbox
echo -e "xset s off\nxset -dpms\nxset s noblank" > /home/pi/.config/openbox/autostart
```

### SSH still works normally?
Yes. The kiosk service only starts X on TTY1 (the physical display). SSH connections are completely unaffected.

### Want to exit kiosk mode temporarily?
Press `Ctrl+Alt+F2` to switch to TTY2 and get a normal console login. `Ctrl+Alt+F1` returns to the kiosk.

---

## File Summary

| File | Purpose |
|------|---------|
| `/var/www/dashboard/index.html` | The dashboard web page |
| `/usr/local/bin/pi-stats.py` | Stats API (CPU, RAM, disk, etc.) |
| `/usr/local/bin/kiosk.sh` | Kiosk launch script |
| `/etc/nginx/sites-available/dashboard` | Nginx configuration |
| `/etc/systemd/system/pi-stats.service` | Stats API systemd service |
| `/etc/systemd/system/kiosk.service` | Kiosk systemd service |
| `/home/pi/kiosk-ext/` | Chromium extension for "Back to Dashboard" button |
