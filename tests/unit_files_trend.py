"""Runs on the Pi: @file attachments and the temperature trend, without a terminal."""
import io
import os
import sys
from pathlib import Path

os.environ["TINYLLM_CONFIG"] = "/tmp/tinyllm-unit"
sys.path.insert(0, os.path.expanduser(os.environ.get("TINYLLM_PI_DIR", "~/pi-projects/tinyllm")))

from rich.console import Console  # noqa: E402

import sysstats  # noqa: E402
import tinyllm_tui  # noqa: E402

failures = []


def check(condition, label):
    print(("ok   " if condition else "FAIL ") + label, flush=True)
    if not condition:
        failures.append(label)


def console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


folder = Path("/tmp/tinyllm-unit")
folder.mkdir(exist_ok=True)
(folder / "note.txt").write_text("The secret code is 4711.")
(folder / "big.txt").write_text("word " * 1400)
(folder / "binary.dat").write_bytes(b"ab\0cd")

out = console()
text = tinyllm_tui.attach_files(out, f"Read @{folder}/note.txt please")
check(text.startswith(f"Read @{folder}/note.txt please\n\nFile note.txt:\n```\nThe secret code is 4711.\n```"), "attaches a file")
check("Attached" in out.file.getvalue() and "24 characters" in out.file.getvalue(), "reports the attachment")

out = console()
text = tinyllm_tui.attach_files(out, f"@{folder}/big.txt")
check(len(text) < tinyllm_tui.FILE_LIMIT_CHARS + 200 and "shortened" in out.file.getvalue(), "shortens big files")

out = console()
check(tinyllm_tui.attach_files(out, f"@{folder}/binary.dat") is None, "refuses binary files")
check("not a text file" in out.file.getvalue(), "says why")

out = console()
check(tinyllm_tui.attach_files(out, "mail me at a@b.com") == "mail me at a@b.com", "ignores e-mail addresses")
check(out.file.getvalue() == "", "no note for e-mail addresses")

out = console()
check(tinyllm_tui.attach_files(out, "hello @nobody") == "hello @nobody", "keeps @words that are no file")
check("No file" in out.file.getvalue(), "notes the missing file")

out = console()
os.chdir(folder)
check("4711" in tinyllm_tui.attach_files(out, "@note.txt"), "relative paths use the current folder")
check("4711" in tinyllm_tui.attach_files(out, "@~/../../tmp/tinyllm-unit/note.txt"), "~ works")

stats = sysstats.SystemStats()
for second in (0, 2, 4, 9, 10, 21, 33):
    stats._measure(None, second)
check(len(stats.temps) == 4, f"one bar per 10 s ({len(stats.temps)} bars)")
for second in range(40, 400, 10):
    stats._measure(None, second)
check(len(stats.temps) == sysstats.TREND_SAMPLES, "keeps 12 bars")

print("failures:", failures)
sys.exit(1 if failures else 0)
