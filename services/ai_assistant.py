#!/usr/bin/env python3
"""
AI Assistant Service for the All-in-one Home Smart Box

- Fast pattern-based intent matcher for system / vision / navigation queries
- Ollama LLM fallback for open-ended questions
- Subscribes to MQTT camera detections to answer "what do you see?"
- Exposes BOTH an MQTT interface (smartbox/assistant/query)
  AND an HTTP API on :8086/ask?q=... for the dashboard chat widget
"""

import os
import sys
import json
import time
import re
import threading
import subprocess
import urllib.parse
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import paho.mqtt.client as mqtt

# ======================================================
# CONFIGURATION
# ======================================================

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_QUERY = "smartbox/assistant/query"
MQTT_TOPIC_RESPONSE = "smartbox/assistant/response"
MQTT_TOPIC_STATUS = "smartbox/assistant/status"

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:1.5b"

HA_URL = "http://localhost:8123"
HA_TOKEN = ""

STATS_URL = "http://localhost:3000/stats"

DETECTION_SERVICE = "detection"

API_PORT = 8086

# Replace with your Pi's LAN IP (used for NAVIGATE responses)
PI_LAN_IP = "192.168.31.117"

# ======================================================
# SHARED STATE
# ======================================================

mqtt_client = None
latest_detections = []
detection_lock = threading.Lock()


# ======================================================
# SYSTEM DATA
# ======================================================

def get_system_stats():
    try:
        r = requests.get(STATS_URL, timeout=3)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_docker_containers():
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}: {{.Status}}"],
            capture_output=True, text=True, timeout=5
        )
        containers = result.stdout.strip().split("\n")
        return {"containers": containers, "count": len(containers)}
    except Exception as e:
        return {"error": str(e)}


def get_storage_info():
    try:
        result = subprocess.run(
            ["df", "-h", "/", "/srv/data"],
            capture_output=True, text=True, timeout=5
        )
        return {"storage": result.stdout.strip()}
    except Exception as e:
        return {"error": str(e)}


# ======================================================
# INTENT MATCHER -- regex patterns mapped to intent names.
# Fast: O(patterns) per query, no LLM needed.
# ======================================================

INTENT_PATTERNS = {
    "cpu_temp": [
        r"cpu\s*(temp|temperature)",
        r"how\s*hot",
        r"temperature",
        r"thermal",
    ],
    "cpu_usage": [
        r"cpu\s*(usage|load|percent|utilization)",
        r"how\s*(busy|loaded)\s*(is)?\s*(the)?\s*(cpu|processor)",
    ],
    "ram_usage": [
        r"(ram|memory)\s*(usage|free|available|used)",
        r"how\s*much\s*(ram|memory)",
    ],
    "disk_usage": [
        r"(disk|storage)\s*(usage|space|free|available|used)",
        r"how\s*much\s*(disk|storage)\s*space",
    ],
    "uptime": [
        r"uptime",
        r"how\s*long\s*(has)?\s*(the)?\s*(system|pi|box)\s*(been)?\s*(running|up|on)",
        r"when\s*did\s*(it|the\s*pi)\s*(boot|start)",
    ],
    "system_status": [
        r"(system|box)\s*status",
        r"how\s*is\s*(the)?\s*(system|box|pi)",
        r"system\s*(health|info|overview)",
        r"status\s*report",
    ],
    "containers": [
        r"(docker\s*)?(container|service)s?\s*(status|running|list)?",
        r"what\s*(services?|containers?)\s*(are)?\s*running",
        r"list\s*(all\s*)?(services?|containers?)",
    ],
    "network": [
        r"(ip|network)\s*(address|info|status)",
        r"what\s*(is|s)\s*(my|the)\s*ip",
    ],
    "camera_status": [
        r"(camera|detection|monitoring)\s*(status|running)",
        r"is\s*(the)?\s*camera\s*(on|running|working)",
    ],
    "vision_query": [
        r"what\s*(do|can)\s*you\s*see",
        r"what\s*is\s*(in\s*front|visible|in\s*view)",
        r"describe\s*(what|the)\s*(you\s*see|view|scene|camera)",
        r"who\s*is\s*(there|home|at\s*the\s*door|in\s*the\s*room)",
        r"is\s*(there\s*)?(anyone|somebody|someone)\s*(there|home|visible|around)",
        r"check\s*(the)?\s*camera",
        r"look\s*(at|around)",
        r"any\s*(people|person|one)\s*(detected|visible|around)",
    ],
    "open_homeassistant": [
        r"open\s*(home\s*assistant|ha|home)",
        r"go\s*to\s*(home\s*assistant|ha|home)",
        r"show\s*(home\s*assistant|ha)",
        r"launch\s*(home\s*assistant|ha)",
    ],
    "open_jellyfin": [
        r"open\s*(jellyfin|jelly|media|movies)",
        r"go\s*to\s*(jellyfin|jelly|media)",
        r"play\s*(media|movies|music)",
        r"launch\s*(jellyfin|jelly)",
    ],
    "open_omv": [
        r"open\s*(omv|openmediavault|nas|storage|files)",
        r"go\s*to\s*(omv|nas|storage|files)",
        r"launch\s*(omv|nas)",
    ],
    "open_portainer": [
        r"open\s*(portainer|docker|containers)",
        r"go\s*to\s*(portainer|docker)",
        r"launch\s*(portainer|docker)",
    ],
    "open_dashboard": [
        r"open\s*(dashboard|home\s*screen|main)",
        r"go\s*(home|back|to\s*dashboard)",
        r"show\s*dashboard",
    ],
    "help": [
        r"^help$",
        r"what\s*can\s*you\s*do",
        r"(available\s*)?commands",
    ],
}


