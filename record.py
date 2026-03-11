"""
Bird Recorder Dashboard
Raspberry Pi field recorder with real-time monitoring, scheduling, and BWF metadata.
"""

import os
import sys
import time
import wave
import json
import math
import struct
import threading
import uuid
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, request
from flask_socketio import SocketIO
import pyaudio
import psutil

# ─────────────────────────────────────────────
# Flask / SocketIO Setup
# ─────────────────────────────────────────────
app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 48000
SAMPLE_WIDTH = 2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(BASE_DIR, 'recordings')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# ─────────────────────────────────────────────
# Application State
# ─────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine_running = False
        self.engine_error = None
        self.recording = False
        self.recording_start_time = None
        self.mode = 'continuous'
        self.current_dbfs = -100.0
        self.frames = []
        self.vad_active = False
        self.vad_triggered_at = None
        self.schedule_active = False
        self.pre_buffer = deque(maxlen=int(RATE / CHUNK * 2))
        self.save_dir = SAVE_DIR
        self.metadata = {
            'location': '',
            'project': '',
            'recorder_id': '',
            'description': ''
        }
        self.vad_settings = {
            'threshold_dbfs': -40.0,
            'hold_time': 3.0,
            'pre_buffer_secs': 1.0
        }
        self.schedules = []
        self._load_config()

    def _load_config(self):
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
            self.metadata = cfg.get('metadata', self.metadata)
            self.vad_settings = cfg.get('vad', self.vad_settings)
            self.schedules = cfg.get('schedules', self.schedules)
            self.mode = cfg.get('mode', self.mode)
            self.save_dir = cfg.get('save_dir', self.save_dir)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        pre_buf_chunks = int(RATE / CHUNK * self.vad_settings['pre_buffer_secs'])
        self.pre_buffer = deque(maxlen=max(pre_buf_chunks, 1))

    def save_config(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        cfg = {
            'metadata': self.metadata,
            'vad': self.vad_settings,
            'schedules': self.schedules,
            'mode': self.mode,
            'save_dir': self.save_dir
        }
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)

state = AppState()

# ─────────────────────────────────────────────
# Audio Utilities
# ─────────────────────────────────────────────
def compute_dbfs(raw_data):
    count = len(raw_data) // SAMPLE_WIDTH
    if count == 0:
        return -100.0
    samples = struct.unpack(f'<{count}h', raw_data)
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / count)
    if rms < 1:
        return -100.0
    return round(20 * math.log10(rms / 32767), 1)


def get_input_device_index(p):
    info = p.get_host_api_info_by_index(0)
    for i in range(info.get('deviceCount', 0)):
        dev = p.get_device_info_by_host_api_device_index(0, i)
        if dev.get('maxInputChannels', 0) > 0:
            print(f"Audio input: {dev.get('name')} (index {i})")
            return i
    print("WARNING: No audio input device found.")
    return None

# ─────────────────────────────────────────────
# BWF Metadata — bext chunk injection
# ─────────────────────────────────────────────
def _pad_bytes(text, length):
    return text.encode('ascii', errors='replace').ljust(length, b'\x00')[:length]


def build_bext_chunk(metadata):
    now = datetime.now()
    desc = (
        f"Location: {metadata.get('location', '')} | "
        f"Project: {metadata.get('project', '')} | "
        f"ID: {metadata.get('recorder_id', '')}"
    )
    coding = f"A=PCM,F={RATE},W={SAMPLE_WIDTH * 8},M=mono,T=INMP441\r\n"
    if metadata.get('description'):
        coding += f"{metadata['description']}\r\n"

    body = bytearray()
    body += _pad_bytes(desc, 256)
    body += _pad_bytes(metadata.get('project', ''), 32)
    body += _pad_bytes(metadata.get('recorder_id', ''), 32)
    body += now.strftime('%Y-%m-%d').encode('ascii')
    body += now.strftime('%H:%M:%S').encode('ascii')
    body += struct.pack('<II', 0, 0)    # TimeReference
    body += struct.pack('<H', 2)        # BWF version 2
    body += b'\x00' * 64               # UMID
    body += b'\x00' * 10               # Loudness fields (5 x int16)
    body += b'\x00' * 180              # Reserved
    body += coding.encode('ascii', errors='replace')

    chunk = b'bext' + struct.pack('<I', len(body)) + bytes(body)
    if len(chunk) % 2:
        chunk += b'\x00'
    return chunk


