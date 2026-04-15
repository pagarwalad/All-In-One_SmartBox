#!/usr/bin/env python3
"""
Voice Pipeline for the All-in-one Home Smart Box
Listens for a wake word, transcribes speech, sends to the AI assistant,
and speaks the response back. Also handles navigation commands by
driving the kiosk Chromium via Chrome DevTools Protocol on port 9222.

Pipeline: Mic -> Wake Word -> Record -> Whisper STT -> Assistant API
       -> [Navigate kiosk OR Piper TTS -> Speaker]
"""

import os
import sys
import time
import wave
import json
import re
import subprocess
import numpy as np
import sounddevice as sd
import soundfile as sf
import requests

# ======================================================
# CONFIGURATION
# ======================================================

MIC_SAMPLE_RATE = 44100        # Native rate for the USB PnP mic
SPEAKER_DEVICE = "plughw:0,0"  # aplay device string

WAKE_WORD = "smart box"
WAKE_CHECK_DURATION = 3        # Seconds of audio per wake-word check

COMMAND_DURATION = 5           # Max seconds to record after wake word
SILENCE_THRESHOLD = 0.01
SILENCE_DURATION = 1.5

WHISPER_MODEL = "tiny"

ASSISTANT_URL = "http://localhost:3000/ask"  # Proxied by nginx to :8086

PIPER_MODEL = "/home/pi/piper-voices/en_US-lessac-medium.onnx"

CDP_URL = "http://localhost:9222"  # Chromium remote-debugging port


# ======================================================
# MIC DISCOVERY (by name, so device index changes don't break it)
# ======================================================

