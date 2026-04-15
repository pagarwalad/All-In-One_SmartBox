#!/usr/bin/env python3
"""
Home Monitoring Detection Service for the All-in-one Home Smart Box

Runs YOLO object detection on the Pi AI Camera via the Hailo-8 NPU,
publishes detection events to Home Assistant via MQTT, and serves an
MJPEG stream with detection overlays on port 8085 for HA's camera card.
"""

# ----- IMPORTANT: env vars must be set before GStreamer / hailo_apps load -----
import os
import sys

os.environ["GST_PLUGIN_FEATURE_RANK"] = "vaapidecodebin:NONE"

_headless = "--headless" in sys.argv
if _headless:
    sys.argv.remove("--headless")
    os.environ["GST_VIDEO_SINK"] = "fakesink"
else:
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("GST_VIDEO_SINK", "ximagesink")

# Monkey-patch hailo_apps' hardcoded video sink constant.
# The framework reads a Python constant from defines.py, not the env var,
# so we have to override it here before the pipeline modules import it.
import hailo_apps.python.core.common.defines as _defines  # noqa: E402
_defines.GST_VIDEO_SINK = "fakesink" if _headless else "ximagesink"
# -----------------------------------------------------------------------------

import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from collections import defaultdict

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: F401

import cv2
import numpy as np
import hailo
import paho.mqtt.client as mqtt

from hailo_apps.python.pipeline_apps.detection.detection_pipeline import GStreamerDetectionApp
from hailo_apps.python.core.common.buffer_utils import get_caps_from_pad, get_numpy_from_buffer
from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class

hailo_logger = get_logger(__name__)

# ======================================================
# CONFIGURATION
# ======================================================

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_DETECTIONS = "smartbox/camera/detections"
MQTT_TOPIC_STATUS = "smartbox/camera/status"
MQTT_TOPIC_SUMMARY = "smartbox/camera/summary"

# COCO classes that trigger MQTT alerts
MONITORED_CLASSES = {"person", "cat", "dog", "car", "bicycle", "bird"}

# Minimum confidence (0-1) to publish an alert
CONFIDENCE_THRESHOLD = 0.55

# Per-class cooldown -- avoid flooding HA with the same alert repeatedly
COOLDOWN_SECONDS = 30

STREAM_PORT = 8085


# ======================================================
# MJPEG STREAM SERVER (Home Assistant pulls this URL)
# ======================================================

latest_jpeg = None
jpeg_lock = threading.Lock()


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with jpeg_lock:
                        frame = latest_jpeg
                    if frame is None:
                        time.sleep(0.1)
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.1)  # cap clients at ~10 FPS
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif self.path == "/snapshot":
            with jpeg_lock:
                frame = latest_jpeg
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


def start_stream_server():
    server = HTTPServer(("0.0.0.0", STREAM_PORT), StreamHandler)
    hailo_logger.info(f"MJPEG stream at  http://0.0.0.0:{STREAM_PORT}/stream")
    hailo_logger.info(f"Snapshot at      http://0.0.0.0:{STREAM_PORT}/snapshot")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ======================================================
# MQTT
# ======================================================

mqtt_client = None


