"""
MOTOTRBO chat wire protocol.

The radio network gives us an IP transport with two hard facts we must design
around:

  1. The gateway radio's PDU is capped at 500 bytes. Anything larger fragments,
     and a fragmented datagram over the air is a lost datagram.
  2. Inbound packets look identical for private and group calls -- both arrive
     from a 13.<sender-radio-id> source address, DNAT'd to the host. The network
     layer therefore cannot tell us which conversation a message belongs to.

Fact (2) is why every payload carries a 4-byte application header:

    byte 0     message type   0x01 = group chat room, 0x02 = private DM
    bytes 1-3  target id      24-bit talkgroup id or destination radio id

Fact (1) sets the input budget:

    500 (PDU)  -  28 (20 IPv4 + 8 UDP)  -  4 (app header)  =  468 body bytes
"""

import struct

# --- PDU budget ------------------------------------------------------------

PDU_LIMIT = 500          # hardware maximum, bytes on the wire
IP_UDP_OVERHEAD = 28     # 20-byte IPv4 header + 8-byte UDP header
APP_HEADER_LEN = 4       # our type + 24-bit target
NET_OVERHEAD = IP_UDP_OVERHEAD + APP_HEADER_LEN          # 32
MAX_BODY_BYTES = PDU_LIMIT - NET_OVERHEAD                # 468
MAX_UDP_PAYLOAD = PDU_LIMIT - IP_UDP_OVERHEAD            # 472

# --- Message types ---------------------------------------------------------

TYPE_GROUP = 0x01
TYPE_PRIVATE = 0x02

TYPE_NAMES = {TYPE_GROUP: "GROUP", TYPE_PRIVATE: "PRIVATE"}

# --- Address blocks --------------------------------------------------------
#
# MOTOTRBO addresses the RADIO on the CAI network (default 12) and the PC or
# application attached to it on CAI+1 (default 13). That is why inbound frames
# arrive from 13.<sender-radio-id>: the source is the far PC, not the far radio.
#
# It follows that a message meant for an application on the far end should be
# addressed to 13.<target-radio-id>. Sending to 12.<target-radio-id> addresses
# the radio itself, which will receive the data call over the air -- lighting
# its RX indicator -- without ever handing it to the host behind it. Confirmed
# on the air: private messages only reached the far application once they were
# addressed to 13.
#
# Group traffic follows the same split: 225 addresses the talkgroup at the
# radios, 226 addresses it at the PCs attached to them. A radio that forwards
# private data to its PC but stays silent on groups is the same fault one layer
# over, which is why groups default to 226.
#
# Radios vary in how strictly they enforce this, so the prefixes are settable
# at startup rather than baked in. See configure().

CAI_NETWORK = 12             # the radios themselves
CAI_PC_NETWORK = 13          # PCs / applications attached to a radio
CAI_GROUP_NETWORK = 225      # talkgroups, addressed to the radios
CAI_GROUP_PC_NETWORK = 226   # talkgroups, addressed to the attached PCs

PRIVATE_TX_PREFIX = CAI_PC_NETWORK      # outbound unicast DM     -> 13.a.b.c
GROUP_TX_PREFIX = CAI_GROUP_PC_NETWORK  # outbound room broadcast -> 226.a.b.c
SOURCE_RX_PREFIX = CAI_PC_NETWORK       # inbound source header   <- 13.a.b.c


def configure(private_tx=None, group_tx=None, source_rx=None):
    """
    Override the address blocks at startup.

    Lets a station be pointed at the radio network (12) instead of the PC
    network (13) for private targets without touching the code, since fleets
    differ in how they are provisioned.
    """
    global PRIVATE_TX_PREFIX, GROUP_TX_PREFIX, SOURCE_RX_PREFIX
    if private_tx is not None:
        PRIVATE_TX_PREFIX = int(private_tx)
    if group_tx is not None:
        GROUP_TX_PREFIX = int(group_tx)
    if source_rx is not None:
        SOURCE_RX_PREFIX = int(source_rx)


def addressing_summary():
    return (f"DM -> {PRIVATE_TX_PREFIX}.x.x.x, group -> {GROUP_TX_PREFIX}.x.x.x, "
            f"inbound source expected {SOURCE_RX_PREFIX}.x.x.x")

RADIO_PORT = 50000
HOST_NAT_ADDRESS = "192.168.10.2"

# byte 0 is the type, bytes 1-3 are the 24-bit target packed big-endian
_HEADER = struct.Struct(">B3s")

ID_MIN = 1
ID_MAX = 0xFFFFFF        # 24 bits is all the header carries


