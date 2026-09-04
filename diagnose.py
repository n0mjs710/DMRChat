#!/usr/bin/env python3
"""
Link diagnostics for a station that transmits but does not receive.

Checks the host-side conditions inbound frames depend on, then listens on UDP
50000 and prints every datagram raw -- no filtering of any kind -- so you can
see whether frames are missing entirely or arriving and being rejected.

    ./.venv/bin/python diagnose.py           # check, then listen 60s
    ./.venv/bin/python diagnose.py --id 3120102   # also check id filtering

Quit DMRChat first: both would be bound to the same port.
"""

import argparse
import socket
import subprocess
import sys
import time

from dmrchat import discovery, protocol

OK, WARN, BAD = "  [ok]  ", "  [warn]", "  [BAD] "


def run(command):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=15)
        return (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as error:
        return f"<could not run {' '.join(command)}: {error}>"


def check_interface():
    print("\n1. MOTOTRBO interface")
    link = discovery.find_radio_link()
    if link is None:
        print(BAD, discovery.LINK_NOT_FOUND_MESSAGE)
        return None
    print(OK, f"{link.interface} at {link.address} (via {link.method})")

    if link.address == protocol.HOST_NAT_ADDRESS:
        print(OK, f"host address is {protocol.HOST_NAT_ADDRESS}, the inbound NAT target")
    else:
        print(BAD, f"host address is {link.address}, but the radio destination-NATs")
        print("       ", f"inbound frames to {protocol.HOST_NAT_ADDRESS}. Nothing will")
        print("       ", "ever be received. THIS IS ALMOST CERTAINLY THE PROBLEM.")
        print("       ", f"Fix: set this interface to {protocol.HOST_NAT_ADDRESS}, or")
        print("       ", "re-point the radio's DNAT at this host's address.")
    return link


def route_present(table, first_octet, gateway):
    """
    Is there a route for <first_octet>.0.0.0/8 via ``gateway``?

    netstat abbreviates trailing zero octets, so the same route can print as
    "12", "225.0.0/8", or "225.0.0.0/8" depending on the entry. Match on the
    octets rather than the exact string.
    """
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] != gateway:
            continue
        octets = fields[0].split("/")[0].split(".")
        if octets[0] == str(first_octet) and all(o == "0" for o in octets[1:]):
            return True
    return False


def check_routes(link):
    print("\n2. Kernel routes")
    table = run(["netstat", "-rn", "-f", "inet"])
    for network, label in ((12, "12.0.0.0/8 (private DM)"), (225, "225.0.0.0/8 (group)")):
        present = link and route_present(table, network, link.gateway)
        print(OK if present else WARN, f"{label}: {'present' if present else 'NOT FOUND'}")
    if link:
        reachable = run(["ping", "-c", "1", "-W", "1500", link.gateway])
        alive = "1 packets received" in reachable or "1 received" in reachable
        print(OK if alive else WARN, f"gateway {link.gateway} "
              f"{'responds to ping' if alive else 'did not answer ping (may be normal)'}")


def check_firewall():
    print("\n3. macOS application firewall")
    state = run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if "disabled" in state.lower():
        print(OK, "firewall is off; inbound UDP is not being blocked")
    elif "enabled" in state.lower():
        print(WARN, "firewall is ON. It can silently drop inbound UDP to python.")
        print("       ", "System Settings > Network > Firewall, or allow the venv python:")
        print("       ", "sudo /usr/libexec/ApplicationFirewall/socketfilterfw \\")
        print("       ", "     --add $(pwd)/.venv/bin/python3 --unblockapp $(pwd)/.venv/bin/python3")
    else:
        print(WARN, f"could not read firewall state: {state.strip()[:80]}")


def check_port():
    print("\n4. UDP port 50000")
    holders = run(["lsof", "-nP", "-iUDP:50000"])
    lines = [l for l in holders.splitlines() if l and not l.startswith("COMMAND")]
    if not lines:
        print(OK, "nothing else is bound to 50000")
    else:
        print(WARN, "port 50000 is already held -- quit DMRChat before capturing:")
        for line in lines[:5]:
            print("        ", line[:100])
    return not lines


