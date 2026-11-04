"""Append-only event log (JSON lines) that the dashboard tails."""
from __future__ import annotations

import json
import os
import time

MAX_BYTES = 5_000_000  # rotate to events.jsonl.1 above this size


class EventLog:
    def __init__(self, log_dir: str):
        self.path = os.path.join(log_dir, "events.jsonl")

    def emit(self, type_: str, **data) -> None:
        rec = {"ts": round(time.time(), 3), "type": type_, **data}
        line = json.dumps(rec, separators=(",", ":"), default=float) + "\n"
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a") as f:
                f.write(line)
        except OSError:
            pass  # never let logging stop the bot


def write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=float)
    os.replace(tmp, path)
