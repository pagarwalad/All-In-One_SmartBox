#!/usr/bin/env python3
"""
Pi Stats API — lightweight system metrics endpoint for the dashboard.
Runs on port 5050 and returns JSON with CPU, RAM, disk, uptime, and network info.
"""

import json
import time
import socket
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

def get_cpu_temp():
    """Read CPU temperature from thermal zone."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None

def get_cpu_percent():
    """Calculate CPU usage from /proc/stat over a 0.5s sample."""
    def read_stat():
        with open('/proc/stat', 'r') as f:
            parts = f.readline().split()
            idle = int(parts[4])
            total = sum(int(x) for x in parts[1:])
            return idle, total
    idle1, total1 = read_stat()
    time.sleep(0.5)
    idle2, total2 = read_stat()
    idle_delta = idle2 - idle1
    total_delta = total2 - total1
    if total_delta == 0:
        return 0
    return round((1 - idle_delta / total_delta) * 100, 1)

def get_memory():
    """Parse /proc/meminfo for RAM usage."""
    info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            parts = line.split()
            key = parts[0].rstrip(':')
            info[key] = int(parts[1])  # in kB
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', 0)
    used = total - available
    return {
        'ram_total': round(total / 1048576, 1),    # GB
        'ram_used': round(used / 1048576, 1),       # GB
        'ram_percent': round(used / total * 100, 1) if total else 0
    }

def get_disk():
    """Get root filesystem usage via statvfs."""
    import os
    st = os.statvfs('/')
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    used = total - free
    return {
        'disk_total': round(total / (1024**3), 1),
        'disk_used': round(used / (1024**3), 1),
        'disk_percent': round(used / total * 100, 1) if total else 0
    }

def get_uptime():
    """Get uptime string and boot time."""
    with open('/proc/uptime', 'r') as f:
        secs = float(f.read().split()[0])
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    mins = int((secs % 3600) // 60)
    if days > 0:
        upstr = f"{days}d {hours}h {mins}m"
    elif hours > 0:
        upstr = f"{hours}h {mins}m"
    else:
        upstr = f"{mins}m"
    boot_ts = time.time() - secs
    boot_time = time.strftime('%b %d %H:%M', time.localtime(boot_ts))
    return upstr, boot_time

def get_ip():
    """Get the primary LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

def get_hostname():
    return socket.gethostname()


class StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/stats':
            self.send_error(404)
            return

        mem = get_memory()
        disk = get_disk()
        uptime_str, boot_time = get_uptime()

        data = {
            'cpu_percent': get_cpu_percent(),
            'cpu_temp': get_cpu_temp(),
            **mem,
            **disk,
            'uptime': uptime_str,
            'boot_time': boot_time,
            'ip': get_ip(),
            'hostname': get_hostname()
        }

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # Silence request logs


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 5050), StatsHandler)
    print('Pi Stats API running on http://127.0.0.1:5050/stats')
    server.serve_forever()
