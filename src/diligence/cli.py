"""Command line: `diligence run`, `diligence audit`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from . import orchestrator
from .config import load_config, load_env
from .data_access import AccessDenied
from .ledger import Ledger

console = Console()


def _pick(cands: list[dict], pick: str | None) -> str:
    if pick:
        return pick
    if len(cands) == 1:
        return cands[0]["id"]
    console.print("[bold]Which company did you mean?[/bold]")
    for i, c in enumerate(cands, 1):
        console.print(f"  {i}. {c['name']} ({c['domain']}) - CRM stage: {c['stage']} - {c['description']}")
    if not sys.stdin.isatty():
        raise SystemExit("Ambiguous company; re-run with --pick <crm id>.")
    choice = int(console.input("Choose a number: ")) - 1
    return cands[choice]["id"]


async def _run(args: argparse.Namespace) -> None:
    cfg = load_config()
    t0 = time.monotonic()
    cands = orchestrator.lookup(cfg, args.user, args.company)
    console.print(f"[dim]CRM lookup: {len(cands)} match(es) in {time.monotonic() - t0:.2f}s[/dim]")
    if not cands:
        raise SystemExit(f"No CRM match for {args.company!r}.")
    crm_id = _pick(cands, args.pick)

    async def progress(event: str, section: str | None) -> None:
        stamp = f"[dim]{time.monotonic() - t0:5.1f}s[/dim]"
        if event == "plan":
            console.print(f"{stamp} Plan: {section}")
        elif event == "start":
            console.print(f"{stamp} [cyan]…[/cyan] {cfg.sections[section]['title']}")
        elif event == "done":
            console.print(f"{stamp} [green]✓[/green] {cfg.sections[section]['title']}")
        elif event == "editor":
            console.print(f"{stamp} Editor pass (summary + consistency)")

    try:
        result = await orchestrator.run(user=args.user, crm_id=crm_id, stage=args.stage,
                                        only=args.section or None, progress=progress, cfg=cfg)
    except AccessDenied as e:
        raise SystemExit(f"Access denied: {e}")

    if not args.quiet:
        console.print(Markdown(result.markdown))
    table = Table(title=f"Cost - run {result.run_id}")
    for col in ("section", "model", "input tok", "output tok", "searches", "USD"):
        table.add_column(col)
    for sec, model, i, o, s, usd in result.cost_rows:
        table.add_row(sec, model, f"{i:,}", f"{o:,}", str(s), f"{usd:.3f}")
    table.add_row("total", "", "", "", "", f"[bold]{result.cost_usd:.3f}[/bold]")
    console.print(table)
    console.print(f"Memo: {result.memo_path}  ({result.seconds:.0f}s)")


def _audit(args: argparse.Namespace) -> None:
    rows = Ledger(load_config().path("db")).audit_rows(args.run)
    table = Table(title="Audit log" + (f" - run {args.run}" if args.run else " (latest 200)"))
    for col in ("time", "run", "user", "actor", "tool", "args", "returned", "decision"):
        table.add_column(col, overflow="fold", no_wrap=col in ("time", "run", "user", "tool"))
    for ts, run, user, _deal, actor, tool, args_, returned, decision in rows:
        style = "red" if decision.startswith("denied") else None
        table.add_row(ts[11:19], run, user, actor, tool, args_, ", ".join(json.loads(returned)),
                      decision, style=style)
    console.print(table)


def _resolve(cfg, user: str, company: str, pick: str | None) -> str:
    cands = orchestrator.lookup(cfg, user, company)
    if not cands:
        raise SystemExit(f"No CRM match for {company!r}.")
    return _pick(cands, pick)


async def _ask(args: argparse.Namespace) -> None:
    from . import qa

    cfg = load_config()
    crm_id = _resolve(cfg, args.user, args.company, args.pick)
    try:
        a = await qa.answer(cfg, user=args.user, crm_id=crm_id, question=args.question)
    except AccessDenied as e:
        raise SystemExit(f"Access denied: {e}")
    console.print(a.markdown())
    note = "cited" if a.confident else "not covered by the brief"
    if a.suggest_section and not a.confident:
        note += f" - try: diligence run {args.company!r} --section {a.suggest_section}"
    console.print(f"[dim]{a.seconds:.1f}s · {note}[/dim]")


def _review(args: argparse.Namespace) -> None:
    from . import review

    cfg = load_config()
    owner = args.owner or args.user
    crm_id = _resolve(cfg, args.user, args.company, args.pick)
    memo = orchestrator.load_memo(cfg, crm_id, owner)
    if memo is None:
        raise SystemExit("No memo yet - run `diligence run` first.")
    r = review.Reviewer(cfg)
    try:
        for key in (list(memo.sections) if args.all_sections else args.section or []):
            r.review_section(memo, args.user, key)
        for fid in args.verify or []:
            r.set_figure(memo, args.user, fid, "verified")
        for fid in args.dispute or []:
            r.set_figure(memo, args.user, fid, "disputed", note=args.note or "")
    except (AccessDenied, KeyError, ValueError) as e:
        raise SystemExit(f"Not applied: {e}")
    path, _ = orchestrator.save_memo(cfg, memo, owner)
    st = review.status(memo)
    table = Table(title=f"{memo.company} v{memo.version} - {st.line()}")
    for col in ("item", "status", "by"):
        table.add_column(col, overflow="fold")
    for key, s in memo.sections.items():
        rv = memo.reviews.get(key)
        table.add_row(f"section: {s.title}", "reviewed" if rv else "pending", rv.by if rv else "")
    for f in memo.figures:
        table.add_row(f"{f.id}: {f.text[:90]}", f.status, f.by or "")
    console.print(table)
    console.print(("[green]Signed off by " + review.signed_off_by(memo) + "[/green]")
                  if review.signed_off_by(memo) else "[yellow]Not signed off yet.[/yellow]")
    console.print(f"Memo: {path}")


def _records(args: argparse.Namespace) -> None:
    from .records import Records

    ok, n, msg = Records(load_config().path("records")).verify(args.run)
    console.print(("[green]OK[/green] " if ok else "[red]TAMPERED[/red] ") + msg)
    raise SystemExit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    load_env()
    p = argparse.ArgumentParser(prog="diligence", description="VC diligence agent (demo MVP)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Draft a brief (or rerun sections) for a company")
    r.add_argument("company")
    r.add_argument("--user", default="vp_alice", help="demo user (see config/users.yaml)")
    r.add_argument("--stage", default="brief", help="stage template from config/sections.yaml")
    r.add_argument("--section", action="append", help="rerun only this section (repeatable)")
    r.add_argument("--pick", help="CRM id to use when the name is ambiguous")
    r.add_argument("--quiet", action="store_true", help="don't print the memo")
    sub.add_parser("check-provider", help="Live-probe the configured LLM provider (a few cents)")
    q = sub.add_parser("ask", help="Quick cited answer from an existing brief (a few seconds)")
    q.add_argument("company")
    q.add_argument("question")
    q.add_argument("--user", default="vp_alice")
    q.add_argument("--pick")
    rv = sub.add_parser("review", help="Show review status; mark sections reviewed, verify/dispute figures")
    rv.add_argument("company")
    rv.add_argument("--user", default="vp_alice", help="the reviewer")
    rv.add_argument("--owner", help="whose draft (default: the reviewer's own)")
    rv.add_argument("--pick")
    rv.add_argument("--section", action="append", help="mark this section reviewed (repeatable)")
    rv.add_argument("--all-sections", action="store_true", help="mark every section reviewed")
    rv.add_argument("--verify", action="append", help="figure id to mark verified (repeatable)")
    rv.add_argument("--dispute", action="append", help="figure id to mark disputed (repeatable)")
    rv.add_argument("--note", help="note for a disputed figure")
    rc = sub.add_parser("records", help="Verify a run's hash-chained records")
    rc.add_argument("action", choices=["verify"])
    rc.add_argument("run")
    a = sub.add_parser("audit", help="Show the audit log")
    a.add_argument("--run", help="filter to one run id")
    args = p.parse_args(argv)
    if args.cmd == "run":
        asyncio.run(_run(args))
    elif args.cmd == "ask":
        asyncio.run(_ask(args))
    elif args.cmd == "review":
        _review(args)
    elif args.cmd == "records":
        _records(args)
    elif args.cmd == "check-provider":
        from .provider_check import run_checks

        ok = asyncio.run(run_checks(load_config(), console))
        raise SystemExit(0 if ok else 1)
    else:
        _audit(args)
