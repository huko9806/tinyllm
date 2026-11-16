#!/usr/bin/env python3
"""Tiny LLM: a terminal interface for language models on the Raspberry Pi."""
import argparse
import base64
import codecs
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, PathCompleter, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.table import Table
from rich.text import Text

import backends
import chats
import downloads
import ggufinfo
import preferences
import sysstats

BANNER_WIDE = """\
████████╗██╗███╗   ██╗██╗   ██╗    ██╗     ██╗     ███╗   ███╗
╚══██╔══╝██║████╗  ██║╚██╗ ██╔╝    ██║     ██║     ████╗ ████║
   ██║   ██║██╔██╗ ██║ ╚████╔╝     ██║     ██║     ██╔████╔██║
   ██║   ██║██║╚██╗██║  ╚██╔╝      ██║     ██║     ██║╚██╔╝██║
   ██║   ██║██║ ╚████║   ██║       ███████╗███████╗██║ ╚═╝ ██║
   ╚═╝   ╚═╝╚═╝  ╚═══╝   ╚═╝       ╚══════╝╚══════╝╚═╝     ╚═╝
""".splitlines()

BANNER_SMALL = r"""
 _____ ___ _  ___   __  _    _    __  __
|_   _|_ _| \| \ \ / / | |  | |  |  \/  |
  | |  | || .` |\ V /  | |__| |__| |\/| |
  |_| |___|_|\_| |_|   |____|____|_|  |_|
""".strip("\n").splitlines()

GRADIENT = ["#5fd7ff", "#5fafff", "#5f87ff", "#875fff", "#af5fff", "#d75fff"]
ACCENT = "#5fd7ff"

ANSI_RESET = "\033[0m"
ANSI_PROMPT = "\033[1;38;5;81m"
ANSI_DIM = "\033[2m"
ANSI_STATUS = "\033[38;5;244m"
ANSI_BOLD = "\033[1m"
ANSI_CODE = "\033[38;5;215m"
ANSI_HEADING = "\033[1;38;5;81m"
ANSI_SAVE_CURSOR = "\0337"
ANSI_RESTORE_CURSOR = "\0338"
ANSI_BULLET = "\033[38;5;81m●\033[0m "
ANSI_CURSOR = "\033[38;5;81m▌\033[0m"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"
INDENT = "  "
STATUS_INTERVAL_S = 2
STATUS_MIN_ROWS = 10
STATUS_ROWS = 2
CONTEXT_CELLS_MIN = 8
CONTEXT_CELLS_MAX = 24
# Fill level from which a colour applies: (percent, toolbar colour, terminal colour).
CONTEXT_LEVELS = ((90, "#ff5f5f", 203), (70, "#ffaf5f", 215), (0, ACCENT, 81))
CODE_FENCE = "```"
HEAT_LIMIT_C = 80
HEAT_COOL_C = 70
BENCH_TOKENS = 60
BENCH_PROMPT = "Explain in three sentences why the sky is blue."
CHAT_PREVIEW = 6
FILE_REFERENCE = re.compile(r"(?<!\S)@(\S+)")
FILE_LIMIT_CHARS = 6000
QUANTIZATION = re.compile(r"(I?Q\d\w*|BF16|F16|F32)$", re.IGNORECASE)
SERVE_PORT = 8080

# Command: (attribute, type, minimum, maximum, label); a maximum of None depends on the model.
SETTINGS = {
    "/temp": ("temp", float, 0.0, 2.0, "Temperature"),
    "/topk": ("top_k", int, 1, 100, "Top-K"),
    "/tokens": ("tokens", int, 1, None, "Length"),
}
COMMANDS = list(SETTINGS) + [
    "/model", "/think", "/system", "/compact", "/clear", "/save", "/load",
    "/bench", "/download", "/heat", "/retry", "/copy", "/info", "/help", "/quit",
    "/think on", "/think off", "/heat on", "/heat off", "/system off",
]


@dataclass
class State:
    temp: float
    top_k: int
    tokens: int
    backend: object = None
    stats: sysstats.SystemStats = field(default_factory=sysstats.SystemStats)
    prefs: preferences.Preferences = field(default_factory=preferences.Preferences)
    thinking: bool = True
    system: str = ""
    heat_guard: bool = True
    last_input: str = ""
    last_answer: str = ""

    @property
    def model_pid(self):
        process = self.backend.process
        return process.pid if process else None


class NullOutput:
    """Swallows an answer, for measurements."""

    def write(self, data):
        pass

    def write_thinking(self, data):
        pass

    def flush(self):
        pass


