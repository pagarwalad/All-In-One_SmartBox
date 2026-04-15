#!/usr/bin/env python3
"""
MJPEG stream server for the Pi AI Camera (standalone, no detection).
Serves a live video feed that Home Assistant can display.
Runs on port 8085.

Note: Use this OR detection_service.py, not both -- they bind the same port.
"""
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import cv2
from picamera2 import Picamera2

# Global frame storage
latest_frame = None
frame_lock = threading.Lock()


def camera_thread():
    """Capture frames from the Pi camera continuously."""
    global latest_frame
    cam = Picamera2()
    config = cam.create_preview_configuration(
        main={"size": (640, 480), "format": "RGB888"}
    )
    cam.configure(config)
    cam.start()
    print("Camera stream started")

    while True:
        frame = cam.capture_array()
        # picamera2 gives RGB; cv2 expects BGR for correct JPEG colors
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        _, jpeg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
        with frame_lock:
            latest_frame = jpeg.tobytes()
        time.sleep(0.1)  # ~10 FPS


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with frame_lock:
                        frame = latest_frame
                    if frame is None:
                        time.sleep(0.1)
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif self.path == "/snapshot":
            with frame_lock:
                frame = latest_frame
            if frame:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(frame)
            else:
                self.send_response(503)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=camera_thread, daemon=True).start()
    server = HTTPServer(("0.0.0.0", 8085), StreamHandler)
    print("MJPEG stream at http://0.0.0.0:8085/stream")
    print("Snapshot at     http://0.0.0.0:8085/snapshot")
    server.serve_forever()
