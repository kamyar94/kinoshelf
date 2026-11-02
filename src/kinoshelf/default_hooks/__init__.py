"""Hooks shipped with Kinoshelf. Each reads the job as JSON on stdin and prints one JSON answer."""

from __future__ import annotations

import json
import sys
from typing import Callable


def run(fn: Callable[[dict], dict]) -> None:
    # Job and answer are UTF-8 whatever the platform's console encoding (names may be in any script).
    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        ans = fn(job)
    except Exception as e:  # a hook must always answer
        ans = {"status": "failed", "message": f"{type(e).__name__}: {e}"[:300]}
    sys.stdout.buffer.write((json.dumps(ans, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()
