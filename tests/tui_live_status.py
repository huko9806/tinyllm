#!/usr/bin/env python3
"""Checks the live status line of Tiny LLM in a real terminal.

Usage: tui_live_status.py [columns rows], default 100 24.
"""
import os
import re
import select
import subprocess
import sys
import time

import pyte

COLS, ROWS = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (100, 24)

proc = subprocess.Popen(
    ["ssh", "-tt", "-o", "BatchMode=yes", "pi",
     f"stty cols {COLS} rows {ROWS}; export TERM=xterm-256color TINYLLM_CONFIG=/tmp/tinyllm-test; exec bash -lc tinyllm"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
)


class Screen(pyte.Screen):
    def write_process_input(self, data):
        proc.stdin.write(data.encode())


screen = Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)
failures = []


def check(condition, label):
    print(("ok   " if condition else "FAIL ") + label, flush=True)
    if not condition:
        failures.append(label)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if ready:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                return
            stream.feed(data)


def show(title):
    print(f"\n===== {title} =====")
    print("\n".join(line.rstrip() for line in screen.display).rstrip(), flush=True)


def has(text):
    return any(text in line for line in screen.display)


def wait_for(text, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.3)
        if has(text):
            return True
    print(f"!!! not found: {text!r}", flush=True)
    return False


def send(text):
    proc.stdin.write(text.encode())
    pump(0.6)


def bottom():
    return screen.display[-1].rstrip()


check(wait_for("Length", 60), "toolbar appears")
pump(2.5)
show("Start")
check(has("██") or has("|_"), "banner visible at start")
check("RAM" in bottom() and "CPU" in bottom(), "readings in the toolbar at the prompt")
check("Context" in screen.display[-2] and "░" in screen.display[-2], "context bar at the prompt")

seen = set()
for _ in range(8):
    pump(1.5)
    seen.add(bottom())
print("idle lines:", seen, flush=True)
check(len(seen) > 1, "toolbar refreshes on its own at the prompt")

send("/temp 0.3\r")
send("/model SmolLM2\r")
check(wait_for("Model: SmolLM2", 120), "model loads")
send("/tokens 400\r")
send("/clear\r")
pump(1.5)

send("Write a very long and detailed story about a robot.\r")
pump(3)
lines = []
contexts = []
for _ in range(10):
    pump(1.5)
    lines.append(bottom())
    contexts.append(screen.display[-2].rstrip())
show("During the answer")
print("context lines:", contexts[0], "...", contexts[-1], flush=True)
used = [int(re.search(r"(\d+)/2048", line).group(1)) for line in contexts if re.search(r"(\d+)/2048", line)]
check(len(used) == len(contexts) and all("Context" in line for line in contexts), "context bar stays above the status line")
check(len(used) > 1 and used[-1] > used[0] > 0, "context bar fills up during the answer")
check(screen.margins is not None and screen.margins.bottom == ROWS - 3, "two rows reserved during the answer")
print("status lines:", lines[:4], flush=True)
check(all("CPU" in line and "RAM" in line for line in lines), "status line stays on the bottom row during the answer")
check(len(set(lines)) > 1, "status line updates during the answer")
check(all(len(line) < COLS for line in lines), "status line fits the width")
check(screen.margins is not None, "scroll region active during the answer")

proc.stdin.write(b"\x03")
check(wait_for("Stopped.", 40), "Ctrl+C stops the answer")
pump(1.5)
show("After Ctrl+C")
check(screen.margins is None or screen.margins.bottom == ROWS - 1, "scroll region released after Ctrl+C")
check("RAM" in bottom(), "toolbar back after Ctrl+C")

send("/tokens 25\r")
send("/clear\r")
pump(1.5)
send("Say hello.\r")
check(wait_for("tokens/s", 120), "answer finishes")
pump(1.5)
show("Normal answer")
check(has("peak CPU"), "peak temperature in the statistics")
check(screen.margins is None or screen.margins.bottom == ROWS - 1, "scroll region released after an answer")
check("RAM" in bottom(), "toolbar back after an answer")
filled_after = re.search(r"(\d+)/2048", screen.display[-2])
check(bool(filled_after) and int(filled_after.group(1)) > 0, "context bar shows the finished answer")

send("/clear\r")
pump(3)
check("0/2048" in screen.display[-2] and "█" not in screen.display[-2], "/clear empties the context bar")

proc.stdin.write(("word " * 2300).encode() + b"\r")
check(wait_for("The context is full", 90), "a message that does not fit gets a clear hint")
pump(2.5)
show("Message too long")
check("0/2048" in screen.display[-2], "context bar unchanged after the rejected message")
send("/tokens 10\r")
send("/clear\r")
pump(1.5)
send("Say hello.\r")
check(wait_for("tokens/s", 60), "model still answers after the rejected message")
pump(1)

send("/help\r")
check(wait_for("Stop the running", 5), "commands still print normally")
pump(1)
send("/quit\r")
pump(2.5)
show("End")
proc.wait(timeout=15)
print("\nexit:", proc.returncode, "failures:", failures, flush=True)
sys.exit(1 if failures or proc.returncode else 0)
