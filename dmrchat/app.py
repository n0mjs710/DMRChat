"""
Application lifecycle: discover, route, chat, tear down.

The teardown path is the important one. Routes injected into the kernel at
startup are removed on every exit -- clean quit, SIGINT, SIGTERM, or an
unhandled exception -- because a crashed chat client that leaves 12.0.0.0/8
pointed at a disconnected radio breaks the host's networking.
"""

import argparse
import curses
import os
import signal
import sys

from . import discovery, protocol, routing
from .session import KIND_DM, KIND_TG, Session
from .transport import RadioTransport, TransportError
from .ui import ChatUI

BANNER = r"""
  ___  __  __ ___  ___ _         _
 |   \|  \/  | _ \/ __| |_  __ _| |_
 | |) | |\/| |   / (__| ' \/ _` |  _|
 |___/|_|  |_|_|_\\___|_||_\__,_|\__|
 MOTOTRBO DMR data chat client
"""


# Deliberately under $HOME rather than in the project folder. A station's radio
# id must match the radio physically attached to THAT Mac, so it is the one piece
# of state that must never travel with the source -- a project folder shared over
# iCloud would otherwise hand both Macs the same id.
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".dmrchat")
RADIO_ID_FILE = os.path.join(CONFIG_DIR, "radio-id")


class Terminated(Exception):
    """Raised by the SIGTERM handler so teardown runs through the normal path."""


def load_saved_radio_id():
    """This Mac's remembered radio id, or None if never saved or unreadable."""
    try:
        with open(RADIO_ID_FILE) as handle:
            return protocol.validate_id(handle.read().strip())
    except (OSError, protocol.ProtocolError):
        return None


def save_radio_id(radio_id):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RADIO_ID_FILE, "w") as handle:
            handle.write(f"{radio_id}\n")
        return True
    except OSError:
        return False        # not worth failing a launch over


def _install_signal_handlers():
    def on_terminate(signum, frame):
        raise Terminated(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_terminate)
    try:
        signal.signal(signal.SIGHUP, on_terminate)
    except (AttributeError, ValueError):
        pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="dmrchat",
        description="Real-time CLI chat over a MOTOTRBO radio network used as IP transport.",
    )
    parser.add_argument(
        "--radio-id", type=int, default=None,
        help="this station's radio id (1-16777215); prompted for if omitted",
    )
    parser.add_argument(
        "--tg", type=int, action="append", default=[], metavar="ID",
        help="open a chat room for this talkgroup at startup (repeatable)",
    )
    parser.add_argument(
        "--dm", type=int, action="append", default=[], metavar="ID",
        help="open a private conversation with this radio at startup (repeatable)",
    )
    parser.add_argument(
        "--sim", action="store_true",
        help="peer-to-peer test mode: no radio, no routes, no server. Instances on "
             "this host share a loopback multicast channel that behaves like an RF channel.",
    )
    parser.add_argument(
        "--skip-routes", action="store_true",
        help="discover the link but do not touch the kernel routing table",
    )
    parser.add_argument(
        "--dry-run-routes", action="store_true",
        help="print the route commands instead of running them",
    )
    parser.add_argument(
        "--forget-id", action="store_true",
        help=f"ignore this Mac's saved radio id and ask again ({RADIO_ID_FILE})",
    )
    return parser.parse_args(argv)


