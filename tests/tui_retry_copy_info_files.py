#!/usr/bin/env python3
"""/retry, /copy, /info, @file, Alt+Enter, temperature trend and /bench results in a real terminal."""
import os
import select
import subprocess
import sys
import time

import pyte

COLS, ROWS = 100, 40
CONFIG = "/tmp/tinyllm-test"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pi"]
TREND = set("▁▂▃▄▅▆▇█")
failures = []
raw = bytearray()

subprocess.run(SSH + [f"rm -rf {CONFIG} && mkdir -p {CONFIG} && printf 'The secret code is 4711.' > {CONFIG}/note.txt"], check=True)
proc = subprocess.Popen(
    ["ssh", "-tt", "-o", "BatchMode=yes", "pi",
     f"stty cols {COLS} rows {ROWS}; export TERM=xterm-256color TINYLLM_CONFIG={CONFIG}; exec bash -lc tinyllm"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
)


class Screen(pyte.Screen):
    def write_process_input(self, data):
        proc.stdin.write(data.encode())


screen = Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


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
            raw.extend(data)
            stream.feed(data)


def show(title):
    print(f"\n===== {title} =====")
    print("\n".join(line.rstrip() for line in screen.display).rstrip(), flush=True)


def has(text):
    return any(text in line for line in screen.display)


def count(text):
    return sum(text in line for line in screen.display)


def prompt_ready():
    return "RAM" in screen.display[-1] and "Model " in screen.display[-3] and "Top-K" in screen.display[-3]


def wait_for(text, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.3)
        if has(text):
            return True
    print(f"!!! not found: {text!r}", flush=True)
    return False


def wait_until(condition, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.3)
        if condition():
            return True
    return False


def send(text):
    proc.stdin.write(text.encode())
    pump(0.7)


def clear():
    send("/clear\r")
    pump(1.5)


def answers():
    return count("tokens/s · temperature")


def finished(before, timeout=240):
    return wait_until(lambda: answers() > before and prompt_ready(), timeout) and (pump(1) or True)


def ask(text, timeout=240):
    before = answers()
    send(text + "\r")
    ok = finished(before, timeout)
    if not ok:
        print(f"!!! no answer to: {text!r}", flush=True)
    return ok


def context_used():
    line = next(line for line in screen.display[-2:] if "Context" in line)
    return int(line.split("·")[1].split("/")[0])


def remote(command):
    return subprocess.run(SSH + [command], capture_output=True, text=True).stdout


pump(1)
check(wait_for("Length", 60), "starts with TinyStories")
pump(3)
check(any(char in screen.display[-1] for char in TREND), "the readings show a temperature trend")
show("Start")

# TinyStories
send("/retry\r")
check(wait_for("There is no answer to retry yet.", 5), "/retry without an answer")
send("/copy\r")
check(wait_for("There is no answer to copy yet.", 5), "/copy without an answer")
check(ask("Once upon a time there was a little cat"), "TinyStories answers")
before = answers()
send("/retry\r")
check(finished(before), "/retry: TinyStories writes a new story")
check(has("❯ Once upon a time there was a little cat") and count("Once upon a time there was a little cat") >= 3, "/retry shows what it sends again")
raw.clear()
send("/copy\r")
check(wait_for("Sent", 5) and wait_for("to the clipboard", 1), "/copy confirms")
check(b"\x1b]52;c;" in raw, "/copy sends OSC 52")
copied = remote(f"cat {CONFIG}/last-answer.txt")
check(copied.startswith("Once upon a time there was a little cat") and len(copied) > 60, "/copy saves the story to a file")
clear()
send("/info\r")
check(wait_for("28.9M", 5) and has("int4") and has("MIT"), "/info for TinyStories")
show("TinyStories /info")

# Chat model
clear()
send("/model SmolLM2\r")
check(wait_for("Model: SmolLM2", 120), "switches to SmolLM2")
pump(1)
send("/tokens 60\r")
send("/temp 0.2\r")
clear()
send("/info\r")
check(wait_for("Architecture", 5) and has("llama") and has("135M") and has("apache-2.0"), "/info reads the GGUF file")
check(has("trained for 8192") and has("Q4_K_M") and has("not yet, /bench"), "/info shows context, quantization, no measurement yet")
show("SmolLM2 /info")

clear()
send("Write the word apple.\x1b\rThen write the word pear.")
pump(1)
check(has("❯ Write the word apple.") and has("  Then write the word pear."), "Alt+Enter starts a new line")
show("Two lines")
before = answers()
send("\r")
check(finished(before), "a message with two lines is sent")
sent = remote(f"tail -c 300 {CONFIG}/history")
check("+Write the word apple.\n+Then write the word pear." in sent, "history keeps both lines")

clear()
send(f"Hi @{CONFIG}/no")
pump(2)
check(has("note.txt"), "@ completes file names")
show("Completion")
send("\t")
pump(1)
check(has(f"❯ Hi @{CONFIG}/note.txt"), "Tab inserts the file name")
before = answers()
send(" What is the secret code?\r")
check(wait_for(f"Attached {CONFIG}/note.txt (24 characters)", 10), "@file is attached")
check(finished(before), "answer with an attached file")
show("Attached file")
check("4711" in " ".join(screen.display), "the model read the file")

first = context_used()
before = answers()
send("/retry\r")
check(finished(before), "/retry answers again")
check(has("❯ Hi @") and abs(context_used() - first) < 40, f"/retry replaces the exchange ({first} -> {context_used()})")
show("Retry")
copied = remote(f"cat {CONFIG}/last-answer.txt 2>/dev/null")
send("/copy\r")
pump(1)
copied = remote(f"cat {CONFIG}/last-answer.txt")
check("4711" in copied or len(copied) > 10, "/copy saves the chat answer")
print("copied:", repr(copied[:200]), flush=True)

clear()
check(ask("My name is Ada."), "chat turn 1")
check(ask("I live in Berlin."), "chat turn 2")
check(ask("Say hello."), "chat turn 3")
send("/compact\r")
pump(3)
compacted = context_used()
before = answers()
send("/retry\r")
check(finished(before), "/retry after /compact")
check(abs(context_used() - compacted) < 60, f"/retry after /compact keeps the count right ({compacted} -> {context_used()})")
show("Retry after compact")
clear()
send("/retry\r")
check(wait_for("There is no answer to retry yet.", 5), "/retry after /clear")

clear()
start = time.monotonic()
send("/bench\r")
check(wait_until(lambda: has("Peak") and prompt_ready(), 600), "/bench runs all models")
print(f"bench took {time.monotonic() - start:.0f} s", flush=True)
show("Bench")
clear()
send("/info\r")
check(wait_for("tokens/s ·", 5) and has("(20") and has("/bench)"), "/info shows the measurement")
show("Info after bench")
saved = remote(f"cat {CONFIG}/settings.json")
check(saved.count('"tokens_per_s"') == 5, "bench values saved for all 5 models")

send("/help\r")
check(wait_for("Alt+Enter", 5) and has("/retry") and has("@path"), "/help lists the new features")
send("/quit\r")
pump(2.5)
proc.wait(timeout=15)
print("\nfailures:", failures, flush=True)
sys.exit(1 if failures else 0)