def match_intent(query):
    q = query.lower().strip()
    for intent, patterns in INTENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, q):
                return intent
    return None


# ======================================================
# VISION QUERY HANDLER
# ======================================================

def handle_vision_query():
    """Describe what the camera sees, using recent MQTT detections
    and (optionally) the LLM for natural-language polish."""
    with detection_lock:
        recent = list(latest_detections)

    if not recent:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "detection"],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() != "active":
                return "The camera detection service is not running. Cannot check what's visible."
        except Exception:
            pass
        return "The camera is active but no objects have been detected recently. The area appears clear."

    now = time.time()
    recent_filtered = [
        d for d in recent
        if (now - datetime.fromisoformat(d["timestamp"]).timestamp()) < 60
    ]

    if not recent_filtered:
        return "No objects detected in the last 60 seconds. The area appears clear."

    class_counts = {}
    max_confidence = {}
    for d in recent_filtered:
        cls = d["class"]
        conf = d["confidence"]
        class_counts[cls] = class_counts.get(cls, 0) + 1
        if cls not in max_confidence or conf > max_confidence[cls]:
            max_confidence[cls] = conf

    descriptions = []
    for cls, count in class_counts.items():
        conf = max_confidence[cls]
        if count > 1:
            descriptions.append(f"{count} {cls} detections (highest confidence: {conf:.0%})")
        else:
            descriptions.append(f"a {cls} (confidence: {conf:.0%})")

    objects_str = ", ".join(descriptions)
    last_det = recent_filtered[-1]
    last_time = last_det["timestamp"].split("T")[1].split(".")[0]
    response = f"The camera currently sees: {objects_str}. Last detection at {last_time}."

    # Polish with LLM if there's something interesting in frame
    if any(cls in ["person", "cat", "dog"] for cls in class_counts):
        detection_context = json.dumps([
            {"class": d["class"], "confidence": round(d["confidence"], 2),
             "bbox": d.get("bbox", {})}
            for d in recent_filtered
        ])
        llm_prompt = (
            f"The AI camera detected the following objects: {detection_context}. "
            f"Describe what the camera sees in 1-2 natural sentences. "
            f"Be specific about the objects and their positions if bbox data is available."
        )
        try:
            llm_response = handle_llm_query(llm_prompt)
            response += f"\n\nAI Analysis: {llm_response}"
        except Exception:
            pass

    return response


# ======================================================
# INTENT EXECUTION
# ======================================================

