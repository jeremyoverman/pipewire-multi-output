"""PipeWire multi-output audio engine.

Creates a null sink as a routing hub, then uses pw-loopback to send audio
to N speakers simultaneously with per-speaker delay compensation.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

NULL_SINK_NAME = "multi_out"

CONFIG_DIR = Path.home() / ".config" / "pipewire-multi-output"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_DIR = Path.home() / ".cache" / "pipewire-multi-output"
STATE_FILE = STATE_DIR / "state.json"

# Also clean up legacy state file from the old script
LEGACY_STATE_FILE = Path.home() / ".cache" / "multi-output-state.json"

SYSTEMD_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE_NAME = "multi-output.service"


@dataclass
class SpeakerConfig:
    """Configuration for a single speaker output."""

    sink_name: str  # PipeWire sink name (or prefix for fuzzy match)
    delay_ms: float = 0  # per-speaker delay in ms (0 = reference speaker)
    label: str = ""  # human-readable label (auto-populated from pactl)


@dataclass
class MultiOutputConfig:
    """Configuration for the multi-output setup."""

    name: str = "Speakers/Soundbar"  # device description shown in GNOME
    speakers: list[SpeakerConfig] = field(default_factory=list)


@dataclass
class SpeakerState:
    """Runtime state for a single speaker's loopback process."""

    sink_id: int
    sink_name: str
    label: str
    delay_ms: float
    pid: int


@dataclass
class MultiOutputState:
    """Runtime state for the entire multi-output setup."""

    null_node_id: str
    monitor_source_id: str
    speakers: list[SpeakerState] = field(default_factory=list)


# --- Sink discovery ---


def get_sinks() -> list[dict]:
    """Get available sinks from pactl."""
    result = subprocess.run(
        ["pactl", "-f", "json", "list", "short", "sinks"],
        capture_output=True,
        text=True,
    )
    try:
        sinks = json.loads(result.stdout)
    except json.JSONDecodeError:
        sinks = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                sinks.append({"index": int(parts[0]), "name": parts[1]})
    return sinks


def get_sink_description(sink_id: int) -> str:
    """Get human-readable description for a sink by its numeric ID."""
    result = subprocess.run(
        ["pactl", "-f", "json", "list", "sinks"],
        capture_output=True,
        text=True,
    )
    try:
        for sink in json.loads(result.stdout):
            if sink.get("index") == sink_id:
                return sink.get("description", str(sink_id))
    except (json.JSONDecodeError, KeyError):
        pass
    return str(sink_id)


def resolve_sink(identifier: str | None) -> int | None:
    """Resolve a sink name, prefix, or ID string to a numeric sink index.

    Returns None if not found.
    """
    if identifier is None:
        return None
    sinks = get_sinks()
    # Try as numeric ID first
    try:
        target_id = int(identifier)
        if any(s["index"] == target_id for s in sinks):
            return target_id
    except ValueError:
        pass
    # Try exact sink name match
    for sink in sinks:
        if sink["name"] == identifier:
            return sink["index"]
    # Try prefix match (handles profile suffix changes)
    for sink in sinks:
        if sink["name"].startswith(identifier):
            return sink["index"]
    return None


def get_available_sinks(exclude_names: list[str] | None = None) -> list[dict]:
    """Get sinks available for selection, excluding null sinks and specified names."""
    exclude = set(exclude_names or [])
    exclude.add(NULL_SINK_NAME)
    sinks = get_sinks()
    available = []
    for s in sinks:
        if s["name"] not in exclude:
            s["description"] = get_sink_description(s["index"])
            available.append(s)
    return available


def select_sink_interactive(
    prompt: str, exclude: list[str] | None = None
) -> tuple[int, str]:
    """Interactive sink selection. Returns (index, name)."""
    sinks = get_available_sinks(exclude)
    if not sinks:
        print("No available sinks found.")
        sys.exit(1)

    print(f"\n{prompt}\n")
    for i, sink in enumerate(sinks):
        print(f"  [{i + 1}] {sink['description']}")
        print(f"      ID: {sink['index']}  Name: {sink['name']}")
        print()

    selection = input(f"Select [1-{len(sinks)}]: ").strip()
    try:
        idx = int(selection) - 1
        sink = sinks[idx]
        return sink["index"], sink["name"]
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)


# --- Config persistence ---