class StreamWriter:
    """Prints text as it arrives, wraps it at word boundaries and renders simple Markdown."""

    def __init__(self, width, markdown=False):
        self.width = width
        self.markdown = markdown
        self.line_start = True
        self.line_style = ""
        self.bold = False
        self.inline_code = False
        self.in_code_block = False
        self.code_header = False
        self.code_line = ""
        self.captured = []
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.word = ""
        self.pending_space = False
        self.column = 0
        self.color = ANSI_PROMPT
        self.cursor_visible = False
        self.thinking = False
        sys.stdout.write(ANSI_HIDE_CURSOR + ANSI_BULLET)

    def write(self, data):
        if self.thinking:
            self._end_thinking()
        self._add(data)

    def write_thinking(self, data):
        """Shows a model's reasoning, dimmed, before its answer."""
        if not self.thinking:
            self.thinking = True
            self.color = ANSI_DIM
        self._add(data)

    def _add(self, data):
        self._hide_typing_cursor()
        text = self.decoder.decode(data)
        if not self.thinking:
            self.captured.append(text)
        for char in text:
            if self.in_code_block:
                self._add_code_char(char)
            elif char == "\n":
                self._flush_word()
                if self.in_code_block:
                    self.code_header = False
                    continue
                self._newline()
                self.line_start = True
                self.line_style = ""
            elif char == " ":
                self._flush_word()
                self.pending_space = True
            else:
                self.word += char
        self._show_typing_cursor()

    def flush(self):
        sys.stdout.flush()

    def end_prompt(self):
        """Finishes the user's text; everything after this comes from the model."""
        self._hide_typing_cursor()
        self._flush_word()
        self.color = ""
        self._show_typing_cursor()
        self.flush()

    def finish(self):
        self._hide_typing_cursor()
        self._flush_word()
        if self.code_line:
            self._print_code_line(self.code_line)
        sys.stdout.write(ANSI_SHOW_CURSOR + "\n")
        self.flush()

    def _end_thinking(self):
        self._hide_typing_cursor()
        self._flush_word()
        self.thinking = False
        self.color = ""
        self._newline()
        self._newline()
        self.line_start = True

    def _add_code_char(self, char):
        if char != "\n":
            self.code_line += char
            return
        line, self.code_line = self.code_line, ""
        if self.code_header:
            self.code_header = False
        elif line.strip().startswith(CODE_FENCE):
            self.in_code_block = False
            self.line_start = True
        else:
            self._print_code_line(line)

    def _print_code_line(self, line):
        for start in range(0, max(len(line), 1), self.width):
            sys.stdout.write(f"{ANSI_CODE}{line[start:start + self.width]}{ANSI_RESET}")
            self._newline()

    def _flush_word(self):
        if not self.word:
            return
        word, self.word = self.word, ""
        rendered = self._render(word)
        if rendered is None:
            return
        text, visible = rendered
        self.line_start = False
        space = 1 if self.pending_space and self.column > 0 else 0
        if self.column + space + visible > self.width:
            self._newline()
        elif space:
            sys.stdout.write(" ")
            self.column += 1
        sys.stdout.write(text)
        self.column += visible
        self.pending_space = False

    def _render(self, word):
        """Returns (text for the terminal, visible length), or None for pure markup."""
        if not self.markdown or self.color:
            return f"{self.color}{word}{ANSI_RESET}", len(word)
        if self.line_start:
            if word.startswith(CODE_FENCE):
                self.in_code_block = True
                self.code_header = True
                return None
            if re.fullmatch(r"#{1,6}", word):
                self.line_style = ANSI_HEADING
                self.line_start = False
                return None
            if word in ("-", "*", "+"):
                word = "•"
        parts = []
        visible = 0
        for part in re.split(r"(\*\*|`)", word):
            if part == "**":
                self.bold = not self.bold
            elif part == "`":
                self.inline_code = not self.inline_code
            elif part:
                style = self.line_style + (ANSI_BOLD if self.bold else "") + (ANSI_CODE if self.inline_code else "")
                parts.append(f"{style}{part}{ANSI_RESET}")
                visible += len(part)
        return "".join(parts), visible

    def _newline(self):
        sys.stdout.write("\n" + INDENT)
        self.column = 0
        self.pending_space = False

    def _show_typing_cursor(self):
        sys.stdout.write(ANSI_CURSOR)
        self.cursor_visible = True

    def _hide_typing_cursor(self):
        if self.cursor_visible:
            sys.stdout.write("\b \b")
            self.cursor_visible = False