def handle_intent(intent):
    stats = get_system_stats()

    if intent == "cpu_temp":
        return f"The CPU temperature is {stats.get('cpu_temp', 'unknown')}\u00B0C."

    elif intent == "cpu_usage":
        return f"CPU usage is currently at {stats.get('cpu_percent', 'unknown')}%."

    elif intent == "ram_usage":
        used = stats.get("ram_used", "?")
        total = stats.get("ram_total", "?")
        pct = stats.get("ram_percent", "?")
        return f"RAM usage: {used} GB / {total} GB ({pct}% used)."

    elif intent == "disk_usage":
        used = stats.get("disk_used", "?")
        total = stats.get("disk_total", "?")
        pct = stats.get("disk_percent", "?")
        return f"Disk usage: {used} GB / {total} GB ({pct}% used)."

    elif intent == "uptime":
        uptime = stats.get("uptime", "unknown")
        boot = stats.get("boot_time", "unknown")
        return f"System uptime: {uptime} (since {boot})."

    elif intent == "system_status":
        return (
            f"System Status Report:\n"
            f"  CPU: {stats.get('cpu_percent', '?')}% at {stats.get('cpu_temp', '?')}\u00B0C\n"
            f"  RAM: {stats.get('ram_percent', '?')}% used\n"
            f"  Disk: {stats.get('disk_percent', '?')}% used\n"
            f"  Uptime: {stats.get('uptime', '?')}\n"
            f"  IP: {stats.get('ip', '?')}"
        )

    elif intent == "containers":
        info = get_docker_containers()
        if "error" in info:
            return f"Error checking containers: {info['error']}"
        return f"Running containers ({info['count']}):\n  " + "\n  ".join(info["containers"])

    elif intent == "network":
        return f"IP address: {stats.get('ip', 'unknown')}\nHostname: {stats.get('hostname', 'unknown')}"

    elif intent == "camera_status":
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "detection"],
                capture_output=True, text=True, timeout=3
            )
            status = result.stdout.strip()
            if status == "active":
                return "The AI camera is running and monitoring. Detection service is active."
            return f"The camera detection service is {status}."
        except Exception:
            return "Unable to check camera status."

    elif intent == "vision_query":
        return handle_vision_query()

    # Navigation intents -- the dashboard / voice pipeline parse the
    # NAVIGATE:url|spoken-message protocol and drive Chromium accordingly.
    elif intent == "open_homeassistant":
        return f"NAVIGATE:http://{PI_LAN_IP}:8123|Opening Home Assistant."
    elif intent == "open_jellyfin":
        return f"NAVIGATE:http://{PI_LAN_IP}:8096|Opening Jellyfin media server."
    elif intent == "open_omv":
        return f"NAVIGATE:http://{PI_LAN_IP}|Opening OpenMediaVault."
    elif intent == "open_portainer":
        return f"NAVIGATE:http://{PI_LAN_IP}:9443|Opening Portainer."
    elif intent == "open_dashboard":
        return "NAVIGATE:http://localhost:3000|Going back to the dashboard."

    elif intent == "help":
        return (
            "I can help you with:\n"
            "  - CPU temperature and usage\n"
            "  - RAM and disk usage\n"
            "  - System status overview\n"
            "  - Docker container status\n"
            "  - Network/IP information\n"
            "  - Camera and detection status\n"
            "  - System uptime\n"
            "  - 'What do you see?' - describe camera view\n"
            "  - 'Open Home Assistant' - navigate to services\n"
            "  - 'Open Jellyfin / OMV / Portainer'\n"
            "For anything else, I'll use the AI model to help."
        )

    return None


# ======================================================
# OLLAMA LLM FALLBACK
# Detection is paused during inference to free CPU.
# ======================================================

def pause_detection():
    try:
        subprocess.run(["sudo", "systemctl", "stop", "detection"],
                       timeout=10, capture_output=True)
        time.sleep(1)
        return True
    except Exception:
        return False


def resume_detection():
    try:
        subprocess.run(["sudo", "systemctl", "start", "detection"],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def query_ollama(prompt):
    system_prompt = (
        "You are the AI assistant for a Raspberry Pi 5-based smart home device called the 'Home Smart Box'. "
        "You help users with their smart home, media, and system questions. "
        "Keep responses concise (2-3 sentences max). "
        "You have access to Home Assistant, Jellyfin media server, OpenMediaVault NAS, and an AI camera."
    )
    try:
        stats = get_system_stats()
        context = (
            f"Current system state: CPU={stats.get('cpu_percent', '?')}%, "
            f"Temp={stats.get('cpu_temp', '?')}\u00B0C, "
            f"RAM={stats.get('ram_percent', '?')}%, "
            f"Disk={stats.get('disk_percent', '?')}%"
        )
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": f"{context}\n\nUser: {prompt}",
                "system": system_prompt,
                "stream": False,
                "options": {"num_predict": 100, "temperature": 0.7},
            },
            timeout=60,
        )
        return response.json().get("response", "Sorry, I couldn't generate a response.")
    except requests.exceptions.Timeout:
        return "The AI model took too long to respond. Try a simpler question."
    except Exception as e:
        return f"Error communicating with AI model: {str(e)}"


def handle_llm_query(prompt):
    paused = pause_detection()
    try:
        return query_ollama(prompt)
    finally:
        if paused:
            resume_detection()


