"""The language models Tiny LLM can drive.

Every model offers the same interface:
  generate(text, options, output)  writes the answer to output, returns (tokens, tokens/s)
  stop()                           cleans up after an interrupted answer
  reset()                          forgets the conversation so far
  close()                          frees the memory

New models: put a GGUF file in models/ and it shows up under /model.
"""
import ctypes
import http.client
import json
import os
import select
import signal
import socket
import subprocess
import time
from pathlib import Path

from tokenizers import Tokenizer

HERE = Path(__file__).resolve().parent
TINYSTORIES_DIR = HERE / "tinystories"
MODELS_DIR = HERE / "models"
LLAMA_SERVER = HERE / "llama/llama-server"
LLAMA_LOG = Path.home() / ".cache/tinyllm/llama-server.log"

TINYSTORIES = "tinystories"
TINYSTORIES_MAX_PROMPT = 128
END_OF_TEXT = b"\x1e"
LLAMA_CONTEXT_SIZE = 2048
LLAMA_THREADS_MAX = 4
LLAMA_THREADS_MIN = 2
COMPACT_HEADER = "Earlier in this conversation I said:\n"
LOAD_TIMEOUT_S = 180
REPLY_TIMEOUT_S = 300


PR_SET_PDEATHSIG = 1


class BackendError(Exception):
    """A message that can be shown to the user."""


def _end_with_parent():
    """Runs in the new process: Linux stops it as soon as Tiny LLM ends, even after a lost SSH connection."""
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


