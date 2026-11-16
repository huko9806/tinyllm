#!/usr/bin/env python3
"""Checks that Tiny LLM starts with the model used last."""
import os
import select
import subprocess
import sys
import time

import pyte

COLS, ROWS = 100, 30
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
proc = screen = stream = None


class Screen(pyte.Screen):
    def write_process_input(self, data):
        proc.stdin.write(data.encode())


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


def toolbar_model():
    line = screen.display[-3]
    return line.split("Model ", 1)[1].split(" │")[0].strip() if "Model " in line else None


def session(arguments="", then=None):
    """Starts Tiny LLM, returns the model in the toolbar, runs an optional command and quits."""
    global proc, screen, stream
    proc = subprocess.Popen(
        ["ssh", "-tt", "-o", "BatchMode=yes", "pi",
         f"stty cols {COLS} rows {ROWS}; export TERM=xterm-256color TINYLLM_CONFIG={CONFIG}; "
         f"exec bash -lc 'tinyllm {arguments}'"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
    )
    screen = Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    wait_for("Length", 120)
    pump(1)
    model = toolbar_model()
    error = has("could not be loaded")
    if then:
        proc.stdin.write(then.encode())
        wait_for("Model: ", 120)
        pump(1)
    proc.stdin.write(b"/quit\r")
    pump(2.5)
    proc.wait(timeout=15)
    return model, error


def remote(command):
    return subprocess.run(SSH + [command], capture_output=True, text=True).stdout


remote(f"rm -rf {CONFIG}")
model, _ = session(then="/model granite\r")
check(model == "tinystories", f"first start uses TinyStories ({model})")
model, _ = session()
check(model == "granite-4.0-h-350m-Q4_K_M", f"next start uses the last model ({model})")
model, _ = session("--model SmolLM2")
check(model == "SmolLM2-135M-Instruct-Q4_K_M", f"--model still works ({model})")
model, _ = session()
check(model == "SmolLM2-135M-Instruct-Q4_K_M", f"a model chosen with --model is remembered too ({model})")

set_last = "python3 -c \"import json; p='" + CONFIG + "/settings.json'; d=json.load(open(p)); d['last_model']='{}'; json.dump(d, open(p, 'w'))\""
remote(set_last.format("gone-model"))
model, error = session()
check(model == "tinystories" and not error, f"a deleted model falls back quietly ({model})")

remote(f"echo not-a-model > {MODELS}/zzz-broken.gguf; " + set_last.format("zzz-broken"))
model, error = session()
check(model == "tinystories" and error, f"a broken model shows an error and falls back ({model})")
print(remote(f"rm -f {MODELS}/zzz-broken.gguf; cat {CONFIG}/settings.json; rm -rf {CONFIG}; pgrep -c llama-server"), flush=True)
print("failures:", failures, flush=True)
sys.exit(1 if failures else 0)
