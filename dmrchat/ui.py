"""
Split-screen curses interface.

Layout, top to bottom:

    status bar      own radio id, interface, gateway, link mode, counters
    tab bar         every open view, unread counts marked
    message pane    scrollback for the active conversation, word-wrapped
    divider         the active view's on-air destination address
    input line      with a live 468-byte budget readout
    hint line       command reference

The curses screen is touched only from the main thread. Inbound traffic arrives
through the transport's queue and is drained once per input tick.
"""

import curses
import textwrap

from . import protocol, session
from .session import KIND_DM, KIND_TG, Message

TICK_MS = 120           # input poll interval; also the inbound drain cadence
MIN_HEIGHT = 9
MIN_WIDTH = 40

HINTS = "/tg <id>  /dm <id>  /close  /views  /id <n>  /help  /quit   TAB view   PgUp/PgDn scroll"

HELP_LINES = [
    "Commands:",
    "  /tg <id>      open or switch to a chat room (talkgroup, sent to 225.x.x.x)",
    "  /dm <id>      open or switch to a private conversation (sent to 12.x.x.x)",
    "  /close        close the active view",
    "  /views        list open views",
    "  /id <n>       show or change this station's radio id",
    "  /stats        transport counters and the PDU budget breakdown",
    "  /clear        clear the active view's scrollback",
    "  /help         this text",
    "  /quit         tear down the radio routes and exit",
    "Keys:",
    "  TAB / SHIFT-TAB   cycle views        PgUp / PgDn   scroll history",
    "  CTRL-U clear line   CTRL-W delete word   HOME / END   move cursor",
    f"  Message body is capped at {protocol.MAX_BODY_BYTES} bytes "
    f"({protocol.PDU_LIMIT}-byte PDU - {protocol.NET_OVERHEAD} bytes overhead).",
]

# Colour pair ids
CP_STATUS = 1
CP_OWN = 2
CP_PEER = 3
CP_SYSTEM = 4
CP_ERROR = 5
CP_TAB_ACTIVE = 6
CP_TAB_UNREAD = 7
CP_DIM = 8
CP_OK = 9


