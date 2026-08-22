"""Persistent FilaScan device allow-list shared by all FilaMan workers."""

import json
import os
from pathlib import Path

from app.core.config import DATA_DIR


STATE_DIR = DATA_DIR / "plugin_state"
STATE_FILE = STATE_DIR / "filascan_import.json"


def load_allowed_device_ids() -> set[int]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        values = payload.get("allowed_device_ids", [])
        if not isinstance(values, list):
            return set()
        return {
            int(value)
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def save_allowed_device_ids(device_ids: set[int]) -> None:
    normalized = sorted(
        value
        for value in device_ids
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"allowed_device_ids": normalized}, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(STATE_FILE)
    os.chmod(STATE_FILE, 0o600)
