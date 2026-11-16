"""Downloads GGUF models from Hugging Face."""
import json
import re
import shutil
import urllib.request

from backends import MODELS_DIR

TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main"
FILE_URL = "https://huggingface.co/{repo}/resolve/main/{name}"
HEADERS = {"User-Agent": "tinyllm"}
VALID_REPO = re.compile(r"[\w.-]+/[\w.-]+")
PREFERRED_QUANTIZATION = "q4_k_m"
CHUNK_BYTES = 1024 * 1024
SPARE_DISK_BYTES = 500 * 1000 * 1000
# Measured on this Pi: the system keeps about 170 MB, models need up to a few 100 MB on top of their file.
SYSTEM_RAM_MB = 170
MODEL_OVERHEAD_MB = 200


class DownloadError(Exception):
    """A message that can be shown to the user."""


def _files(repo):
    if not VALID_REPO.fullmatch(repo):
        raise DownloadError("Please name a repository like owner/name.")
    request = urllib.request.Request(TREE_URL.format(repo=repo), headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            tree = json.load(response)
        return {
            entry["path"]: entry["size"]
            for entry in tree
            if entry.get("type") == "file"
            and entry["path"].endswith(".gguf")
            and "/" not in entry["path"]
            and "-of-" not in entry["path"]
            and "mmproj" not in entry["path"].lower()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as failure:
        raise DownloadError(f"Could not read {repo}: {failure}") from None


def pick(repo, wanted=None):
    """Returns (file name, size in bytes) of the requested or the most suitable file."""
    files = _files(repo)
    if not files:
        raise DownloadError(f"{repo} has no single-file GGUF models.")
    if wanted:
        if wanted not in files:
            raise DownloadError(f"{wanted} is not in {repo}.")
        return wanted, files[wanted]
    preferred = [name for name in files if PREFERRED_QUANTIZATION in name.lower()]
    name = min(preferred or files, key=files.get)
    return name, files[name]


def check_space(name, size):
    MODELS_DIR.mkdir(exist_ok=True)  # a fresh checkout has no models/ yet
    if (MODELS_DIR / name).exists():
        raise DownloadError(f"{name} is already downloaded.")
    if shutil.disk_usage(MODELS_DIR).free < size + SPARE_DISK_BYTES:
        raise DownloadError("There is not enough free space on the SD card.")


def too_big_for_ram(size, ram_total_mb):
    return size // 1000000 + MODEL_OVERHEAD_MB > ram_total_mb - SYSTEM_RAM_MB


def fetch(repo, name, on_progress):
    part = MODELS_DIR / f"{name}.part"
    request = urllib.request.Request(FILE_URL.format(repo=repo, name=name), headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as target:
            while chunk := response.read(CHUNK_BYTES):
                target.write(chunk)
                on_progress(len(chunk))
        with part.open("rb") as check:
            if check.read(4) != b"GGUF":
                raise DownloadError(f"{name} is not a valid GGUF file.")
        part.replace(MODELS_DIR / name)
    except OSError as failure:
        raise DownloadError(f"The download failed: {failure}") from None
    finally:
        part.unlink(missing_ok=True)
