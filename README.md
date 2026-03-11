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

## Pi Audio Setup (Required Before First Run)

The INMP441 is an I2S microphone — it won't work until the Pi's operating system is configured to use it.

### 1. Enable the I2S overlay

Edit your boot config:

```bash
sudo nano /boot/firmware/config.txt
```

> On older Pi OS versions the file may be at `/boot/config.txt` instead.

Add this line at the bottom:

```
dtoverlay=googlevoicehat-soundcard
```

Save (Ctrl+O, Enter, Ctrl+X) and reboot:

```bash
sudo reboot
```

### 2. Verify the hardware is detected

After reboot, check that ALSA sees the sound card:

```bash
arecord -l
```

You should see output like:

```
card 0: sndrpigooglevoi [snd_rpi_googlevoicehat_soundcar], device 0: ...
```

If you see **"no soundcards found"**, double-check your I2S wiring and the `dtoverlay` line above.

### 3. Find the device index

Run this to list audio devices as Python sees them:

```bash
python3 -c "
import pyaudio
p = pyaudio.PyAudio()
for i in range(p.get_device_count()):
    d = p.get_device_info_by_index(i)
    if d['maxInputChannels'] > 0:
        print(f'ID {i}: {d[\"name\"]}')
p.terminate()
"
```

The recorder auto-detects the first available input device, so this step is just for verification. You should see your VoiceHAT / INMP441 listed.

### 4. (Optional) Silence ALSA warnings

ALSA prints harmless warnings about missing PCM devices (surround, modem, etc.) on every Pi. To suppress them, create `~/.asoundrc`:

```bash
cat > ~/.asoundrc << 'EOF'
pcm.!default {
    type hw
    card 0
}
ctl.!default {
    type hw
    card 0
}
EOF
```

Replace `card 0` with the card number from `arecord -l` if different.

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/bird-recorder.git ~/Desktop/bird
cd ~/Desktop/bird
sudo apt install -y portaudio19-dev
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