@contextlib.contextmanager
def no_echo():
    """Keeps key presses such as ^C from disturbing the running text."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    silent = termios.tcgetattr(fd)
    silent[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, silent)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


def context_line(state, width):
    """Draws how full the context window is: returns (bar, details, colour level)."""
    size = state.backend.context_size
    used = min(state.backend.context_used, size)
    percent = 100 * used // size
    cells = max(CONTEXT_CELLS_MIN, min(CONTEXT_CELLS_MAX, width - 36))
    filled = min(cells, max(1 if used else 0, round(cells * used / size)))
    details = f"{percent}% · {used}/{size}"
    if percent >= CONTEXT_LEVELS[0][0]:
        details += " · /compact"
    level = next(level for level in CONTEXT_LEVELS if percent >= level[0])
    return "█" * filled + "░" * (cells - filled), details[:max(0, width - cells - 10)], level


class LiveStatus:
    """Keeps the context bar and system readings on the bottom rows while an answer streams."""

    def __init__(self, state):
        self.state = state
        self.peak_temp = 0.0
        self.peak_model_mb = 0
        self.rows = 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        sys.stdout.flush()
        self.rows = os.get_terminal_size(sys.stdout.fileno()).lines
        if self.rows >= STATUS_MIN_ROWS:
            # The line feeds make sure the cursor is not on the rows that get reserved.
            self._send("\n" * STATUS_ROWS + f"\033[{STATUS_ROWS}A{self._set_region()}")
            self._draw()
            self.thread.start()
        return self

    def _note_peaks(self, reading):
        self.peak_temp = max(self.peak_temp, reading.temp_c)
        self.peak_model_mb = max(self.peak_model_mb, reading.model_mb or 0)

    def __exit__(self, *_):
        self._note_peaks(self.state.stats.read(self.state.model_pid))
        if self.thread.is_alive():
            self.done.set()
            self.thread.join(timeout=3)
            self._send(
                f"{ANSI_SAVE_CURSOR}\033[{self.rows - 1};1H\033[2K\033[{self.rows};1H\033[2K"
                f"\033[r{ANSI_RESTORE_CURSOR}"
            )

    def _run(self):
        while not self.done.wait(STATUS_INTERVAL_S):
            self._draw()

    def _draw(self):
        size = os.get_terminal_size(sys.stdout.fileno())
        region = ""
        if size.lines != self.rows:
            self.rows = size.lines
            region = self._set_region()
        reading = self.state.stats.read(self.state.model_pid)
        self._note_peaks(reading)
        line = sysstats.format_line(reading, size.columns - 1)
        bar, details, level = context_line(self.state, size.columns - 1)
        self._send(
            f"{region}{ANSI_SAVE_CURSOR}\033[{self.rows - 1};1H\033[2K"
            f"{ANSI_STATUS} Context \033[38;5;{level[2]}m{bar}{ANSI_STATUS} {details}"
            f"\033[{self.rows};1H\033[2K{line}{ANSI_RESET}{ANSI_RESTORE_CURSOR}"
        )

    def _set_region(self):
        return f"{ANSI_SAVE_CURSOR}\033[1;{self.rows - STATUS_ROWS}r{ANSI_RESTORE_CURSOR}"

    @staticmethod
    def _send(text):
        # Goes straight to the terminal, so it never mixes with the answer's buffered text.
        os.write(sys.stdout.fileno(), text.encode())


def show_banner(console):
    console.print()
    if console.width >= max(map(len, BANNER_WIDE)):
        lines = BANNER_WIDE
    elif console.width >= max(map(len, BANNER_SMALL)):
        lines = BANNER_SMALL
    else:
        lines = ["TINY LLM"]
    for index, line in enumerate(lines):
        console.print(Text(line, style=GRADIENT[min(index, len(GRADIENT) - 1)]), no_wrap=True, crop=True)


def show_help(console):
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column()
    table.add_row("/model", "List all models")
    table.add_row("/model 2", "Switch model, by number or name")
    table.add_row("/temp 0.8", "Randomness from 0 (always the same) to 2 (very wild)")
    table.add_row("/topk 40", "Candidates per token, 1 to 100")
    table.add_row("/tokens 200", "Maximum answer length in tokens, remembered per model")
    table.add_row("/think on|off", "Let models that can reason think before answering")
    table.add_row("/system text", "Set rules for the model, /system off removes them")
    table.add_row("/compact", "Free context: keep what you said and the latest exchange")
    table.add_row("/clear", "Clear the screen and forget the conversation")
    table.add_row("/save name", "Save the conversation as JSON and Markdown")
    table.add_row("/load name", "Continue a saved conversation, /load lists them")
    table.add_row("/bench", "Compare speed, RAM and heat of all models")
    table.add_row("/download repo", "Get a GGUF model from Hugging Face")
    table.add_row("/heat on|off", "Use fewer cores after an answer above 80°C")
    table.add_row("/retry", "Answer the last message again")
    table.add_row("/copy", "Copy the last answer, also saved as a file")
    table.add_row("/info", "Facts and measurements of the current model")
    table.add_row("@path", "Add a text file to your message, e.g. @notes.txt")
    table.add_row("Alt+Enter", "New line inside a message")
    table.add_row("/quit", "Quit, Ctrl+D works too")
    table.add_row("Ctrl+C", "Stop the running answer")
    console.print(table)
    console.print(f"  New models: put a GGUF file in [{ACCENT}]{backends.MODELS_DIR}[/{ACCENT}]")


def change_setting(console, state, command, parts):
    attribute, convert, low, high, label = SETTINGS[command]
    high = high if high is not None else state.backend.max_tokens
    try:
        value = convert(parts[1].replace(",", "."))
    except (IndexError, ValueError):
        console.print(f"  [red]Example:[/red] {command} {getattr(state, attribute)}")
        return
    if not low <= value <= high:
        console.print(f"  [red]{label} must be between {low} and {high}.[/red]")
        return
    setattr(state, attribute, value)
    if attribute == "tokens":
        state.prefs.remember_length(state.backend.name, value)
    console.print(f"  [{ACCENT}]{label} = {value}[/{ACCENT}]")


def find_model(query, names):
    """Finds a model by number, name or unique start of its name."""
    if query.isdigit() and 1 <= int(query) <= len(names):
        return names[int(query) - 1]
    lowered = query.lower()
    exact = [name for name in names if name.lower() == lowered]
    matches = exact or [name for name in names if name.lower().startswith(lowered)]
    return matches[0] if len(matches) == 1 else None


def list_models(console, state, names):
    for number, name in enumerate(names, start=1):
        marker = f"[{ACCENT}]●[/{ACCENT}]" if name == state.backend.name else " "
        console.print(f"  {number} {marker} {escape(name)}")


def switch_model(console, state, name):
    if name == state.backend.name:
        console.print(f"  [{ACCENT}]{escape(name)} is already running.[/{ACCENT}]")
        return
    load_model(console, state, name)
    state.prefs.set("last_model", state.backend.name)
    console.print(f"  [{ACCENT}]Model: {escape(state.backend.name)}[/{ACCENT}]")


def load_model(console, state, name, fallback=True):
    """Loads a model into state; the current one is closed first, two do not fit in the RAM.

    When loading fails, the error is shown and TinyStories starts instead, so that the program
    stays usable. Without fallback, or when TinyStories itself fails, the error is raised.
    Returns True when the wanted model runs.
    """
    if state.backend is not None:
        state.backend.close()
    try:
        with console.status(f"Loading {escape(name)} ..."):
            state.backend = backends.load_model(name)
    except backends.BackendError as failure:
        if not fallback or name == backends.TINYSTORIES:
            raise
        console.print(f"  [red]{escape(str(failure))}[/red]")
        with console.status(f"Loading {backends.TINYSTORIES} ..."):
            state.backend = backends.load_model(backends.TINYSTORIES)
        apply_model_length(state)
        return False
    apply_model_length(state)
    return True


def apply_model_length(state):
    state.tokens = min(state.prefs.length_for(state.backend.name), state.backend.max_tokens)


def handle_model_command(console, state, parts):
    names = backends.model_names()
    if len(parts) == 1:
        list_models(console, state, names)
        return
    name = find_model(" ".join(parts[1:]), names)
    if name is None:
        console.print("  [red]No matching model.[/red] /model lists them all.")
        return
    switch_model(console, state, name)


def stream_reply(console, state, generate, echo=None):
    """Streams one model reply with the live status; returns the peak temperature, or None if it failed."""
    backend = state.backend
    console.print()
    interrupted = False
    error = None
    status = LiveStatus(state)
    with no_echo(), status:
        writer = StreamWriter(min(console.width - len(INDENT) - 2, 88), markdown=not backend.continues_text)
        try:
            if echo:
                writer.write(echo.encode())
            writer.end_prompt()
            count, speed = generate(writer)
        except KeyboardInterrupt:
            interrupted = True
        except backends.BackendError as failure:
            error = failure
        finally:
            writer.finish()

    if interrupted:
        with console.status("Stopping ..."):
            backend.stop()
        console.print("  [yellow]Stopped.[/yellow]")
        return None
    if error:
        console.print(f"  [red]{escape(str(error))}[/red]")
        return None
    state.last_answer = "".join(writer.captured).strip()
    console.print(
        f"\n  [dim]{count} tokens · {speed:.1f} tokens/s · temperature {state.temp}"
        f" · peak CPU {status.peak_temp:.0f}°C[/dim]"
    )
    return status.peak_temp


def answer(console, state, text):
    backend = state.backend
    state.last_input = text
    peak = stream_reply(
        console, state,
        lambda output: backend.generate(text, state, output),
        echo=text if backend.continues_text else None,
    )
    if peak is not None:
        guard_heat(console, state, peak)


def guard_heat(console, state, peak):
    """Adjusts the number of cores after an answer; the model only reads it when loading."""
    backend = state.backend
    if backend.continues_text or not state.heat_guard:
        return
    threads = backend.threads
    if peak >= HEAT_LIMIT_C and threads > backends.LLAMA_THREADS_MIN:
        threads -= 1
    elif peak < HEAT_COOL_C and threads < backends.LLAMA_THREADS_MAX:
        threads += 1
    if threads == backend.threads:
        return
    try:
        with console.status(f"Peak {peak:.0f}°C: reloading with {threads} cores ..."):
            backend.set_threads(threads)
    except backends.BackendError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        load_model(console, state, backends.TINYSTORIES)
        return
    console.print(f"  [yellow]Peak {peak:.0f}°C: now using {threads} cores.[/yellow]")


def handle_toggle(console, state, parts, attribute, label):
    if len(parts) == 2 and parts[1] in ("on", "off"):
        value = parts[1] == "on"
        setattr(state, attribute, value)
        state.prefs.set(attribute, value)
    console.print(f"  [{ACCENT}]{label}: {'on' if getattr(state, attribute) else 'off'}[/{ACCENT}]")


def handle_heat(console, state, parts):
    cores = getattr(state.backend, "threads", 1)
    handle_toggle(console, state, parts, "heat_guard", f"Heat guard ({cores} cores in use)")


def handle_system(console, state, text):
    rules = text[len("/system"):].strip()
    if rules:
        state.system = "" if rules == "off" else rules
        state.prefs.set("system", state.system)
    if state.backend.continues_text and state.system:
        console.print("  [yellow]TinyStories only continues text and ignores these rules.[/yellow]")
    console.print(f"  [{ACCENT}]System rules: {escape(state.system) or 'none'}[/{ACCENT}]")


def handle_compact(console, state):
    backend = state.backend
    if backend.continues_text or not backend.messages:
        console.print("  [red]There is no conversation to compact.[/red]")
        return
    before = backend.context_used
    try:
        backend.compact()
    except backends.BackendError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return
    console.print(f"  [{ACCENT}]Context: {before} → {backend.context_used} tokens[/{ACCENT}]")


def handle_save(console, state, parts):
    backend = state.backend
    if backend.continues_text or not backend.messages:
        console.print("  [red]There is no conversation to save.[/red]")
        return
    name = parts[1] if len(parts) > 1 else time.strftime("chat-%Y-%m-%d-%H%M")
    try:
        path = chats.save(name, backend.name, backend.messages, backend.context_used, state.system)
    except (chats.ChatError, OSError) as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return
    console.print(f"  [{ACCENT}]Saved {escape(name)}[/{ACCENT}] [dim]{escape(str(path.with_suffix('.md')))}[/dim]")


def handle_load(console, state, parts):
    backend = state.backend
    if len(parts) == 1:
        names = chats.names()
        for name in names:
            console.print(f"  {escape(name)}")
        if not names:
            console.print("  [dim]No saved chats yet.[/dim]")
        return
    if backend.continues_text:
        console.print("  [red]TinyStories cannot hold a conversation, switch model first.[/red]")
        return
    try:
        data = chats.load(parts[1])
    except chats.ChatError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return
    backend.messages = data["messages"]
    backend.context_used = data.get("context_used", 0)
    state.system = data.get("system", "")
    shown = backend.messages[-CHAT_PREVIEW:]
    if len(backend.messages) > len(shown):
        console.print(f"  [dim]... {len(backend.messages) - len(shown)} earlier messages[/dim]")
    for message in shown:
        marker = "❯" if message["role"] == "user" else "●"
        console.print(f"  [{ACCENT}]{marker}[/{ACCENT}] {escape(message['content'])}")
    console.print(f"  [{ACCENT}]Loaded {escape(parts[1])}[/{ACCENT}] [dim]saved with {escape(data['model'])}[/dim]")


def bench_model(console, state, name, table):
    state.backend.close()  # before the clock starts: closing the previous model is not part of loading
    start = time.monotonic()
    loaded = load_model(console, state, name)
    load_s = time.monotonic() - start
    if not loaded:
        table.add_row(escape(name), "failed", "-", "-", "-")
        return
    console.print(f"  Testing {escape(name)} ...")
    options = replace(state, tokens=BENCH_TOKENS, temp=0.3, thinking=False, system="")
    try:
        with no_echo(), LiveStatus(state) as status:
            _, speed = state.backend.generate(BENCH_PROMPT, options, NullOutput())
    except backends.BackendError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        table.add_row(escape(name), "failed", "-", "-", "-")
        return
    table.add_row(
        escape(name), f"{load_s:.0f} s", f"{speed:.1f}", f"{status.peak_model_mb} MB", f"{status.peak_temp:.0f}°C"
    )
    measured = {
        "tokens_per_s": round(speed, 1), "ram_mb": status.peak_model_mb,
        "peak_c": round(status.peak_temp), "load_s": round(load_s), "date": time.strftime("%Y-%m-%d"),
    }
    state.prefs.set("bench", {**state.prefs.get("bench"), name: measured})


def run_bench(console, state):
    original = state.backend.name
    history = list(state.backend.messages) if not state.backend.continues_text else None
    used = state.backend.context_used
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("Model", style=ACCENT, no_wrap=True)
    for heading in ("Load", "Tokens/s", "RAM", "Peak"):
        table.add_column(heading, justify="right")
    try:
        for name in backends.model_names():
            bench_model(console, state, name, table)
    except KeyboardInterrupt:
        console.print("  [yellow]Stopped.[/yellow]")
    console.print(table)

    if load_model(console, state, original) and history is not None:
        state.backend.messages = history
        state.backend.context_used = used
    console.print(f"  [{ACCENT}]Model: {escape(state.backend.name)}[/{ACCENT}]")


def confirm(console, question):
    try:
        return console.input(f"  {question} \\[y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def handle_download(console, state, parts):
    if len(parts) < 2:
        console.print("  [red]Example:[/red] /download bartowski/SmolLM2-360M-Instruct-GGUF")
        return
    repo = parts[1]
    try:
        name, size = downloads.pick(repo, parts[2] if len(parts) > 2 else None)
        downloads.check_space(name, size)
    except downloads.DownloadError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return

    console.print(f"  {escape(name)} · {size // 1000000} MB")
    if downloads.too_big_for_ram(size, state.stats.read().ram_total_mb):
        console.print("  [yellow]This is probably too big for the RAM of this Pi and would be very slow.[/yellow]")
    if not confirm(console, "Download?"):
        console.print("  [yellow]Stopped.[/yellow]")
        return

    columns = (TextColumn("  "), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    try:
        with Progress(*columns, console=console, transient=True) as progress:
            task = progress.add_task("download", total=size)
            downloads.fetch(repo, name, lambda count: progress.advance(task, count))
    except KeyboardInterrupt:
        console.print("  [yellow]Stopped.[/yellow]")
        return
    except downloads.DownloadError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return
    console.print(f"  [{ACCENT}]Downloaded. /model lists it now.[/{ACCENT}]")


def handle_retry(console, state):
    backend = state.backend
    try:
        text = state.last_input if backend.continues_text else backend.forget_last_exchange()
    except backends.BackendError as failure:
        console.print(f"  [red]{escape(str(failure))}[/red]")
        return
    if not text:
        console.print("  [red]There is no answer to retry yet.[/red]")
        return
    # Only the start of the first line; attached files and further lines are sent but not shown again.
    shown = text.splitlines()[0][:70]
    if len(shown) < len(text.rstrip()):
        shown += " ..."
    console.print(f"  [dim]❯ {escape(shown)}[/dim]")
    answer(console, state, text)


def attach_files(console, text):
    """Adds the content of files named as @path; returns the new text, or None after an error."""
    attachments = []
    for match in FILE_REFERENCE.finditer(text):
        path = Path(match.group(1)).expanduser()
        if not path.is_file():
            console.print(f"  [dim]No file {escape(str(path))}, @{escape(match.group(1))} is sent as text.[/dim]")
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError as failure:
            console.print(f"  [red]{escape(str(failure))}[/red]")
            return None
        if "\0" in content:
            console.print(f"  [red]{escape(str(path))} is not a text file.[/red]")
            return None
        shortened = len(content) > FILE_LIMIT_CHARS
        content = content[:FILE_LIMIT_CHARS]
        attachments.append(f"File {path.name}:\n```\n{content}\n```")
        note = ", shortened" if shortened else ""
        console.print(f"  [dim]Attached {escape(str(path))} ({len(content)} characters{note})[/dim]")
    if not attachments:
        return text
    return text + "\n\n" + "\n\n".join(attachments)


def handle_copy(console, state):
    if not state.last_answer:
        console.print("  [red]There is no answer to copy yet.[/red]")
        return
    # OSC 52 asks the terminal on the PC to fill its clipboard, also across SSH.
    encoded = base64.b64encode(state.last_answer.encode()).decode()
    os.write(sys.stdout.fileno(), f"\033]52;c;{encoded}\a".encode())
    path = preferences.CONFIG_DIR / "last-answer.txt"
    path.write_text(state.last_answer + "\n")
    console.print(
        f"  [{ACCENT}]Sent {len(state.last_answer)} characters to the clipboard.[/{ACCENT}]"
        f" [dim]Not every terminal accepts this; the text is also in {escape(str(path))}[/dim]"
    )


def handle_info(console, state):
    backend = state.backend
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column()
    if backend.continues_text:
        model_file = backends.TINYSTORIES_DIR / "model.bin"
        try:
            meta = json.loads((backends.TINYSTORIES_DIR / "metadata.json").read_text())
            parameters = f"{meta['parameters']['total'] / 1e6:.1f}M"
            weights = meta["quantization"]["weights"]
            license_name = meta["weights_license"]
        except (OSError, ValueError, KeyError, TypeError):
            parameters = weights = license_name = "?"
        table.add_row("Name", "TinyStories")
        table.add_row("Architecture", "PLE, runs in tinystories_pi")
        table.add_row("Parameters", escape(parameters))
        table.add_row("Quantization", escape(weights))
        table.add_row("License", f"{escape(license_name)}, see {escape(str(backends.TINYSTORIES_DIR / 'Licence'))}")
        table.add_row("Context", f"{backend.context_size} tokens")
        table.add_row("Cores", "1")
    else:
        model_file = backend.path
        try:
            meta = ggufinfo.read_metadata(model_file)
        except (OSError, ValueError) as failure:
            console.print(f"  [red]{escape(str(failure))}[/red]")
            meta = {}
        architecture = meta.get("general.architecture", "?")
        quantization = QUANTIZATION.search(backend.name)
        trained = meta.get(f"{architecture}.context_length")
        table.add_row("Name", escape(str(meta.get("general.name", backend.name))))
        table.add_row("Architecture", escape(str(architecture)))
        table.add_row("Parameters", escape(str(meta.get("general.size_label", "?"))))
        table.add_row("Quantization", quantization.group(1) if quantization else "?")
        license_name = meta.get("general.license", "not stated in the file")
        if license_name == "other":
            license_name = meta.get("general.license.name", license_name)
        table.add_row("License", escape(str(license_name)))
        table.add_row("Context", f"{backend.context_size} tokens here" + (f", trained for {trained}" if trained else ""))
        table.add_row("Cores", str(backend.threads))
    table.add_row("File", f"{model_file.stat().st_size // 1000000} MB")
    table.add_row("RAM now", f"{state.stats.read(state.model_pid).model_mb or 0} MB")
    measured = state.prefs.get("bench").get(backend.name)
    if measured:
        table.add_row(
            "Measured",
            f"{measured['tokens_per_s']} tokens/s · {measured['ram_mb']} MB · peak {measured['peak_c']}°C"
            f" · loads in {measured['load_s']} s ({measured['date']}, /bench)",
        )
    else:
        table.add_row("Measured", "not yet, /bench measures all models")
    console.print(table)


class InputCompleter(Completer):
    """Completes commands after / and file paths after @."""

    def __init__(self):
        self.paths = PathCompleter(expanduser=True)

    def get_completions(self, document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            yield from self.paths.get_completions(Document(word[1:]), complete_event)
        elif document.text.startswith("/"):
            yield from WordCompleter(completions(), sentence=True).get_completions(document, complete_event)


def serve(args, prefs):
    """Offers one model with llama-server's web interface to the home network."""
    names = backends.model_names()
    name = find_model(args.model, names) if args.model else prefs.get("last_model")
    if name not in names or name == backends.TINYSTORIES:
        sys.exit("--serve needs a GGUF model, for example: tinyllm --serve --model Qwen3")
    with socket.socket() as probe:
        # Like llama-server: connections that just ended do not block the port, a running server does.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", args.port))
        except OSError:
            sys.exit(f"Port {args.port} is already in use, choose another one with --port.")
    print(f"Loading {name} ...", flush=True)
    try:
        server = backends.LlamaServer(backends.MODELS_DIR / f"{name}.gguf", host="0.0.0.0", port=args.port, webui=True)
    except backends.BackendError as failure:
        sys.exit(str(failure))
    except KeyboardInterrupt:
        sys.exit("Stopped.")
    address = (subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split() or ["localhost"])[0]
    print(f"\nServing {name} to everyone in this network:")
    print(f"  http://{address}:{args.port}")
    print("Ctrl+C stops it.\n", flush=True)
    stats = sysstats.SystemStats()
    try:
        while server.process.poll() is None:
            width = os.get_terminal_size(sys.stdout.fileno()).columns - 1
            line = sysstats.format_line(stats.read(server.process.pid), width)
            sys.stdout.write(f"\r\033[2K{line}")
            sys.stdout.flush()
            time.sleep(STATUS_INTERVAL_S)
        print(f"\nThe model server stopped, see {backends.LLAMA_LOG}")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.close()


