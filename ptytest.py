#!/usr/bin/env python3
"""
Drive two real DMRChat instances through pseudo-terminals and verify that a
group message and a private DM actually cross between them on screen.

This exercises the whole stack -- curses UI, input budget, struct packer,
sockets -- with no radio and no server.

    ./.venv/bin/python ptytest.py
"""

import os
import pty
import re
import select
import subprocess
import sys
import time

PYTHON = "./.venv/bin/python"
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]|\r")


class Station:
    def __init__(self, radio_id, args):
        self.radio_id = radio_id
        self.master, slave = pty.openpty()
        env = dict(os.environ, TERM="xterm-256color", LINES="30", COLUMNS="110")
        self.proc = subprocess.Popen(
            [PYTHON, "dmrchat.py", "--sim", "--radio-id", str(radio_id)] + args,
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
        )
        os.close(slave)
        self.buffer = ""

    def read(self, seconds=0.6):
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.buffer += chunk.decode("utf-8", errors="replace")
        return self.buffer

    def type(self, text):
        os.write(self.master, text.encode())
        time.sleep(0.35)
        self.read(0.4)

    def screen(self):
        self.read(0.3)
        return ANSI.sub(" ", self.buffer)

    def stop(self):
        try:
            os.write(self.master, b"/quit\r")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            os.close(self.master)


results = []


def check(label, condition, detail=""):
    results.append(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  {detail}" if not condition else ""))


def main():
    print("DMRChat pseudo-terminal end-to-end test\n")
    alice = Station(1111, ["--tg", "100"])
    bob = Station(2222, ["--tg", "100"])
    time.sleep(2.0)

    alice.read(1.0)
    bob.read(1.0)

    check("alice's UI drew the status bar", "DMRChat" in alice.screen() and "1111" in alice.screen())
    check("chat room view opened on 225.0.0.100",
          "225.0.0.100:50000" in bob.screen(), bob.screen()[-300:])
    check("byte budget shown in the input field", "/468B]" in alice.screen())

    # --- group chat ---
    alice.type("hello everyone on talkgroup 100\r")
    time.sleep(0.8)
    check("group message reached bob's room view",
          "hello everyone on talkgroup 100" in bob.screen())
    check("bob sees alice's radio id as the sender", "<1111>" in bob.screen())

    # --- private DM ---
    bob.type("/dm 1111\r")
    time.sleep(0.5)
    check("bob's DM view targets 13.0.4.87 (PC network)",
          "13.0.4.87:50000" in bob.screen(), "expected 13.<1111> unicast target")
    bob.type("this is a private message\r")
    time.sleep(0.8)

    alice_screen = alice.screen()
    check("alice's DM view opened automatically from the app header",
          "DM 2222" in alice_screen)
    check("unread marker raised on the inactive DM view",
          "DM 2222*1" in alice_screen, "expected an unread count on the tab")
    check("private text stayed out of the active group room",
          "this is a private message" not in alice_screen)

    # Switching to the DM view must reveal the private message there.
    alice.buffer = ""
    alice.type("\t")
    time.sleep(0.6)
    switched = alice.screen()
    check("private message renders after switching to the DM view",
          "this is a private message" in switched, switched[-300:])
    check("DM view shows the 13.x.x.x unicast reply target",
          "13.0.8.174:50000" in switched, "expected 13.<2222>")

    # --- input budget enforcement ---
    alice.type("X" * 500)
    time.sleep(0.5)
    screen = alice.screen()
    check("input field clamped at exactly 468 bytes",
          "[468/468B]" in screen, "counter never reached the boundary")
    check("no overflow past the limit was accepted", "[469/468B]" not in screen)
    alice.type("\x15")          # Ctrl-U to clear

    alice.stop()
    bob.stop()

    passed = sum(1 for r in results if r)
    print(f"\n{passed} passed, {len(results) - passed} failed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
