"""Entry point:  python -m tracker.main record --source sim"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

from .sampler import Sampler
from .server import Server
from .source import make_source
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def start_game(cfg: dict) -> bool:
    """Start Elden Ring and wait for its process. True once it is up."""
    from . import launch as game_launch

    game = cfg.get("game", {})
    if game_launch.is_running():
        print("game:    already running")
        return True

    configured = game.get("exe") or None
    exe = game_launch.find_game(configured)
    if exe is None:
        if configured:
            print(f"game:    {configured} is not there. Fix [game] exe in "
                  f"config.toml, or clear it and let this look for the game.")
        else:
            print("game:    could not find eldenring.exe in any Steam library.")
            print("         Set [game] exe in config.toml to its full path, "
                  "e.g.")
            print(r'         exe = "D:/SteamLibrary/steamapps/common/'
                  r'ELDEN RING/Game/eldenring.exe"')
        return False

    print(f"game:    starting {exe}")
    try:
        game_launch.start(exe)
    except Exception as e:
        print(f"game:    could not start it: {e}")
        return False

    timeout = float(game.get("launch_timeout_s", 180))
    up = game_launch.wait_for_process(
        timeout, on_tick=lambda s: print(f"         still waiting ({s}s)")
    )
    if not up:
        print(f"game:    it did not come up within {timeout:.0f}s.")
        print("         If you launch through Steam or with anti-cheat on, "
              "point [game] exe at the launcher you actually use.")
        return False
    return True


def cmd_record(args) -> int:
    # Before the game is launched, the database opened or a session started:
    # all three leave something behind, and a port that is already taken is
    # the one failure that happens after the point of no return.
    if not args.no_server and not claim_port(args.host, args.port):
        return 1

    cfg = load_config(args.config)
    store = Store(args.db)

    if args.launch and args.source == "game":
        if not start_game(cfg):
            return 1

    # The process existing is not the same as being readable: the module has
    # to be loaded before the signature scan can find anything, which takes a
    # few seconds more. Only worth waiting for when we started the game
    # ourselves -- otherwise a missing game should fail immediately.
    deadline = time.time() + 120 if args.launch else 0.0
    while True:
        try:
            source = make_source(args.source, cfg)
            break
        except Exception as e:
            if time.time() < deadline:
                time.sleep(2.0)
                continue
            print(f"could not start '{args.source}' source: {e}")
            if args.source == "game":
                print(
                    "Is the game running, and are your signatures filled in "
                    "under [memory.pointers] in config.toml? Use --source sim "
                    "to test the rest of the pipeline meanwhile."
                )
            return 1

    server = Server(store, cfg, args.host, args.port) if not args.no_server else None
    sampler = Sampler(
        source, store, cfg,
        on_sample=server.push if server else None,
        on_event=server.push_event if server else None,
    )
    if server:
        # So the viewer's delete button can refuse to remove the session that
        # is being written to as you look at it.
        server.active_session = sampler.session_id

    print(f"source:  {source.describe()}")
    print(f"database: {Path(args.db).resolve()}  (session {sampler.session_id})")

    stop = threading.Event()
    # Set by the worker when the game has gone. Its own flag rather than
    # `stop`, which the shutdown path sets on the way out: the difference is
    # whether anybody needs telling.
    quit_with_game = threading.Event()
    stop_with_game = bool(cfg.get("recording", {}).get("stop_with_game", True))

    def loop() -> None:
        interval = sampler.interval
        last_error = None
        while not stop.is_set():
            if stop_with_game and source.gone():
                quit_with_game.set()
                return
            try:
                sampler.step()
                last_error = None
            except Exception as e:  # never let one bad read kill the session
                # Said once, not four times a second: while the game is still
                # loading this is the same complaint over and over, and a wall
                # of it buries the line that says what to do about it.
                if str(e) != last_error:
                    last_error = str(e)
                    print(f"  sample error: {e}")
            time.sleep(interval)

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()

    async def serve() -> None:
        if server:
            await server.start()
            url = f"http://{args.host}:{args.port}/"
            print(f"viewer:  {url}")
            if args.open:
                webbrowser.open(url)
        else:
            print("viewer:  disabled")
        print("recording. ctrl-c to stop"
              + (", or quit the game.\n" if stop_with_game else ".\n"))
        try:
            # Ticked in tenths so quitting the game is noticed at once, and
            # the counts are still only printed every five seconds -- a line
            # rewriting itself ten times a second is unreadable.
            since = 0.0
            while not quit_with_game.is_set():
                await asyncio.sleep(0.1)
                since += 0.1
                if since < 5:
                    continue
                since = 0.0
                c = sampler.counts
                print(
                    f"  {c['samples']} points  {c['breaks']} breaks  "
                    f"{c['events']} map changes  ({c['skipped']} idle)",
                    end="\r",
                )
            print("\nthe game has closed.")
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except OSError as e:
        # Between the check above and the bind, something else took it. Rare,
        # but the traceback that used to come out of here said nothing about
        # what to do.
        print(f"\ncould not serve on port {args.port}: {e}")
        print(f"Something took it in the last second. Try --port "
              f"{args.port + 1}.")
    finally:
        stop.set()
        worker.join(timeout=2)
        sampler.finish()
        c = sampler.counts
        print(f"\nsaved {c['samples']} points across {c['events']} map changes.")
        if c["deaths"]:
            print(f"marked {c['deaths']} death(s).")
        store.close()
    return 0


def port_holder(host: str, port: int) -> tuple[str, dict]:
    """Who has that port: nobody, a viewer of ours, a recorder, or a stranger.

    Worth knowing before anything else happens, because the three answers
    have three different fixes and only one of them is an error. Asked with
    a request rather than by looking at the process list: what matters is
    what is answering, not what is running.
    """
    import json
    import socket
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/meta", timeout=3
        ) as r:
            meta = json.loads(r.read())
        if "sessions" not in meta:
            return "other", {}
        if meta.get("recording") is not None:
            return "recorder", meta
        # A server too old to say either way. Treat it as a recorder: the
        # cost of being wrong is a message instead of a stolen port.
        if "recording" not in meta:
            return "unknown", meta
        return "viewer", meta
    except (urllib.error.URLError, ValueError, OSError):
        pass

    # Nothing of ours answered. Either the port is free or something else has
    # it, and binding is the only way to tell them apart.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return "free", {}
    except OSError:
        return "other", {}
    finally:
        sock.close()


def already_serving(host: str, port: int) -> bool:
    """Is one of ours already answering on that port?

    A recorder serves the same viewer, so finding one is not a clash: it is
    the thing you were about to start, already running.
    """
    return port_holder(host, port)[0] in ("viewer", "recorder", "unknown")


def claim_port(host: str, port: int) -> bool:
    """Make the port ours to bind, or explain why it cannot be.

    Called before the database is opened, because everything after this point
    leaves a mark: the old order started a session, wrote a sample and then
    died on the bind, which is where the run of one-point sessions in
    routes.db came from.
    """
    import json
    import socket
    import urllib.error
    import urllib.request

    kind, meta = port_holder(host, port)
    if kind == "free":
        return True

    if kind in ("recorder", "unknown"):
        sid = meta.get("recording")
        which = f"session {sid}" if sid is not None else "a session"
        print(f"port {port}: a recorder is already running here, writing "
              f"{which}.")
        print("         That window is the one recording -- its map is at "
              f"http://{host}:{port}/ .")
        print("         Two recorders on one database would each miss half "
              "the route, so this one")
        print("         is stopping rather than starting a second. Close the "
              "other window first,")
        print(f"         or run this one with --port {port + 1}.")
        return False

    if kind == "other":
        print(f"port {port}: something that is not ours already has it.")
        print(f"         Free it, or record on another port:")
        print(f"             python -m tracker.main record --source game "
              f"--port {port + 1} --open")
        return False

    # A read-only viewer. It serves the same page this recorder is about to,
    # minus the live feed, so it is the one that gives way.
    print(f"port {port}: a viewer was already open here. Taking it over so "
          f"the map goes live.")
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/standdown", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except urllib.error.HTTPError as e:
        print(f"         it would not: {e.read().decode('utf-8', 'replace')}")
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"         could not ask it to: {e}")
        return False

    # It has to finish answering and let go before the bind can succeed.
    for _ in range(40):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            time.sleep(0.25)
        finally:
            sock.close()
    print(f"         it did not let go of the port. Close that window, or "
          f"use --port {port + 1}.")
    return False


def cmd_serve(args) -> int:
    url = f"http://{args.host}:{args.port}/"

    # Already up -- from `record`, or from a viewer left open in another
    # window. Opening a second one on top of it would fail on the port for no
    # reason; the map you asked for is right there.
    if already_serving(args.host, args.port):
        print(f"viewer: {url}  (already running -- opening that one)")
        if args.open:
            webbrowser.open(url)
        return 0

    cfg = load_config(args.config)
    store = Store(args.db)
    server = Server(store, cfg, args.host, args.port)

    async def run() -> None:
        await server.start()
        print(f"viewer: {url}  (read-only, no recording)")
        print("The game does not need to be running for this.")
        if args.open:
            webbrowser.open(url)
        # Until a recorder asks for the port. It serves this same page plus
        # the live feed, so there is nothing here worth making it wait for.
        await server.stopping.wait()
        await asyncio.sleep(0.3)   # let the answer to that request go out
        print("\nhanded the port to a recorder.")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped.")
    except OSError as e:
        # Something else has the port, and it is not one of ours.
        print(f"could not serve on port {args.port}: {e}")
        print(f"Something else is using it. Try a different one, e.g.")
        print(f"    python -m tracker.main serve --port {args.port + 1} --open")
        return 1
    finally:
        store.close()
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    b = store.bounds()
    sessions = store.sessions()
    print(f"database: {Path(args.db).resolve()}")
    print(f"sessions: {len(sessions)}")
    if not b:
        print("no route data yet.")
        return 0
    print(f"points:   {b['n']}")
    print(f"extent:   x {b['x0']:.0f}..{b['x1']:.0f}   z {b['z0']:.0f}..{b['z1']:.0f}")
    span = (b["t1"] - b["t0"]) / 3_600_000
    print(f"span:     {span:.1f} hours of wall clock")
    visits = store.interior_visits()
    print(f"interiors visited: {len(visits)}")
    store.close()
    return 0


DEFAULTS = {
    "config": ROOT / "config" / "config.toml",
    "db": str(ROOT / "data" / "routes.db"),
    "host": "127.0.0.1",
    "port": 8731,
}


def add_common(parser: argparse.ArgumentParser) -> None:
    """Shared flags, defaulting to None so they work before or after the
    subcommand instead of the later default silently winning."""
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)


def cmd_probe(args) -> int:
    """Read a few positions and sanity-check them, without recording."""
    cfg = load_config(args.config)
    try:
        source = make_source("game", cfg)
    except Exception as e:
        print(f"could not attach: {e}")
        return 1

    print(f"attached: {source.describe()}\n")
    try:
        print(f"WorldChrMan static address: 0x{source.mem.scan(source.pos):X}")
    except Exception as e:
        print(f"pattern did not resolve: {e}")
        return 1

    from .coords import MapConfig, MapId

    maps = MapConfig(cfg)
    print("\nreading 10 samples. Load a save and MOVE AROUND while this runs.\n")
    good = 0
    seen: set[tuple] = set()
    track: list[tuple] = []
    hp_seen: list[int] = []
    for i in range(10):
        r = source.read()
        if r is None:
            print("  no reading (loading screen, main menu, or no character loaded)")
        else:
            m = MapId.unpack(r.map_id)
            layer = maps.classify(m, r.y)
            world = maps.to_world(m, r.x, r.z)
            seen.add((round(r.x, 2), round(r.y, 2), round(r.z, 2)))
            track.append((r.x, r.y, r.z, r.map_id))
            flag = ""
            # Chunk-local coordinates sit within a 256m tile, but the game
            # tolerates a small overhang past the boundary before the chunk ID
            # switches, so modest negatives and slight overshoot are normal.
            # Global coordinates would read in the thousands.
            absolute = maps.is_absolute(r.x, r.z)
            in_tile = -80 <= r.x < 340 and -80 <= r.z < 340
            if not absolute and not in_tile:
                flag = "   <-- not tile-local and not clearly absolute"
            elif m.area == 0 or m.area == 255:
                flag = "   <-- map id looks wrong"
            else:
                good += 1
            wtxt = f"world {world[0]:9.1f},{world[1]:9.1f}" if world else "interior"
            hptxt = ""
            if r.hp is not None:
                hp_seen.append(r.hp)
                hptxt = f"  hp={r.hp:5d}"
            print(f"  {m}  {layer:11s} x={r.x:7.2f} y={r.y:8.2f} z={r.z:7.2f}  "
                  f"{wtxt}{hptxt}{flag}")
        time.sleep(0.5)

    print()
    if good < 5:
        print(f"Only {good} of 10 readings looked sane. Most likely the offset chain")
        print("is for a different game version, or this is the global position")
        print("rather than the chunk one.")
        return 1

    # A rebasing position source snaps back toward its origin every few
    # seconds. Drawn as a route it resets repeatedly, so catch it here.
    jumps = 0
    for a, b in zip(track, track[1:]):
        step = math.dist((a[0], a[2]), (b[0], b[2]))
        if a[3] == b[3] and step > 25:
            jumps += 1
    if jumps:
        print(f"WARNING: {jumps} large jump(s) within the same map tile.")
        print("That is the signature of a rebasing position source: the game")
        print("periodically resets the local origin, so a route drawn from it")
        print("snaps back every few seconds. Check that player_position uses")
        print("the global position chain [0x1E508, 0x6C0], not the chunk one.")
        return 1

    if len(seen) <= 1:
        print("Values are in a plausible range, but they never changed.")
        print("That could mean you stood still, or it could mean the chain resolves")
        print("to something that isn't the live player position. Run this again")
        print("while walking: if the numbers don't move, the offsets are wrong.")
        return 1

    # HP only feeds death marks, so a bad chain is worth saying plainly
    # without failing the probe over it.
    if getattr(source, "hp", None) is None:
        print("HP is not configured, so deaths will not be marked. Add "
              "[memory.pointers.player_hp] to config.toml to turn that on.")
    elif not hp_seen:
        print("HP is configured but never read. Deaths will not be marked.")
    elif min(hp_seen) <= 0 or max(hp_seen) > 20000:
        print(f"HP read as {hp_seen} -- that does not look like health. Check "
              f"the player_hp chain, or comment it out; nothing else uses it.")
    else:
        print(f"HP reads {hp_seen[-1]}. If that is your health, death marks "
              f"will work; if not, fix player_hp.")

    space = "absolute" if maps.is_absolute(track[-1][0], track[-1][2]) else "tile-local"
    print(f"Looks right. {len(seen)} distinct positions, coordinates read as {space}.")
    if good < 10:
        print(f"({10 - good} reading(s) were flagged; near a chunk boundary that is normal.)")
    print("Next: record --source game, then align the map in the viewer.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tracker",
        description="Elden Ring route tracker",
        epilog="Flags go after the subcommand, e.g. tracker record --source sim",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record positions and serve the live viewer")
    add_common(r)
    r.add_argument("--source", choices=["game", "sim"], default="game")
    r.add_argument("--launch", action="store_true",
                   help="start Elden Ring first if it is not already running")
    r.add_argument("--no-server", action="store_true")
    r.add_argument("--open", action="store_true", help="open the viewer in a browser")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("serve", help="view existing routes without recording")
    add_common(s)
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_serve)

    pb = sub.add_parser("probe", help="check signatures against the running game")
    add_common(pb)
    pb.set_defaults(func=cmd_probe)

    st = sub.add_parser("stats", help="summarise the database")
    add_common(st)
    st.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    for k, fallback in DEFAULTS.items():
        if getattr(args, k, None) is None:
            setattr(args, k, fallback)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
