"""
Conversation state: the set of open views and the messages filed into each.

A view is either a chat room keyed by talkgroup id (transmitted on 225.x.x.x)
or a private conversation keyed by the other party's radio id (transmitted on
12.x.x.x). Views are created on demand, whether the user opens one or a message
arrives for one we were not watching -- nothing received is ever discarded for
want of somewhere to put it.
"""

import time
from collections import deque

from . import protocol

HISTORY_LIMIT = 500       # messages retained per view

KIND_TG = "TG"
KIND_DM = "DM"


class Message:
    """One line of conversation history."""

    def __init__(self, text, sender_id=None, outbound=False, system=False, error=False):
        self.text = text
        self.sender_id = sender_id
        self.outbound = outbound
        self.system = system
        self.error = error
        self.timestamp = time.time()

    @property
    def clock(self):
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))

    def prefix(self, own_id):
        if self.system:
            return "**"
        if self.outbound:
            return f"{own_id}"
        return f"{self.sender_id}"


class View:
    """A single conversation: a talkgroup room or a private DM thread."""

    def __init__(self, kind, target_id):
        self.kind = kind
        self.target_id = target_id
        self.messages = deque(maxlen=HISTORY_LIMIT)
        self.unread = 0
        self.scroll = 0           # lines scrolled back from the newest
        self.title = self._default_title()

    def _default_title(self):
        if self.kind == KIND_TG:
            return f"Chat Room -- Talkgroup {self.target_id}"
        if self.kind == KIND_DM:
            return f"Private DM -- Radio {self.target_id}"
        return self.kind

    @property
    def key(self):
        return (self.kind, self.target_id)

    @property
    def msg_type(self):
        return protocol.TYPE_GROUP if self.kind == KIND_TG else protocol.TYPE_PRIVATE

    @property
    def label(self):
        return f"{self.kind} {self.target_id}"

    @property
    def address(self):
        """The destination this view transmits to, shown in the status bar."""
        return f"{protocol.target_address(self.msg_type, self.target_id)}:{protocol.RADIO_PORT}"

    def add(self, message):
        self.messages.append(message)


class Session:
    """The open views, the active one, and the system log view."""

    def __init__(self, own_id):
        self.own_id = own_id
        self.views = {}
        self.order = []
        self.active_key = None

        # A view that is always present, holding startup/route/transport notices.
        self.system = View("SYS", 0)
        self.system.title = "System Log"

    # --- view management ---------------------------------------------------

    def open_view(self, kind, target_id, activate=True):
        target_id = protocol.validate_id(target_id, "talkgroup id" if kind == KIND_TG else "radio id")
        key = (kind, target_id)
        view = self.views.get(key)
        if view is None:
            view = View(kind, target_id)
            self.views[key] = view
            self.order.append(key)
        if activate:
            self.activate(key)
        return view

    def close_view(self, key):
        if key not in self.views:
            return False
        del self.views[key]
        position = self.order.index(key)
        self.order.remove(key)
        if self.active_key == key:
            if self.order:
                self.active_key = self.order[min(position, len(self.order) - 1)]
            else:
                self.active_key = None
        return True

    def activate(self, key):
        if key in self.views:
            self.active_key = key
            self.views[key].unread = 0
            self.views[key].scroll = 0

    def cycle(self, step):
        if not self.order:
            return
        if self.active_key not in self.views:
            self.activate(self.order[0])
            return
        index = (self.order.index(self.active_key) + step) % len(self.order)
        self.activate(self.order[index])

    @property
    def active(self):
        if self.active_key is None:
            return None
        return self.views.get(self.active_key)

    def all_views(self):
        return [self.views[key] for key in self.order]

    # --- message routing ---------------------------------------------------

    def log(self, text, error=False):
        """Record a system notice. Mirrored into the active view so it is seen."""
        self.system.add(Message(text, system=True, error=error))
        if self.active_key is not None and self.active_key in self.views:
            self.views[self.active_key].add(Message(text, system=True, error=error))
        elif self.active_key is None:
            self.system.unread += 1

    def record_outbound(self, view, text):
        view.add(Message(text, sender_id=self.own_id, outbound=True))

    def deliver(self, inbound):
        """
        File an inbound message into the view its application header selects.

        The view is created if it does not exist, so a message from a talkgroup
        or radio we were not already watching opens its own conversation.
        """
        kind, target_id = inbound.view_key
        key = (kind, target_id)
        view = self.views.get(key)
        if view is None:
            view = self.open_view(kind, target_id, activate=False)
        view.add(Message(inbound.text, sender_id=inbound.sender_id))
        if key != self.active_key:
            view.unread += 1
        return view