def completions():
    return (
        COMMANDS
        + [f"/model {name}" for name in backends.model_names()]
        + [f"/load {name}" for name in chats.names()]
    )


def run(console, state):
    style = Style.from_dict({"prompt": "#5fd7ff bold", "bottom-toolbar": "noreverse #808080"})
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _new_line(event):
        event.current_buffer.insert_text("\n")

    session = PromptSession(
        completer=InputCompleter(),
        history=FileHistory(str(preferences.HISTORY_FILE)),
        key_bindings=bindings,
        prompt_continuation="  ",
        style=style,
    )

    def menu_wanted():
        # The menu reserves space and would otherwise push the banner off the screen.
        document = session.default_buffer.document
        return document.text.startswith("/") or document.get_word_before_cursor(WORD=True).startswith("@")

    session.complete_while_typing = Condition(menu_wanted)

    def toolbar():
        width = console.width - 1
        readings = sysstats.format_line(state.stats.read(state.model_pid), width)
        bar, details, level = context_line(state, width)
        return HTML(
            " Model <b>{}</b> │ Temp <b>{}</b> │ Top-K <b>{}</b> │ Length <b>{}</b>{} │ /help\n"
            ' Context <style fg="{}">{}</style> {}\n{}'
        ).format(
            state.backend.name, state.temp, state.top_k, state.tokens,
            "" if state.thinking else " │ No thinking", level[1], bar, details, readings,
        )

    while True:
        console.print()
        console.rule(style="grey37")
        placeholder = HTML('<style fg="#6c6c6c">{}</style>').format(state.backend.hint)
        try:
            text = session.prompt(
                [("class:prompt", "❯ ")],
                bottom_toolbar=toolbar,
                placeholder=placeholder,
                refresh_interval=STATUS_INTERVAL_S,
            ).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            return

        if not text:
            continue
        parts = text.split()
        command = parts[0]
        if command in SETTINGS:
            change_setting(console, state, command, parts)
        elif command == "/model":
            handle_model_command(console, state, parts)
        elif command == "/think":
            handle_toggle(console, state, parts, "thinking", "Thinking")
        elif command == "/heat":
            handle_heat(console, state, parts)
        elif command == "/system":
            handle_system(console, state, text)
        elif command == "/compact":
            handle_compact(console, state)
        elif command == "/save":
            handle_save(console, state, parts)
        elif command == "/load":
            handle_load(console, state, parts)
        elif command == "/bench":
            run_bench(console, state)
        elif command == "/download":
            handle_download(console, state, parts)
        elif command == "/retry":
            handle_retry(console, state)
        elif command == "/copy":
            handle_copy(console, state)
        elif command == "/info":
            handle_info(console, state)
        elif command == "/clear":
            state.backend.reset()
            console.clear()
            show_banner(console)
        elif command == "/help":
            show_help(console)
        elif command in ("/q", "/quit"):
            return
        elif command.startswith("/"):
            console.print(f"  [red]Unknown command {escape(command)}.[/red] /help lists all commands.")
        else:
            message = attach_files(console, text)
            if message is not None:
                answer(console, state, message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="model to load at startup, default: the last one used")
    parser.add_argument("--temp", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--tokens", type=int, default=None, help="answer length, default: remembered per model")
    parser.add_argument("--serve", action="store_true", help="offer the model with a web interface in the network")
    parser.add_argument("--port", type=int, default=SERVE_PORT, help="port for --serve")
    args = parser.parse_args()
    # The same limits as the /temp and /topk commands; the answer length depends on the model.
    for attribute, _, low, high, label in SETTINGS.values():
        if high is not None and not low <= getattr(args, attribute) <= high:
            parser.error(f"{label} must be between {low} and {high}")

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Tiny LLM needs a terminal.")

    prefs = preferences.Preferences()
    if args.serve:
        serve(args, prefs)
        return
    names = backends.model_names()
    if args.model:
        name = find_model(args.model, names)
        if name is None:
            sys.exit(f"No matching model: {args.model}")
    else:
        name = prefs.get("last_model") if prefs.get("last_model") in names else backends.TINYSTORIES

    console = Console(highlight=False)
    state = State(args.temp, args.top_k, 0, prefs=prefs)
    state.thinking = prefs.get("thinking")
    state.system = prefs.get("system")
    state.heat_guard = prefs.get("heat_guard")
    try:
        # A remembered model that no longer works gives way to TinyStories; one named with --model does not.
        load_model(console, state, name, fallback=not args.model)
    except backends.BackendError as failure:
        sys.exit(str(failure))
    prefs.set("last_model", state.backend.name)
    if args.tokens:
        state.tokens = min(args.tokens, state.backend.max_tokens)
    try:
        show_banner(console)
        run(console, state)
    finally:
        state.backend.close()
    console.print(f"[{ACCENT}]Bye![/{ACCENT}]")


if __name__ == "__main__":
    main()
