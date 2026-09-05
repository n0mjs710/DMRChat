# DMRChat

Real-time CLI chat that treats a MOTOTRBO digital radio network as an unmanaged
IP transport. No server, no broker, no infrastructure — stations address each
other directly through the radio's own address blocks.

## Quick start

```sh
./run.sh                          # discover radio, install routes, chat
./run.sh --radio-id 1234567       # skip the radio-id prompt
./run.sh --sim --radio-id 1111    # local peer-to-peer test, no radio needed
```

The venv is created automatically on first run; the system Python is never
modified. To build it by hand:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

## The 500-byte PDU budget

The gateway radios cap the PDU at 500 bytes, and a fragmented datagram over the
air is a lost datagram. Every byte is accounted for:

| Component            | Bytes |
| -------------------- | ----: |
| IPv4 header          |    20 |
| UDP header           |     8 |
| Application header   |     4 |
| **Message body**     | **468** |
| **Total on air**     | **500** |

The input field enforces the 468-byte boundary as you type — measured in UTF-8
bytes, not characters, so multi-byte text cannot smuggle the frame over the
limit. The counter turns amber at 40 bytes remaining and red at zero, and the
packer refuses an oversized body before it ever reaches the socket.

## The 4-byte application header

Inbound packets are indistinguishable at the network layer: private and group
calls both arrive from a `13.<sender-radio-id>` source, DNAT'd to the host at
`192.168.10.2`. The network cannot tell us which conversation a message belongs
to, so the payload carries its own routing data:

```
byte 0     message type   0x01 = group chat room, 0x02 = private DM
bytes 1-3  target id      24-bit talkgroup id or destination radio id
```

Packed with `struct` as `>B3s`. See [`dmrchat/protocol.py`](dmrchat/protocol.py).

## Addressing

MOTOTRBO addresses the **radio** on the CAI network (12) and the **PC or
application attached to it** on CAI+1 (13). That is why inbound frames arrive
from `13.<sender-radio-id>` — the source is the far PC, not the far radio.

It follows that a private message must be addressed to `13.<target-radio-id>`.
Sending to `12.<target-radio-id>` addresses the radio itself: it receives the
data call over the air and lights its RX indicator, but never hands the frame
to the host behind it.

Group traffic splits the same way: `225` addresses the talkgroup at the radios,
`226` addresses it at the PCs attached to them. Groups therefore default to
`226`, for the same reason DMs default to `13`.

| Direction | Kind    | Address                      |
| --------- | ------- | ---------------------------- |
| Outbound  | Private | `13.<radio-id>:50000`        |
| Outbound  | Group   | `226.<talkgroup-id>:50000`   |
| Inbound   | Both    | from `13.<sender-radio-id>`  |

IDs are 24-bit and map onto the low three octets, so radio `3120101` is
`13.47.155.229` and talkgroup `100` is `226.0.0.100`.

Fleets differ in how they are provisioned, so the blocks are settable at
startup: `--dm-prefix`, `--tg-prefix`, `--src-prefix`. `--dm-prefix 12` and
`--tg-prefix 225` address radios directly instead of their attached PCs. `diagnose.py --addr <id>` prints the addresses
derived from an id so you can compare them against one you can ping.

A private frame files under its **sender**, not its target — the target is our
own id and would make a useless conversation key. A group frame files under its
talkgroup. Messages for a conversation you were not watching open their own view
rather than being dropped.

## Lifecycle

On launch the app scans the host's adapters via `psutil.net_if_addrs()` for an
interface on `192.168.10.0/24` (falling back to `ifconfig`/`ip` if psutil is
absent), derives the gateway as `192.168.10.1`, and injects:

```
sudo route -n add -net 12.0.0.0/8 192.168.10.1
sudo route -n add -net 13.0.0.0/8 192.168.10.1
sudo route -n add -net 225.0.0.0/8 192.168.10.1
sudo route -n add -net 226.0.0.0/8 192.168.10.1
```

The networks are derived from the addressing actually in use, not hardcoded —
private messages go to the PC network, so a route for `12/8` alone would send
every DM out the host's default route and off the radio entirely.

