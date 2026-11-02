"""Hooks shipped with Kinoshelf. Each reads the job as JSON on stdin and prints one JSON answer."""

from __future__ import annotations

import json
import sys
from typing import Callable


def run(fn: Callable[[dict], dict]) -> None:
    try:
        job = json.loads(sys.stdin.read() or "{}")
        ans = fn(job)
    except Exception as e:  # a hook must always answer
        ans = {"status": "failed", "message": f"{type(e).__name__}: {e}"[:300]}
    sys.stdout.write(json.dumps(ans, ensure_ascii=False) + "\n")
