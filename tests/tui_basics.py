#!/usr/bin/env python3
"""Drives the interface on the Pi through a real terminal and prints the screens.

Usage: tui_basics.py [columns rows] [--start-only], default 100 24 with all checks.
"""
import os
import select
import subprocess
import sys
import time

import pyte

numbers = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
COLS, ROWS = numbers if len(numbers) == 2 else (100, 24)
ALL_CHECKS = "--start-only" not in sys.argv
REMOTE = (
    f"stty cols {COLS} rows {ROWS}; export TERM=xterm-256color TINYLLM_CONFIG=/tmp/tinyllm-test; "
    "exec bash -lc tinyllm"
)

proc = subprocess.Popen(
    ["ssh", "-tt", "-o", "BatchMode=yes", "pi", REMOTE],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
)


class Screen(pyte.Screen):
    def write_process_input(self, data):
        proc.stdin.write(data.encode())


screen = Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if ready:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                return False
            stream.feed(data)
    return True


def show(title):
    print(f"\n===== {title} =====")
    print("\n".join(line.rstrip() for line in screen.display).rstrip())


def wait_for(text, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.3)
        if any(text in line for line in screen.display):
            return True
    print(f"!!! not found: {text!r}")
    return False


def send(text):
    proc.stdin.write(text.encode())
    pump(0.5)


ok = wait_for("Length", 60)
pump(1)
show("Start")

if ALL_CHECKS:
    send("/t")
    pump(0.7)
    show("Command menu")
    ok &= any("/tokens" in line for line in screen.display)
    send("\x7f\x7f")
    pump(0.3)
    send("/temp 0.5\r")
    ok &= wait_for("Temperature = 0.5", 5)
    send("/tokens 45\r")
    ok &= wait_for("Length = 45", 5)
    send("/temp 9\r")
    ok &= wait_for("must be between", 5)
    send("/foo\r")
    ok &= wait_for("Unknown command", 5)
    send("/help\r")
    ok &= wait_for("Stop the running", 5)
    pump(1)
    show("Commands")

    send("/clear\r")
    pump(1.5)
    send("Once upon a time, a little robot found a dragon in the dark forest.\r")
    ok &= wait_for("tokens/s", 40)
    pump(1)
    show("Story")

    send("/tokens 256\r")
    pump(0.5)
    send("/clear\r")
    pump(1.5)
    proc.stdin.write(b"The little cat was very hungry.\r")
    pump(0.3)
    proc.stdin.write(b"\x03")
    ok &= wait_for("Stopped.", 40)
    pump(1)
    show("After Ctrl+C")

    send("/temp 0.5\r")
    send("/tokens 20\r")
    send("Tim had a red ball.\r")
    ok &= wait_for("20 tokens", 30) or wait_for("tokens/s", 5)
    pump(1)
    show("Ready again after the restart")

send("/quit\r")
pump(2)
show("End")
proc.wait(timeout=10)
print("\nexit:", proc.returncode, "all ok:", bool(ok))
sys.exit(0 if ok and proc.returncode == 0 else 1)