class TinyStories:
    """Continues English stories, using tinystories_pi."""

    name = TINYSTORIES
    continues_text = True
    max_tokens = 256
    context_size = 256
    context_used = 0
    hint = "Start of a story (English)"

    def __init__(self):
        self.tokenizer = Tokenizer.from_file(str(TINYSTORIES_DIR / "tokenizer.json"))
        self.process = None
        self._start()

    def generate(self, text, options, output):
        ids = self.tokenizer.encode(text).ids
        if len(ids) > TINYSTORIES_MAX_PROMPT:
            raise BackendError(
                f"The start is too long: {len(ids)} tokens, the limit is {TINYSTORIES_MAX_PROMPT}."
            )
        self.context_used = len(ids)
        numbers = " ".join(str(i) for i in ids)
        self.process.stdin.write(
            f"GEN {options.temp} {options.top_k} {options.tokens} {numbers}\n".encode()
        )

        last_data = time.monotonic()
        while time.monotonic() - last_data < REPLY_TIMEOUT_S:
            chunk = self._read()
            if not chunk:
                continue
            last_data = time.monotonic()
            if END_OF_TEXT in chunk:
                text_part, _, rest = chunk.partition(END_OF_TEXT)
                output.write(text_part)
                output.flush()
                count, speed = self._read_stats(rest)
                self.context_used = len(ids) + count
                return count, speed
            output.write(chunk)
            output.flush()
        raise BackendError("The model stopped responding.")

    def stop(self):
        """The C program cannot be interrupted while it writes, so a fresh one takes its place."""
        self.close()
        self.context_used = 0
        self._start()

    def _start(self):
        program = TINYSTORIES_DIR / "tinystories_pi"
        try:
            # Own session, so that Ctrl+C only reaches Python.
            self.process = subprocess.Popen(
                [str(program), str(TINYSTORIES_DIR / "model.bin")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
                preexec_fn=_end_with_parent,
            )
        except FileNotFoundError:
            raise BackendError(f"{program} is missing.") from None
        self._wait_until_ready()

    def reset(self):
        self.context_used = 0

    def close(self):
        if self.process is None:
            return
        self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.process = None

    def _wait_until_ready(self):
        received = b""
        end = time.monotonic() + LOAD_TIMEOUT_S
        while time.monotonic() < end:
            received += self._read()
            if b"READY" in received:
                return
            if b"FATAL" in received:
                break
        raise BackendError("TinyStories failed to start: " + received.decode(errors="replace"))

    def _read(self):
        fd = self.process.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            return b""
        data = os.read(fd, 65536)
        if not data:
            raise BackendError("tinystories_pi exited unexpectedly.")
        return data

    def _read_stats(self, received):
        while b"\n" not in received:
            received += self._read()
        count, speed = received.split(b"\n")[0].split()
        return int(count), float(speed)


class LlamaServer:
    """Runs a GGUF file with llama-server and holds a conversation."""

    continues_text = False
    max_tokens = 1024
    context_size = LLAMA_CONTEXT_SIZE
    hint = "Message to the model"

    def __init__(self, path, host="127.0.0.1", port=None, webui=False):
        self.name = path.stem
        self.host = host
        self.fixed_port = port
        self.webui = webui
        self.messages = []
        self.context_used = 0
        self.context_before_turn = 0
        self.path = path
        self.threads = LLAMA_THREADS_MAX
        self._start()

    def set_threads(self, threads):
        """Reloads the model with another number of cores; the conversation stays."""
        self.close()
        self.threads = threads
        self._start()

    def forget_last_exchange(self):
        """Removes the last question and answer and returns the question, or None."""
        if len(self.messages) < 2 or self.messages[-1]["role"] != "assistant":
            return None
        question = self.messages[-2]["content"]
        # Counted again, because /compact and /load change the conversation between two answers.
        self.context_used = self._count_tokens(self.messages[:-2]) if len(self.messages) > 2 else 0
        del self.messages[-2:]
        return question

    def _start(self):
        self.port = self.fixed_port or self._free_port()
        LLAMA_LOG.parent.mkdir(parents=True, exist_ok=True)
        self.log = LLAMA_LOG.open("w")
        try:
            self.process = subprocess.Popen(
                [
                    str(LLAMA_SERVER),
                    "--model", str(self.path),
                    "--host", self.host,
                    "--port", str(self.port),
                    "--ctx-size", str(LLAMA_CONTEXT_SIZE),
                    "--threads", str(self.threads),
                    "--parallel", "1",
                    "--webui" if self.webui else "--no-webui",
                ],
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_end_with_parent,
            )
        except FileNotFoundError:
            self.process = None
            self.log.close()
            raise BackendError(f"{LLAMA_SERVER} is missing.") from None
        try:
            self._wait_until_loaded()
        except KeyboardInterrupt:
            self.close()
            raise

    def generate(self, text, options, output):
        self.context_before_turn = self.context_used
        self.messages.append({"role": "user", "content": text})
        messages = self.messages
        if options.system:
            messages = [{"role": "system", "content": options.system}] + messages
        request = {
            "messages": messages,
            "stream": True,
            "timings_per_token": True,
            "temperature": options.temp,
            "top_k": options.top_k,
            "max_tokens": options.tokens,
            "cache_prompt": True,
        }
        if not options.thinking:
            request["chat_template_kwargs"] = {"enable_thinking": False}
        body = json.dumps(request)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=REPLY_TIMEOUT_S)
        try:
            connection.request(
                "POST", "/v1/chat/completions", body, {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            if response.status != 200:
                self.stop()
                raise BackendError(self._error_text(response))
            return self._stream(response, output)
        except (OSError, http.client.HTTPException) as failure:
            self.stop()
            raise BackendError(f"Lost the connection to the model: {failure}") from None
        finally:
            # Closing also stops the computation in the server after Ctrl+C.
            connection.close()

    def stop(self):
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
            self.context_used = self.context_before_turn

    def reset(self):
        self.messages.clear()
        self.context_used = 0

    def compact(self):
        """Keeps everything the user said and the latest exchange, and drops older answers.

        Done without the model: in tests, models of this size lost or invented facts when summarizing.
        """
        older = self.messages[:-2]
        if not older:
            raise BackendError("Nothing to compact yet: there is only one exchange.")
        said = []
        for message in older:
            if message["role"] == "user":
                content = message["content"]
                said.append(content.removeprefix(COMPACT_HEADER) if content.startswith(COMPACT_HEADER) else f"- {content}")
        messages = [
            {"role": "user", "content": COMPACT_HEADER + "\n".join(said)},
            {"role": "assistant", "content": "Understood."},
        ] + self.messages[-2:]
        self.context_used = self._count_tokens(messages)
        self.messages = messages

    def _count_tokens(self, messages):
        prompt = self._post_json("/apply-template", {"messages": messages})["prompt"]
        return len(self._post_json("/tokenize", {"content": prompt})["tokens"])

    def _post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise BackendError(self._error_text(response))
            return json.loads(response.read())
        except (OSError, ValueError, http.client.HTTPException) as failure:
            raise BackendError(f"Lost the connection to the model: {failure}") from None
        finally:
            connection.close()

    def close(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
        self.log.close()

    def _stream(self, response, output):
        answer = []
        count, speed = 0, 0.0
        for raw_line in response:
            line = raw_line.strip()
            if not line.startswith(b"data: ") or line == b"data: [DONE]":
                continue
            data = json.loads(line[len(b"data: "):])
            for choice in data.get("choices", []):
                delta = choice.get("delta", {})
                thinking = delta.get("reasoning_content")
                if thinking:
                    output.write_thinking(thinking.encode())
                    output.flush()
                piece = delta.get("content")
                if piece:
                    answer.append(piece)
                    output.write(piece.encode())
                    output.flush()
            timings = data.get("timings")
            if timings:
                count = timings.get("predicted_n", count)
                speed = timings.get("predicted_per_second", speed)
                self.context_used = timings.get("cache_n", 0) + timings.get("prompt_n", 0) + count
        self.messages.append({"role": "assistant", "content": "".join(answer)})
        return count, speed

    def _wait_until_loaded(self):
        end = time.monotonic() + LOAD_TIMEOUT_S
        while time.monotonic() < end:
            if self.process.poll() is not None:
                self.process = None
                self.log.close()
                raise BackendError(f"The model could not be loaded, see {LLAMA_LOG}")
            if self._is_healthy():
                return
            time.sleep(0.3)
        self.close()
        raise BackendError(f"The model did not finish loading in time, see {LLAMA_LOG}")

    def _is_healthy(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/health")
            return connection.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    @staticmethod
    def _error_text(response):
        try:
            error = json.loads(response.read())["error"]
            if error.get("type") == "exceed_context_size_error":
                return (
                    f"The context is full ({error['n_prompt_tokens']} of {error['n_ctx']} tokens)."
                    " /compact or /clear make room."
                )
            message = error["message"]
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {response.status}"
        return f"The model reported an error: {message}"

    @staticmethod
    def _free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def model_names():
    return [TINYSTORIES] + sorted((path.stem for path in MODELS_DIR.glob("*.gguf")), key=str.lower)


def load_model(name):
    if name == TINYSTORIES:
        return TinyStories()
    return LlamaServer(MODELS_DIR / f"{name}.gguf")
