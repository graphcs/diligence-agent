"""SQLite store for the evidence ledger, the audit log and the cost log.

Production would use Postgres (ledger) and Snowflake (audit/cost); SQLite keeps the demo
self-contained. Both the agent process and the MCP server processes write here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import EvidenceRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY, run_id TEXT, deal_id TEXT, section TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    ts TEXT, run_id TEXT, user TEXT, deal_id TEXT, actor TEXT, tool TEXT,
    args TEXT, returned TEXT, decision TEXT
);
CREATE TABLE IF NOT EXISTS slack_threads (
    thread TEXT PRIMARY KEY, data TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS cost (
    ts TEXT, run_id TEXT, user TEXT, deal_id TEXT, section TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    web_searches INTEGER, usd REAL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # MCP servers run tool calls on worker threads; one connection must not be used
        # concurrently, so every statement goes through this lock.
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    def add_evidence(self, rec: EvidenceRecord) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?)",
                (rec.id, rec.run_id, rec.deal_id, rec.section, rec.model_dump_json()),
            )

    def get_evidence(self, ids: list[str]) -> dict[str, EvidenceRecord]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.lock:
            rows = list(self.conn.execute(f"SELECT data FROM evidence WHERE id IN ({marks})", ids))
        recs = [EvidenceRecord.model_validate_json(r[0]) for r in rows]
        return {r.id: r for r in recs}

    def audit(self, *, run_id: str, user: str, deal_id: str, actor: str, tool: str,
              args: dict[str, Any], returned: list[str], decision: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO audit VALUES (?,?,?,?,?,?,?,?,?)",
                (now(), run_id, user, deal_id, actor, tool, json.dumps(args, sort_keys=True),
                 json.dumps(returned), decision),
            )

    def log_cost(self, *, run_id: str, user: str, deal_id: str, section: str, model: str,
                 input_tokens: int, output_tokens: int, cache_read_tokens: int,
                 web_searches: int, usd: float) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO cost VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now(), run_id, user, deal_id, section, model, input_tokens, output_tokens,
                 cache_read_tokens, web_searches, usd),
            )

    def audit_rows(self, run_id: str | None = None) -> list[tuple]:
        q = "SELECT ts, run_id, user, deal_id, actor, tool, args, returned, decision FROM audit"
        with self.lock:
            if run_id:
                return list(self.conn.execute(q + " WHERE run_id=? ORDER BY ts", (run_id,)))
            return list(self.conn.execute(q + " ORDER BY ts DESC LIMIT 200"))

    def cost_by_section(self, run_id: str) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT section, model, SUM(input_tokens), SUM(output_tokens), SUM(web_searches), "
                "SUM(usd) FROM cost WHERE run_id=? GROUP BY section, model ORDER BY section",
                (run_id,),
            ))

    # ---- Slack thread state (survives restarts) ----------------------------------------

    def put_thread(self, thread: str, data: dict[str, Any]) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO slack_threads VALUES (?,?,?)",
                              (thread, json.dumps(data), now()))

    def get_thread(self, thread: str | None) -> dict[str, Any] | None:
        if not thread:
            return None
        with self.lock:
            row = self.conn.execute("SELECT data FROM slack_threads WHERE thread=?",
                                    (thread,)).fetchone()
        return json.loads(row[0]) if row else None