def save_wav_with_bwf(filepath, frames, metadata):
    """Write WAV then inject a BWF bext chunk."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    p = pyaudio.PyAudio()
    wf = wave.open(filepath, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()
    p.terminate()

    if not any(metadata.get(k) for k in ('location', 'project', 'recorder_id', 'description')):
        return

    try:
        with open(filepath, 'rb') as f:
            data = bytearray(f.read())

        if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            return

        bext = build_bext_chunk(metadata)
        pos = 12
        while pos < len(data) - 8:
            ck_id = bytes(data[pos:pos + 4])
            ck_size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
            next_pos = pos + 8 + ck_size + (ck_size % 2)
            if ck_id == b'fmt ':
                insert_at = next_pos
                break
            pos = next_pos
        else:
            return

        data[insert_at:insert_at] = bext
        struct.pack_into('<I', data, 4, len(data) - 8)

        with open(filepath, 'wb') as f:
            f.write(data)
    except Exception as e:
        print(f"BWF inject warning: {e}")

# ─────────────────────────────────────────────
# Recording helpers
# ─────────────────────────────────────────────
def _start_recording():
    with state.lock:
        if state.recording:
            return
        state.recording = True
        state.recording_start_time = time.time()
        state.frames = list(state.pre_buffer)
    print(f"Recording started ({state.mode} mode)")


def _stop_and_save():
    with state.lock:
        if not state.recording:
            return
        state.recording = False
        state.vad_active = False
        frames_copy = list(state.frames)
        state.frames = []

    if not frames_copy:
        print("No audio captured, skipping save.")
        return

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"bird_{ts}.wav"
    filepath = os.path.join(state.save_dir, filename)
    save_wav_with_bwf(filepath, frames_copy, state.metadata)
    print(f"Saved {filepath} ({len(frames_copy)} chunks)")

# ─────────────────────────────────────────────
# Audio Engine (runs continuously)
# ─────────────────────────────────────────────
def audio_engine():
    state.engine_running = True
    p = pyaudio.PyAudio()
    dev_idx = get_input_device_index(p)

    try:
        stream = p.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, input_device_index=dev_idx,
            frames_per_buffer=CHUNK
        )
    except Exception as e:
        state.engine_error = str(e)
        state.engine_running = False
        p.terminate()
        print(f"Audio engine failed: {e}")
        return

    print("Audio engine running.")
    last_above_threshold = 0

    while state.engine_running:
        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            print(f"Stream read error: {e}")
            time.sleep(0.01)
            continue

        dbfs = compute_dbfs(data)
        state.current_dbfs = dbfs
        state.pre_buffer.append(data)

        with state.lock:
            if state.recording:
                state.frames.append(data)

        if state.mode == 'vad' and not state.schedule_active:
            _handle_vad(dbfs, last_above_threshold)
            if dbfs > state.vad_settings['threshold_dbfs']:
                last_above_threshold = time.time()

        if state.mode == 'scheduled':
            _handle_vad_in_schedule(dbfs, last_above_threshold)
            if dbfs > state.vad_settings['threshold_dbfs']:
                last_above_threshold = time.time()

    stream.stop_stream()
    stream.close()
    p.terminate()
    print("Audio engine stopped.")


def _handle_vad(dbfs, last_above):
    threshold = state.vad_settings['threshold_dbfs']
    hold = state.vad_settings['hold_time']

    if dbfs > threshold:
        if not state.recording:
            _start_recording()
            state.vad_active = True
    elif state.vad_active and state.recording:
        if time.time() - last_above > hold:
            _stop_and_save()


def _handle_vad_in_schedule(dbfs, last_above):
    """VAD within an active schedule window."""
    if not state.schedule_active:
        if state.recording and state.vad_active:
            _stop_and_save()
        return

    active_sched = _get_active_schedule()
    if not active_sched:
        return

    sched_mode = active_sched.get('mode', 'continuous')
    if sched_mode == 'vad':
        _handle_vad(dbfs, last_above)
    elif sched_mode == 'continuous' and not state.recording:
        _start_recording()

# ─────────────────────────────────────────────
# System Health
# ─────────────────────────────────────────────
def get_system_health():
    health = {
        'cpu': psutil.cpu_percent(interval=0),
        'ram': psutil.virtual_memory().percent,
        'disk': psutil.disk_usage('/').percent,
        'temp': None,
        'battery': None
    }
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ('cpu_thermal', 'cpu-thermal', 'coretemp'):
                if name in temps and temps[name]:
                    health['temp'] = round(temps[name][0].current, 1)
                    break
            if health['temp'] is None:
                first = list(temps.values())[0]
                if first:
                    health['temp'] = round(first[0].current, 1)
    except (AttributeError, Exception):
        pass
    try:
        bat = psutil.sensors_battery()
        if bat:
            health['battery'] = round(bat.percent, 1)
    except (AttributeError, Exception):
        pass
    return health

# ─────────────────────────────────────────────
# WebSocket Monitor Loop
# ─────────────────────────────────────────────
def monitor_loop():
    """Emit real-time data to connected WebSocket clients."""
    tick = 0
    while True:
        socketio.emit('audio_level', {
            'dbfs': state.current_dbfs,
            'recording': state.recording,
            'mode': state.mode,
            'vad_active': state.vad_active,
            'schedule_active': state.schedule_active
        })
        if tick % 10 == 0:
            socketio.emit('system_health', get_system_health())
        tick += 1
        socketio.sleep(0.2)

# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────
def _get_active_schedule():
    now = datetime.now()
    current_day = now.weekday()
    current_minutes = now.hour * 60 + now.minute
    for sched in state.schedules:
        if not sched.get('enabled', True):
            continue
        if current_day not in sched.get('days', []):
            continue
        parts = sched.get('start', '00:00').split(':')
        start_m = int(parts[0]) * 60 + int(parts[1])
        parts = sched.get('end', '00:00').split(':')
        end_m = int(parts[0]) * 60 + int(parts[1])
        if end_m <= start_m:
            if current_minutes >= start_m or current_minutes < end_m:
                return sched
        else:
            if start_m <= current_minutes < end_m:
                return sched
    return None


def scheduler_loop():
    while True:
        if state.mode == 'scheduled':
            active = _get_active_schedule()
            was_active = state.schedule_active
            state.schedule_active = active is not None

            if active and not was_active:
                sched_mode = active.get('mode', 'continuous')
                print(f"Schedule window opened ({sched_mode})")
                if sched_mode == 'continuous':
                    _start_recording()

            if not active and was_active:
                print("Schedule window closed")
                if state.recording:
                    _stop_and_save()
        else:
            if state.schedule_active:
                state.schedule_active = False

        socketio.sleep(15)

# ─────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────
@app.route('/start')
def api_start():
    if state.mode != 'scheduled':
        _start_recording()
    return jsonify(status='started')


@app.route('/stop')
def api_stop():
    _stop_and_save()
    return jsonify(status='stopped')


@app.route('/status')
def api_status():
    elapsed = None
    if state.recording and state.recording_start_time:
        elapsed = round(time.time() - state.recording_start_time)
    return jsonify(
        recording=state.recording,
        mode=state.mode,
        vad_active=state.vad_active,
        schedule_active=state.schedule_active,
        elapsed=elapsed,
        engine_error=state.engine_error
    )


@app.route('/api/mode', methods=['GET', 'POST'])
def api_mode():
    if request.method == 'POST':
        new_mode = request.json.get('mode')
        if new_mode in ('continuous', 'vad', 'scheduled'):
            if state.recording and state.mode != new_mode:
                _stop_and_save()
            state.mode = new_mode
            state.save_config()
    return jsonify(mode=state.mode)


@app.route('/api/metadata', methods=['GET', 'POST'])
def api_metadata():
    if request.method == 'POST':
        data = request.json
        for key in ('location', 'project', 'recorder_id', 'description'):
            if key in data:
                state.metadata[key] = str(data[key])
        state.save_config()
    return jsonify(state.metadata)


@app.route('/api/schedules', methods=['GET', 'POST', 'DELETE'])
def api_schedules():
    if request.method == 'POST':
        entry = request.json
        entry.setdefault('id', str(uuid.uuid4())[:8])
        entry.setdefault('start', '06:00')
        entry.setdefault('end', '10:00')
        entry.setdefault('days', [0, 1, 2, 3, 4, 5, 6])
        entry.setdefault('mode', 'continuous')
        entry.setdefault('enabled', True)
        existing = [s for s in state.schedules if s['id'] == entry['id']]
        if existing:
            existing[0].update(entry)
        else:
            state.schedules.append(entry)
        state.save_config()
    elif request.method == 'DELETE':
        sid = request.json.get('id')
        state.schedules = [s for s in state.schedules if s['id'] != sid]
        state.save_config()
    return jsonify(schedules=state.schedules)


@app.route('/api/vad', methods=['GET', 'POST'])
def api_vad():
    if request.method == 'POST':
        data = request.json
        for key in ('threshold_dbfs', 'hold_time', 'pre_buffer_secs'):
            if key in data:
                state.vad_settings[key] = float(data[key])
        pre_buf = int(RATE / CHUNK * state.vad_settings['pre_buffer_secs'])
        state.pre_buffer = deque(state.pre_buffer, maxlen=max(pre_buf, 1))
        state.save_config()
    return jsonify(state.vad_settings)


@app.route('/api/savedir', methods=['GET', 'POST'])
def api_savedir():
    if request.method == 'POST':
        new_dir = request.json.get('save_dir', '').strip()
        if new_dir:
            expanded = os.path.expanduser(new_dir)
            if not os.path.isabs(expanded):
                expanded = os.path.join(BASE_DIR, expanded)
            try:
                os.makedirs(expanded, exist_ok=True)
                state.save_dir = expanded
                state.save_config()
            except OSError as e:
                return jsonify(save_dir=state.save_dir, error=str(e)), 400
    return jsonify(save_dir=state.save_dir)

# ─────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return DASHBOARD_HTML


DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bird Recorder</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
:root{
  --bg:#121820;--card:#1a2332;--border:#2a3a4e;
  --text:#d8dee9;--dim:#6b7b8d;--accent:#4fc3f7;
  --red:#ef5350;--green:#66bb6a;--yellow:#ffd54f;
  --radius:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
a{color:var(--accent)}

header{background:var(--card);border-bottom:1px solid var(--border);padding:16px 20px;display:flex;flex-wrap:wrap;align-items:center;gap:12px}
header h1{font-size:1.15rem;font-weight:600;white-space:nowrap}
header h1 span{color:var(--accent)}

.controls{display:flex;gap:8px;align-items:center;margin-left:auto}
.controls button{padding:7px 18px;border:none;border-radius:6px;font-size:.85rem;font-weight:600;cursor:pointer;transition:opacity .15s}
.controls button:active{opacity:.7}
#btn-rec{background:var(--red);color:#fff}
#btn-rec.active{animation:pulse 1.2s infinite}
#btn-stop{background:var(--border);color:var(--text)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

.status-pill{font-size:.78rem;padding:4px 12px;border-radius:20px;background:var(--border);white-space:nowrap}
.status-pill.rec{background:rgba(239,83,80,.2);color:var(--red)}

.mode-bar{display:flex;gap:0;background:var(--card);border-bottom:1px solid var(--border);padding:0 20px}
.mode-btn{flex:1;max-width:160px;padding:10px 0;border:none;background:none;color:var(--dim);font-size:.82rem;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.mode-btn.active{color:var(--accent);border-bottom-color:var(--accent)}

nav.tabs{display:flex;background:var(--card);border-bottom:1px solid var(--border);padding:0 20px}
.tab-btn{padding:10px 20px;border:none;background:none;color:var(--dim);font-size:.82rem;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab-btn.active{color:var(--text);border-bottom-color:var(--accent)}

main{max-width:860px;margin:0 auto;padding:20px}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ── Monitor ── */
.meter-section{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.meter-label{font-size:.75rem;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.meter-track{height:28px;background:#0d1117;border-radius:6px;overflow:hidden;position:relative}
.meter-fill{height:100%;width:0%;border-radius:6px;transition:width .18s linear;background:linear-gradient(90deg,var(--green),var(--yellow),var(--red))}
.meter-value{font-size:1.4rem;font-weight:700;margin-top:8px;font-variant-numeric:tabular-nums}
.meter-threshold{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);opacity:.7;z-index:2}

.health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:16px}
.h-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;text-align:center}
.h-card .label{font-size:.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.h-card .value{font-size:1.5rem;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}

.vad-settings{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-top:16px}
.vad-settings summary{cursor:pointer;font-weight:600;font-size:.85rem;color:var(--dim)}
.vad-row{display:flex;align-items:center;gap:12px;margin-top:12px;flex-wrap:wrap}
.vad-row label{font-size:.78rem;color:var(--dim);min-width:110px}
.vad-row input[type=range]{flex:1;min-width:120px;accent-color:var(--accent)}
.vad-row .rv{font-size:.82rem;width:60px;font-variant-numeric:tabular-nums}

/* ── Schedule ── */
.sched-list{display:flex;flex-direction:column;gap:10px;margin-bottom:16px}
.sched-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 18px;display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.sched-card .time{font-size:1.05rem;font-weight:700;min-width:130px}
.sched-card .days{display:flex;gap:3px}
.sched-card .days span{font-size:.65rem;padding:2px 6px;border-radius:4px;background:var(--border);color:var(--dim)}
.sched-card .days span.on{background:var(--accent);color:#000;font-weight:600}
.sched-card .smode{font-size:.72rem;padding:3px 10px;border-radius:12px;background:rgba(79,195,247,.15);color:var(--accent);font-weight:600}
.sched-card .actions{margin-left:auto;display:flex;gap:6px}
.sched-card .actions button{background:none;border:1px solid var(--border);color:var(--dim);border-radius:6px;padding:4px 10px;font-size:.72rem;cursor:pointer}
.sched-card .actions button:hover{border-color:var(--red);color:var(--red)}
.sched-card .toggle{width:40px;height:22px;border-radius:11px;border:none;cursor:pointer;position:relative;transition:background .2s}
.sched-card .toggle.on{background:var(--green)}
.sched-card .toggle.off{background:var(--border)}
.sched-card .toggle::after{content:'';position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .2s}
.sched-card .toggle.on::after{transform:translateX(18px)}

.sched-form{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.sched-form h3{font-size:.9rem;margin-bottom:14px}
.sf-row{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.sf-row label{font-size:.78rem;color:var(--dim);min-width:80px}
.sf-row input[type=time]{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:.85rem}
.sf-row select{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:.85rem}
.day-picks{display:flex;gap:4px}
.day-pick{width:34px;height:30px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--dim);font-size:.7rem;font-weight:600;cursor:pointer;transition:all .15s}
.day-pick.on{background:var(--accent);color:#000;border-color:var(--accent)}
.btn-row{display:flex;gap:8px;margin-top:6px}
.btn-primary{padding:8px 20px;border:none;border-radius:6px;background:var(--accent);color:#000;font-weight:600;font-size:.82rem;cursor:pointer}
.btn-secondary{padding:8px 20px;border:1px solid var(--border);border-radius:6px;background:none;color:var(--dim);font-size:.82rem;cursor:pointer}
.btn-add{padding:8px 20px;border:1px dashed var(--border);border-radius:6px;background:none;color:var(--dim);font-size:.82rem;cursor:pointer;width:100%}
.btn-add:hover{border-color:var(--accent);color:var(--accent)}

/* ── Metadata ── */
.meta-form{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.mf-group{margin-bottom:16px}
.mf-group label{display:block;font-size:.75rem;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.mf-group input,.mf-group textarea{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:.88rem;font-family:inherit}
.mf-group textarea{resize:vertical;min-height:70px}
.mf-group input:focus,.mf-group textarea:focus{outline:none;border-color:var(--accent)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--green);color:#000;padding:8px 24px;border-radius:8px;font-size:.82rem;font-weight:600;opacity:0;transition:opacity .3s;pointer-events:none;z-index:99}
.toast.show{opacity:1}

.engine-err{background:rgba(239,83,80,.12);border:1px solid var(--red);color:var(--red);border-radius:var(--radius);padding:14px 18px;margin-bottom:16px;font-size:.85rem}
</style>
</head>
<body>

<header>
  <h1><span>&#9679;</span> Bird Recorder</h1>
  <div class="controls">
    <button id="btn-rec" onclick="startRec()">Record</button>
    <button id="btn-stop" onclick="stopRec()">Stop &amp; Save</button>
    <span class="status-pill" id="status-pill">Idle</span>
  </div>
</header>

<div class="mode-bar">
  <button class="mode-btn active" data-mode="continuous">Continuous</button>
  <button class="mode-btn" data-mode="vad">VAD</button>
  <button class="mode-btn" data-mode="scheduled">Scheduled</button>
</div>

<nav class="tabs">
  <button class="tab-btn active" data-tab="monitor">Monitor</button>
  <button class="tab-btn" data-tab="schedule">Schedule</button>
  <button class="tab-btn" data-tab="metadata">Metadata</button>
</nav>

<main>
  <div id="engine-err" class="engine-err" style="display:none"></div>

  <!-- ── Monitor ── -->
  <div class="tab-panel active" id="p-monitor">
    <div class="meter-section">
      <div class="meter-label">Audio Level</div>
      <div class="meter-track">
        <div class="meter-fill" id="meter-fill"></div>
        <div class="meter-threshold" id="meter-thresh"></div>
      </div>
      <div class="meter-value" id="dbfs-val">-&infin; dBFS</div>
    </div>
    <div class="health-grid">
      <div class="h-card"><div class="label">CPU</div><div class="value" id="h-cpu">--%</div></div>
      <div class="h-card"><div class="label">Disk</div><div class="value" id="h-disk">--%</div></div>
      <div class="h-card"><div class="label">Temp</div><div class="value" id="h-temp">--</div></div>
      <div class="h-card"><div class="label">RAM</div><div class="value" id="h-ram">--%</div></div>
      <div class="h-card" id="h-bat-card" style="display:none"><div class="label">Battery</div><div class="value" id="h-bat">--%</div></div>
    </div>
    <details class="vad-settings">
      <summary>VAD Settings</summary>
      <div class="vad-row">
        <label>Threshold</label>
        <input type="range" id="vad-thresh" min="-80" max="0" step="1" value="-40">
        <span class="rv" id="vad-thresh-v">-40 dB</span>
      </div>
      <div class="vad-row">
        <label>Hold time</label>
        <input type="range" id="vad-hold" min="0.5" max="15" step="0.5" value="3">
        <span class="rv" id="vad-hold-v">3.0 s</span>
      </div>
      <div class="vad-row">
        <label>Pre-buffer</label>
        <input type="range" id="vad-prebuf" min="0.5" max="5" step="0.5" value="1">
        <span class="rv" id="vad-prebuf-v">1.0 s</span>
      </div>
      <div class="btn-row" style="margin-top:14px">
        <button class="btn-primary" onclick="saveVad()">Save VAD Settings</button>
      </div>
    </details>
  </div>

  <!-- ── Schedule ── -->
  <div class="tab-panel" id="p-schedule">
    <div class="sched-list" id="sched-list"></div>
    <button class="btn-add" onclick="showSchedForm()">+ Add Recording Window</button>
    <div class="sched-form" id="sched-form" style="display:none">
      <h3 id="sched-form-title">New Schedule</h3>
      <input type="hidden" id="sf-id" value="">
      <div class="sf-row">
        <label>Start</label><input type="time" id="sf-start" value="06:00">
        <label>End</label><input type="time" id="sf-end" value="10:00">
      </div>
      <div class="sf-row">
        <label>Days</label>
        <div class="day-picks" id="sf-days">
          <button class="day-pick on" data-d="0">Mo</button>
          <button class="day-pick on" data-d="1">Tu</button>
          <button class="day-pick on" data-d="2">We</button>
          <button class="day-pick on" data-d="3">Th</button>
          <button class="day-pick on" data-d="4">Fr</button>
          <button class="day-pick on" data-d="5">Sa</button>
          <button class="day-pick on" data-d="6">Su</button>
        </div>
      </div>
      <div class="sf-row">
        <label>Mode</label>
        <select id="sf-mode"><option value="continuous">Continuous</option><option value="vad">VAD</option></select>
      </div>
      <div class="btn-row">
        <button class="btn-primary" onclick="saveSched()">Save</button>
        <button class="btn-secondary" onclick="hideSchedForm()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- ── Metadata ── -->
  <div class="tab-panel" id="p-metadata">
    <div class="meta-form" style="margin-bottom:16px">
      <div class="mf-group">
        <label>Save Folder</label>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="m-savedir" style="flex:1" placeholder="e.g. /home/pi/recordings">
          <button class="btn-primary" onclick="saveSaveDir()">Set</button>
        </div>
        <p style="margin-top:6px;font-size:.7rem;color:var(--dim)">Absolute path or relative to script directory. Folder is created automatically.</p>
      </div>
    </div>
    <div class="meta-form">
      <div class="mf-group"><label>Location</label><input id="m-loc" placeholder="e.g. Forest Edge, GPS coords"></div>
      <div class="mf-group"><label>Project</label><input id="m-proj" placeholder="e.g. Dawn Chorus 2026"></div>
      <div class="mf-group"><label>Recorder ID</label><input id="m-id" placeholder="e.g. PI-ZERO-01"></div>
      <div class="mf-group"><label>Description / Notes</label><textarea id="m-desc" placeholder="Additional notes for BWF CodingHistory..."></textarea></div>
      <div class="btn-row">
        <button class="btn-primary" onclick="saveMeta()">Save BWF Metadata</button>
      </div>
      <p style="margin-top:14px;font-size:.72rem;color:var(--dim)">
        These tags are injected as a BWF <code>bext</code> chunk into every WAV file header at save time.
      </p>
    </div>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
const socket = io();
const DAY_NAMES = ['Mo','Tu','We','Th','Fr','Sa','Su'];

// ── Tabs ──
document.querySelectorAll('.tab-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    document.getElementById('p-' + b.dataset.tab).classList.add('active');
  });
});

// ── Mode switching ──
document.querySelectorAll('.mode-btn').forEach(b => {
  b.addEventListener('click', () => {
    fetch('/api/mode', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:b.dataset.mode})})
    .then(r=>r.json()).then(d => syncMode(d.mode));
  });
});
function syncMode(m) {
  document.querySelectorAll('.mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
}

// ── Recording controls ──
function startRec() { fetch('/start'); }
function stopRec()  { fetch('/stop'); }

// ── Real-time monitor ──
socket.on('audio_level', d => {
  const dbfs = Math.max(-80, d.dbfs);
  const pct = ((dbfs + 80) / 80) * 100;
  document.getElementById('meter-fill').style.width = Math.max(0,Math.min(100,pct)) + '%';
  document.getElementById('dbfs-val').textContent = (d.dbfs <= -100 ? '-∞' : d.dbfs.toFixed(1)) + ' dBFS';

  const pill = document.getElementById('status-pill');
  const btn = document.getElementById('btn-rec');
  if (d.recording) {
    pill.textContent = d.vad_active ? 'VAD Triggered' : (d.schedule_active ? 'Scheduled Rec' : 'Recording');
    pill.className = 'status-pill rec';
    btn.classList.add('active');
  } else {
    pill.textContent = d.schedule_active ? 'Sched. Standby' : 'Idle';
    pill.className = 'status-pill';
    btn.classList.remove('active');
  }
  syncMode(d.mode);
});

socket.on('system_health', d => {
  document.getElementById('h-cpu').textContent = d.cpu + '%';
  document.getElementById('h-disk').textContent = d.disk + '%';
  document.getElementById('h-ram').textContent = d.ram + '%';
  document.getElementById('h-temp').textContent = d.temp !== null ? d.temp + '°C' : 'N/A';
  if (d.battery !== null) {
    document.getElementById('h-bat-card').style.display = '';
    document.getElementById('h-bat').textContent = d.battery + '%';
  }
});

// ── VAD threshold marker on meter ──
function updateThreshMarker() {
  const v = parseFloat(document.getElementById('vad-thresh').value);
  const pct = ((v + 80) / 80) * 100;
  document.getElementById('meter-thresh').style.left = Math.max(0,Math.min(100,pct)) + '%';
}

// ── VAD settings ──
function initVad() {
  fetch('/api/vad').then(r=>r.json()).then(d => {
    document.getElementById('vad-thresh').value = d.threshold_dbfs;
    document.getElementById('vad-thresh-v').textContent = d.threshold_dbfs + ' dB';
    document.getElementById('vad-hold').value = d.hold_time;
    document.getElementById('vad-hold-v').textContent = d.hold_time + ' s';
    document.getElementById('vad-prebuf').value = d.pre_buffer_secs;
    document.getElementById('vad-prebuf-v').textContent = d.pre_buffer_secs + ' s';
    updateThreshMarker();
  });
}
['vad-thresh','vad-hold','vad-prebuf'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', () => {
    const unit = id === 'vad-thresh' ? ' dB' : ' s';
    document.getElementById(id + '-v').textContent = el.value + unit;
    if (id === 'vad-thresh') updateThreshMarker();
  });
});
function saveVad() {
  fetch('/api/vad', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      threshold_dbfs: parseFloat(document.getElementById('vad-thresh').value),
      hold_time: parseFloat(document.getElementById('vad-hold').value),
      pre_buffer_secs: parseFloat(document.getElementById('vad-prebuf').value)
    })
  }).then(() => toast('VAD settings saved'));
}

// ── Schedules ──
function loadSchedules() {
  fetch('/api/schedules').then(r=>r.json()).then(d => renderSchedules(d.schedules));
}
function renderSchedules(list) {
  const el = document.getElementById('sched-list');
  if (!list.length) { el.innerHTML = '<p style="color:var(--dim);font-size:.85rem;text-align:center;padding:30px 0">No recording windows configured.</p>'; return; }
  el.innerHTML = list.map(s => {
    const days = DAY_NAMES.map((n,i) => `<span class="${s.days.includes(i)?'on':''}">${n}</span>`).join('');
    return `<div class="sched-card">
      <button class="toggle ${s.enabled?'on':'off'}" onclick="toggleSched('${s.id}',${!s.enabled})"></button>
      <span class="time">${s.start} – ${s.end}</span>
      <div class="days">${days}</div>
      <span class="smode">${s.mode.toUpperCase()}</span>
      <div class="actions">
        <button onclick="editSched('${s.id}')">Edit</button>
        <button onclick="delSched('${s.id}')">Del</button>
      </div>
    </div>`;
  }).join('');
}
function toggleSched(id, enabled) {
  const sched = {id, enabled};
  fetch('/api/schedules', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(sched)})
  .then(r=>r.json()).then(d => renderSchedules(d.schedules));
}
function delSched(id) {
  fetch('/api/schedules', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})})
  .then(r=>r.json()).then(d => renderSchedules(d.schedules));
}
function showSchedForm(data) {
  const f = document.getElementById('sched-form');
  f.style.display = '';
  document.getElementById('sched-form-title').textContent = data ? 'Edit Schedule' : 'New Schedule';
  document.getElementById('sf-id').value = data ? data.id : '';
  document.getElementById('sf-start').value = data ? data.start : '06:00';
  document.getElementById('sf-end').value = data ? data.end : '10:00';
  document.getElementById('sf-mode').value = data ? data.mode : 'continuous';
  document.querySelectorAll('.day-pick').forEach(b => {
    const d = parseInt(b.dataset.d);
    b.classList.toggle('on', data ? data.days.includes(d) : true);
  });
}
function hideSchedForm() { document.getElementById('sched-form').style.display = 'none'; }
document.querySelectorAll('.day-pick').forEach(b => {
  b.addEventListener('click', () => b.classList.toggle('on'));
});
function editSched(id) {
  fetch('/api/schedules').then(r=>r.json()).then(d => {
    const s = d.schedules.find(x => x.id === id);
    if (s) showSchedForm(s);
  });
}
function saveSched() {
  const days = [...document.querySelectorAll('.day-pick.on')].map(b => parseInt(b.dataset.d));
  const entry = {
    id: document.getElementById('sf-id').value || undefined,
    start: document.getElementById('sf-start').value,
    end: document.getElementById('sf-end').value,
    days,
    mode: document.getElementById('sf-mode').value,
    enabled: true
  };
  fetch('/api/schedules', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(entry)})
  .then(r=>r.json()).then(d => { renderSchedules(d.schedules); hideSchedForm(); toast('Schedule saved'); });
}

// ── Save Directory ──
function loadSaveDir() {
  fetch('/api/savedir').then(r=>r.json()).then(d => {
    document.getElementById('m-savedir').value = d.save_dir || '';
  });
}
function saveSaveDir() {
  const dir = document.getElementById('m-savedir').value;
  fetch('/api/savedir', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({save_dir: dir})
  }).then(r => {
    if (!r.ok) return r.json().then(d => { toast('Error: ' + d.error); throw ''; });
    return r.json();
  }).then(d => {
    if (d) { document.getElementById('m-savedir').value = d.save_dir; toast('Save folder updated'); }
  }).catch(() => {});
}

// ── Metadata ──
function loadMeta() {
  fetch('/api/metadata').then(r=>r.json()).then(d => {
    document.getElementById('m-loc').value = d.location || '';
    document.getElementById('m-proj').value = d.project || '';
    document.getElementById('m-id').value = d.recorder_id || '';
    document.getElementById('m-desc').value = d.description || '';
  });
}
function saveMeta() {
  fetch('/api/metadata', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      location: document.getElementById('m-loc').value,
      project: document.getElementById('m-proj').value,
      recorder_id: document.getElementById('m-id').value,
      description: document.getElementById('m-desc').value
    })
  }).then(() => toast('BWF metadata saved'));
}

// ── Toast ──
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

// ── Engine error check ──
fetch('/status').then(r=>r.json()).then(d => {
  if (d.engine_error) {
    const el = document.getElementById('engine-err');
    el.style.display = '';
    el.textContent = 'Audio engine error: ' + d.engine_error;
  }
});

// ── Init ──
initVad();
loadSchedules();
loadSaveDir();
loadMeta();
</script>
</body>
</html>
'''

# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(state.save_dir, exist_ok=True)
    socketio.start_background_task(audio_engine)
    socketio.start_background_task(scheduler_loop)
    socketio.start_background_task(monitor_loop)
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
