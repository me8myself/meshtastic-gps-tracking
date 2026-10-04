"""Command line entry point: ``python -m tracker``."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
import threading
import webbrowser

from .state import GROUND_SPEED_UNITS, GROUND_TRACK_SCALES, Tracker
from .tiles import TileStore

FROZEN = getattr(sys, "frozen", False)  # running as the packaged MeshTracker.exe
SETTINGS_FILE = "settings.txt"


def app_dir() -> str:
    """Folder that holds tiles/, logs/ and settings.txt: next to the .exe, or the current folder."""
    return os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.getcwd()


def read_settings_file() -> list:
    """Extra command line options from settings.txt (one or more per line, # for comments)."""
    path = os.path.join(app_dir(), SETTINGS_FILE)
    if not os.path.exists(path):
        return []
    args = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                # Non-POSIX mode on Windows keeps backslashes in paths but leaves quotes on.
                for tok in shlex.split(line, posix=os.name != "nt"):
                    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
                        tok = tok[1:-1]
                    args.append(tok)
    return args


def entry(argv=None) -> None:
    """Entry point for both ``python -m tracker`` and the packaged executable."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["tiles"]:
            from .tiles import main as tiles_main

            if FROZEN and "--tiles-dir" not in argv:
                argv += ["--tiles-dir", os.path.join(app_dir(), "tiles")]
            tiles_main(argv[1:])
        else:
            if not argv:
                argv = read_settings_file()
            main(argv)
    except KeyboardInterrupt:
        pass
    except SystemExit as ex:
        if FROZEN and ex.code not in (0, None):
            input("\nPress Enter to close this window...")
        raise
    except Exception:
        if not FROZEN:
            raise
        import traceback

        traceback.print_exc()
        input("\nSomething went wrong (details above). Press Enter to close this window...")
        sys.exit(1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="MeshTracker" if FROZEN else "python -m tracker",
        epilog=f"Download map tiles: {'MeshTracker' if FROZEN else 'python -m tracker'} tiles --help. "
               f"Options can also be put in {SETTINGS_FILE} next to the program.",
        description="Track Meshtastic nodes (e.g. an RC plane) on an offline satellite map.",
    )
    conn = ap.add_argument_group("radio connection (default: auto-detect USB serial)")
    conn.add_argument("--port", help="serial port, e.g. /dev/ttyUSB0, /dev/ttyACM0 or COM3")
    conn.add_argument("--host", help="connect over WiFi/TCP to this hostname or IP[:port]")
    conn.add_argument("--ble", nargs="?", const="", metavar="NAME_OR_ADDRESS",
                      help="connect over Bluetooth LE (optionally give the device name or MAC)")
    conn.add_argument("--demo", action="store_true", help="no radio: simulate an RC plane flying a figure-eight")
    conn.add_argument("--demo-lat", type=float, default=45.3887)
    conn.add_argument("--demo-lon", type=float, default=-75.7038)

    web = ap.add_argument_group("web interface")
    web.add_argument("--listen", default="127.0.0.1",
                     help="address to serve on (0.0.0.0 to allow phones/tablets on your network)")
    web.add_argument("--http-port", type=int, default=8080)
    web.add_argument("--no-browser", action="store_true", help="don't open a web browser on start")

    maps = ap.add_argument_group("offline map")
    maps.add_argument("--tiles-dir", default=os.path.join(app_dir(), "tiles") if FROZEN else "tiles", help="folder with {z}/{x}/{y}.jpg tiles and/or .mbtiles files")
    maps.add_argument("--fetch-missing", action="store_true",
                      help="download (and keep) any tile that is missing while internet is available")
    maps.add_argument("--center", type=float, nargs=2, metavar=("LAT", "LON"),
                      help="where the map starts if no node has a position yet")

    data = ap.add_argument_group("data")
    data.add_argument("--log-dir", default=os.path.join(app_dir(), "logs") if FROZEN else "logs", help="CSV log of every received fix ('' to disable)")
    data.add_argument("--ground-speed-unit", choices=sorted(GROUND_SPEED_UNITS), default="kmh",
                      help="unit of the GPS groundSpeed field sent by the tracker firmware (default kmh)")
    data.add_argument("--ground-track-scale", choices=sorted(GROUND_TRACK_SCALES), default="1e-5",
                      help="scale of the groundTrack field (default 1e-5 degrees)")
    data.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    tracker = Tracker(ground_speed_unit=args.ground_speed_unit, ground_track_scale=args.ground_track_scale,
                      log_dir=args.log_dir or None)
    tiles = TileStore(args.tiles_dir, fetch_missing=args.fetch_missing)

    if args.demo:
        from .simulator import Simulator

        source = Simulator(tracker, args.demo_lat, args.demo_lon)
        initial = {"lat": args.demo_lat, "lon": args.demo_lon, "zoom": 17}
    else:
        from .mesh import MeshConnection

        source = MeshConnection(tracker, port=args.port, host=args.host, ble=args.ble)
        initial = None
    if args.center:
        initial = {"lat": args.center[0], "lon": args.center[1], "zoom": 16}

    from .server import create_app
    import uvicorn

    app = create_app(tracker, tiles, initial)
    source.start()

    url = f"http://{'localhost' if args.listen in ('0.0.0.0', '127.0.0.1') else args.listen}:{args.http_port}/"
    print(f"\n  Meshtastic tracker running at {url}")
    print("  Close this window (or press Ctrl+C) to stop.\n")
    if tracker.log_path:
        print(f"  Logging fixes to {tracker.log_path}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.listen, port=args.http_port, log_level="warning")
    finally:
        source.stop()
        tracker.close()


if __name__ == "__main__":
    entry()
