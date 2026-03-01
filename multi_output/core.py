"""PipeWire multi-output audio engine.

Creates a null sink as a routing hub, then uses pw-loopback to send audio
to N speakers simultaneously with per-speaker delay compensation.

Supports multiple concurrent profiles, each with its own null sink,
loopbacks, config, and state.
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

CONFIG_DIR = Path.home() / ".config" / "pipewire-multi-output"
PROFILES_DIR = CONFIG_DIR / "profiles"
STATE_DIR = Path.home() / ".cache" / "pipewire-multi-output"

# Legacy paths (pre-profile era)
LEGACY_CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_STATE_FILE_NEW = STATE_DIR / "state.json"
LEGACY_STATE_FILE_OLD = Path.home() / ".cache" / "multi-output-state.json"

SYSTEMD_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_TEMPLATE_NAME = "multi-output@.service"
LEGACY_SERVICE_NAME = "multi-output.service"

MAX_SLUG_LENGTH = 32


def slugify(name: str) -> str:
    """Convert a human-readable name into a filesystem-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return slug[:MAX_SLUG_LENGTH] or "default"


def null_sink_name(slug: str) -> str:
    """PipeWire null sink node name for a profile."""
    return f"multi_out_{slug}"


def _config_path(slug: str) -> Path:
    return PROFILES_DIR / f"{slug}.json"


def _state_path(slug: str) -> Path:
    return STATE_DIR / f"{slug}.json"


@dataclass
class SpeakerConfig:
    """Configuration for a single speaker output."""

    sink_name: str  # PipeWire sink name (or prefix for fuzzy match)
    delay_ms: float = 0  # per-speaker delay in ms (0 = reference speaker)
    label: str = ""  # human-readable label (auto-populated from pactl)


@dataclass
class MultiOutputConfig:
    """Configuration for the multi-output setup."""

    slug: str = "default"
    name: str = "Speakers/Soundbar"  # device description shown in sound settings
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

    slug: str = "default"
    null_node_id: str = ""
    monitor_source_id: str = ""
    speakers: list[SpeakerState] = field(default_factory=list)


# --- Migration ---


def migrate_if_needed() -> None:
    """Migrate legacy single-config layout to per-profile layout."""
    if PROFILES_DIR.exists() or not LEGACY_CONFIG_FILE.exists():
        return

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # Migrate config
    try:
        data = json.loads(LEGACY_CONFIG_FILE.read_text())
        data["slug"] = "default"
        _config_path("default").write_text(json.dumps(data, indent=2) + "\n")
        LEGACY_CONFIG_FILE.unlink()
    except (json.JSONDecodeError, OSError):
        pass

    # Migrate state
    for legacy_state in (LEGACY_STATE_FILE_NEW, LEGACY_STATE_FILE_OLD):
        try:
            data = json.loads(legacy_state.read_text())
            data["slug"] = "default"
            _state_path("default").write_text(json.dumps(data, indent=2) + "\n")
            legacy_state.unlink()
            break
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

    # Migrate systemd service
    old_service = SYSTEMD_SERVICE_DIR / LEGACY_SERVICE_NAME
    if old_service.exists():
        was_enabled = False
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", LEGACY_SERVICE_NAME],
            capture_output=True, text=True,
        )
        was_enabled = result.stdout.strip() == "enabled"

        if was_enabled:
            subprocess.run(
                ["systemctl", "--user", "disable", LEGACY_SERVICE_NAME],
                capture_output=True,
            )
        old_service.unlink(missing_ok=True)

        install_service()
        if was_enabled:
            subprocess.run(
                ["systemctl", "--user", "enable", f"multi-output@default.service"],
                capture_output=True,
            )
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
        )


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
    # Exclude null sinks from all running profiles
    for slug in list_running_profiles():
        exclude.add(null_sink_name(slug))
    sinks = get_sinks()
    available = []
    for s in sinks:
        # Exclude by name and any multi_out* sink (prevent feedback loops)
        if s["name"] not in exclude and not s["name"].startswith("multi_out"):
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


# --- Profile management ---


def list_profiles() -> list[str]:
    """Return all saved profile slugs."""
    if not PROFILES_DIR.exists():
        return []
    return sorted(
        p.stem for p in PROFILES_DIR.glob("*.json")
    )


def list_running_profiles() -> list[str]:
    """Return slugs that have state files (potentially running)."""
    if not STATE_DIR.exists():
        return []
    slugs = []
    for p in STATE_DIR.glob("*.json"):
        # Skip legacy state file name
        if p.name == "state.json":
            continue
        slugs.append(p.stem)
    return sorted(slugs)