def prompt_radio_id():
    while True:
        try:
            raw = input("Enter this station's radio id (1-16777215): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return None
        try:
            return protocol.validate_id(raw, "radio id")
        except protocol.ProtocolError as error:
            print(f"  {error}")


def startup(args):
    """
    Discover the link and install routes.

    Returns ``(link, route_manager, notes)``, or ``None`` if the radio is absent
    and the app should exit gracefully.
    """
    notes = []

    if args.sim:
        print("[sim] Simulation mode: skipping adapter discovery and kernel routing.")
        print("[sim] Peers on this host share a loopback channel; every station hears every frame.")
        notes.append("Simulation mode -- no radio link, no kernel routes installed.")
        return None, None, notes

    print("Scanning host network adapters for the MOTOTRBO link "
          f"({discovery.RADIO_SUBNET})...")
    link = discovery.find_radio_link()

    if link is None:
        print()
        print(discovery.LINK_NOT_FOUND_MESSAGE)
        print()
        print("  (Run with --sim to test private and group chat locally without a radio.)")
        return None

    print(f"  Found {link.interface} at {link.address} via {link.method}")
    print(f"  Radio gateway calculated as {link.gateway}")
    print(f"  Inbound frames expected at {protocol.HOST_NAT_ADDRESS}:{protocol.RADIO_PORT}")
    notes.append(f"Link {link.interface} ({link.address}), gateway {link.gateway}")

    # The radio destination-NATs inbound traffic to one fixed host address. On a
    # host that came up with a different address, transmit still works and every
    # inbound frame is silently lost, so this is worth shouting about.
    if link.address != protocol.HOST_NAT_ADDRESS:
        warning = (
            f"WARNING: this host is {link.address}, but the radio destination-NATs "
            f"inbound frames to {protocol.HOST_NAT_ADDRESS}. Transmit will work and "
            f"NOTHING will be received until this interface holds "
            f"{protocol.HOST_NAT_ADDRESS}."
        )
        print(f"\n  {warning}\n")
        notes.append(warning)

    if args.skip_routes:
        print("  Skipping route installation (--skip-routes)")
        notes.append("Kernel routes not installed (--skip-routes).")
        return link, None, notes

    manager = routing.RouteManager(link.gateway, dry_run=args.dry_run_routes)
    print(f"\nInstalling MOTOTRBO routes via {link.gateway} (sudo may prompt):")
    ok, results = manager.install()
    for result in results:
        print(f"  {result}")
        notes.append(str(result))
    if not ok:
        print("\n  One or more routes failed. Traffic to 12/8 or 225/8 may not reach the radio.")
        notes.append("WARNING: not every route installed cleanly.")

    return link, manager, notes


def shutdown(manager, transport):
    if transport is not None:
        transport.close()
    if manager is None:
        return
    print("\nTearing down MOTOTRBO routes:")
    for result in manager.teardown():
        print(f"  {result}")


def _terminal_ready():
    """The curses UI needs a real terminal; say so plainly instead of traceback."""
    if not sys.stdout.isatty():
        return "stdout is not a terminal"
    term = os.environ.get("TERM")
    if not term:
        return "the TERM environment variable is not set"
    if term in ("dumb", "unknown"):
        return f"TERM={term} provides no cursor addressing"
    return None


def run(argv=None):
    args = parse_args(argv)
    print(BANNER)
    _install_signal_handlers()

    problem = _terminal_ready()
    if problem:
        print(f"DMRChat needs an interactive terminal ({problem}).")
        print("Run it directly from a shell rather than through a pipe or service.")
        return 4

    outcome = startup(args)
    if outcome is None:
        return 1
    link, manager, notes = outcome

    transport = None
    try:
        radio_id = args.radio_id
        if radio_id is None and not args.forget_id:
            radio_id = load_saved_radio_id()
            if radio_id is not None:
                print(f"Using this Mac's saved radio id {radio_id} "
                      f"(override with --radio-id, or /id in the app).")
        if radio_id is None:
            radio_id = prompt_radio_id()
            if radio_id is None:
                return 1
        try:
            radio_id = protocol.validate_id(radio_id, "radio id")
        except protocol.ProtocolError as error:
            print(f"{error}")
            return 2
        save_radio_id(radio_id)

        state = Session(radio_id)
        for note in notes:
            state.log(note)
        state.log(
            f"Station radio id {radio_id}; peers reach this station at "
            f"{protocol.target_address(protocol.TYPE_PRIVATE, radio_id)}:{protocol.RADIO_PORT}"
        )
        state.log(
            f"PDU budget: {protocol.PDU_LIMIT}B - {protocol.IP_UDP_OVERHEAD}B IP/UDP - "
            f"{protocol.APP_HEADER_LEN}B app header = {protocol.MAX_BODY_BYTES}B of body text"
        )

        transport = RadioTransport(radio_id, sim=args.sim)
        try:
            transport.open()
        except TransportError as error:
            print(f"\nTransport error: {error}")
            return 3

        for talkgroup in args.tg:
            state.open_view(KIND_TG, talkgroup)
        for radio in args.dm:
            state.open_view(KIND_DM, radio)

        try:
            curses.wrapper(_curses_main, state, transport, link, args.sim)
        except curses.error as error:
            # An unusable terminfo entry is a configuration problem, not a bug.
            # Report it plainly; the finally block still removes the routes.
            print(f"\nTerminal setup failed: {error}")
            print(f"  TERM is currently {os.environ.get('TERM')!r}.")
            print("  Try:  TERM=xterm-256color ./run.sh")
            return 4
        return 0

    except (KeyboardInterrupt, Terminated):
        print("\nInterrupted.")
        return 0
    finally:
        shutdown(manager, transport)


def _curses_main(screen, state, transport, link, sim):
    ui = ChatUI(screen, state, transport, link, sim=sim)
    ui.setup()
    try:
        ui.run()
    except KeyboardInterrupt:
        pass


def main():
    sys.exit(run())
