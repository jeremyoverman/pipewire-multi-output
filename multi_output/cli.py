"""CLI interface for pipewire-multi-output.

Usage:
    python3 -m multi_output [-p PROFILE] start
    python3 -m multi_output [-p PROFILE] stop [--all]
    python3 -m multi_output [-p PROFILE] status [--all]
    python3 -m multi_output [-p PROFILE] set-delay INDEX MS
    python3 -m multi_output [-p PROFILE] add
    python3 -m multi_output [-p PROFILE] remove INDEX
    python3 -m multi_output [-p PROFILE] save
    python3 -m multi_output [-p PROFILE] autostart on|off
    python3 -m multi_output test
    python3 -m multi_output list
    python3 -m multi_output create-profile NAME
    python3 -m multi_output delete-profile SLUG
"""

from __future__ import annotations

import argparse
import sys

from . import core


def cmd_start(args: argparse.Namespace) -> None:
    slug = args.profile
    config = core.load_config(slug)

    if args.speakers:
        # Build config from CLI args
        speakers = []
        for i, sink_name in enumerate(args.speakers):
            delay = args.delays[i] if i < len(args.delays) else 0
            speakers.append(core.SpeakerConfig(sink_name=sink_name, delay_ms=delay))
        config = core.MultiOutputConfig(
            slug=slug,
            name=args.name or "Speakers/Soundbar",
            speakers=speakers,
        )
    elif config and config.speakers:
        print(f"Loading saved config for profile '{slug}' ({len(config.speakers)} speakers)...")
    else:
        # Interactive selection
        print(f"No saved config for profile '{slug}'. Select speakers interactively.\n")
        speakers = []
        selected_names: list[str] = []
        while True:
            idx, name = core.select_sink_interactive(
                f"Select speaker #{len(speakers) + 1} (or Ctrl+C to finish):",
                exclude=selected_names,
            )
            delay_str = input(f"  Delay for this speaker in ms [0]: ").strip()
            delay = float(delay_str) if delay_str else 0
            label = core.get_sink_description(idx)
            speakers.append(
                core.SpeakerConfig(sink_name=name, delay_ms=delay, label=label)
            )
            selected_names.append(name)
            if len(speakers) >= 2:
                more = input("\nAdd another speaker? [y/N]: ").strip().lower()
                if more != "y":
                    break
        name_str = input("\nDevice name shown in GNOME [Speakers/Soundbar]: ").strip()
        config = core.MultiOutputConfig(
            slug=slug,
            name=name_str or "Speakers/Soundbar",
            speakers=speakers,
        )
        save = input("Save this config for next time? [Y/n]: ").strip().lower()
        if save != "n":
            core.save_config(config)
            print(f"Config saved to {core._config_path(slug)}")

    try:
        state = core.start(config, wait=args.wait)
    except (RuntimeError, TimeoutError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Multi-output started (profile '{slug}'):")
    for i, speaker in enumerate(state.speakers):
        delay_str = f"{speaker.delay_ms}ms delay" if speaker.delay_ms > 0 else "no delay"
        print(f"  [{i}] {speaker.label} ({delay_str})")
    print(f"\nUse 'set-delay <index> <ms>' to adjust.")
    print(f"Use 'stop' to tear down.")


def cmd_stop(args: argparse.Namespace) -> None:
    if args.all:
        core.stop_all()
    else:
        core.stop(args.profile)


def cmd_set_delay(args: argparse.Namespace) -> None:
    slug = args.profile
    try:
        core.update_speaker_delay(slug, args.index, args.ms)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    state = core.load_state(slug)
    if state and args.index < len(state.speakers):
        speaker = state.speakers[args.index]
        print(f"Delay updated to {args.ms}ms on {speaker.label}")


def cmd_status(args: argparse.Namespace) -> None:
    if args.all:
        running = core.list_running_profiles()
        if not running:
            print("No multi-output sessions running.")
            return
        for slug in running:
            _print_profile_status(slug)
            print()
    else:
        slug = args.profile
        state = core.load_state(slug)
        if state is None or not core.is_running(slug):
            print(f"No multi-output session running for profile '{slug}'.")
            return
        _print_profile_status(slug)


def _print_profile_status(slug: str) -> None:
    state = core.load_state(slug)
    if state is None:
        return
    running = core.is_running(slug)
    status = "active" if running else "stopped (stale state)"
    print(f"Profile '{slug}' — {status}:")
    for i, speaker in enumerate(state.speakers):
        delay_str = f"{speaker.delay_ms}ms delay" if speaker.delay_ms > 0 else "no delay"
        print(f"  [{i}] {speaker.label} (ID {speaker.sink_id}, {delay_str})")
        print(f"      Sink: {speaker.sink_name}  PID: {speaker.pid}")


def cmd_test(args: argparse.Namespace) -> None:
    core.play_ping(
        device=args.device,
        freq=args.freq,
        duration=args.duration,
        interval=args.interval,
        rate=args.rate,
    )


def cmd_save(args: argparse.Namespace) -> None:
    slug = args.profile
    config = core.load_config(slug)
    if config is None or not config.speakers:
        print("No config to save. Use 'start' or 'add' first.")
        sys.exit(1)
    core.save_config(config)
    print(f"Config saved to {core._config_path(slug)}")


def cmd_add(args: argparse.Namespace) -> None:
    slug = args.profile
    config = core.load_config(slug) or core.MultiOutputConfig(slug=slug)
    selected = [s.sink_name for s in config.speakers]

    if args.sink:
        # Non-interactive: add by sink name
        if args.sink in selected:
            print(f"Speaker already added: {args.sink}")
            sys.exit(1)
        label = ""
        # Try to resolve a description
        for sink in core.get_available_sinks():
            if sink["name"] == args.sink:
                label = sink["description"]
                break
        config.speakers.append(
            core.SpeakerConfig(
                sink_name=args.sink,
                delay_ms=args.delay,
                label=label,
            )
        )
    else:
        # Interactive selection
        idx, name = core.select_sink_interactive(
            "Select a speaker to add:", exclude=selected,
        )
        delay_str = input("  Delay in ms [0]: ").strip()
        delay = float(delay_str) if delay_str else 0
        label = core.get_sink_description(idx)
        config.speakers.append(
            core.SpeakerConfig(sink_name=name, delay_ms=delay, label=label)
        )

    core.save_config(config)
    print(f"Added speaker to profile '{slug}' ({len(config.speakers)} total). Config saved.")


def cmd_remove(args: argparse.Namespace) -> None:
    slug = args.profile
    config = core.load_config(slug)
    if config is None or not config.speakers:
        print("No speakers configured.")
        sys.exit(1)

    if args.index < 0 or args.index >= len(config.speakers):
        print(f"Index {args.index} out of range (0-{len(config.speakers) - 1}).")
        sys.exit(1)

    removed = config.speakers.pop(args.index)
    core.save_config(config)
    print(f"Removed: {removed.label or removed.sink_name} ({len(config.speakers)} remaining). Config saved.")


def cmd_autostart(args: argparse.Namespace) -> None:
    slug = args.profile
    if args.state == "status":
        enabled = core.is_service_enabled(slug)
        installed = core.is_service_installed()
        if not installed:
            print("Service not installed. Run install.sh first.")
        elif enabled:
            print(f"Auto-start is enabled for profile '{slug}'.")
        else:
            print(f"Auto-start is disabled for profile '{slug}'.")
    else:
        enable = args.state == "on"
        try:
            core.set_service_enabled(slug, enable)
            print(f"Auto-start {'enabled' if enable else 'disabled'} for profile '{slug}'.")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_list(_args: argparse.Namespace) -> None:
    profiles = core.list_profiles()
    running = set(core.list_running_profiles())

    if not profiles:
        print("No profiles configured.")
        print("Create one with: python3 -m multi_output create-profile <name>")
        print("Or just run: python3 -m multi_output start")
        return

    print("Profiles:")
    for slug in profiles:
        status = "running" if slug in running else "stopped"
        config = core.load_config(slug)
        name = config.name if config else slug
        n_speakers = len(config.speakers) if config else 0
        print(f"  {slug} — {name} ({n_speakers} speakers) [{status}]")


def cmd_create_profile(args: argparse.Namespace) -> None:
    slug = core._unique_slug(args.name)
    if core.load_config(slug) is not None:
        print(f"Profile '{slug}' already exists.")
        sys.exit(1)
    config = core.MultiOutputConfig(slug=slug, name=args.name)
    core.save_config(config)
    print(f"Created profile '{slug}'. Add speakers with: python3 -m multi_output -p {slug} add")


def cmd_delete_profile(args: argparse.Namespace) -> None:
    slug = args.slug
    if core.load_config(slug) is None:
        print(f"Profile '{slug}' not found.")
        sys.exit(1)
    if core.is_running(slug):
        core.stop(slug)
    core.delete_profile(slug)
    print(f"Deleted profile '{slug}'.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-output speaker sync tool using PipeWire pw-loopback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-p", "--profile",
        default="default",
        metavar="SLUG",
        help="Profile to operate on (default: 'default')",
    )
    sub = parser.add_subparsers(dest="command")

    # start
    start_p = sub.add_parser("start", help="Start multi-output routing")
    start_p.add_argument(
        "--speakers",
        nargs="+",
        metavar="SINK",
        help="Sink names or IDs for each speaker",
    )
    start_p.add_argument(
        "--delays",
        nargs="+",
        type=float,
        default=[],
        metavar="MS",
        help="Delay in ms for each speaker (same order as --speakers)",
    )
    start_p.add_argument(
        "--name",
        type=str,
        default=None,
        help='Device name shown in GNOME (default: "Speakers/Soundbar")',
    )
    start_p.add_argument(
        "--wait",
        action="store_true",
        help="Wait for all sinks to appear before starting (for systemd)",
    )

    # stop
    stop_p = sub.add_parser("stop", help="Stop multi-output and restore defaults")
    stop_p.add_argument(
        "--all", action="store_true", help="Stop all running profiles",
    )

    # set-delay
    delay_p = sub.add_parser("set-delay", help="Adjust delay on a speaker")
    delay_p.add_argument("index", type=int, help="Speaker index (0-based)")
    delay_p.add_argument("ms", type=float, help="New delay in milliseconds")

    # status
    status_p = sub.add_parser("status", help="Show current multi-output state")
    status_p.add_argument(
        "--all", action="store_true", help="Show all running profiles",
    )

    # save
    sub.add_parser("save", help="Save current config to disk")

    # add
    add_p = sub.add_parser("add", help="Add a speaker to the config")
    add_p.add_argument(
        "sink", nargs="?", default=None, help="Sink name (interactive if omitted)"
    )
    add_p.add_argument(
        "--delay", type=float, default=0, help="Delay in ms (default: 0)"
    )

    # remove
    remove_p = sub.add_parser("remove", help="Remove a speaker from the config")
    remove_p.add_argument("index", type=int, help="Speaker index (0-based, see 'status')")

    # autostart
    autostart_p = sub.add_parser("autostart", help="Manage auto-start on login")
    autostart_p.add_argument(
        "state",
        nargs="?",
        default="status",
        choices=["on", "off", "status"],
        help="Enable, disable, or check status (default: status)",
    )

    # test
    test_p = sub.add_parser("test", help="Play repeating ping for sync testing")
    test_p.add_argument(
        "device", nargs="?", default=None, help="Sink name or ID to play on"
    )
    test_p.add_argument(
        "--freq", type=int, default=1000, help="Frequency in Hz (default: 1000)"
    )
    test_p.add_argument(
        "--duration", type=int, default=100, help="Ping duration in ms (default: 100)"
    )
    test_p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between pings (default: 2.0)",
    )
    test_p.add_argument(
        "--rate", type=int, default=48000, help="Sample rate (default: 48000)"
    )

    # list
    sub.add_parser("list", help="List all profiles and their status")

    # create-profile
    create_p = sub.add_parser("create-profile", help="Create a new empty profile")
    create_p.add_argument("name", help="Human-readable profile name")

    # delete-profile
    delete_p = sub.add_parser("delete-profile", help="Delete a profile")
    delete_p.add_argument("slug", help="Profile slug to delete")

    args = parser.parse_args()

    # Run migration on any CLI invocation
    core.migrate_if_needed()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "set-delay": cmd_set_delay,
        "status": cmd_status,
        "save": cmd_save,
        "add": cmd_add,
        "remove": cmd_remove,
        "autostart": cmd_autostart,
        "test": cmd_test,
        "list": cmd_list,
        "create-profile": cmd_create_profile,
        "delete-profile": cmd_delete_profile,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