class ProtocolError(ValueError):
    """Raised for anything that would put a malformed frame on the air."""


def validate_id(value, label="id"):
    """Coerce ``value`` to a legal 24-bit radio/talkgroup id."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ProtocolError(f"{label} must be a number, got {value!r}")
    if not ID_MIN <= number <= ID_MAX:
        raise ProtocolError(
            f"{label} must be {ID_MIN}-{ID_MAX} (24-bit), got {number}"
        )
    return number


def id_to_octets(radio_id):
    """24-bit id -> the low three octets of its address, big-endian."""
    radio_id = validate_id(radio_id)
    return (radio_id >> 16) & 0xFF, (radio_id >> 8) & 0xFF, radio_id & 0xFF


def octets_to_id(a, b, c):
    """The low three octets of an address -> the 24-bit id they encode."""
    return (a << 16) | (b << 8) | c


def target_address(msg_type, target_id):
    """Destination IP for a message type: 12.a.b.c for DM, 225.a.b.c for group."""
    if msg_type == TYPE_PRIVATE:
        prefix = PRIVATE_TX_PREFIX
    elif msg_type == TYPE_GROUP:
        prefix = GROUP_TX_PREFIX
    else:
        raise ProtocolError(f"unknown message type 0x{msg_type:02X}")
    a, b, c = id_to_octets(target_id)
    return f"{prefix}.{a}.{b}.{c}"


def source_address(sender_id):
    """The 13.a.b.c source address the radio network stamps on inbound frames."""
    a, b, c = id_to_octets(sender_id)
    return f"{SOURCE_RX_PREFIX}.{a}.{b}.{c}"


def sender_id_from_source_ip(ip_string):
    """
    Pull the sender's radio id out of an inbound ``13.a.b.c`` source address.

    Returns ``None`` when the address is not from the radio source block, so the
    caller can flag the frame rather than silently attributing it to someone.
    """
    parts = ip_string.split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return None
    if any(not 0 <= octet <= 255 for octet in octets):
        return None
    if octets[0] != SOURCE_RX_PREFIX:
        return None
    return octets_to_id(*octets[1:])


def body_length(text):
    """Length of ``text`` in the bytes that actually travel, not characters."""
    return len(text.encode("utf-8"))


def remaining_budget(text):
    """Body bytes still available before the 468-byte boundary."""
    return MAX_BODY_BYTES - body_length(text)


def fits(text):
    return body_length(text) <= MAX_BODY_BYTES


def pack(msg_type, target_id, text):
    """
    Build a complete on-air payload: 4-byte header followed by UTF-8 body.

    Refuses to produce anything that would exceed the 500-byte PDU once the
    28 bytes of IP/UDP framing are added, so a fragmenting frame can never
    reach the socket layer.
    """
    if msg_type not in TYPE_NAMES:
        raise ProtocolError(f"unknown message type 0x{msg_type:02X}")
    target_id = validate_id(target_id, "target id")

    body = text.encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ProtocolError(
            f"body is {len(body)} bytes, limit is {MAX_BODY_BYTES} "
            f"({PDU_LIMIT}-byte PDU minus {NET_OVERHEAD} bytes of overhead)"
        )

    frame = _HEADER.pack(msg_type, bytes(id_to_octets(target_id))) + body

    on_air = len(frame) + IP_UDP_OVERHEAD
    if on_air > PDU_LIMIT:
        # Belt and braces: the body check above should make this unreachable.
        raise ProtocolError(f"frame would be {on_air} bytes on air, over PDU limit")
    return frame


def unpack(frame):
    """
    Split an inbound payload into ``(msg_type, target_id, text)``.

    Raises :class:`ProtocolError` for frames too short to hold a header or too
    long to have legitimately come through a 500-byte PDU.
    """
    if len(frame) < APP_HEADER_LEN:
        raise ProtocolError(f"frame is {len(frame)} bytes, shorter than the 4-byte header")
    if len(frame) > MAX_UDP_PAYLOAD:
        raise ProtocolError(
            f"frame is {len(frame)} bytes, over the {MAX_UDP_PAYLOAD}-byte payload ceiling"
        )

    msg_type, target_bytes = _HEADER.unpack(frame[:APP_HEADER_LEN])
    if msg_type not in TYPE_NAMES:
        raise ProtocolError(f"unknown message type 0x{msg_type:02X}")

    target_id = octets_to_id(*target_bytes)
    text = frame[APP_HEADER_LEN:].decode("utf-8", errors="replace")
    return msg_type, target_id, text