def setup_mqtt():
    global mqtt_client
    mqtt_client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="smartbox-detection",
    )
    mqtt_client.will_set(
        MQTT_TOPIC_STATUS,
        json.dumps({"status": "offline", "timestamp": datetime.now().isoformat()}),
        qos=1, retain=True,
    )
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        hailo_logger.info(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
        mqtt_client.publish(
            MQTT_TOPIC_STATUS,
            json.dumps({"status": "online", "timestamp": datetime.now().isoformat()}),
            qos=1, retain=True,
        )
    except Exception as e:
        hailo_logger.error(f"Failed to connect to MQTT broker: {e}")
        raise


# ======================================================
# DETECTION CALLBACK
# ======================================================

class DetectionCallbackData(app_callback_class):
    def __init__(self):
        super().__init__()
        self.use_frame = True  # Enable frame capture for streaming
        self.last_published = defaultdict(float)
        self.confidence_threshold = CONFIDENCE_THRESHOLD
        self.cooldown_seconds = COOLDOWN_SECONDS
        self.total_alerts_sent = 0


def detection_callback(element, buffer, user_data):
    """Called per frame: extract detections, draw overlays, publish MQTT."""
    global latest_jpeg

    if buffer is None:
        return

    frame_count = user_data.get_count()
    now = time.time()
    timestamp = datetime.now().isoformat()

    # ----- pull frame for streaming -----
    pad = element.get_static_pad("src")
    fmt, width, height = get_caps_from_pad(pad)
    frame = None
    if fmt is not None and width is not None and height is not None:
        try:
            frame = get_numpy_from_buffer(buffer, fmt, width, height)
        except Exception:
            frame = None

    # ----- extract Hailo detections -----
    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)

    all_objects = []
    alerts = []

    for detection in detections:
        label = detection.get_label()
        confidence = detection.get_confidence()
        all_objects.append({"class": label, "confidence": round(float(confidence), 3)})

        # ----- draw overlay -----
        if frame is not None:
            bbox = detection.get_bbox()
            x1 = int(bbox.xmin() * width)
            y1 = int(bbox.ymin() * height)
            x2 = int(bbox.xmax() * width)
            y2 = int(bbox.ymax() * height)
            color = (0, 255, 0) if label in MONITORED_CLASSES else (128, 128, 128)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{label} {confidence:.0%}",
                        (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, color, 1)

        # ----- filter for alerts -----
        if label not in MONITORED_CLASSES:
            continue
        if confidence < user_data.confidence_threshold:
            continue
        if now - user_data.last_published[label] < user_data.cooldown_seconds:
            continue

        track_id = 0
        track = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
        if len(track) == 1:
            track_id = track[0].get_id()

        bbox = detection.get_bbox()
        alerts.append({
            "class": label,
            "confidence": round(float(confidence), 3),
            "track_id": track_id,
            "bbox": {
                "xmin": round(float(bbox.xmin()), 4),
                "ymin": round(float(bbox.ymin()), 4),
                "xmax": round(float(bbox.xmax()), 4),
                "ymax": round(float(bbox.ymax()), 4),
            },
            "timestamp": timestamp,
            "frame": frame_count,
        })
        user_data.last_published[label] = now

    # ----- update MJPEG (every 3rd frame keeps CPU low) -----
    if frame is not None and frame_count % 3 == 0:
        try:
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            _, jpeg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
            with jpeg_lock:
                latest_jpeg = jpeg.tobytes()
        except Exception:
            pass

    # ----- publish alerts -----
    for alert in alerts:
        mqtt_client.publish(MQTT_TOPIC_DETECTIONS, json.dumps(alert), qos=1)
        user_data.total_alerts_sent += 1
        hailo_logger.info(
            f"ALERT: {alert['class']} (conf={alert['confidence']}, "
            f"track_id={alert['track_id']}) | Total alerts: {user_data.total_alerts_sent}"
        )

    # ----- periodic summary -----
    if frame_count % 200 == 0 and frame_count > 0:
        unique_classes = list(set(obj["class"] for obj in all_objects))
        summary = {
            "frame": frame_count,
            "timestamp": timestamp,
            "objects_in_frame": unique_classes,
            "detection_count": len(detections),
            "total_alerts_sent": user_data.total_alerts_sent,
        }
        mqtt_client.publish(MQTT_TOPIC_SUMMARY, json.dumps(summary), qos=0)

    if frame_count % 50 == 0:
        print(f"Frame: {frame_count} | Detections: {len(detections)} | "
              f"Alerts: {user_data.total_alerts_sent}")


# ======================================================
# MAIN
# ======================================================

def main():
    hailo_logger.info(
        f"Display mode: GST_VIDEO_SINK={os.environ.get('GST_VIDEO_SINK', 'not set')}"
    )

    setup_mqtt()
    start_stream_server()

    hailo_logger.info("Starting Home Monitoring Detection Service")
    hailo_logger.info(f"MJPEG stream: http://0.0.0.0:{STREAM_PORT}/stream")
    hailo_logger.info(f"Monitored classes: {MONITORED_CLASSES}")

    user_data = DetectionCallbackData()

    try:
        app = GStreamerDetectionApp(detection_callback, user_data)
        app.run()
    except KeyboardInterrupt:
        hailo_logger.info("Shutting down...")
    finally:
        if mqtt_client:
            mqtt_client.publish(
                MQTT_TOPIC_STATUS,
                json.dumps({"status": "offline", "timestamp": datetime.now().isoformat()}),
                qos=1, retain=True,
            )
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()