If no interface matches, it prints `MOTOTRBO link not found. Please connect
radio via USB.` and exits without touching anything.

On exit — `/quit`, Ctrl+C, SIGTERM, SIGHUP, or an unhandled exception — it
removes exactly the routes it installed. A chat client that dies leaving
`12.0.0.0/8` pointed at a disconnected radio breaks the host's networking, so
teardown runs from a `try/finally` around the entire session.

## Interface

```
 DMRChat  RADIO 1111  LIVE en12 gw 192.168.10.1     tx 4  rx 7  drop 0  udp/50000
 1:TG 100   2:DM 2222*3
 14:22:07 <2222> message text, word-wrapped to the pane
 ...
 Chat Room -- Talkgroup 100  ->  226.0.0.100:50000 ------------------------------
 > typing here                                                        [37/468B]
 /tg <id>  /dm <id>  /close  /views  /id <n>  /help  /quit   TAB view  PgUp scroll
```

| Command | Effect |
| --- | --- |
| `/tg <id>` | open or switch to a chat room (transmits to `226.x.x.x`) |
| `/dm <id>` | open or switch to a private conversation (transmits to `13.x.x.x`) |
| `/close` | close the active view |
| `/views` | list open views with unread counts |
| `/id <n>` | show or change this station's radio id |
| `/stats` | transport counters and the PDU budget breakdown |
| `/clear` | clear the active view's scrollback |
| `/quit` | tear down routes and exit |

`TAB`/`SHIFT-TAB` cycle views, `PgUp`/`PgDn` scroll history, `CTRL-U` clears the
line, `CTRL-W` deletes a word, `HOME`/`END` move the cursor.

## Testing without hardware

`--sim` needs no radio, no routes, and no server. Every instance on the host
joins one admin-scoped multicast group on loopback, which behaves like a shared
RF channel — every station hears every transmission, and each filters what is
not addressed to it. Because loopback cannot supply a `13.x.x.x` source, sim
frames are prefixed with the four bytes of that address and the receiver
converts them straight back into the dotted-quad the live path parses. **The
4-byte application header is byte-identical in both modes.**

Two terminals:

```sh
./run.sh --sim --radio-id 1111 --tg 100
./run.sh --sim --radio-id 2222 --tg 100
```

Type in either. `/dm 1111` from the second station opens a private thread.

```sh
./.venv/bin/python selftest.py    # 38 checks: protocol, budget, demux, peer-to-peer
./.venv/bin/python ptytest.py     # 13 checks: two real UIs driven through ptys
```

## Layout

| File | Role |
| --- | --- |
| [`dmrchat/protocol.py`](dmrchat/protocol.py) | header packing, address mapping, PDU budget |
| [`dmrchat/discovery.py`](dmrchat/discovery.py) | adapter scan for the `192.168.10.0/24` link |
| [`dmrchat/routing.py`](dmrchat/routing.py) | kernel route install and teardown |
| [`dmrchat/transport.py`](dmrchat/transport.py) | UDP 50000, receiver thread, sim channel |
| [`dmrchat/session.py`](dmrchat/session.py) | views and message history |
| [`dmrchat/ui.py`](dmrchat/ui.py) | curses split-screen |
| [`dmrchat/app.py`](dmrchat/app.py) | startup, shutdown, signal handling |

### Concurrency

A synchronous main loop owns the curses screen; one daemon thread drains the
socket into a queue that the UI empties each input tick. curses is not
thread-safe, so exactly one thread ever draws. Blocking startup calls (`sudo`
prompting for a password) stay blocking, and route teardown sits in a plain
`try/finally` rather than a loop-shutdown sequence.

`asyncio` would earn its place here if the client grew ARQ — retransmit backoff,
delivery confirmation, presence beacons — where many concurrent timers make an
event loop cheaper than hand-rolled scheduling.

## Exit codes

`0` clean · `1` no radio link · `2` bad radio id · `3` transport/bind failure ·
`4` not an interactive terminal
