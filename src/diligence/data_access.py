"""Permission-enforcing access to the data room and CRM.

This is the "data layer": the MCP servers are thin wrappers around these functions, and
every check happens here *before* anything is returned to a model. Nothing downstream
filters model output for permissions - a document the user cannot open never reaches the
agent at all. Every call, allowed or denied, is written to the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .ledger import Ledger

MAX_PAGES_PER_READ = 20
MAX_CHARS_PER_READ = 24_000
FILE_ID_RE = re.compile(r"^[a-z0-9_]+/[A-Za-z0-9_.-]+$")


class AccessDenied(Exception):
    """Raised when the requesting user may not see an object."""


@dataclass(frozen=True)
class AccessContext:
    user: str
    deal_id: str
    run_id: str
    actor: str = "agent"


class DataAccess:
    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger
        self.dataroom = cfg.path("dataroom")

    # ---- helpers -------------------------------------------------------------------

    def _audit(self, ctx: AccessContext, tool: str, args: dict[str, Any],
               returned: list[str], decision: str) -> None:
        self.ledger.audit(run_id=ctx.run_id, user=ctx.user, deal_id=ctx.deal_id,
                          actor=ctx.actor, tool=tool, args=args, returned=returned,
                          decision=decision)

    def _user(self, ctx: AccessContext) -> dict[str, Any]:
        user = self.cfg.users.get(ctx.user)
        if user is None:
            raise AccessDenied(f"unknown user {ctx.user!r}")
        return user

    def _folder_allowed(self, ctx: AccessContext, folder: str) -> str | None:
        """Return None if allowed, else the reason it is denied."""
        user = self._user(ctx)
        meta = self.cfg.folders.get(folder)
        if meta is None:
            return "unknown folder"
        if folder not in user["folders"]:
            return "user has no access to this folder"
        if meta["confidentiality"] == "portfolio" and meta["deal"] != ctx.deal_id:
            return "information barrier: portfolio-company data cannot be used on another deal"
        return None

    def _authorize_file(self, ctx: AccessContext, file_id: str, tool: str) -> Path:
        if not FILE_ID_RE.match(file_id):
            self._audit(ctx, tool, {"file_id": file_id}, [], "denied: malformed id")
            raise AccessDenied("malformed file id")
        folder, name = file_id.split("/", 1)
        reason = self._folder_allowed(ctx, folder)
        path = self.dataroom / folder / name
        if reason is None and not path.is_file():
            reason = "no such file"
        if reason:
            self._audit(ctx, tool, {"file_id": file_id}, [], f"denied: {reason}")
            raise AccessDenied(f"{file_id}: {reason}")
        return path

    @lru_cache(maxsize=64)
    def _pages(self, path: Path) -> tuple[str, ...]:
        return tuple(path.read_text(encoding="utf-8").split("\f"))

    def _meta(self, folder: str) -> dict[str, Any]:
        p = self.dataroom / folder / "_meta.yaml"
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}

    # ---- documents -----------------------------------------------------------------

    def list_files(self, ctx: AccessContext) -> list[dict[str, Any]]:
        user = self._user(ctx)
        out = []
        for folder in user["folders"]:
            if self._folder_allowed(ctx, folder):
                continue
            meta = self._meta(folder)
            for path in sorted((self.dataroom / folder).glob("*")):
                if path.name.startswith("_") or not path.is_file():
                    continue
                m = meta.get(path.name, {})
                out.append({
                    "file_id": f"{folder}/{path.name}",
                    "title": m.get("title", path.name),
                    "as_of": m.get("as_of"),
                    "pages": len(self._pages(path)),
                })
        self._audit(ctx, "list_files", {}, [f["file_id"] for f in out], "allowed")
        return out

    def get_file_manifest(self, ctx: AccessContext, file_id: str) -> dict[str, Any]:
        path = self._authorize_file(ctx, file_id, "get_file_manifest")
        pages = self._pages(path)
        tokens = [max(1, len(p) // 4) for p in pages]
        folder, name = file_id.split("/", 1)
        m = self._meta(folder).get(name, {})
        self._audit(ctx, "get_file_manifest", {"file_id": file_id}, [file_id], "allowed")
        return {
            "file_id": file_id,
            "title": m.get("title", name),
            "as_of": m.get("as_of"),
            "pages_total": len(pages),
            "tokens_total_estimate": sum(tokens),
            "has_text_layer": True,
            "max_pages_per_read": MAX_PAGES_PER_READ,
        }

    def read_file(self, ctx: AccessContext, file_id: str, start_page: int = 1,
                  end_page: int | None = None) -> dict[str, Any]:
        """Read a page range. Always reports exactly what was read and what was not."""
        path = self._authorize_file(ctx, file_id, "read_file")
        pages = self._pages(path)
        total = len(pages)
        start = max(1, start_page)
        end = min(total, end_page or total, start + MAX_PAGES_PER_READ - 1)
        chunks, read, chars = [], [], 0
        for n in range(start, end + 1):
            text = pages[n - 1]
            if chars + len(text) > MAX_CHARS_PER_READ and read:
                break
            chunks.append(f"[page {n}]\n{text}")
            read.append(n)
            chars += len(text)
        last = read[-1] if read else start - 1
        folder, name = file_id.split("/", 1)
        m = self._meta(folder).get(name, {})
        self._audit(ctx, "read_file", {"file_id": file_id, "start_page": start_page,
                                       "end_page": end_page}, [file_id], "allowed")
        return {
            "file_id": file_id,
            "title": m.get("title", name),
            "as_of": m.get("as_of"),
            "text": "\n\n".join(chunks),
            "coverage": {
                "pages_read": read,
                "pages_total": total,
                "truncated": last < total or start > 1,
                "next_page": last + 1 if last < total else None,
            },
        }

    def search_files(self, ctx: AccessContext, query: str, limit: int = 8) -> list[dict[str, Any]]:
        terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
        hits = []
        for f in self.list_files(ctx):
            path = self.dataroom / f["file_id"]
            for n, page in enumerate(self._pages(path), start=1):
                low = page.lower()
                score = sum(low.count(t) for t in terms)
                if score:
                    idx = min((low.find(t) for t in terms if t in low), default=0)
                    snippet = page[max(0, idx - 150): idx + 250].replace("\n", " ")
                    hits.append({"file_id": f["file_id"], "page": n, "score": score,
                                 "snippet": snippet})
        hits.sort(key=lambda h: -h["score"])
        hits = hits[:limit]
        self._audit(ctx, "search_files", {"query": query},
                    sorted({h["file_id"] for h in hits}), "allowed")
        return hits

    # ---- CRM -----------------------------------------------------------------------

    def lookup_company(self, ctx: AccessContext, query: str) -> list[dict[str, Any]]:
        """Firm-wide company directory: names and stages only, no deal details."""
        q = query.lower().strip()
        out = []
        if not q:
            return out
        for c in self.cfg.crm:
            names = [c["name"].lower(), *c.get("aliases", []), c["domain"]]
            if any(q == n or q in n or n in q for n in names) or q.split()[0] in c["name"].lower().split():
                out.append({k: c[k] for k in ("id", "name", "domain", "stage", "description")})
        self._audit(ctx, "lookup_company", {"query": query}, [c["id"] for c in out], "allowed")
        return out

    def get_deal(self, ctx: AccessContext, crm_id: str) -> dict[str, Any]:
        user = self._user(ctx)
        rec = next((c for c in self.cfg.crm if c["id"] == crm_id), None)
        if rec is None or crm_id not in user["deals"]:
            self._audit(ctx, "get_deal", {"crm_id": crm_id}, [], "denied: not on deal team")
            raise AccessDenied(f"{crm_id}: not on this deal's team")
        self._audit(ctx, "get_deal", {"crm_id": crm_id}, [crm_id], "allowed")
        return dict(rec)

    def data_room_access(self, ctx: AccessContext, deal: dict[str, Any]) -> str | None:
        """Explain why the deal's data room is unavailable to this user, if it is."""
        folder = deal.get("folder")
        if not folder:
            return "No data room on file for this company."
        reason = self._folder_allowed(ctx, folder)
        if reason:
            self._audit(ctx, "data_room_access", {"folder": folder}, [], f"denied: {reason}")
            return f"Data room exists but is not accessible to you ({reason}). Ask the deal owner for access."
        return None
