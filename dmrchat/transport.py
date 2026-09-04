"""
Bidirectional UDP transport over the radio link.

A single socket bound to 0.0.0.0:50000 handles both directions, so our source
port matches the port the radios expect to see traffic on. A receiver thread
drains the socket and hands frames to the UI thread through a queue -- curses is
touched by exactly one thread, always the main one.

Two modes:

  live -- what runs against real hardware. DMs go to 12.a.b.c, rooms go to
          225.a.b.c, and inbound frames carry a 13.a.b.c source address from
          which we recover the sender's radio id.

  sim  -- peer-to-peer testing on one host with no radio and no server. Every
          instance joins one admin-scoped multicast group on loopback, which
          behaves like a shared RF channel: everyone hears every transmission.
          Because loopback cannot give us a 13.x.x.x source address, sim frames
          are prefixed with the four bytes of that address, and the receiver
          converts them straight back into the dotted-quad the live path parses.
          The 4-byte application header is byte-identical in both modes.
"""

import queue
import socket
import struct
import threading
import time

from . import protocol

SIM_GROUP = "239.255.77.1"
SIM_INTERFACE = "127.0.0.1"
RECV_BUFFER = 2048          # oversized on purpose, so we can detect fat frames


class Inbound:
    """One received message, already attributed and demultiplexed."""

    def __init__(self, sender_id, source_ip, msg_type, target_id, text, received_at=None):
        self.sender_id = sender_id
        self.source_ip = source_ip
        self.msg_type = msg_type
        self.target_id = target_id
        self.text = text
        self.received_at = received_at or time.time()

    @property
    def is_group(self):
        return self.msg_type == protocol.TYPE_GROUP

    @property
    def view_key(self):
        """
        Which conversation this belongs to.

        A group frame files under its talkgroup room. A private frame files
        under the *sender*, because that is the person we would reply to -- its
        target id is our own radio id and would make a useless conversation key.
        """
        if self.is_group:
            return ("TG", self.target_id)
        return ("DM", self.sender_id)


class TransportError(RuntimeError):
    pass


class RadioTransport:
    """UDP endpoint on port 50000, live over the radio or simulated on loopback."""

    def __init__(self, own_id, sim=False, port=protocol.RADIO_PORT, bind_address="0.0.0.0"):
        self.own_id = protocol.validate_id(own_id, "radio id")
        self.sim = sim
        self.port = port
        self.bind_address = bind_address

        self.inbox = queue.Queue()
        self.events = queue.Queue()      # transport-level notices for the UI log

        self.sock = None
        self._thread = None
        self._stop = threading.Event()

        self.sent_count = 0
        self.recv_count = 0
        self.dropped_count = 0

    # --- lifecycle ---------------------------------------------------------

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            # Sim peers share port 50000 on one host; multicast delivers to all.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        try:
            self.sock.bind((self.bind_address, self.port))
        except OSError as error:
            self.sock.close()
            raise TransportError(
                f"cannot bind UDP {self.bind_address}:{self.port} -- {error}"
            ) from error

        if self.sim:
            self._join_sim_channel()
        else:
            # Group targets are multicast destinations routed via the radio
            # gateway, so give them enough TTL to survive the hop.
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)

        self.sock.settimeout(0.4)
        self._thread = threading.Thread(target=self._receive_loop, name="radio-rx", daemon=True)
        self._thread.start()

    def _join_sim_channel(self):
        try:
            interface = socket.inet_aton(SIM_INTERFACE)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            self.sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(SIM_GROUP) + interface,
            )
        except OSError as error:
            self.sock.close()
            raise TransportError(f"cannot join simulated channel {SIM_GROUP} -- {error}") from error

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # --- transmit ----------------------------------------------------------

    def send(self, msg_type, target_id, text):
        """
        Put one message on the air.

        Returns the number of bytes the datagram occupies of the 500-byte PDU.
        Raises :class:`protocol.ProtocolError` before touching the socket if the
        body is over budget.
        """
        if self.sock is None:
            raise TransportError("transport is not open")

        frame = protocol.pack(msg_type, target_id, text)

        if self.sim:
            # Stand in for the network layer: the four bytes the radio side
            # would have supplied as the 13.a.b.c source address.
            prefix = struct.pack(
                ">B3s",
                protocol.SOURCE_RX_PREFIX,
                bytes(protocol.id_to_octets(self.own_id)),
            )
            self.sock.sendto(prefix + frame, (SIM_GROUP, self.port))
        else:
            destination = protocol.target_address(msg_type, target_id)
            self.sock.sendto(frame, (destination, self.port))

        self.sent_count += 1
        return len(frame) + protocol.IP_UDP_OVERHEAD

    # --- receive -----------------------------------------------------------

    def _receive_loop(self):
        while not self._stop.is_set():
            try:
                data, address = self.sock.recvfrom(RECV_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue

            try:
                message = self._decode(data, address[0])
            except protocol.ProtocolError as error:
                self.dropped_count += 1
                self.events.put(f"dropped frame from {address[0]}: {error}")
                continue

            if message is None:
                continue

            self.recv_count += 1
            self.inbox.put(message)

    def _decode(self, data, source_ip):
        """Turn a raw datagram into an :class:`Inbound`, or ``None`` to ignore it."""
        if self.sim:
            if len(data) < 4:
                raise protocol.ProtocolError("simulated frame missing its source header")
            source_ip = socket.inet_ntoa(data[:4])
            data = data[4:]

        sender_id = protocol.sender_id_from_source_ip(source_ip)
        if sender_id is None:
            raise protocol.ProtocolError(
                f"source {source_ip} is outside the {protocol.SOURCE_RX_PREFIX}.x.x.x radio block"
            )

        # We never hear our own transmissions over the air; keep sim honest.
        if sender_id == self.own_id:
            if not self.sim:
                # Over the air this should be impossible. It means this station's
                # configured id matches the sender's -- two stations sharing an id,
                # or an id here that belongs to the other radio. Everything from
                # that peer would vanish, so say so rather than dropping quietly.
                self.dropped_count += 1
                self.events.put(
                    f"IGNORED frame from {source_ip}: sender id {sender_id} is this "
                    f"station's own id. Two stations are configured with the same "
                    f"radio id -- change one with /id <n>."
                )
            return None

        msg_type, target_id, text = protocol.unpack(data)

        # A private frame addressed to a different radio is not ours to render.
        if msg_type == protocol.TYPE_PRIVATE and target_id != self.own_id:
            self.dropped_count += 1
            self.events.put(
                f"IGNORED private frame from {sender_id}: addressed to radio "
                f"{target_id}, this station is {self.own_id}. If {target_id} is "
                f"actually this radio, set it with /id {target_id}."
            )
            return None

        return Inbound(sender_id, source_ip, msg_type, target_id, text)

    # --- draining ----------------------------------------------------------

    def drain(self):
        """Yield everything received since the last call. Called from the UI thread."""
        while True:
            try:
                yield self.inbox.get_nowait()
            except queue.Empty:
                return

    def drain_events(self):
        while True:
            try:
                yield self.events.get_nowait()
            except queue.Empty:
                return
