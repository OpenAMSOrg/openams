"""`python3 -m mock_moonraker --fixture PATH [...]`, run with `tools/` on
`sys.path` (the README's `cd tools && python3 -m mock_moonraker ...`)."""

import argparse
import asyncio
import sys

from .fixtures import FixtureError, load
from .server import GCODE_MODES, MockMoonraker


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m mock_moonraker",
        description="Serve a recorded OpenAMS fixture over Moonraker's "
                    "WebSocket JSON-RPC API.",
    )
    parser.add_argument("--fixture", required=True,
                        help="the fixture JSON file to replay")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7125,
                        help="bind port (default 7125; 0 picks a free port)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="replay speed multiplier; 0 applies every event "
                             "on connect (default 1.0)")
    parser.add_argument("--loop", action="store_true",
                        help="restart the timeline when it ends")
    parser.add_argument("--gcode-mode", default="ok", choices=GCODE_MODES,
                        help="what printer.gcode.script answers: ok, "
                             "refuse (the toolchange scripts fail before any "
                             "motion, as on a configuration the macros refuse) "
                             "or simulate (a load or unload "
                             "plays on the unit's openams_ui lane) "
                             "(default ok)")
    parser.add_argument("--record", default=None, metavar="PATH",
                        help="append every request to this JSON-lines file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        fixture = load(args.fixture)
    except (OSError, FixtureError) as exc:
        sys.stderr.write("mock_moonraker: %s\n" % exc)
        return 2

    server = MockMoonraker(fixture, host=args.host, port=args.port,
                           speed=args.speed, loop=args.loop,
                           record_path=args.record,
                           gcode_mode=args.gcode_mode)

    async def run():
        await server.start()
        sys.stdout.write("mock_moonraker: fixture %r on %s (speed %s, gcode %s%s)\n"
                         % (fixture.scenario, server.url, args.speed,
                            args.gcode_mode, ", loop" if args.loop else ""))
        sys.stdout.flush()
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
