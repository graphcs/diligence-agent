"""Editor pass: a short summary, cross-section consistency checks and diagram data, cited by id."""

from __future__ import annotations

from ..llm import LLM, BudgetExceeded, RefusalError, parse_json
from ..models import (Diagrams, FundingRound, MemoState, ModelEdge, Quadrant, QuadrantPoint,
                      RevenuePoint, RevenueTrend, SummaryPoint)
from .. import guards

SYSTEM = """You are the editor of an investment diligence brief drafted by several analysts.
Using only the claims provided (each with evidence ids), write:
1. summary: 3-6 bullets a partner can read in 30 seconds. Each bullet cites the evidence
   ids of the claims it draws on. No recommendation, no new facts.
2. consistency_issues: places where sections contradict each other or the same metric
   has different values (e.g. two ARR figures). Cite the evidence ids involved. Empty if none.
3. diagrams: structured data for business diagrams. Use only facts in the claims, cite the
   evidence ids each item rests on, and leave a part empty when the claims don't support it.
   - quadrant: the company and its named competitors placed on two axes that the evidence
     discusses (e.g. "Narrow" to "Broad" product scope). x and y are 0-1.
   - revenue: ARR or revenue values with their period (e.g. "FY2025", "Q2 2026"), oldest
     first, as plain numbers in one unit (e.g. unit "$M"). Only cited figures.
   - business_model: who pays or supplies whom, as edges between short names (e.g. source
     "Mid-size 3PLs", target the company, label "per-robot subscription").
   - funding: cited funding rounds with date, round name, amount and lead investor. Use ""
     for any part the claims don't state (never "Not disclosed" or "Unknown").
Do not invent ids."""

def _obj(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _cited(**props: dict) -> dict:
    return _obj(**props, evidence_ids={"type": "array", "items": {"type": "string"}})


def _list(item: dict) -> dict:
    return {"type": "array", "items": item}


STR, NUM = {"type": "string"}, {"type": "number"}

DIAGRAMS_SCHEMA = _obj(
    quadrant=_obj(x_low=STR, x_high=STR, y_low=STR, y_high=STR,
                  points=_list(_cited(name=STR, x=NUM, y=NUM))),
    revenue=_obj(metric=STR, unit=STR, points=_list(_cited(period=STR, value=NUM))),
    business_model=_list(_cited(source=STR, target=STR, label=STR)),
    funding=_list(_cited(date=STR, round=STR, amount=STR, lead=STR)),
)

SCHEMA = _obj(summary=_list(_cited(text=STR)), consistency_issues=_list(_cited(text=STR)),
              diagrams=DIAGRAMS_SCHEMA)


async def edit(llm: LLM, memo: MemoState) -> None:
    lines, known = [], set()
    for sec in memo.sections.values():
        lines.append(f"## {sec.title}")
        for c in sec.claims:
            if c.evidence_ids:
                known.update(c.evidence_ids)
                lines.append(f"- {c.text} [{', '.join(c.evidence_ids)}]")
    if not known:
        memo.summary, memo.consistency_issues, memo.diagrams = [], [], Diagrams()
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
    memo.diagrams = clean_diagrams(out.get("diagrams") or {}, known,
                                   has_financials="financials" in memo.sections)


def clean_diagrams(raw: dict, known: set[str], has_financials: bool = True) -> Diagrams:
    """Keep only diagram items that cite known evidence; strip markup from every label."""
    def ids(item: dict) -> list[str]:
        return [e for e in item.get("evidence_ids", []) if e in known]

    def txt(v: object) -> str:
        return guards.strip_markup(str(v or ""))

    def unit(v: object) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    q, r = raw.get("quadrant") or {}, raw.get("revenue") or {}
    quadrant = Quadrant(
        x_low=txt(q.get("x_low")), x_high=txt(q.get("x_high")),
        y_low=txt(q.get("y_low")), y_high=txt(q.get("y_high")),
        points=[QuadrantPoint(name=txt(p.get("name")), x=unit(p.get("x")), y=unit(p.get("y")),
                              evidence_ids=ids(p))
                for p in q.get("points", []) if ids(p) and txt(p.get("name"))])
    revenue = RevenueTrend(metric=txt(r.get("metric")) or "ARR", unit=txt(r.get("unit")))
    if has_financials:
        for p in r.get("points", []):
            if ids(p) and txt(p.get("period")) and isinstance(p.get("value"), (int, float)):
                revenue.points.append(RevenuePoint(period=txt(p["period"]), value=float(p["value"]),
                                                   evidence_ids=ids(p)))
    edges = [ModelEdge(source=txt(e.get("source")), target=txt(e.get("target")),
                       label=txt(e.get("label")), evidence_ids=ids(e))
             for e in raw.get("business_model", [])
             if ids(e) and txt(e.get("source")) and txt(e.get("target"))]
    funding = [FundingRound(date=txt(f.get("date")), round=txt(f.get("round")),
                            amount=txt(f.get("amount")), lead=txt(f.get("lead")), evidence_ids=ids(f))
               for f in raw.get("funding", []) if ids(f) and txt(f.get("date")) and txt(f.get("round"))]
    return Diagrams(quadrant=quadrant, revenue=revenue, business_model=edges, funding=funding)
