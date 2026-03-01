# pipewire-multi-output

Route audio to multiple speakers simultaneously on Linux using PipeWire, with per-speaker delay compensation to keep Bluetooth and wired speakers in sync.

Bluetooth audio has inherent latency (~100-200ms). If you play audio through both a Bluetooth speaker and a wired speaker at the same time, the wired one plays noticeably ahead. This tool fixes that by adding a configurable delay to the faster speakers, so everything sounds in sync.

Includes a GTK4/libadwaita GUI, a CLI, and a systemd user service for auto-start on login.

## Installation

### Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/jeremyoverman/pipewire-multi-output/main/install-remote.sh | bash
```

This clones the repo to `~/.local/share/pipewire-multi-output` and runs the installer. If already installed, it pulls the latest version instead.

### Manual install

If you prefer to inspect the script first:

```bash
git clone https://github.com/jeremyoverman/pipewire-multi-output.git
cd pipewire-multi-output
./install.sh
```

### What the installer does

The install script checks for missing dependencies and tells you what to install. It sets up:

- A `.desktop` launcher (shows up in your app menu)
- A systemd user service (for auto-start on login)

### Dependencies

| | Fedora | Ubuntu/Debian | Arch |
|---|---|---|---|
| PipeWire | `pipewire pipewire-utils pulseaudio-utils` | `pipewire pipewire-pulse pulseaudio-utils` | `pipewire pipewire-pulse` |
| GUI | `python3-gobject libadwaita` | `python3-gi gir1.2-adw-1 libadwaita-1-0` | `python-gobject libadwaita` |

## Usage

### GUI

Launch **Multi-Output Audio** from your app menu, or run:

```bash
python3 -m multi_output.gui
```

From the GUI you can:

- Add/remove speakers from detected PipeWire sinks
- Expand a speaker row to adjust its delay with a slider or exact ms input
- Start/stop multi-output routing (changes apply live while running)
- Save your configuration
- Toggle auto-start on login

### CLI

```bash
# Interactive setup (walks you through picking speakers)
python3 -m multi_output start

# Start from saved config
python3 -m multi_output start

# Start with specific sinks and delays
python3 -m multi_output start \
  --speakers bluez_output.XX_XX_XX_XX_XX_XX alsa_output.usb-MyDevice-00 \
  --delays 0 150

# Adjust delay live
python3 -m multi_output set-delay 1 120

# Check what's running
python3 -m multi_output status

# Stop
python3 -m multi_output stop
```

### Auto-start on login

Toggle "Auto-start on login" in the GUI, or manually:

```bash
systemctl --user enable multi-output.service
```

The service reads your saved config and waits up to 5 minutes for all sinks to appear, which handles Bluetooth speakers that take time to auto-connect after login.

```bash
# Check service logs
journalctl --user -u multi-output.service
```

## Tuning delay

1. Start multi-output with your speakers
2. Play test pings: `python3 -m multi_output test --interval 1`
3. Listen for the offset between speakers and adjust the faster speaker's delay
4. In the GUI, expand a speaker row and drag the slider (or type an exact ms value)
5. Save when it sounds right

**Tip:** Bluetooth SBC codec latency is typically 100-200ms. Start there for the wired speaker's delay and fine-tune by ear.

## Configuration

Config is stored at `~/.config/pipewire-multi-output/config.json`:

```json
{
  "name": "Speakers/Soundbar",
  "speakers": [
    {
      "sink_name": "bluez_output.AA_BB_CC_DD_EE_FF.1",
      "delay_ms": 0,
      "label": "Living Room Soundbar"
    },
    {
      "sink_name": "alsa_output.usb-MyDevice-00",
      "delay_ms": 150,
      "label": "USB Audio Device"
    }
  ]
}
```

Runtime state (PIDs, resolved sink IDs) is stored at `~/.cache/pipewire-multi-output/state.json` and is not meant to be edited.

## Troubleshooting

### No sound from a speaker

- Check the sink exists: `pactl list short sinks | grep bluez` (or `alsa`)
- Try playing directly: `pw-play --target=<sink_name> /usr/share/sounds/freedesktop/stereo/bell.oga`
- For Bluetooth, verify it's connected: `bluetoothctl info <MAC> | grep Connected`

### Apps don't switch to multi-output

Some apps hold onto a specific sink. After starting, move existing streams:

```bash
pactl list short sink-inputs
pactl move-sink-input <stream_id> multi_out
```

Or just restart the app.

### Low-frequency buzzing / feedback

This can happen if pw-loopback captures its own output. The tool sets `node.dont-monitor=true` to prevent this. If it happens anyway:

```bash
python3 -m multi_output stop
```

### Service fails on boot

Check logs: `journalctl --user -u multi-output.service`

Common causes:
- **Bluetooth not connected** -- the service waits up to 5 minutes, but the speaker must be in range and paired
- **Stale state** -- delete `~/.cache/pipewire-multi-output/state.json` if it has stale PIDs

## How it works

```
                          +-------------------+
                          |    Null Sink      |
  App audio  ----------> |   (multi_out)     |
                          +--------+----------+
                                   | .monitor
                       +-----------+-----------+
                       |           |           |
                 pw-loopback  pw-loopback  pw-loopback
                 (no delay)   (150ms)      (150ms)
                       |           |           |
                       v           v           v
                +-----------+ +---------+ +---------+
                | Bluetooth | |  Wired  | |  Wired  |
                | Soundbar  | |  Dock   | | HDMI TV |
                | (slow)    | | (fast)  | | (fast)  |
                +-----------+ +---------+ +---------+
```

1. A **null sink** (`multi_out`) is created as the default audio output via `pw-cli`
2. **pw-loopback** instances capture the null sink's monitor and route to each physical speaker
3. Faster speakers get an artificial delay to match the slowest speaker's natural latency

### Why not module-combine-sink?

PulseAudio's `module-combine-sink` has poor latency compensation for Bluetooth. Its `adjust_time` doesn't account for codec latency, and `pactl set-port-latency-offset` has no audible effect. PipeWire's native `pw-loopback --delay` works reliably.

### Project structure

```
multi_output/
  core.py       # All PipeWire/PulseAudio logic (start, stop, loopback, config)
  gui.py        # GTK4/libadwaita GUI
  cli.py        # Command-line interface
  __main__.py   # Entry point for `python3 -m multi_output`
install.sh                # Installs .desktop file and systemd service
multi-output-service.py   # Systemd service entry point
multi-output.desktop      # Desktop launcher template
```

## Contributing

Contributions are welcome! This project is intentionally small and focused.

1. Fork the repo and create a branch
2. Make your changes
3. Test manually with at least two speakers (or mock the PipeWire commands)
4. Open a pull request

### Useful PipeWire commands for development

```bash
pactl list short sinks          # List output devices
pactl list short sources        # List input devices / monitors
pactl list short sink-inputs    # List active audio streams
pw-link -l                      # Show PipeWire node graph
pw-cli ls Node                  # List all PipeWire nodes
```

## License

[MIT](LICENSE)
