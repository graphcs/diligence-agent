"""Editor pass: a short summary and cross-section consistency checks, cited by id."""

from __future__ import annotations

from ..llm import LLM, BudgetExceeded, RefusalError, parse_json
from ..models import MemoState, SummaryPoint
from .. import guards

SYSTEM = """You are the editor of an investment diligence brief drafted by several analysts.
Using only the claims provided (each with evidence ids), write:
1. summary: 3-6 bullets a partner can read in 30 seconds. Each bullet cites the evidence
   ids of the claims it draws on. No recommendation, no new facts.
2. consistency_issues: places where sections contradict each other or the same metric
   has different values (e.g. two ARR figures). Cite the evidence ids involved. Empty if none.
Do not invent ids."""

SCHEMA = {
    "type": "object",
    "properties": {
        k: {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_ids"],
                "additionalProperties": False,
            },
        }
        for k in ("summary", "consistency_issues")
    },
    "required": ["summary", "consistency_issues"],
    "additionalProperties": False,
}


async def edit(llm: LLM, memo: MemoState) -> None:
    lines, known = [], set()
    for sec in memo.sections.values():
        lines.append(f"## {sec.title}")
        for c in sec.claims:
            if c.evidence_ids:
                known.update(c.evidence_ids)
                lines.append(f"- {c.text} [{', '.join(c.evidence_ids)}]")
    if not known:
        memo.summary, memo.consistency_issues = [], []
        return
    try:
        resp = await llm.create("editor", section="editor", system=SYSTEM, max_tokens=8000,
                                schema=SCHEMA, messages=[{"role": "user", "content": "\n".join(lines)}])
        out = parse_json(resp)
    except (BudgetExceeded, RefusalError, ValueError):
        return

    def clean(items: list[dict]) -> list[SummaryPoint]:
        pts = []
        for i in items:
            ids = [e for e in i["evidence_ids"] if e in known]
            if ids:  # an uncited summary bullet is dropped, not shown
                pts.append(SummaryPoint(text=guards.strip_markup(i["text"]), evidence_ids=ids))
        return pts

    memo.summary = clean(out["summary"])
    memo.consistency_issues = clean(out["consistency_issues"])