def save_config(config: MultiOutputConfig) -> None:
    """Save configuration to config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "name": config.name,
        "speakers": [asdict(s) for s in config.speakers],
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def load_config() -> MultiOutputConfig | None:
    """Load configuration from config.json."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
        speakers = [SpeakerConfig(**s) for s in data.get("speakers", [])]
        return MultiOutputConfig(name=data.get("name", "Speakers/Soundbar"), speakers=speakers)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


# --- Runtime state ---


def save_state(state: MultiOutputState) -> None:
    """Save runtime state to state.json."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "null_node_id": state.null_node_id,
        "monitor_source_id": state.monitor_source_id,
        "speakers": [asdict(s) for s in state.speakers],
    }
    STATE_FILE.write_text(json.dumps(data, indent=2) + "\n")


def load_state() -> MultiOutputState | None:
    """Load runtime state from state.json."""
    # Try new state file first, then legacy
    for path in (STATE_FILE, LEGACY_STATE_FILE):
        try:
            data = json.loads(path.read_text())
            # Handle legacy 2-speaker state format
            if "speakers" not in data:
                speakers = []
                if "slow_pid" in data:
                    speakers.append(
                        SpeakerState(
                            sink_id=data["slow_id"],
                            sink_name=data.get("slow_name", ""),
                            label=data.get("slow_desc", ""),
                            delay_ms=0,
                            pid=data["slow_pid"],
                        )
                    )
                if "fast_pid" in data:
                    speakers.append(
                        SpeakerState(
                            sink_id=data["fast_id"],
                            sink_name=data.get("fast_name", ""),
                            label=data.get("fast_desc", ""),
                            delay_ms=data.get("delay_ms", 0),
                            pid=data["fast_pid"],
                        )
                    )
                return MultiOutputState(
                    null_node_id=data.get("null_node_id", ""),
                    monitor_source_id=data.get("monitor_source_id", ""),
                    speakers=speakers,
                )
            speakers = [SpeakerState(**s) for s in data["speakers"]]
            return MultiOutputState(
                null_node_id=data["null_node_id"],
                monitor_source_id=data["monitor_source_id"],
                speakers=speakers,
            )
        except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError):
            continue
    return None


def clear_state() -> None:
    """Remove state files."""
    for path in (STATE_FILE, LEGACY_STATE_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --- Audio engine ---


def _find_monitor_source_id() -> str | None:
    """Find the monitor source ID for the null sink."""
    result = subprocess.run(
        ["pactl", "list", "short", "sources"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if f"{NULL_SINK_NAME}.monitor" in line:
            return line.split("\t")[0]
    return None


def _launch_loopback(
    monitor_source_id: str,
    sink_id: int,
    delay_ms: float,
    index: int,
) -> subprocess.Popen:
    """Launch a pw-loopback process for one speaker."""
    cmd = [
        "pw-loopback",
        "-C",
        monitor_source_id,
        "-P",
        str(sink_id),
        "-n",
        f"loopback-{index}",
        "-i",
        "node.dont-reconnect=true",
        "-o",
        "node.dont-reconnect=true node.dont-monitor=true",
    ]
    if delay_ms > 0:
        cmd.extend(["--delay", str(delay_ms / 1000.0)])

    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start(config: MultiOutputConfig, wait: bool = False, timeout: int = 300) -> MultiOutputState:
    """Start multi-output routing.

    Creates a null sink, launches pw-loopback for each speaker, and sets
    the null sink as the default output.

    Args:
        config: Speaker configuration.
        wait: If True, poll until all sinks appear (for systemd use).
        timeout: Max seconds to wait for sinks (only if wait=True).

    Returns:
        The runtime state.

    Raises:
        RuntimeError: If setup fails.
    """
    # Stop any existing setup first
    stop(quiet=True)

    if not config.speakers:
        raise RuntimeError("No speakers configured.")

    # Resolve all sinks
    sink_ids: list[int] = []
    if wait:
        sink_ids = wait_for_sinks(config, timeout=timeout)
    else:
        for speaker in config.speakers:
            sid = resolve_sink(speaker.sink_name)
            if sid is None:
                raise RuntimeError(f"Sink not found: {speaker.sink_name}")
            sink_ids.append(sid)

    # Populate labels from pactl if not set
    for i, speaker in enumerate(config.speakers):
        if not speaker.label:
            speaker.label = get_sink_description(sink_ids[i])

    # Create null sink via pw-cli (handles spaces in description correctly)
    # Sanitize the name to prevent property injection via quote characters
    safe_name = re.sub(r'[^a-zA-Z0-9 /\-_]', '', config.name) or "Multi Output"
    result = subprocess.run(
        [
            "pw-cli",
            "create-node",
            "adapter",
            "{ "
            f"factory.name=support.null-audio-sink node.name={NULL_SINK_NAME} "
            f'node.description="{safe_name}" '
            "media.class=Audio/Sink object.linger=true audio.position=[FL,FR]"
            " }",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    null_node_id = result.stdout.strip().removeprefix("id:").strip().rstrip(",")

    # Find monitor source
    monitor_source_id = _find_monitor_source_id()
    if monitor_source_id is None:
        raise RuntimeError("Could not find multi_out monitor source.")

    # Set as default sink
    subprocess.run(["pactl", "set-default-sink", NULL_SINK_NAME], check=True)

    # Launch loopbacks
    speaker_states: list[SpeakerState] = []
    for i, (speaker, sink_id) in enumerate(zip(config.speakers, sink_ids)):
        proc = _launch_loopback(monitor_source_id, sink_id, speaker.delay_ms, i)
        # Look up current sink name (in case we resolved from prefix)
        sink_name = speaker.sink_name
        for s in get_sinks():
            if s["index"] == sink_id:
                sink_name = s["name"]
                break
        speaker_states.append(
            SpeakerState(
                sink_id=sink_id,
                sink_name=sink_name,
                label=speaker.label,
                delay_ms=speaker.delay_ms,
                pid=proc.pid,
            )
        )

    state = MultiOutputState(
        null_node_id=null_node_id,
        monitor_source_id=monitor_source_id,
        speakers=speaker_states,
    )
    save_state(state)
    return state


def _is_pw_loopback(pid: int) -> bool:
    """Check if a PID belongs to a pw-loopback process."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"pw-loopback" in cmdline
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _kill_loopback(pid: int) -> None:
    """Send SIGTERM to a PID, but only if it's a pw-loopback process."""
    if not _is_pw_loopback(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def stop(quiet: bool = False) -> None:
    """Stop multi-output and restore default sink."""
    state = load_state()
    if state is None:
        if not quiet:
            print("No multi-output session running.")
        return

    # Kill loopback processes (verified before sending signal)
    for speaker in state.speakers:
        _kill_loopback(speaker.pid)

    # Destroy null sink node
    subprocess.run(
        ["pw-cli", "destroy", NULL_SINK_NAME],
        capture_output=True,
    )

    # Restore default to the first wired (non-Bluetooth) speaker, or just the first one
    restore_sink = None
    for speaker in state.speakers:
        if not speaker.sink_name.startswith("bluez_"):
            restore_sink = speaker.sink_name
            break
    if restore_sink is None and state.speakers:
        restore_sink = state.speakers[0].sink_name
    if restore_sink:
        subprocess.run(
            ["pactl", "set-default-sink", restore_sink],
            capture_output=True,
        )

    clear_state()
    if not quiet:
        print("Multi-output stopped.")


def update_speaker_delay(index: int, delay_ms: float) -> None:
    """Update the delay for a single speaker (kills and relaunches its loopback).

    Args:
        index: Speaker index (0-based).
        delay_ms: New delay in milliseconds.

    Raises:
        RuntimeError: If no session is running or index is out of range.
    """
    state = load_state()
    if state is None:
        raise RuntimeError("No multi-output session running.")

    if index < 0 or index >= len(state.speakers):
        raise RuntimeError(
            f"Speaker index {index} out of range (0-{len(state.speakers) - 1})."
        )

    speaker = state.speakers[index]

    # Kill existing loopback (verified before sending signal)
    _kill_loopback(speaker.pid)

    # Relaunch with new delay
    proc = _launch_loopback(
        state.monitor_source_id, speaker.sink_id, delay_ms, index
    )

    speaker.pid = proc.pid
    speaker.delay_ms = delay_ms
    save_state(state)


def wait_for_sinks(
    config: MultiOutputConfig,
    timeout: int = 300,
    poll_interval: int = 5,
    on_progress: callable | None = None,
) -> list[int]:
    """Poll until all configured sinks are available.

    Args:
        config: Speaker configuration.
        timeout: Max seconds to wait.
        poll_interval: Seconds between polls.
        on_progress: Optional callback(found: int, total: int, missing: list[str]).

    Returns:
        List of resolved sink IDs in the same order as config.speakers.

    Raises:
        TimeoutError: If sinks don't appear within timeout.
    """
    deadline = time.time() + timeout
    total = len(config.speakers)

    while time.time() < deadline:
        ids: list[int | None] = []
        missing: list[str] = []
        for speaker in config.speakers:
            sid = resolve_sink(speaker.sink_name)
            ids.append(sid)
            if sid is None:
                missing.append(speaker.label or speaker.sink_name)

        if all(sid is not None for sid in ids):
            return ids  # type: ignore[return-value]

        if on_progress:
            on_progress(total - len(missing), total, missing)

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Timed out waiting for sinks: {', '.join(missing)}"
    )