def find_mic_device():
    """Find the USB PnP Sound Device by name, not by fixed index.
    The kernel sometimes shuffles ALSA device numbers across reboots."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if "USB PnP Sound Device" in d["name"] and d["max_input_channels"] > 0:
            print(f"  Found mic: device {i} ({d['name']})")
            return i
    for i, d in enumerate(devices):
        if "USB" in d["name"] and d["max_input_channels"] > 0:
            print(f"  Found USB mic: device {i} ({d['name']})")
            return i
    print("  WARNING: No USB mic found, using default")
    return None


# ======================================================
# INIT
# ======================================================

print("=" * 60)
print("  Home Smart Box Voice Pipeline")
print("=" * 60)

print("Loading Whisper STT model...")
import whisper
whisper_model = whisper.load_model(WHISPER_MODEL)
print(f"  Whisper '{WHISPER_MODEL}' loaded")

MIC_DEVICE = find_mic_device()

print(f"\nMicrophone: device {MIC_DEVICE} at {MIC_SAMPLE_RATE} Hz")
print(f"Speaker: {SPEAKER_DEVICE}")
print(f"Wake word: '{WAKE_WORD}'")
print(f"TTS: Piper CLI with {PIPER_MODEL}")
print(f"Assistant: {ASSISTANT_URL}")
print()


# ======================================================
# TEXT-TO-SPEECH (Piper CLI -- the Python API has a bug
# in the version we're on, the CLI is rock solid)
# ======================================================

def speak(text):
    """Synthesize speech via Piper CLI, play via aplay."""
    try:
        tts_path = "/tmp/voice_response.wav"
        process = subprocess.run(
            ["python3", "-m", "piper",
             "--model", PIPER_MODEL,
             "--output_file", tts_path],
            input=text, capture_output=True, text=True, timeout=30
        )
        if process.returncode != 0:
            print(f"  [TTS Error] {process.stderr[:100]}")
            return

        subprocess.run(
            ["aplay", "-D", SPEAKER_DEVICE, tts_path],
            capture_output=True, timeout=30
        )
        # Let the audio device fully release before re-opening for capture
        time.sleep(1)
    except Exception as e:
        print(f"  [TTS Error] {e}")


def play_beep(freq=800, duration=0.15):
    """Short beep used as audio cue (listening / no-input / done)."""
    try:
        sr = 44100
        t = np.linspace(0, duration, int(sr * duration), False)
        tone = (np.sin(2 * np.pi * freq * t) * 0.3 * 32767).astype(np.int16)
        wav_path = "/tmp/beep.wav"
        with wave.open(wav_path, "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr)
            f.writeframes(tone.tobytes())
        subprocess.run(["aplay", "-D", SPEAKER_DEVICE, wav_path],
                       capture_output=True, timeout=5)
        time.sleep(0.5)
    except Exception:
        pass


# ======================================================
# AUDIO CAPTURE (with retry -- USB mic occasionally returns
# "device unavailable" right after the speaker finishes)
# ======================================================

def record_audio(duration):
    """Record from MIC_DEVICE; retry up to 3 times on transient failures."""
    samples = int(duration * MIC_SAMPLE_RATE)
    for attempt in range(3):
        try:
            sd.default.reset()  # Clear any stuck state from prior streams
            audio = sd.rec(samples, samplerate=MIC_SAMPLE_RATE,
                           channels=1, dtype="float32", device=MIC_DEVICE)
            sd.wait()
            return audio
        except Exception as e:
            print(f"  [Mic retry {attempt + 1}/3: {e}]")
            try:
                sd.stop()
            except Exception:
                pass
            time.sleep(2)
    return np.zeros((samples, 1), dtype="float32")


def record_command():
    """Record up to COMMAND_DURATION seconds, stopping early if the user
    goes silent for SILENCE_DURATION seconds."""
    print("  [Recording command...]")
    chunk_size = int(MIC_SAMPLE_RATE * 0.5)
    max_chunks = int(COMMAND_DURATION / 0.5)
    all_audio = []
    silent_chunks = 0
    max_silent = int(SILENCE_DURATION / 0.5)

    for i in range(max_chunks):
        chunk = sd.rec(chunk_size, samplerate=MIC_SAMPLE_RATE,
                       channels=1, dtype="float32", device=MIC_DEVICE)
        sd.wait()
        all_audio.append(chunk)

        peak = np.max(np.abs(chunk))
        if peak < SILENCE_THRESHOLD:
            silent_chunks += 1
            if silent_chunks >= max_silent and i > 1:
                print(f"  [Silence detected, stopping after {(i + 1) * 0.5:.1f}s]")
                break
        else:
            silent_chunks = 0

    return np.concatenate(all_audio)


# ======================================================
# SPEECH-TO-TEXT
# ======================================================

def transcribe(audio):
    """Run Whisper on the recorded command."""
    wav_path = "/tmp/voice_command.wav"
    sf.write(wav_path, audio, MIC_SAMPLE_RATE)
    result = whisper_model.transcribe(wav_path, language="en", fp16=False)
    return result["text"].strip()


# ======================================================
# WAKE WORD DETECTION
# Whisper-based with three-tier flexible matching:
#   1. Exact match
#   2. Known mishearings ("smart fox", "smart blocks", etc.)
#   3. "smart" + any short word fallback
# ======================================================

def check_wake_word(audio):
    """Return (detected, transcript) for the recorded clip."""
    wav_path = "/tmp/wake_check.wav"
    sf.write(wav_path, audio, MIC_SAMPLE_RATE)
    result = whisper_model.transcribe(wav_path, language="en", fp16=False)
    text = result["text"].strip().lower()
    clean = re.sub(r'[^\w\s]', '', text)

    # Tier 1: exact matches
    for phrase in ("smart box", "smartbox"):
        if phrase in clean:
            return True, text

    # Tier 2: common Whisper mishearings of "smart box"
    fuzzy_matches = [
        "smart blocks", "smart books", "smart fox",
        "smart bots", "smart talks", "smart locks",
        "smart docs", "smart rocks", "smart socks",
        "smart pox", "smart hawks", "smart knocks",
    ]
    for phrase in fuzzy_matches:
        if phrase in clean:
            return True, text

    # Tier 3: "smart" + any short word
    words = clean.split()
    if "smart" in words:
        idx = words.index("smart")
        if idx < len(words) - 1 and len(words[idx + 1]) <= 6:
            return True, text

    return False, text


# ======================================================
# ASSISTANT QUERY
# ======================================================

def query_assistant(text):
    """POST the transcript to the AI assistant API."""
    try:
        response = requests.get(ASSISTANT_URL, params={"q": text}, timeout=120)
        data = response.json()
        return data.get("response", "Sorry, I didn't get a response.")
    except requests.exceptions.Timeout:
        return "Sorry, the request timed out."
    except Exception as e:
        return f"Sorry, there was an error: {str(e)}"


# ======================================================
# KIOSK NAVIGATION via Chrome DevTools Protocol
# Requires Chromium to be launched with --remote-debugging-port=9222
# ======================================================

def navigate_kiosk(url):
    """Drive the running kiosk Chromium to a new URL."""
    try:
        import websocket  # pip install websocket-client
        pages = requests.get(f"{CDP_URL}/json/list", timeout=3).json()
        if not pages:
            print("  [No CDP page found]")
            return
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"])
        ws.send(json.dumps({
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": url}
        }))
        ws.recv()
        ws.close()
        print(f"  [Navigated kiosk to: {url}]")
    except Exception as e:
        import traceback
        print(f"  [Navigation error: {e}]")
        traceback.print_exc()


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    # Wait for audio devices to be fully ready (matters at boot)
    print("Waiting for audio devices...")
    time.sleep(5)

    print("Listening for wake word... (say 'Smart Box')")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            # Step 1: continuous wake-word listening
            audio = record_audio(WAKE_CHECK_DURATION)
            peak = np.max(np.abs(audio))

            # Energy pre-filter -- skip Whisper on silence
            if peak > 0.005:
                print(f"  [Audio detected: peak={peak:.4f}, running Whisper...]")
            if peak < 0.005:
                continue

            detected, heard_text = check_wake_word(audio)
            if not detected:
                if heard_text:
                    print(f"  [Heard: '{heard_text}' -- not wake word]")
                continue

            # Step 2: wake word triggered
            print(f"\n>>> Wake word detected! (heard: '{heard_text}')")
            play_beep(800, 0.15)  # high beep = listening

            # Step 3: capture the command
            command_audio = record_command()

            # Step 4: STT
            print("  [Transcribing...]")
            start = time.time()
            command_text = transcribe(command_audio)
            stt_time = time.time() - start
            print(f"  [STT ({stt_time:.1f}s): '{command_text}']")

            if not command_text or len(command_text) < 2:
                print("  [No command detected]")
                play_beep(400, 0.2)  # low beep = nothing heard
                continue

            # Step 5: query assistant
            print("  [Querying assistant...]")
            start = time.time()
            response_text = query_assistant(command_text)
            query_time = time.time() - start
            print(f"  [Response ({query_time:.1f}s): '{response_text[:80]}...']")

            # Step 6: navigate kiosk OR speak the answer
            if response_text.startswith("NAVIGATE:"):
                print(f"  [DEBUG: Navigation command received: {response_text}]")
                parts = response_text[9:].split("|")
                nav_url = parts[0]
                speak_text = parts[1] if len(parts) > 1 else "Navigating."
                navigate_kiosk(nav_url)
            else:
                speak_text = response_text

            # Truncate overly long replies for TTS responsiveness
            if len(speak_text) > 300:
                cut = speak_text[:300].rfind('.')
                if cut > 50:
                    speak_text = speak_text[:cut + 1]
                else:
                    speak_text = speak_text[:300] + "."
                print(f"  [Speaking truncated response ({len(speak_text)} chars)...]")
            else:
                print("  [Speaking response...]")

            speak(speak_text)
            print(f"  [Done -- total: {stt_time + query_time:.1f}s]")
            print("\nListening for wake word...")

        except KeyboardInterrupt:
            print("\n\nVoice pipeline shutting down.")
            break
        except Exception as e:
            print(f"  [Error: {e}]")
            time.sleep(1)


if __name__ == "__main__":
    main()