# ======================================================
# HTTP API SERVER -- the dashboard chat widget hits this
# ======================================================

class AssistantAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ask?"):
            params = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            query = params.get("q", [""])[0]
            if not query:
                self._send_json(400, {"error": "Missing ?q= parameter"})
                return

            start_time = time.time()
            intent = match_intent(query)
            if intent:
                response = handle_intent(intent)
                method = "intent_match"
            else:
                response = handle_llm_query(query)
                method = "llm"
            elapsed = time.time() - start_time

            self._send_json(200, {
                "query": query,
                "response": response,
                "method": method,
                "elapsed_seconds": round(elapsed, 2),
            })
        elif self.path == "/health":
            self._send_json(200, {"status": "online"})
        else:
            self._send_json(404, {"error": "Not found. Use /ask?q=your+question"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.end_headers()

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        pass


def start_api_server():
    server = HTTPServer(("0.0.0.0", API_PORT), AssistantAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"HTTP API running on http://0.0.0.0:{API_PORT}/ask?q=your+question")


# ======================================================
# MQTT MESSAGE HANDLER
# ======================================================

def on_message(client, userdata, msg):
    global latest_detections

    # Camera detections -- update the rolling window for vision queries
    if msg.topic == "smartbox/camera/detections":
        try:
            det = json.loads(msg.payload.decode())
            with detection_lock:
                latest_detections.append(det)
                if len(latest_detections) > 10:
                    latest_detections = latest_detections[-10:]
        except Exception:
            pass
        return

    if msg.topic == "smartbox/camera/summary":
        return

    if msg.topic != MQTT_TOPIC_QUERY:
        return

    # Assistant query via MQTT
    try:
        payload = json.loads(msg.payload.decode())
        query = payload.get("query", "").strip()
        query_id = payload.get("id", "unknown")
        if not query:
            return

        print(f"\n[Query {query_id}] {query}")
        start_time = time.time()

        intent = match_intent(query)
        if intent:
            response = handle_intent(intent)
            method = "intent_match"
            print(f"  [Intent: {intent}] Matched!")
        else:
            print(f"  [LLM] No intent match, using {OLLAMA_MODEL}...")
            response = handle_llm_query(query)
            method = "llm"

        elapsed = time.time() - start_time
        result = {
            "query": query,
            "response": response,
            "method": method,
            "model": OLLAMA_MODEL if method == "llm" else "intent_matcher",
            "elapsed_seconds": round(elapsed, 2),
            "timestamp": datetime.now().isoformat(),
            "id": query_id,
        }
        client.publish(MQTT_TOPIC_RESPONSE, json.dumps(result), qos=1)
        print(f"  [Response in {elapsed:.1f}s via {method}] {response[:80]}...")

    except Exception as e:
        print(f"  [Error] {e}")
        client.publish(
            MQTT_TOPIC_RESPONSE,
            json.dumps({
                "query": msg.payload.decode(),
                "response": f"Error processing query: {str(e)}",
                "method": "error",
                "timestamp": datetime.now().isoformat(),
            }),
            qos=1,
        )


# ======================================================
# MAIN
# ======================================================

def main():
    global mqtt_client

    print("=" * 60)
    print("  Home Smart Box AI Assistant")
    print(f"  Intent matcher + {OLLAMA_MODEL} LLM fallback")
    print("=" * 60)

    mqtt_client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="smartbox-assistant",
    )
    mqtt_client.will_set(
        MQTT_TOPIC_STATUS,
        json.dumps({"status": "offline", "timestamp": datetime.now().isoformat()}),
        qos=1, retain=True,
    )
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.subscribe(MQTT_TOPIC_QUERY, qos=1)
        mqtt_client.subscribe("smartbox/camera/detections", qos=0)
        mqtt_client.subscribe("smartbox/camera/summary", qos=0)
        mqtt_client.publish(
            MQTT_TOPIC_STATUS,
            json.dumps({"status": "online", "timestamp": datetime.now().isoformat()}),
            qos=1, retain=True,
        )
        print(f"\nListening on MQTT topic: {MQTT_TOPIC_QUERY}")
        print(f"Responses published to: {MQTT_TOPIC_RESPONSE}")

        start_api_server()
        mqtt_client.loop_forever()

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        mqtt_client.publish(
            MQTT_TOPIC_STATUS,
            json.dumps({"status": "offline", "timestamp": datetime.now().isoformat()}),
            qos=1, retain=True,
        )
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