def delete_profile(slug: str) -> None:
    """Remove a profile's config file."""
    path = _config_path(slug)
    if path.exists():
        path.unlink()


def _unique_slug(desired: str) -> str:
    """Return a slug that doesn't collide with existing profiles."""
    slug = slugify(desired)
    if not _config_path(slug).exists():
        return slug
    for i in range(2, 100):
        candidate = f"{slug}_{i}"[:MAX_SLUG_LENGTH]
        if not _config_path(candidate).exists():
            return candidate
    return slug


# --- Config persistence ---


def save_config(config: MultiOutputConfig) -> None:
    """Save configuration to its profile file."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "slug": config.slug,
        "name": config.name,
        "speakers": [asdict(s) for s in config.speakers],
    }
    _config_path(config.slug).write_text(json.dumps(data, indent=2) + "\n")


def load_config(slug: str = "default") -> MultiOutputConfig | None:
    """Load configuration for a profile slug."""
    try:
        data = json.loads(_config_path(slug).read_text())
        speakers = [SpeakerConfig(**s) for s in data.get("speakers", [])]
        return MultiOutputConfig(
            slug=data.get("slug", slug),
            name=data.get("name", "Speakers/Soundbar"),
            speakers=speakers,
        )
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


# --- Runtime state ---


def save_state(state: MultiOutputState) -> None:
    """Save runtime state for a profile."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "slug": state.slug,
        "null_node_id": state.null_node_id,
        "monitor_source_id": state.monitor_source_id,
        "speakers": [asdict(s) for s in state.speakers],
    }
    _state_path(state.slug).write_text(json.dumps(data, indent=2) + "\n")


def load_state(slug: str = "default") -> MultiOutputState | None:
    """Load runtime state for a profile slug."""
    try:
        data = json.loads(_state_path(slug).read_text())
        speakers = [SpeakerState(**s) for s in data["speakers"]]
        return MultiOutputState(
            slug=data.get("slug", slug),
            null_node_id=data["null_node_id"],
            monitor_source_id=data["monitor_source_id"],
            speakers=speakers,
        )
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError):
        return None


def clear_state(slug: str = "default") -> None:
    """Remove state file for a profile."""
    try:
        _state_path(slug).unlink()
    except FileNotFoundError:
        pass
    # Also clean up legacy files if they match
    for path in (LEGACY_STATE_FILE_NEW, LEGACY_STATE_FILE_OLD):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --- Audio engine ---


