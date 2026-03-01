"""CLI interface for pipewire-multi-output.

Usage:
    python3 -m multi_output.cli start
    python3 -m multi_output.cli stop
    python3 -m multi_output.cli set-delay 1 150
    python3 -m multi_output.cli status
    python3 -m multi_output.cli test
"""

from __future__ import annotations

import argparse
import sys

from . import core


def cmd_start(args: argparse.Namespace) -> None:
    config = core.load_config()

    if args.speakers:
        # Build config from CLI args
        speakers = []
        for i, sink_name in enumerate(args.speakers):
            delay = args.delays[i] if i < len(args.delays) else 0
            speakers.append(core.SpeakerConfig(sink_name=sink_name, delay_ms=delay))
        config = core.MultiOutputConfig(
            name=args.name or "Speakers/Soundbar",
            speakers=speakers,
        )
    elif config and config.speakers:
        print(f"Loading saved config ({len(config.speakers)} speakers)...")
    else:
        # Interactive selection
        print("No saved config found. Select speakers interactively.\n")
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
            name=name_str or "Speakers/Soundbar",
            speakers=speakers,
        )
        save = input("Save this config for next time? [Y/n]: ").strip().lower()
        if save != "n":
            core.save_config(config)
            print(f"Config saved to {core.CONFIG_FILE}")

    try:
        state = core.start(config, wait=args.wait)
    except (RuntimeError, TimeoutError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Multi-output started:")
    for i, speaker in enumerate(state.speakers):
        delay_str = f"{speaker.delay_ms}ms delay" if speaker.delay_ms > 0 else "no delay"
        print(f"  [{i}] {speaker.label} ({delay_str})")
    print(f"\nUse 'set-delay <index> <ms>' to adjust.")
    print(f"Use 'stop' to tear down.")


def cmd_stop(_args: argparse.Namespace) -> None:
    core.stop()


def cmd_set_delay(args: argparse.Namespace) -> None:
    try:
        core.update_speaker_delay(args.index, args.ms)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    state = core.load_state()
    if state and args.index < len(state.speakers):
        speaker = state.speakers[args.index]
        print(f"Delay updated to {args.ms}ms on {speaker.label}")


def cmd_status(_args: argparse.Namespace) -> None:
    state = core.load_state()
    if state is None or not core.is_running():
        print("No multi-output session running.")
        return

    print("Multi-output active:")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-output speaker sync tool using PipeWire pw-loopback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    sub.add_parser("stop", help="Stop multi-output and restore defaults")

    # set-delay
    delay_p = sub.add_parser("set-delay", help="Adjust delay on a speaker")
    delay_p.add_argument("index", type=int, help="Speaker index (0-based)")
    delay_p.add_argument("ms", type=float, help="New delay in milliseconds")

    # status
    sub.add_parser("status", help="Show current multi-output state")

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

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "set-delay":
        cmd_set_delay(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "test":
        cmd_test(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
