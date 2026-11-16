"""Runs on the Pi: Markdown rendering and the heat guard, without a terminal."""
import io
import os
import re
import shutil
import sys

os.environ.setdefault("TINYLLM_CONFIG", "/tmp/tinyllm-unit")  # never the real settings
# The Tiny LLM folder: TINYLLM_PI_DIR, or the folder the tinyllm command points to.
command = shutil.which("tinyllm") or os.path.expanduser("~/.local/bin/tinyllm")
sys.path.insert(0, os.path.expanduser(os.environ.get("TINYLLM_PI_DIR") or os.path.dirname(os.path.realpath(command))))
import backends  # noqa: E402
import tinyllm_tui as tui  # noqa: E402
from rich.console import Console  # noqa: E402

real_stdout = sys.stdout
failures = []


def check(condition, label):
    print(("ok   " if condition else "FAIL ") + label, file=real_stdout, flush=True)
    if not condition:
        failures.append(label)


def render(text, markdown=True):
    sys.stdout = io.StringIO()
    writer = tui.StreamWriter(60, markdown=markdown)
    writer.end_prompt()
    for char in text:
        writer.write(char.encode())
    writer.finish()
    raw = sys.stdout.getvalue()
    sys.stdout = real_stdout
    return raw, re.sub(r"\033\[[?0-9;]*[A-Za-z]", "", raw).replace("\b \b", "").replace("▌", "")


raw, plain = render(
    "# Title here\nSome **bold words** and `code` end.\n- first\n- second\n"
    "```python\nx  =  1\n  y = 2\n```\nAfter the block.\n"
)
print(plain, file=real_stdout)
check("**" not in plain and "`" not in plain and "#" not in plain, "markup characters are removed")
check("python" not in plain, "code block language is hidden")
check("• first" in plain and "• second" in plain, "list items get bullets")
check("x  =  1" in plain and "  y = 2" in plain, "code keeps its spaces")
check(tui.ANSI_HEADING + "Title" in raw, "heading is styled")
check(tui.ANSI_BOLD + "bold" in raw and tui.ANSI_BOLD + "words" in raw, "bold spans several words")
check(tui.ANSI_CODE + "code" in raw, "inline code is styled")
check("After the block." in plain, "normal text continues after the code block")

raw, plain = render("A **story** with `marks`.", markdown=False)
check("**story**" in plain, "no Markdown when it is switched off")

raw, plain = render("```\nunfinished code")
check("unfinished code" in plain, "an unfinished code block is still printed")

console = Console(file=io.StringIO())
state = tui.State(0.3, 40, 20)
state.backend = backends.load_model("SmolLM2-135M-Instruct-Q4_K_M")
backend = state.backend
backend.messages.extend([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])


def cores():
    with open(f"/proc/{backend.process.pid}/cmdline", "rb") as cmdline:
        arguments = cmdline.read().split(b"\0")
    return int(arguments[arguments.index(b"--threads") + 1])


check(cores() == 4, "starts with 4 cores")
tui.guard_heat(console, state, 85.0)
check(backend.threads == 3 and cores() == 3, "too hot: reloads with 3 cores")
check(len(backend.messages) == 2, "conversation survives the reload")
count, _ = backend.generate("Say hi.", state, tui.NullOutput())
check(count > 0, "answers after the reload")
tui.guard_heat(console, state, 75.0)
check(backend.threads == 3, "stays at 3 cores between the limits")
tui.guard_heat(console, state, 60.0)
check(backend.threads == 4 and cores() == 4, "cool again: back to 4 cores")
state.heat_guard = False
tui.guard_heat(console, state, 85.0)
check(backend.threads == 4, "guard off: no change")
state.heat_guard = True
backend.set_threads(2)
tui.guard_heat(console, state, 85.0)
check(backend.threads == 2, "never fewer than 2 cores")
backend.close()
print("failures:", failures, file=real_stdout)
sys.exit(1 if failures else 0)