class ChatUI:
    """Renders the session and turns keystrokes into transmissions."""

    def __init__(self, screen, state, transport, link, sim=False):
        self.screen = screen
        self.state = state
        self.transport = transport
        self.link = link
        self.sim = sim

        self.buffer = ""
        self.cursor = 0
        self.running = True
        self.has_colour = False

    # --- setup -------------------------------------------------------------

    def setup(self):
        # Terminfo entries vary in what they support. None of the niceties below
        # are worth failing a launch over, so every optional capability is tried
        # and shrugged off -- a terminal without them still gets a working chat.
        try:
            curses.curs_set(1)
        except curses.error:
            pass                    # no cnorm capability; the cursor stays as-is

        self.screen.nodelay(False)
        self.screen.timeout(TICK_MS)
        self.screen.keypad(True)
        self._setup_colour()

    def _setup_colour(self):
        if not curses.has_colors():
            return
        try:
            curses.start_color()
        except curses.error:
            return

        # -1 means "keep the terminal's own background", which needs
        # use_default_colors(). Fall back to a real colour if it is unavailable.
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = curses.COLOR_BLACK

        pairs = [
            (CP_STATUS, curses.COLOR_WHITE, curses.COLOR_BLUE),
            (CP_OWN, curses.COLOR_CYAN, background),
            (CP_PEER, curses.COLOR_GREEN, background),
            (CP_SYSTEM, curses.COLOR_YELLOW, background),
            (CP_ERROR, curses.COLOR_RED, background),
            (CP_TAB_ACTIVE, curses.COLOR_BLACK, curses.COLOR_CYAN),
            (CP_TAB_UNREAD, curses.COLOR_YELLOW, background),
            (CP_DIM, curses.COLOR_BLUE, background),
            (CP_OK, curses.COLOR_GREEN, background),
        ]
        try:
            for pair, foreground, back in pairs:
                curses.init_pair(pair, foreground, back)
        except curses.error:
            self.has_colour = False     # monochrome rather than half-painted
            return
        self.has_colour = True

    def colour(self, pair, bold=False):
        if not self.has_colour:
            return curses.A_BOLD if bold else curses.A_NORMAL
        attr = curses.color_pair(pair)
        return attr | curses.A_BOLD if bold else attr

    # --- drawing primitives ------------------------------------------------

    def _put(self, row, col, text, attr=curses.A_NORMAL):
        """Write clipped to the window; curses errors at the last cell are normal."""
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or col >= width:
            return
        space = width - col
        try:
            self.screen.addnstr(row, col, text, space, attr)
        except curses.error:
            pass

    def _fill(self, row, attr):
        height, width = self.screen.getmaxyx()
        if 0 <= row < height:
            try:
                self.screen.addnstr(row, 0, " " * width, width, attr)
            except curses.error:
                pass

    # --- layout ------------------------------------------------------------

    def _geometry(self):
        height, width = self.screen.getmaxyx()
        return {
            "height": height,
            "width": width,
            "status": 0,
            "tabs": 1,
            "msg_top": 2,
            "msg_bottom": height - 4,
            "divider": height - 3,
            "input": height - 2,
            "hint": height - 1,
            "msg_height": max(0, (height - 4) - 2 + 1),
        }

    def draw(self):
        geo = self._geometry()
        if geo["height"] < MIN_HEIGHT or geo["width"] < MIN_WIDTH:
            self.screen.erase()
            self._put(0, 0, f"Terminal too small ({MIN_WIDTH}x{MIN_HEIGHT} min)")
            self.screen.refresh()
            return

        self.screen.erase()
        self._draw_status(geo)
        self._draw_tabs(geo)
        self._draw_messages(geo)
        self._draw_divider(geo)
        self._draw_input(geo)
        self._put(geo["hint"], 0, HINTS[: geo["width"] - 1], self.colour(CP_DIM))

        # Park the hardware cursor inside the input field.
        prompt_width = 2
        visible = min(self.cursor, geo["width"] - prompt_width - 12)
        try:
            self.screen.move(geo["input"], prompt_width + max(0, visible))
        except curses.error:
            pass
        self.screen.refresh()

    def _draw_status(self, geo):
        attr = self.colour(CP_STATUS, bold=True)
        self._fill(geo["status"], attr)
        if self.sim:
            mode = "SIM (loopback channel)"
        else:
            mode = f"LIVE {self.link.interface} gw {self.link.gateway}"
        left = f" DMRChat  RADIO {self.state.own_id}  {mode}"

        # How long since the socket saw anything at all. On an intermittent link
        # this is the number that matters -- it distinguishes "nobody is talking"
        # from "the radio stopped forwarding to this host".
        age = self.transport.age(self.transport.last_datagram_at)
        if age is None:
            heard = "last rx never"
        elif age < 90:
            heard = f"last rx {int(age)}s"
        else:
            heard = f"last rx {int(age // 60)}m"

        right = (
            f"tx {self.transport.sent_count}  rx {self.transport.recv_count}  "
            f"drop {self.transport.dropped_count}  {heard}  udp/{protocol.RADIO_PORT} "
        )
        self._put(geo["status"], 0, left, attr)
        if len(left) + len(right) < geo["width"]:
            self._put(geo["status"], geo["width"] - len(right), right, attr)

    def _draw_tabs(self, geo):
        views = self.state.all_views()
        if not views:
            self._put(
                geo["tabs"], 0,
                " no views open -- /tg <id> for a chat room, /dm <id> for a private DM",
                self.colour(CP_SYSTEM),
            )
            return

        column = 0
        for index, view in enumerate(views, start=1):
            unread = f"*{view.unread}" if view.unread else ""
            label = f" {index}:{view.label}{unread} "
            if column + len(label) >= geo["width"]:
                self._put(geo["tabs"], column, ">", self.colour(CP_DIM))
                break
            if view.key == self.state.active_key:
                attr = self.colour(CP_TAB_ACTIVE, bold=True)
            elif view.unread:
                attr = self.colour(CP_TAB_UNREAD, bold=True)
            else:
                attr = self.colour(CP_DIM)
            self._put(geo["tabs"], column, label, attr)
            column += len(label)

    def _wrap(self, view, width):
        """Render a view's history into display lines, newest last."""
        lines = []
        for message in view.messages:
            if message.system:
                attr = self.colour(CP_ERROR, bold=True) if message.error else self.colour(CP_SYSTEM)
                head = f"{message.clock} ** "
            elif message.outbound:
                attr = self.colour(CP_OWN, bold=True)
                head = f"{message.clock} <{self.state.own_id}> "
            else:
                attr = self.colour(CP_PEER, bold=True)
                head = f"{message.clock} <{message.sender_id}> "

            body_width = max(8, width - len(head))
            wrapped = textwrap.wrap(message.text, body_width) or [""]
            lines.append((head + wrapped[0], attr, len(head)))
            for continuation in wrapped[1:]:
                lines.append((" " * len(head) + continuation, attr, len(head)))
        return lines

    def _draw_messages(self, geo):
        view = self.state.active or self.state.system
        lines = self._wrap(view, geo["width"] - 1)
        capacity = geo["msg_height"]
        if capacity <= 0:
            return

        # scroll counts lines back from the newest; clamp to what exists
        max_scroll = max(0, len(lines) - capacity)
        view.scroll = min(view.scroll, max_scroll)
        end = len(lines) - view.scroll
        visible = lines[max(0, end - capacity):end]

        for offset, (text, attr, head_len) in enumerate(visible):
            row = geo["msg_top"] + offset
            self._put(row, 0, text[: geo["width"] - 1], attr)
            # Dim the timestamp/sender prefix so the message text carries the eye.
            self._put(row, 0, text[:head_len], self.colour(CP_DIM))

        if view.scroll > 0:
            marker = f" scrolled back {view.scroll} line{'s' if view.scroll != 1 else ''} "
            self._put(geo["msg_top"], max(0, geo["width"] - len(marker) - 1), marker,
                      self.colour(CP_TAB_ACTIVE))

    def _draw_divider(self, geo):
        view = self.state.active
        if view is None:
            text = " no active view "
        else:
            text = f" {view.title}  ->  {view.address} "
        line = text + "-" * max(0, geo["width"] - len(text) - 1)
        self._put(geo["divider"], 0, line, self.colour(CP_DIM, bold=True))

    def _draw_input(self, geo):
        used = protocol.body_length(self.buffer)
        remaining = protocol.MAX_BODY_BYTES - used
        counter = f"[{used}/{protocol.MAX_BODY_BYTES}B]"

        if remaining <= 0:
            counter_attr = self.colour(CP_ERROR, bold=True)
        elif remaining <= 40:
            counter_attr = self.colour(CP_SYSTEM, bold=True)
        else:
            counter_attr = self.colour(CP_DIM)

        field_width = max(8, geo["width"] - len(counter) - 4)
        # Horizontal scroll so a long line keeps the cursor in view.
        start = max(0, self.cursor - field_width + 1)
        shown = self.buffer[start:start + field_width]

        prompt_attr = self.colour(CP_OK, bold=True) if self.state.active else self.colour(CP_ERROR, bold=True)
        self._put(geo["input"], 0, "> ", prompt_attr)
        self._put(geo["input"], 2, shown)
        self._put(geo["input"], geo["width"] - len(counter) - 1, counter, counter_attr)

    # --- input handling ----------------------------------------------------

    def run(self):
        self.state.log("Ready. /tg <id> opens a chat room, /dm <id> opens a private DM.")
        while self.running:
            self._pump_transport()
            self.draw()
            self._read_key()

    def _pump_transport(self):
        for note in self.transport.drain_events():
            self.state.log(note, error=True)
        for inbound in self.transport.drain():
            self.state.deliver(inbound)

    def _read_key(self):
        try:
            key = self.screen.get_wch()
        except curses.error:
            return          # tick timeout, nothing typed
        except KeyboardInterrupt:
            self.running = False
            return

        if isinstance(key, str):
            self._handle_character(key)
        else:
            self._handle_special(key)

    def _handle_character(self, key):
        code = ord(key)

        if key in ("\n", "\r") or code == curses.KEY_ENTER:
            self._submit()
        elif code in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
        elif code == 21:                                    # Ctrl-U
            self.buffer, self.cursor = "", 0
        elif code == 23:                                    # Ctrl-W
            left = self.buffer[: self.cursor].rstrip()
            cut = left.rfind(" ") + 1
            self.buffer = self.buffer[:cut] + self.buffer[self.cursor:]
            self.cursor = cut
        elif code == 9:                                     # Tab
            self.state.cycle(1)
        elif code == 1:                                     # Ctrl-A
            self.cursor = 0
        elif code == 5:                                     # Ctrl-E
            self.cursor = len(self.buffer)
        elif code == 3:                                     # Ctrl-C
            self.running = False
        elif code >= 32:
            self._insert(key)

    def _insert(self, char):
        """Insert a character only if it keeps the body inside 468 bytes."""
        candidate = self.buffer[: self.cursor] + char + self.buffer[self.cursor:]
        if protocol.body_length(candidate) > protocol.MAX_BODY_BYTES:
            try:
                curses.beep()
            except curses.error:
                pass                # the red byte counter is the real feedback
            return
        self.buffer = candidate
        self.cursor += 1

    def _handle_special(self, key):
        view = self.state.active or self.state.system
        geo = self._geometry()

        if key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.buffer)
        elif key in (curses.KEY_BACKSPACE, 127):
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1:]
        elif key == curses.KEY_PPAGE:
            view.scroll += max(1, geo["msg_height"] // 2)
        elif key == curses.KEY_NPAGE:
            view.scroll = max(0, view.scroll - max(1, geo["msg_height"] // 2))
        elif key == curses.KEY_BTAB:
            self.state.cycle(-1)
        elif key == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
            except (curses.error, AttributeError):
                pass                # geometry is re-read from getmaxyx() anyway
        elif key in (curses.KEY_ENTER, 10, 13):
            self._submit()

    # --- submitting --------------------------------------------------------

    def _submit(self):
        text = self.buffer.strip()
        self.buffer, self.cursor = "", 0
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
        else:
            self._transmit(text)

    def _transmit(self, text):
        view = self.state.active
        if view is None:
            self.state.log("No view selected. Use /tg <id> or /dm <id> first.", error=True)
            return
        try:
            on_air = self.transport.send(view.msg_type, view.target_id, text)
        except (protocol.ProtocolError, OSError) as error:
            self.state.log(f"Transmit failed: {error}", error=True)
            return
        self.state.record_outbound(view, text)
        view.scroll = 0
        if on_air > protocol.PDU_LIMIT:      # unreachable; the packer enforces it
            self.state.log(f"WARNING: {on_air} bytes on air exceeds the PDU limit", error=True)

    # --- commands ----------------------------------------------------------

    def _command(self, line):
        parts = line[1:].split()
        if not parts:
            return
        name, args = parts[0].lower(), parts[1:]

        if name in ("quit", "exit", "q"):
            self.running = False

        elif name in ("tg", "room", "group"):
            self._open(KIND_TG, args, "talkgroup id")

        elif name in ("dm", "private", "pm"):
            self._open(KIND_DM, args, "radio id")

        elif name == "close":
            view = self.state.active
            if view is None:
                self.state.log("No view to close.", error=True)
            else:
                self.state.close_view(view.key)
                self.state.log(f"Closed {view.label}.")

        elif name in ("views", "list", "who"):
            views = self.state.all_views()
            if not views:
                self.state.log("No views open.")
            for index, view in enumerate(views, start=1):
                marker = "*" if view.key == self.state.active_key else " "
                self.state.log(
                    f"{marker}{index}. {view.title} -> {view.address} "
                    f"({len(view.messages)} msgs, {view.unread} unread)"
                )

        elif name == "id":
            if not args:
                self.state.log(f"This station is radio id {self.state.own_id}.")
                return
            try:
                new_id = protocol.validate_id(args[0], "radio id")
            except protocol.ProtocolError as error:
                self.state.log(str(error), error=True)
                return
            self.state.own_id = new_id
            self.transport.own_id = new_id
            self.state.log(
                f"Radio id set to {new_id}. Inbound DMs now match "
                f"{protocol.target_address(protocol.TYPE_PRIVATE, new_id)}."
            )

        elif name == "stats":
            self.state.log(
                f"tx {self.transport.sent_count}  rx {self.transport.recv_count}  "
                f"dropped {self.transport.dropped_count}"
            )
            self.state.log(
                f"PDU {protocol.PDU_LIMIT}B = {protocol.IP_UDP_OVERHEAD}B IP/UDP + "
                f"{protocol.APP_HEADER_LEN}B app header + {protocol.MAX_BODY_BYTES}B body"
            )

        elif name == "clear":
            view = self.state.active or self.state.system
            view.messages.clear()
            view.scroll = 0

        elif name in ("help", "h", "?"):
            for line_text in HELP_LINES:
                self.state.log(line_text)

        else:
            self.state.log(f"Unknown command /{name}. Try /help.", error=True)

    def _open(self, kind, args, label):
        if not args:
            self.state.log(f"Usage: /{'tg' if kind == KIND_TG else 'dm'} <{label}>", error=True)
            return
        try:
            view = self.state.open_view(kind, args[0])
        except protocol.ProtocolError as error:
            self.state.log(str(error), error=True)
            return
        self.state.log(f"{view.title}  transmitting to {view.address}")
