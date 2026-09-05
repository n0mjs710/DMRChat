"""
Machine-local settings.

Deliberately under $HOME rather than in the project folder. A station's radio id
must match the radio physically attached to THAT Mac, so it is the one piece of
state that must never travel with the source: a repo shared over iCloud or
cloned from GitHub would otherwise hand both stations the same id, and each
would then discard everything the other sent as its own transmission.

Lives in its own module so both the startup path and the in-session /id command
can write it without importing each other.
"""

import os

from . import protocol

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".dmrchat")
RADIO_ID_FILE = os.path.join(CONFIG_DIR, "radio-id")


def load_radio_id():
    """This Mac's remembered radio id, or None if never saved or unreadable."""
    try:
        with open(RADIO_ID_FILE) as handle:
            return protocol.validate_id(handle.read().strip())
    except (OSError, protocol.ProtocolError):
        return None


def save_radio_id(radio_id):
    """
    Remember ``radio_id`` for the next launch on this Mac.

    Called both at startup and whenever /id changes the id mid-session, so the
    last id actually in use is the one that comes back -- not whichever was
    entered the first time the app was ever run.

    Returns True on success. A failure here is never worth interrupting a
    session over, so the caller may ignore it.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RADIO_ID_FILE, "w") as handle:
            handle.write(f"{radio_id}\n")
        return True
    except OSError:
        return False