def listen(seconds, own_id):
    print(f"\n5. Passive capture on UDP {protocol.RADIO_PORT} for {seconds}s")
    print("   Transmit from the OTHER radio now. Every datagram is shown unfiltered.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("0.0.0.0", protocol.RADIO_PORT))
    except OSError as error:
        print(BAD, f"cannot bind: {error}")
        return
    sock.settimeout(0.5)

    count = 0
    end = time.time() + seconds
    while time.time() < end:
        try:
            data, (source, port) = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError as error:
            print(BAD, f"recv error: {error}")
            break

        count += 1
        print(f"  [{count}] {len(data)} bytes from {source}:{port}")
        print(f"        header hex: {data[:4].hex(' ')}   body: {data[4:40]!r}")

        sender = protocol.sender_id_from_source_ip(source)
        if sender is None:
            print(f"        !! source {source} is not in the "
                  f"{protocol.SOURCE_RX_PREFIX}.x.x.x block -- DMRChat would reject this")
        else:
            print(f"        sender radio id: {sender}")
            if own_id is not None and sender == own_id:
                print(f"        !! sender id equals the id you gave (--id {own_id}).")
                print(f"        !! DMRChat treats this as its own transmission and drops it.")
                print(f"        !! Set a DIFFERENT radio id on this station.")
        try:
            msg_type, target, text = protocol.unpack(data)
            kind = protocol.TYPE_NAMES[msg_type]
            print(f"        decoded: {kind} -> target {target}: {text[:60]!r}")
            if msg_type == protocol.TYPE_PRIVATE and own_id is not None and target != own_id:
                print(f"        !! private frame is addressed to {target}, "
                      f"but you gave --id {own_id}. DMRChat would drop it.")
                print(f"        !! If this radio really is {target}, start with "
                      f"--radio-id {target}.")
        except protocol.ProtocolError as error:
            print(f"        !! header will not decode: {error}")
        print()

    sock.close()
    print(f"   Captured {count} datagram(s).")
    if count == 0:
        print("\n   NOTHING ARRIVED AT THE SOCKET. The frames are being lost below the")
        print("   application: wrong host address for the DNAT (check 1), the firewall")
        print("   (check 3), or the radio is not forwarding to this host.")
        print("   Since the radio shows RX, it is receiving over the air but not")
        print("   delivering to the host -- check 1 is the usual cause.")
    else:
        print("\n   Frames ARE reaching the socket. Any '!!' line above is the reason")
        print("   DMRChat filtered them out.")


def show_addresses(ids):
    """
    Print the addresses this app derives from an id.

    A radio id is a 24-bit number mapped onto the low three octets, the same
    MOTOTRBO CAI convention the radio itself uses. If these do not match the
    address you can successfully ping, the id in the app is not the id of the
    radio you are aiming at -- which is the whole bug.
    """
    print("\n6. Address mapping")
    for value in ids:
        try:
            radio_id = protocol.validate_id(value)
        except protocol.ProtocolError as error:
            print(BAD, error)
            continue
        dm = protocol.target_address(protocol.TYPE_PRIVATE, radio_id)
        tg = protocol.target_address(protocol.TYPE_GROUP, radio_id)
        src = protocol.source_address(radio_id)
        print(f"   id {radio_id}:")
        print(f"     as a DM target        -> {dm}:{protocol.RADIO_PORT}")
        print(f"     as a talkgroup target -> {tg}:{protocol.RADIO_PORT}")
        print(f"     inbound source would be  {src}")
        print(f"     >> compare with:  ping {dm}")


def probe(kind, target_id, count, own_id):
    """Transmit test frames so they can be watched leaving on tcpdump."""
    msg_type = protocol.TYPE_PRIVATE if kind == "dm" else protocol.TYPE_GROUP
    destination = protocol.target_address(msg_type, target_id)

    print(f"\n7. Transmit probe ({protocol.TYPE_NAMES[msg_type]})")
    print(f"   Sending {count} frame(s) to {destination}:{protocol.RADIO_PORT}")
    print(f"   Watch on this Mac with:")
    print(f"     sudo tcpdump -ni <radio-iface> -vv 'udp port {protocol.RADIO_PORT} or icmp'")
    print()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
    try:
        sock.bind(("0.0.0.0", protocol.RADIO_PORT))
    except OSError as error:
        print(BAD, f"cannot bind source port {protocol.RADIO_PORT}: {error}")
        print("       ", "quit DMRChat first")
        return

    for index in range(1, count + 1):
        text = f"probe {index} from {own_id or 'unknown'}"
        frame = protocol.pack(msg_type, target_id, text)
        try:
            sock.sendto(frame, (destination, protocol.RADIO_PORT))
        except OSError as error:
            print(BAD, f"send failed: {error}")
            break
        source = sock.getsockname()
        print(f"   [{index}] sent {len(frame)}B payload "
              f"({len(frame) + protocol.IP_UDP_OVERHEAD}B on air) "
              f"{source[0]}:{source[1]} -> {destination}:{protocol.RADIO_PORT}")
        print(f"        header {frame[:4].hex(' ')}  body {text!r}")
        time.sleep(1.0)
    sock.close()

    print("\n   If tcpdump shows these leaving but the far Mac sees nothing, the")
    print("   frames are being dropped by the radio or over the air.")
    print("   If tcpdump shows NOTHING here, they never left this host -- check the")
    print("   source address above is on the radio interface, not another NIC.")


def main():
    parser = argparse.ArgumentParser(description="Diagnose a station that transmits but does not receive.")
    parser.add_argument("--id", type=int, default=None,
                        help="the radio id this station is configured with, to test id filtering")
    parser.add_argument("--seconds", type=int, default=60, help="capture duration (default 60)")
    parser.add_argument("--addr", type=int, nargs="+", metavar="ID",
                        help="show the addresses derived from these ids and exit")
    parser.add_argument("--probe-dm", type=int, metavar="ID",
                        help="transmit test frames to a radio id, to watch on tcpdump")
    parser.add_argument("--probe-tg", type=int, metavar="ID",
                        help="transmit test frames to a talkgroup id")
    parser.add_argument("--count", type=int, default=5, help="probe frames to send (default 5)")
    parser.add_argument("--dm-prefix", type=int, default=None, metavar="N",
                        help=f"first octet for private targets "
                             f"(default {protocol.CAI_PC_NETWORK}; try 12 to address radios)")
    parser.add_argument("--tg-prefix", type=int, default=None, metavar="N",
                        help=f"first octet for group targets (default {protocol.CAI_GROUP_NETWORK})")
    parser.add_argument("--src-prefix", type=int, default=None, metavar="N",
                        help=f"expected inbound source octet (default {protocol.CAI_PC_NETWORK})")
    args = parser.parse_args()
    protocol.configure(args.dm_prefix, args.tg_prefix, args.src_prefix)

    print("DMRChat link diagnostics")
    print("=" * 60)

    # Address mapping and transmit probes are about the sending side, so they
    # skip the inbound checks and the capture entirely.
    if args.addr:
        show_addresses(args.addr)
        return 0
    if args.probe_dm is not None or args.probe_tg is not None:
        check_interface()
        if args.probe_dm is not None:
            show_addresses([args.probe_dm])
            probe("dm", args.probe_dm, args.count, args.id)
        else:
            show_addresses([args.probe_tg])
            probe("tg", args.probe_tg, args.count, args.id)
        print("\nDone.")
        return 0

    link = check_interface()
    check_routes(link)
    check_firewall()
    free = check_port()
    if free:
        try:
            listen(args.seconds, args.id)
        except KeyboardInterrupt:
            print("\n   Capture stopped.")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