def _find_monitor_source_id(slug: str) -> str | None:
    """Find the monitor source ID for a profile's null sink."""
    sink_name = null_sink_name(slug)
    result = subprocess.run(
        ["pactl", "list", "short", "sources"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if f"{sink_name}.monitor" in line:
            return line.split("\t")[0]
    return None


def _launch_loopback(
    monitor_source_id: str,
    sink_id: int,
    delay_ms: float,
    slug: str,
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
        f"loopback-{slug}-{index}",
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


def _check_speaker_conflicts(config: MultiOutputConfig) -> None:
    """Raise if any speaker in config is already used by another running profile."""
    wanted = {s.sink_name for s in config.speakers}
    for slug in list_running_profiles():
        if slug == config.slug:
            continue
        state = load_state(slug)
        if state is None:
            continue
        for speaker in state.speakers:
            if speaker.sink_name in wanted:
                raise RuntimeError(
                    f"Speaker '{speaker.label or speaker.sink_name}' is already "
                    f"in use by running profile '{slug}'."
                )


def start(config: MultiOutputConfig, wait: bool = False, timeout: int = 300) -> MultiOutputState:
    """Start multi-output routing for a profile.

    Creates a null sink, launches pw-loopback for each speaker, and sets
    the null sink as the default output.

    Args:
        config: Speaker configuration (carries its own slug).
        wait: If True, poll until all sinks appear (for systemd use).
        timeout: Max seconds to wait for sinks (only if wait=True).

    Returns:
        The runtime state.

    Raises:
        RuntimeError: If setup fails or speakers conflict with another profile.
    """
    slug = config.slug

    # Stop this profile if already running (not other profiles)
    stop(slug, quiet=True)

    if not config.speakers:
        raise RuntimeError("No speakers configured.")

    # Check for conflicts with other running profiles
    _check_speaker_conflicts(config)

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

    sink_node_name = null_sink_name(slug)

    # Create null sink via pw-cli
    safe_name = re.sub(r'[^a-zA-Z0-9 /\-_]', '', config.name) or "Multi Output"
    result = subprocess.run(
        [
            "pw-cli",
            "create-node",
            "adapter",
            "{ "
            f"factory.name=support.null-audio-sink node.name={sink_node_name} "
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
    monitor_source_id = _find_monitor_source_id(slug)
    if monitor_source_id is None:
        raise RuntimeError(f"Could not find {sink_node_name} monitor source.")

    # Set as default sink
    subprocess.run(["pactl", "set-default-sink", sink_node_name], check=True)

    # Launch loopbacks
    speaker_states: list[SpeakerState] = []
    for i, (speaker, sink_id) in enumerate(zip(config.speakers, sink_ids)):
        proc = _launch_loopback(monitor_source_id, sink_id, speaker.delay_ms, slug, i)
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
        slug=slug,
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


def stop(slug: str = "default", quiet: bool = False) -> None:
    """Stop multi-output for a specific profile and restore default sink."""
    state = load_state(slug)
    if state is None:
        if not quiet:
            print(f"No multi-output session running for profile '{slug}'.")
        return

    # Kill loopback processes (verified before sending signal)
    for speaker in state.speakers:
        _kill_loopback(speaker.pid)

    # Destroy null sink node
    subprocess.run(
        ["pw-cli", "destroy", null_sink_name(slug)],
        capture_output=True,
    )

    clear_state(slug)

    # Only restore default sink when no other profiles are still running
    if not list_running_profiles():
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

    if not quiet:
        print(f"Multi-output stopped for profile '{slug}'.")


def stop_all(quiet: bool = False) -> None:
    """Stop all running profiles."""
    running = list_running_profiles()
    if not running:
        if not quiet:
            print("No multi-output sessions running.")
        return
    for slug in running:
        stop(slug, quiet=quiet)


def update_speaker_delay(slug: str, index: int, delay_ms: float) -> None:
    """Update the delay for a single speaker (kills and relaunches its loopback).

    Args:
        slug: Profile slug.
        index: Speaker index (0-based).
        delay_ms: New delay in milliseconds.

    Raises:
        RuntimeError: If no session is running or index is out of range.
    """
    state = load_state(slug)
    if state is None:
        raise RuntimeError(f"No multi-output session running for profile '{slug}'.")

    if index < 0 or index >= len(state.speakers):
        raise RuntimeError(
            f"Speaker index {index} out of range (0-{len(state.speakers) - 1})."
        )

    speaker = state.speakers[index]

    # Kill existing loopback (verified before sending signal)
    _kill_loopback(speaker.pid)

    # Relaunch with new delay
    proc = _launch_loopback(
        state.monitor_source_id, speaker.sink_id, delay_ms, slug, index
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


def is_running(slug: str = "default") -> bool:
    """Check if a profile's multi-output session is active."""
    state = load_state(slug)
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
    clear_state(slug)
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
    """Write (or overwrite) the systemd user service template and reload the daemon."""
    project = _project_dir()
    service_path = SYSTEMD_SERVICE_DIR / SYSTEMD_TEMPLATE_NAME
    SYSTEMD_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    shell_path = shlex.quote(str(project / "multi-output-service.py"))
    python_path = str(project).replace("\\", "\\\\").replace("'", "\\'")
    service_path.write_text(
        f"""\
[Unit]
Description=Multi-output speaker sync (PipeWire) - profile %i
After=pipewire.service pipewire-pulse.service
Requires=pipewire.service pipewire-pulse.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {shell_path} %i
ExecStop=/usr/bin/python3 -c "import sys; sys.path.insert(0, '{python_path}'); from multi_output import core; core.stop('%i')"

[Install]
WantedBy=default.target
"""
    )

    # Remove legacy non-template service if present
    legacy = SYSTEMD_SERVICE_DIR / LEGACY_SERVICE_NAME
    if legacy.exists():
        legacy.unlink()

    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
    )


def _service_instance_name(slug: str) -> str:
    return f"multi-output@{slug}.service"


def is_service_installed() -> bool:
    """Check if the systemd user service template exists."""
    return (SYSTEMD_SERVICE_DIR / SYSTEMD_TEMPLATE_NAME).exists()


def is_service_enabled(slug: str = "default") -> bool:
    """Check if the systemd user service is enabled for a profile."""
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", _service_instance_name(slug)],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "enabled"


def set_service_enabled(slug: str, enabled: bool) -> None:
    """Enable or disable the systemd user service for a profile.

    Installs the service template first if it doesn't exist.
    """
    if enabled:
        install_service()
    subprocess.run(
        ["systemctl", "--user", "enable" if enabled else "disable",
         _service_instance_name(slug)],
        capture_output=True,
        check=True,
    )
