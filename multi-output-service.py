#!/usr/bin/env python3
"""Systemd service entry point for pipewire-multi-output.

Loads saved config for the given profile slug and starts multi-output
routing, waiting for all configured sinks to appear (useful when
Bluetooth speakers take time to connect after login).

Usage:
    python3 multi-output-service.py [slug]

If no slug is given, defaults to "default".
"""

import sys
from pathlib import Path

# Add project directory to path so we can import multi_output
sys.path.insert(0, str(Path(__file__).parent))

from multi_output import core


def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else "default"

    core.migrate_if_needed()

    config = core.load_config(slug)
    if config is None or not config.speakers:
        print(f"No config found for profile '{slug}'.")
        print(f"Run the GUI or CLI to configure speakers first.")
        print(f"Expected config at: {core._config_path(slug)}")
        sys.exit(1)

    print(f"Starting multi-output profile '{slug}' with {len(config.speakers)} speakers...")
    for i, speaker in enumerate(config.speakers):
        delay_str = f"{speaker.delay_ms}ms" if speaker.delay_ms > 0 else "no delay"
        print(f"  [{i}] {speaker.label or speaker.sink_name} ({delay_str})")

    try:
        state = core.start(config, wait=True, timeout=300)
    except TimeoutError as e:
        print(f"Timeout: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Multi-output started successfully for profile '{slug}'.")
    for speaker in state.speakers:
        delay_str = f"{speaker.delay_ms}ms delay" if speaker.delay_ms > 0 else "no delay"
        print(f"  {speaker.label} (ID {speaker.sink_id}, {delay_str})")


if __name__ == "__main__":
    main()
