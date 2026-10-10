"""Citation-support check: does each claim's cited evidence actually back it?

The grounding check in section_agent already proves every quote exists in a source the
agent read. This catches the next failure: a real quote attached to a claim it doesn't
support (wrong number, overreach, a different company). One cheap model call per section.
"""

from __future__ import annotations

import json

from ..llm import LLM, BudgetExceeded, RefusalError, parse_json
from ..models import Claim, EvidenceRecord

SYSTEM = """You check citations in an investment memo. For each numbered claim you get the
exact quotes it cites. Judge only whether the quotes support the claim:
- supported: every specific fact in the claim (names, numbers, dates) is stated in the quotes.
- partial: the gist is supported but some detail (a number, date, or qualifier) is not, or the
  claim is labelled "Inferred:" and reasonably follows from the quotes.
- unsupported: the quotes do not back the claim, or contradict it.
Be strict about numbers. Give a short reason for anything not "supported"."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


async def check_support(llm: LLM, section: str, claims: list[Claim],
                        evidence: dict[str, EvidenceRecord]) -> None:
    """Annotate claims in place. Uncited claims stay unverified and are not sent."""
    items = []
    for i, c in enumerate(claims):
        quotes = [evidence[e].quote for e in c.evidence_ids if e in evidence]
        if quotes:
            items.append({"index": i, "claim": c.text, "quotes": quotes})
    if not items:
        return
    try:
        resp = await llm.create("screen", section=section, system=SYSTEM, max_tokens=4000,
                                schema=SCHEMA, messages=[{"role": "user", "content": json.dumps(items)}])
        results = parse_json(resp)["results"]
    except (BudgetExceeded, RefusalError, ValueError, KeyError):
        return  # claims stay "unchecked"; the memo says so
    for r in results:
        if not 0 <= r["index"] < len(claims):
            continue
        c = claims[r["index"]]
        c.support = r["verdict"]
        c.support_note = r["reason"] if r["verdict"] != "supported" else ""
        if r["verdict"] == "unsupported":
            c.unverified = True
