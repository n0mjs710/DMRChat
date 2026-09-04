#!/usr/bin/env python3
"""
Self-test: protocol correctness, the 500-byte PDU boundary, and a live
peer-to-peer exchange between two transports with no server involved.

    ./.venv/bin/python selftest.py
"""

import sys
import time

from dmrchat import protocol, session
from dmrchat.transport import RadioTransport

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def test_budget():
    section("PDU budget arithmetic")
    check("28B IP/UDP + 4B app header = 32B overhead",
          protocol.NET_OVERHEAD == 32, f"got {protocol.NET_OVERHEAD}")
    check("body limit is exactly 468 bytes",
          protocol.MAX_BODY_BYTES == 468, f"got {protocol.MAX_BODY_BYTES}")
    check("468B body + 4B header + 28B framing = 500B PDU",
          protocol.MAX_BODY_BYTES + protocol.APP_HEADER_LEN + protocol.IP_UDP_OVERHEAD
          == protocol.PDU_LIMIT)


def test_header():
    section("4-byte application header")
    frame = protocol.pack(protocol.TYPE_GROUP, 0xABCDEF, "hello room")
    check("header is 4 bytes ahead of the body", len(frame) == 4 + len("hello room"))
    check("byte 0 carries the message type", frame[0] == 0x01, f"got 0x{frame[0]:02X}")
    check("bytes 1-3 carry the 24-bit target big-endian",
          frame[1:4] == b"\xAB\xCD\xEF", f"got {frame[1:4]!r}")

    msg_type, target, text = protocol.unpack(frame)
    check("round-trips type", msg_type == protocol.TYPE_GROUP)
    check("round-trips target id", target == 0xABCDEF, f"got {target}")
    check("round-trips body text", text == "hello room")

    dm = protocol.pack(protocol.TYPE_PRIVATE, 1234567, "private words")
    check("private frames use type 0x02", dm[0] == 0x02)
    check("group and private headers are distinguishable on the wire",
          protocol.unpack(dm)[0] != protocol.unpack(frame)[0])


def test_addressing():
    section("Address block mapping")
    check("DM target -> 12.x.x.x",
          protocol.target_address(protocol.TYPE_PRIVATE, 0x0A0B0C) == "12.10.11.12")
    check("group target -> 225.x.x.x",
          protocol.target_address(protocol.TYPE_GROUP, 0x0A0B0C) == "225.10.11.12")
    check("sender id recovered from 13.x.x.x source",
          protocol.sender_id_from_source_ip("13.10.11.12") == 0x0A0B0C)
    check("non-radio source rejected",
          protocol.sender_id_from_source_ip("192.168.10.2") is None)
    check("id survives the id->octets->id round trip",
          protocol.octets_to_id(*protocol.id_to_octets(16777215)) == 16777215)


def test_limits():
    section("Fragmentation guard")
    body = "A" * protocol.MAX_BODY_BYTES
    frame = protocol.pack(protocol.TYPE_GROUP, 100, body)
    on_air = len(frame) + protocol.IP_UDP_OVERHEAD
    check("468-byte body is accepted", on_air == 500, f"on air {on_air}")

    try:
        protocol.pack(protocol.TYPE_GROUP, 100, "A" * 469)
        check("469-byte body is refused", False, "no error raised")
    except protocol.ProtocolError:
        check("469-byte body is refused", True)

    # Multi-byte characters must be counted in bytes, not characters.
    emoji_body = "é" * 234                     # 2 bytes each = 468 bytes
    check("234 two-byte chars = 468 bytes and fits", protocol.fits(emoji_body))
    check("one more two-byte char overflows", not protocol.fits(emoji_body + "é"))

    try:
        protocol.pack(protocol.TYPE_GROUP, 0x1000000, "too big an id")
        check("id beyond 24 bits is refused", False, "no error raised")
    except protocol.ProtocolError:
        check("id beyond 24 bits is refused", True)

    try:
        protocol.unpack(b"\x01\x00")
        check("truncated frame is refused", False, "no error raised")
    except protocol.ProtocolError:
        check("truncated frame is refused", True)


def test_demux():
    section("Inbound demultiplexing")
    state = session.Session(own_id=1001)
    room = state.open_view(session.KIND_TG, 100)
    check("open view addresses the right block", room.address == "225.0.0.100:50000")

    class FakeInbound:
        def __init__(self, key, text, sender):
            self.view_key = key
            self.text = text
            self.sender_id = sender

    state.deliver(FakeInbound(("TG", 100), "room traffic", 2002))
    state.deliver(FakeInbound(("DM", 3003), "direct traffic", 3003))

    check("group frame filed under its talkgroup",
          state.views[("TG", 100)].messages[-1].text == "room traffic")
    check("private frame opened a DM view keyed by sender",
          ("DM", 3003) in state.views)
    check("unwatched conversation is not dropped",
          state.views[("DM", 3003)].messages[-1].text == "direct traffic")
    check("inactive view accrues unread count",
          state.views[("DM", 3003)].unread == 1)


def test_peer_to_peer():
    section("Peer-to-peer exchange (two stations, no server)")
    alice = RadioTransport(own_id=1111, sim=True)
    bob = RadioTransport(own_id=2222, sim=True)

    try:
        alice.open()
        bob.open()
    except Exception as error:                       # noqa: BLE001 - reported, not swallowed
        check("both stations open UDP 50000", False, f"{error}")
        return

    check("both stations open UDP 50000", True)
    time.sleep(0.3)

    alice.send(protocol.TYPE_GROUP, 100, "group call from alice")
    alice.send(protocol.TYPE_PRIVATE, 2222, "private call for bob")
    alice.send(protocol.TYPE_PRIVATE, 9999, "private call for someone else")

    deadline = time.time() + 3.0
    received = []
    while time.time() < deadline and len(received) < 2:
        received.extend(bob.drain())
        time.sleep(0.05)

    check("bob received both frames addressed to him",
          len(received) == 2, f"got {len(received)}")

    if len(received) == 2:
        group, private = received[0], received[1]
        check("sender id recovered over the air", group.sender_id == 1111)
        check("group frame identified as a room message", group.is_group)
        check("group frame routes to the talkgroup view", group.view_key == ("TG", 100))
        check("private frame identified as a DM", not private.is_group)
        check("private frame routes to a DM view keyed by sender",
              private.view_key == ("DM", 1111))
        check("private body intact", private.text == "private call for bob")

    check("DM addressed to another radio was filtered out",
          all(m.text != "private call for someone else" for m in received))

    alice_heard = list(alice.drain())
    check("a station does not hear its own transmissions", not alice_heard)

    # Full-size message across the wire.
    bob.send(protocol.TYPE_GROUP, 100, "B" * protocol.MAX_BODY_BYTES)
    deadline = time.time() + 3.0
    big = []
    while time.time() < deadline and not big:
        big.extend(alice.drain())
        time.sleep(0.05)
    check("full 468-byte body survives the round trip",
          bool(big) and len(big[0].text) == 468,
          f"got {len(big[0].text) if big else 'nothing'}")

    alice.close()
    bob.close()


def main():
    print("DMRChat self-test")
    test_budget()
    test_header()
    test_addressing()
    test_limits()
    test_demux()
    test_peer_to_peer()

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
