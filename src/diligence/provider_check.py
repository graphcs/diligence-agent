"""Live probes for the configured provider (a few cents). Confirms each API feature the
agent depends on actually works through the endpoint before a demo."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from rich.console import Console
from rich.table import Table

from .agents import web_agent
from .config import Config
from .ledger import Ledger
from .llm import LLM, RunContext, final_text, parse_json

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
TOOL = {"name": "lookup_ticker", "description": "Look up a stock ticker for a company name.",
        "strict": True, "input_schema": {"type": "object", "properties": {"company": {"type": "string"}},
                                          "required": ["company"], "additionalProperties": False}}


async def run_checks(cfg: Config, console: Console) -> bool:
    ledger = Ledger(cfg.path("db"))
    llm = LLM(cfg, ledger, RunContext("provider-check", "system", "-"))
    msg = lambda t: [{"role": "user", "content": t}]  # noqa: E731

    async def basic() -> str:
        r = await llm.create("screen", section="check", system="Be brief.", max_tokens=200,
                             messages=msg("Reply with the single word OK."))
        return f"{r.model}: {final_text(r).strip()[:40]!r}"

    async def structured() -> str:
        r = await llm.create("section", section="check", system="Be brief.", max_tokens=2000,
                             schema=SCHEMA, messages=msg("What is 2+2? Put it in `answer`."))
        return f"{r.model}: {parse_json(r)}"

    async def tools() -> str:
        r = await llm.create("section", section="check", system="Use tools when helpful.",
                             max_tokens=2000, tools=[TOOL],
                             messages=msg("Use lookup_ticker to find Lattice Semiconductor's ticker."))
        uses = [b for b in r.content if b.type == "tool_use"]
        if not uses:
            raise AssertionError(f"no tool_use block (stop_reason={r.stop_reason})")
        return f"called {uses[0].name}({uses[0].input})"

    async def editor_model() -> str:
        r = await llm.create("editor", section="check", system="Be brief.", max_tokens=2000,
                             schema=SCHEMA, messages=msg("Name the capital of France in `answer`."))
        return f"{r.model}: {parse_json(r)}"

    async def web() -> str:
        res = await web_agent.research(llm, cfg, section="check",
                                       question="What does Lattice (lattice.com) sell? One fact.")
        if not res.sources_seen:
            raise AssertionError("no web sources came back - web tools may not be supported")
        return f"{res.sources_seen} sources, {len(res.facts)} facts, e.g. {res.facts[0].url if res.facts else '-'}"

    checks: list[tuple[str, Callable[[], Awaitable[str]]]] = [
        ("basic call (screen model)", basic), ("structured output + effort (section model)", structured),
        ("strict client tool use", tools), ("editor model", editor_model),
        ("server-side web search/fetch", web),
    ]
    table = Table(title=f"Provider check: {cfg.provider_name}")
    for col in ("check", "result", "detail", "s"):
        table.add_column(col, overflow="fold")
    ok_all = True
    for name, fn in checks:
        t0 = time.monotonic()
        try:
            detail, ok = await fn(), True
        except Exception as e:  # report every failure, keep probing
            detail, ok = f"{type(e).__name__}: {e}"[:400], False
        ok_all &= ok
        table.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", detail,
                      f"{time.monotonic() - t0:.1f}")
    rows = ledger.cost_by_section("provider-check")
    total = sum(r[5] or 0 for r in rows)
    console.print(table)
    console.print(f"Probe cost: ${total:.4f} (logged under run 'provider-check')")
    return ok_all


__all__: list[Any] = ["run_checks"]
