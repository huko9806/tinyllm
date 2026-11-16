#!/usr/bin/env python3
"""tinyllm --serve: start, use from this PC over the network, port in use, Ctrl+C."""
import atexit
import gzip
import json
import os
import select
import subprocess
import sys
import time
import urllib.request

CONFIG = "/tmp/tinyllm-test"
PORT = 8090
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pi"]
# The Pi's address in the network, found the same way tinyllm --serve finds it.
ADDRESS = subprocess.run(SSH + ["hostname -I"], capture_output=True, text=True, check=True).stdout.split()[0]
failures = []


def check(condition, label):
    print(("ok   " if condition else "FAIL ") + label, flush=True)
    if not condition:
        failures.append(label)


started = []
atexit.register(lambda: [proc.kill() for proc in started])


def start(arguments):
    started.append(subprocess.Popen(
        ["ssh", "-tt", "-o", "BatchMode=yes", "pi",
         f"stty cols 100 rows 30; export TERM=xterm-256color TINYLLM_CONFIG={CONFIG}; exec bash -lc 'tinyllm {arguments}'"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
    ))
    return started[-1]


def read_until(proc, text, timeout):
    received = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            received += data
            if text.encode() in received:
                break
    return received.decode(errors="replace")


subprocess.run(SSH + [f"rm -rf {CONFIG}"], check=True)
output = read_until(start("--serve"), "needs", 30)
check("--serve needs a GGUF model" in output, "--serve refuses TinyStories")

server = start(f"--serve --model SmolLM2 --port {PORT}")
output = read_until(server, "Ctrl+C", 120)
print(output, flush=True)
base = f"http://{ADDRESS}:{PORT}"
check(base in output, "prints the address in the network")
with urllib.request.urlopen(base + "/health", timeout=10) as response:
    check(response.status == 200, "health from this PC")
try:
    # Browsers always accept gzip; llama-server only sends the page packed.
    page_request = urllib.request.Request(base + "/", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(page_request, timeout=10) as response:
        page = gzip.decompress(response.read()) if response.headers.get("Content-Encoding") == "gzip" else response.read()
        check(response.status == 200 and b"<html" in page.lower(), f"web interface is served ({len(page)} bytes)")
except Exception as failure:  # noqa: BLE001 - the remaining checks still clean up
    check(False, f"web interface is served ({failure})")
request = urllib.request.Request(
    base + "/v1/chat/completions",
    json.dumps({"messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 20}).encode(),
    {"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=60) as response:
    reply = json.loads(response.read())["choices"][0]["message"]["content"]
    print("reply:", repr(reply), flush=True)
    check(bool(reply.strip()), "chat answer over the network")
status_line = read_until(server, "RAM", 6)
check("RAM" in status_line and "Model" in status_line, "shows live readings while serving")

second = start(f"--serve --model SmolLM2 --port {PORT}")
output = read_until(second, "in use", 30)
check(f"Port {PORT} is already in use" in output, "refuses a port in use")

server.stdin.write(b"\x03")
output = read_until(server, "Stopped.", 20)
check("Stopped." in output, "Ctrl+C stops")
time.sleep(2)
left = subprocess.run(SSH + ["pgrep -c llama-server"], capture_output=True, text=True).stdout.strip()
check(left == "0", f"no model server left ({left})")

# A lost connection: the SSH client disappears without Ctrl+C.
server = start(f"--serve --model SmolLM2 --port {PORT}")
output = read_until(server, "Ctrl+C", 120)
check("Ctrl+C" in output, "serves again right after stopping")
if "Ctrl+C" not in output:
    print(output, flush=True)
running = subprocess.run(SSH + ["pgrep -c llama-server"], capture_output=True, text=True).stdout.strip()
check(running == "1", f"the model server runs before the connection is lost ({running})")
server.kill()
time.sleep(4)
left = subprocess.run(SSH + ["pgrep -c llama-server"], capture_output=True, text=True).stdout.strip()
check(left == "0", f"a lost connection leaves no model server ({left})")

# The same for a chat in the normal terminal interface.
chat = start("--model SmolLM2")
check("Message to the model" in read_until(chat, "Message to the model", 120), "chat starts")
chat.kill()
time.sleep(4)
left = subprocess.run(SSH + ["pgrep -c llama-server; pgrep -c tinystories_pi"], capture_output=True, text=True).stdout.split()
check(left == ["0", "0"], f"a lost connection leaves no model behind ({left})")
subprocess.run(SSH + [f"rm -rf {CONFIG}"])
print("\nfailures:", failures, flush=True)
sys.exit(1 if failures else 0)
