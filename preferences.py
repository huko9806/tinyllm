"""Settings of Tiny LLM that survive a restart."""
import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TINYLLM_CONFIG", Path.home() / ".config/tinyllm"))
HISTORY_FILE = CONFIG_DIR / "history"
DEFAULT_LENGTH = 200
# Models that need more room by default: thinking uses up part of the answer length.
BUILT_IN_LENGTHS = (("qwen3", 600),)
DEFAULTS = {"lengths": {}, "thinking": True, "system": "", "heat_guard": True, "last_model": "", "bench": {}}


class Preferences:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = CONFIG_DIR / "settings.json"
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        self.values = {**DEFAULTS, **saved}

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        self.values[key] = value
        self.path.write_text(json.dumps(self.values, indent=2))

    def length_for(self, model):
        if model in self.values["lengths"]:
            return self.values["lengths"][model]
        for prefix, length in BUILT_IN_LENGTHS:
            if model.lower().startswith(prefix):
                return length
        return DEFAULT_LENGTH

    def remember_length(self, model, tokens):
        self.set("lengths", {**self.values["lengths"], model: tokens})
