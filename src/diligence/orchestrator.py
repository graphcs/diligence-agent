"""Orchestrator: resolve the company, plan from the stage template, fan out sections in
parallel, run the editor pass, and persist the memo so single sections can be rerun."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import render, review
from .agents import editor
from .agents.section_agent import SectionContext, SectionExecutor
from .config import Config, load_config
from .data_access import AccessContext, AccessDenied, DataAccess
from .ledger import Ledger, now
from .llm import LLM, RunContext
from .models import MemoState

Progress = Callable[[str, str | None], Awaitable[None]]


async def _noop(event: str, section: str | None) -> None:
    return None


@dataclass
class RunResult:
    run_id: str
    memo: MemoState
    memo_path: Path
    markdown: str
    seconds: float
    cost_rows: list[tuple]

    @property
    def cost_usd(self) -> float:
        return sum(r[5] or 0 for r in self.cost_rows)


def lookup(cfg: Config, user: str, query: str) -> list[dict[str, Any]]:
    """CRM directory lookup (no LLM): fast enough for the instant Slack reply."""
    access = DataAccess(cfg, Ledger(cfg.path("db")))
    return access.lookup_company(AccessContext(user=user, deal_id="-", run_id="lookup",
                                               actor="orchestrator"), query)


def plan(cfg: Config, stage: str, only: list[str] | None) -> list[list[str]]:
    """Order sections into dependency levels; each level runs in parallel."""
    wanted = only or cfg.stages[stage]["sections"]
    unknown = [s for s in wanted if s not in cfg.sections]
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(unknown)}")
    levels, done = [], set()
    pending = list(wanted)
    while pending:
        ready = [s for s in pending
                 if all(d in done or d not in wanted for d in cfg.sections[s]["depends_on"])]
        if not ready:
            raise ValueError("circular section dependencies")
        levels.append(ready)
        done.update(ready)
        pending = [s for s in pending if s not in ready]
    return levels


def memo_path(cfg: Config, deal_id: str, user: str) -> Path:
    """Drafts are scoped to the user who requested them. A shared per-deal draft would hand
    one user's data-room findings to a teammate without data-room access; sharing a memo is
    a human action (share the doc), not something the agent does implicitly."""
    return cfg.path("out") / deal_id / user / "memo_state.json"


def save_memo(cfg: Config, memo: MemoState, user: str, run_id: str | None = None) -> tuple[Path, str]:
    """Persist the memo state and re-render its Markdown (also after review actions)."""
    path = memo_path(cfg, memo.deal_id, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(memo.model_dump_json(indent=1))
    md = render.to_markdown(Ledger(cfg.path("db")), memo)
    out_md = path.parent / "memo.md"
    out_md.write_text(md)
    if run_id:
        (path.parent / f"memo_{run_id}.md").write_text(md)
    return out_md, md


def load_memo(cfg: Config, deal_id: str, user: str) -> MemoState | None:
    p = memo_path(cfg, deal_id, user)
    return MemoState.model_validate_json(p.read_text()) if p.exists() else None


async def run(*, user: str, crm_id: str, stage: str = "brief", only: list[str] | None = None,
              progress: Progress = _noop, cfg: Config | None = None) -> RunResult:
    cfg = cfg or load_config()
    t0 = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    ledger = Ledger(cfg.path("db"))
    access = DataAccess(cfg, ledger)
    ctx = AccessContext(user=user, deal_id=crm_id, run_id=run_id, actor="orchestrator")
    deal = access.get_deal(ctx, crm_id)  # raises AccessDenied if not on the deal team
    company = deal["name"]

    memo = load_memo(cfg, crm_id, user) if only else None
    if memo is None:
        memo = MemoState(deal_id=crm_id, company=company, stage=stage, requested_by=user)
    levels = plan(cfg, memo.stage if only else stage, only)
    names = [s for lvl in levels for s in lvl]

    room_note = access.data_room_access(ctx, deal)
    memo.access_gaps = [room_note] if room_note else []
    llm = LLM(cfg, ledger, RunContext(run_id=run_id, user=user, deal_id=crm_id))

    async with AsyncExitStack() as stack:
        docs, docs_tools = None, []
        if room_note is None:
            docs, docs_tools = await _start_docs_server(stack, cfg, user, crm_id, run_id)
        await progress("plan", ", ".join(cfg.sections[s]["title"] for s in names))

        async def one(key: str) -> None:
            await progress("start", key)
            sc = SectionContext(key=key, spec=cfg.sections[key], company=company, deal=deal,
                                run_id=run_id, docs=docs, docs_tools=docs_tools,
                                data_room_note=room_note)
            memo.sections[key] = await SectionExecutor(cfg, ledger, llm, sc).run()
            await progress("done", key)

        for level in levels:
            await asyncio.gather(*(one(k) for k in level))

    # Keep sections in template order even after a single-section rerun.
    order = cfg.stages[memo.stage]["sections"]
    memo.sections = dict(sorted(memo.sections.items(),
                                key=lambda kv: order.index(kv[0]) if kv[0] in order else 99))
    await progress("editor", None)
    await editor.edit(llm, memo)
    memo.version += 1
    what = f"refreshed {', '.join(names)}" if only else f"drafted {len(names)} sections"
    memo.changelog.append(f"v{memo.version} {now()} - {user} {what} (run {run_id})")
    review.refresh_figures(memo, names)  # new drafts need fresh human review

    out_md, md = save_memo(cfg, memo, user, run_id)
    return RunResult(run_id=run_id, memo=memo, memo_path=out_md, markdown=md,
                     seconds=time.monotonic() - t0, cost_rows=ledger.cost_by_section(run_id))


async def _start_docs_server(stack: AsyncExitStack, cfg: Config, user: str, deal_id: str,
                             run_id: str) -> tuple[ClientSession, list[dict[str, Any]]]:
    """Launch the data-room MCP server as the requesting user (stand-in for per-user OAuth)."""
    env = {**os.environ, "DILIGENCE_USER": user, "DILIGENCE_DEAL": deal_id,
           "DILIGENCE_RUN": run_id, "DILIGENCE_ROOT": str(cfg.root),
           "DILIGENCE_DB": str(cfg.path("db"))}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "diligence.mcp_servers.docs_server"], env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    listed = await session.list_tools()
    tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
             for t in listed.tools]
    return session, tools


__all__ = ["AccessDenied", "RunResult", "lookup", "plan", "run", "load_memo"]
