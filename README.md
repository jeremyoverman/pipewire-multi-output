# pipewire-multi-output

Route audio to multiple speakers simultaneously on Linux using PipeWire, with per-speaker delay compensation to keep Bluetooth and wired speakers in sync.

Bluetooth audio has inherent latency (~100-200ms). If you play audio through both a Bluetooth speaker and a wired speaker at the same time, the wired one plays noticeably ahead. This tool fixes that by adding a configurable delay to the faster speakers, so everything sounds in sync.

Supports multiple concurrent profiles — run independent speaker groups simultaneously (e.g., "Living Room" with a soundbar + Bluetooth speaker, and "Office" with desk speakers).

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
- A systemd user service template (for auto-start on login, per profile)
- Migrates any existing single-profile config to the new profiles layout

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

- **Switch profiles** using the dropdown at the top — each profile has its own speakers, delays, and running state
- **Create/delete profiles** with the +/- buttons next to the profile selector
- Add/remove speakers from detected PipeWire sinks
- Expand a speaker row to adjust its delay with a slider or exact ms input
- Start/stop multi-output routing per profile (multiple profiles can run simultaneously)
- Play a test tone to help tune delay between speakers
- Save your configuration
- Toggle auto-start on login (per profile)

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

# Add/remove speakers from saved config
python3 -m multi_output add                    # interactive picker
python3 -m multi_output add alsa_output.usb-MyDevice-00 --delay 150
python3 -m multi_output remove 1               # by index (see 'status')

# Adjust delay live
python3 -m multi_output set-delay 1 120

# Save config, check status, stop
python3 -m multi_output save
python3 -m multi_output status
python3 -m multi_output stop

# Auto-start on login
python3 -m multi_output autostart on
python3 -m multi_output autostart off
python3 -m multi_output autostart              # check status
```

### Profiles

Create and manage multiple independent speaker groups:

```bash
# List all profiles and their status
python3 -m multi_output list

# Create a new profile
python3 -m multi_output create-profile "Living Room"

# Work with a specific profile using -p
python3 -m multi_output -p living_room add
python3 -m multi_output -p living_room start
python3 -m multi_output -p living_room status

# Run multiple profiles simultaneously
python3 -m multi_output -p default start
python3 -m multi_output -p living_room start

# Check all running profiles at once
python3 -m multi_output status --all

# Stop everything
python3 -m multi_output stop --all

# Enable auto-start for a specific profile
python3 -m multi_output -p living_room autostart on

# Delete a profile
python3 -m multi_output delete-profile living_room
```

Each profile gets its own null sink, loopback processes, config file, and systemd service instance. A speaker can be configured in multiple profiles but can only be actively used by one running profile at a time.

### Auto-start on login

Toggle "Auto-start on login" in the GUI, or manually:

```bash
# Enable for the default profile
systemctl --user enable multi-output@default.service

# Enable for a specific profile
systemctl --user enable multi-output@living_room.service
```

The service reads your saved config and waits up to 5 minutes for all sinks to appear, which handles Bluetooth speakers that take time to auto-connect after login.

```bash
# Check service logs
journalctl --user -u multi-output@default.service
```

## Tuning delay

1. Start multi-output with your speakers
2. Click the speaker icon in the header bar to play a repeating test tone (or from the CLI: `python3 -m multi_output test --interval 1`)
3. Listen for the offset between speakers and adjust the faster speaker's delay
4. Expand a speaker row and drag the slider (or type an exact ms value)
5. Click the speaker icon again to stop the test tone
6. Save when it sounds right

**Tip:** Bluetooth SBC codec latency is typically 100-200ms. Start there for the wired speaker's delay and fine-tune by ear.

## Configuration

Profile configs are stored at `~/.config/pipewire-multi-output/profiles/<slug>.json`:

```json
{
  "slug": "default",
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

Runtime state (PIDs, resolved sink IDs) is stored at `~/.cache/pipewire-multi-output/<slug>.json` and is not meant to be edited.

Existing single-file configs (`config.json`) are automatically migrated to `profiles/default.json` on first run.

## Troubleshooting

### No sound from a speaker

- Check the sink exists: `pactl list short sinks | grep bluez` (or `alsa`)
- Try playing directly: `pw-play --target=<sink_name> /usr/share/sounds/freedesktop/stereo/bell.oga`
- For Bluetooth, verify it's connected: `bluetoothctl info <MAC> | grep Connected`

### Apps don't switch to multi-output

Some apps hold onto a specific sink. After starting, move existing streams:

```bash
pactl list short sink-inputs
pactl move-sink-input <stream_id> multi_out_default
```

Or just restart the app.

### Speaker conflict error

A speaker can only be used by one running profile at a time. If you get a conflict error, stop the other profile first:

```bash
python3 -m multi_output status --all    # see which profile has the speaker
python3 -m multi_output -p other stop   # stop that profile
```

### Low-frequency buzzing / feedback

This can happen if pw-loopback captures its own output. The tool sets `node.dont-monitor=true` to prevent this. If it happens anyway:

```bash
python3 -m multi_output stop --all
```

### Service fails on boot

Check logs: `journalctl --user -u multi-output@default.service`

Common causes:
- **Bluetooth not connected** -- the service waits up to 5 minutes, but the speaker must be in range and paired
- **Stale state** -- delete `~/.cache/pipewire-multi-output/<slug>.json` if it has stale PIDs

## How it works

```
                          +-------------------+
                          |    Null Sink      |
  App audio  ----------> | (multi_out_default)|
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

1. A **null sink** (e.g., `multi_out_default`) is created as the default audio output via `pw-cli`
2. **pw-loopback** instances capture the null sink's monitor and route to each physical speaker
3. Faster speakers get an artificial delay to match the slowest speaker's natural latency
4. Multiple profiles can run concurrently, each with their own null sink and loopbacks

### Why not module-combine-sink?

PulseAudio's `module-combine-sink` has poor latency compensation for Bluetooth. Its `adjust_time` doesn't account for codec latency, and `pactl set-port-latency-offset` has no audible effect. PipeWire's native `pw-loopback --delay` works reliably.

### Project structure

```
multi_output/
  core.py       # All PipeWire/PulseAudio logic (start, stop, loopback, config, profiles)
  gui.py        # GTK4/libadwaita GUI
  cli.py        # Command-line interface
  __main__.py   # Entry point for `python3 -m multi_output`
install.sh                # Installs .desktop file and systemd service template
multi-output-service.py   # Systemd service entry point (accepts profile slug)
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
