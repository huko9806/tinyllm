"""Saves conversations, as JSON to continue them and as Markdown to read them."""
import json
import re

from preferences import CONFIG_DIR

CHATS_DIR = CONFIG_DIR / "chats"
VALID_NAME = re.compile(r"\w[\w.-]{0,59}")
TITLES = {"user": "You", "assistant": "Model"}


class ChatError(Exception):
    """A message that can be shown to the user."""


def _path(name):
    if not VALID_NAME.fullmatch(name):
        raise ChatError("Names may only contain letters, digits, dots, dashes and underscores.")
    return CHATS_DIR / f"{name}.json"


def save(name, model, messages, context_used, system):
    path = _path(name)
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"model": model, "messages": messages, "context_used": context_used, "system": system},
        indent=2,
    ))
    sections = [f"# {name}\n\nModel: {model}"]
    sections += [f"## {TITLES[m['role']]}\n\n{m['content']}" for m in messages]
    path.with_suffix(".md").write_text("\n\n".join(sections) + "\n")
    return path


def names():
    return sorted(path.stem for path in CHATS_DIR.glob("*.json"))


def load(name):
    try:
        data = json.loads(_path(name).read_text())
        valid = isinstance(data.get("model"), str) and all(
            m["role"] in TITLES and isinstance(m["content"], str) for m in data["messages"]
        )
    except FileNotFoundError:
        raise ChatError(f"No saved chat called {name}.") from None
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ChatError(f"The saved chat {name} is damaged.")
    return data
