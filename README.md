# Bird Recorder

Raspberry Pi field recorder with a web dashboard for real-time audio monitoring, scheduled recording, and BWF metadata tagging. Designed for the Pi Zero 2 W with an INMP441 I2S MEMS microphone (via Google VoiceHAT).

## Features

- **Real-Time Monitor** — WebSocket-powered dBFS meter with live CPU, disk, temperature, and RAM readouts
- **Recording Modes** — Continuous (manual), VAD (auto-trigger on sound), and Scheduled (time-window based)
- **Smart Scheduler** — Set recording windows by time-of-day and day-of-week, with per-window mode selection
- **BWF Metadata** — Injects Broadcast Wave Format `bext` chunks (Location, Project, Recorder ID) into every WAV header
- **Configurable Save Folder** — Change the recording output directory from the web UI
- **Background Operation** — Runs headless via `start.sh` with `nohup`, persists config across restarts

## Hardware

| Component | Role |
|---|---|
| Raspberry Pi Zero 2 W | Host |
| INMP441 MEMS microphone | Audio input (I2S) |
| Google VoiceHAT (or direct I2S wiring) | Audio interface |

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/bird-recorder.git ~/Desktop/bird
cd ~/Desktop/bird
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
chmod +x start.sh
./start.sh
```

Open `http://<pi-ip>:5000` in a browser.

## Auto-Start on Boot

```bash
chmod +x ~/Desktop/bird/start.sh
crontab -e
```

Add this line:

```
@reboot /home/YOUR_USER/Desktop/bird/start.sh
```

## Project Structure

```
bird/
├── record.py          # Main application (server, audio engine, dashboard)
├── start.sh           # Background launcher (activates venv, runs with nohup)
├── requirements.txt   # Python dependencies
├── config.json        # Auto-generated settings (metadata, schedules, VAD, mode)
└── recordings/        # Auto-created WAV output directory
```

## Configuration

All settings are managed through the web dashboard and persisted to `config.json`:

- **Monitor tab** — Live audio levels, system health, VAD threshold/hold/pre-buffer tuning
- **Schedule tab** — Add/edit/delete recording windows with day and mode selection
- **Metadata tab** — Set save folder and BWF tags (Location, Project, Recorder ID, Description)

## Audio Specs

| Setting | Value |
|---|---|
| Sample rate | 48,000 Hz |
| Bit depth | 16-bit signed PCM |
| Channels | Mono |
| Format | WAV with optional BWF `bext` chunk |

## Dependencies

- **flask** — Web server
- **flask-socketio** — WebSocket support for real-time metering
- **simple-websocket** — WebSocket transport for threading mode
- **pyaudio** — Audio capture (requires `portaudio19-dev` on Pi)
- **psutil** — System health metrics

If `pip install pyaudio` fails, install the system dependency first:

```bash
sudo apt install portaudio19-dev
```

## License

MIT
