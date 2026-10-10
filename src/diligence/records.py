"""Append-only, hash-chained run records (books-and-records, SEC Rule 204-2 style).

Every model call and every human review action is appended to out/records/<run_id>.jsonl.
Each line carries the SHA-256 of the previous line, so editing or deleting any earlier
entry breaks the chain and `diligence records verify <run>` reports where.
Production would write to WORM storage (e.g. S3 Object Lock) instead of local files.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .ledger import now

GENESIS = "0" * 64
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _digest(prev: str, body: dict[str, Any]) -> str:
    raw = prev + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Records:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return self.dir / f"{safe}.jsonl"

    def _lock(self, run_id: str) -> threading.Lock:
        with _guard:
            return _locks.setdefault(str(self.path(run_id)), threading.Lock())

    def _last_hash(self, path: Path) -> tuple[str, int]:
        if not path.exists():
            return GENESIS, 0
        last, n = GENESIS, 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last, n = json.loads(line)["hash"], n + 1
        return last, n

    def append(self, run_id: str, kind: str, payload: dict[str, Any]) -> str:
        path = self.path(run_id)
        with self._lock(run_id):
            prev, n = self._last_hash(path)
            body = {"seq": n + 1, "ts": now(), "run_id": run_id, "kind": kind,
                    "payload": _jsonable(payload), "prev": prev}
            h = _digest(prev, body)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**body, "hash": h}, default=str) + "\n")
            return h

    def verify(self, run_id: str) -> tuple[bool, int, str]:
        """Return (ok, entries checked, message)."""
        path = self.path(run_id)
        if not path.exists():
            return False, 0, f"no records for run {run_id}"
        prev, n = GENESIS, 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                n += 1
                entry = json.loads(line)
                h = entry.pop("hash")
                if entry.get("prev") != prev or _digest(prev, entry) != h:
                    return False, n, f"chain broken at entry {n} ({entry.get('kind')})"
                prev = h
        return True, n, f"{n} entries, chain intact"
