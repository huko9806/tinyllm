#!/usr/bin/env python3
"""/compact with LFM2 and granite, /save, /load and /download in a real terminal."""
import os
import select
import subprocess
import sys
import time

import pyte

COLS, ROWS = 100, 40
CONFIG = "/tmp/tinyllm-test"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pi"]
failures = []


def pi_dir():
    """The Tiny LLM folder on the Pi: TINYLLM_PI_DIR, or the folder the tinyllm command there points to."""
    folder = os.environ.get("TINYLLM_PI_DIR")
    if not folder:
        found = subprocess.run(SSH + ["bash -lc 'readlink -f \"$(command -v tinyllm)\"'"],
                               capture_output=True, text=True).stdout.strip()
        if not found:
            sys.exit("Tiny LLM not found on the Pi: put the tinyllm symlink into ~/.local/bin there, or set TINYLLM_PI_DIR")
        folder = os.path.dirname(found)
    return folder


MODELS = pi_dir() + "/models"

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


def send(text):
    proc.stdin.write(text.encode())
    pump(0.7)


def clear():
    send("/clear\r")
    pump(1.5)


def ask(text, timeout=240):
    """Waits until the new answer has really finished and the prompt is back."""
    send(text + "\r")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.3)
        if "tokens/s" in answer_after(text) and prompt_ready():
            pump(1)
            return True
    print(f"!!! no answer to: {text!r}", flush=True)
    return False


def answer_after(question):
    """The text below the last prompt that asked this question."""
    rows = [i for i, line in enumerate(screen.display) if question in line and "❯" in line]
    return " ".join(screen.display[rows[-1] + 1:]) if rows else ""


def context_used():
    line = next(line for line in screen.display[-2:] if "Context" in line)
    return int(line.split("·")[1].split("/")[0])


def compact_on(prefix, name):
    clear()
    send(f"/model {prefix}\r")
    check(wait_for(f"Model: {name}", 240), f"switches to {name}")
    pump(1)
    send("/temp 0.3\r")
    send("/tokens 60\r")
    clear()
    check(ask("My name is Ada and I live in Berlin."), "first turn")
    check(ask("I have two cats named Tom and Kit."), "second turn")
    check(ask("I also have a dog named Rex."), "third turn")
    before = context_used()
    send("/compact\r")
    check(wait_for("Context: ", 30), "/compact finishes")
    pump(2.5)
    show(f"{name}: compact")
    after = context_used()
    print("context:", before, "->", after, flush=True)
    check(0 < after < before, f"{name}: /compact frees context")
    check(ask("What are my cats called?"), "continues after /compact")
    show(f"{name}: after compact")
    reply = answer_after("What are my cats called?")
    check("Tom" in reply and "Kit" in reply, f"{name}: the answer after /compact knows the facts")


pump(1)
check(wait_for("Length", 60), "starts")
pump(1)
compact_on("LFM2", "LFM2-700M-Q4_K_M")
compact_on("granite", "granite-4.0-h-350m-Q4_K_M")
check(ask("Thanks for your help."), "another turn")
send("/compact\r")
check(wait_for("Context: ", 30), "/compact works a second time")
pump(2.5)

used = context_used()
send("/save testchat\r")
check(wait_for("Saved testchat", 5), "/save")
send("/save ../bad\r")
check(wait_for("Names may only", 5), "/save refuses bad names")
clear()
check(context_used() == 0, "context empty after /clear")
send("/load\r")
check(wait_for("testchat", 5), "/load lists chats")
send("/load testchat\r")
check(wait_for("Loaded testchat", 5), "/load restores the chat")
pump(2.5)
show("Loaded chat")
check(context_used() == used, f"context is restored ({used})")
check(ask("What city do I live in?"), "continues a loaded chat")
show("Continued chat")
send("/load nothing\r")
check(wait_for("No saved chat", 5), "/load reports missing chats")
files = subprocess.run(SSH + [f"ls {CONFIG}/chats; head -8 {CONFIG}/chats/testchat.md"], capture_output=True, text=True).stdout
print(files, flush=True)
check("testchat.json" in files and "testchat.md" in files, "JSON and Markdown exist")
check(files.count("Earlier in this conversation") == 1, "a second /compact does not nest the summary")

clear()
check(ask("Hello."), "single exchange")
send("/compact\r")
check(wait_for("Nothing to compact", 10), "/compact refuses a single exchange")

clear()
send("/download TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF\r")
check(wait_for("[y/N]", 40), "/download asks first")
check(has("probably too big"), "/download warns about big models")
show("Download warning")
send("n\r")
check(wait_for("Stopped.", 5), "/download can be declined")
clear()
send("/download ggml-org/tiny-llamas stories260K.gguf\r")
check(wait_for("[y/N]", 40), "/download finds the file")
check(not has("probably too big"), "no warning for a small model")
send("y\r")
check(wait_for("Downloaded.", 90), "/download finishes")
send("/model\r")
check(wait_for("stories260K", 5), "the new model appears under /model")
models = subprocess.run(SSH + [f"ls -la {MODELS} | grep -c part; rm -f {MODELS}/stories260K.gguf; ls {MODELS}"], capture_output=True, text=True).stdout
print(models, flush=True)
check(models.startswith("0") and "stories260K" not in models, "no leftovers")

send("/quit\r")
pump(2.5)
proc.wait(timeout=15)
print("\nfailures:", failures, flush=True)
sys.exit(1 if failures else 0)