def is_running() -> bool:
    """Check if a multi-output session is active."""
    state = load_state()
    if state is None:
        return False
    # Verify at least one loopback process is alive
    for speaker in state.speakers:
        try:
            os.kill(speaker.pid, 0)
            return True
        except (ProcessLookupError, OSError):
            continue
    # All processes dead — clean up stale state
    clear_state()
    return False


# --- Test tone ---


def generate_ping(
    frequency: int = 1000,
    duration_ms: int = 100,
    sample_rate: int = 48000,
) -> bytes:
    """Generate a short sine wave ping with fade-in/fade-out envelope."""
    n_samples = int(sample_rate * duration_ms / 1000)
    fade_samples = min(500, n_samples // 4)
    samples = []
    for i in range(n_samples):
        envelope = min(1.0, i / fade_samples) * min(
            1.0, (n_samples - i) / fade_samples
        )
        value = int(
            32767 * envelope * math.sin(2 * math.pi * frequency * i / sample_rate)
        )
        samples.append(struct.pack("<h", value))
    return b"".join(samples)


def play_ping(
    device: str | None = None,
    freq: int = 1000,
    duration: int = 100,
    interval: float = 2.0,
    rate: int = 48000,
) -> None:
    """Play repeating ping for sync testing. Blocks until KeyboardInterrupt."""
    ping_data = generate_ping(freq, duration, rate)

    cmd = ["paplay", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le"]
    if device:
        cmd.append(f"--device={device}")

    device_label = device or "default"
    print(f"Playing {freq}Hz ping every {interval}s on '{device_label}'")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(input=ping_data)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


# --- Systemd integration ---


def _project_dir() -> Path:
    """Return the root directory of this project."""
    return Path(__file__).resolve().parent.parent


def install_service() -> None:
    """Write (or overwrite) the systemd user service file and reload the daemon."""
    project = _project_dir()
    service_path = SYSTEMD_SERVICE_DIR / SYSTEMD_SERVICE_NAME
    SYSTEMD_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    # Escape the project path for safe embedding in shell and Python contexts
    shell_path = shlex.quote(str(project / "multi-output-service.py"))
    python_path = str(project).replace("\\", "\\\\").replace("'", "\\'")
    service_path.write_text(
        f"""\
[Unit]
Description=Multi-output speaker sync (PipeWire)
After=pipewire.service pipewire-pulse.service
Requires=pipewire.service pipewire-pulse.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {shell_path}
ExecStop=/usr/bin/python3 -c "import sys; sys.path.insert(0, '{python_path}'); from multi_output import core; core.stop()"

[Install]
WantedBy=default.target
"""
    )
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
    )


def is_service_installed() -> bool:
    """Check if the systemd user service file exists."""
    return (SYSTEMD_SERVICE_DIR / SYSTEMD_SERVICE_NAME).exists()


def is_service_enabled() -> bool:
    """Check if the systemd user service is enabled."""
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", SYSTEMD_SERVICE_NAME],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "enabled"


def set_service_enabled(enabled: bool) -> None:
    """Enable or disable the systemd user service.

    Installs the service file first if it doesn't exist.
    """
    if enabled:
        install_service()
    subprocess.run(
        ["systemctl", "--user", "enable" if enabled else "disable", SYSTEMD_SERVICE_NAME],
        capture_output=True,
        check=True,
    )
