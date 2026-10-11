"""Quick Q&A (pull mode): answer a question from the user's existing memo in a few seconds.

No tools and no web: one model call over the memo's claims and their exact evidence
quotes, so latency is predictable (target under 5s) and every answer is cited. If the memo
doesn't cover the question, it says so and suggests which section to refresh.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .config import Config
from .data_access import AccessContext, DataAccess
from .ledger import Ledger
from .llm import LLM, RunContext, parse_json
from .models import EvidenceRecord
from .orchestrator import load_memo

SYSTEM = """You answer an investor's question using ONLY the diligence memo provided: its
claims and the exact evidence quotes behind them. Rules:
- Answer in 1-4 short sentences. Cite the evidence ids you used.
- If sources disagree, give both values and say they disagree.
- Never use outside knowledge. If the memo does not answer the question, set confident to
  false, say what is missing, and name the memo section that should be refreshed.
- Do not give investment recommendations."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "confident": {"type": "boolean"},
        "suggest_section": {"type": "string"},
    },
    "required": ["answer", "evidence_ids", "confident", "suggest_section"],
    "additionalProperties": False,
}


@dataclass
class Answer:
    text: str
    confident: bool
    suggest_section: str | None
    seconds: float
    sources: list[tuple[int, EvidenceRecord]] = field(default_factory=list)
    has_memo: bool = True

    def markdown(self) -> str:
        if not self.sources:
            return self.text
        refs = "\n".join(f"[{n}] {r.title}" + (f", p. {r.page}" if r.page else "")
                         + f" ({r.confidence})" for n, r in self.sources)
        return f"{self.text}\n\n{refs}"

    def source_lines(self) -> list[str]:
        return [f"[{n}] {r.title}" + (f", p. {r.page}" if r.page else "") + f" · {r.confidence}"
                for n, r in self.sources]


async def answer(cfg: Config, *, user: str, crm_id: str, question: str) -> Answer:
    t0 = time.monotonic()
    ledger = Ledger(cfg.path("db"))
    # Still on the deal team? (Access can be revoked after the memo was drafted.)
    DataAccess(cfg, ledger).get_deal(AccessContext(user=user, deal_id=crm_id, run_id="qa",
                                                   actor="orchestrator"), crm_id)
    memo = load_memo(cfg, crm_id, user)
    if memo is None:
        return Answer("There's no brief for this company yet.", False, None,
                      time.monotonic() - t0, has_memo=False)

    ids = sorted({e for s in memo.sections.values() for c in s.claims for e in c.evidence_ids})
    ev = ledger.get_evidence(ids)
    lines = [f"Company: {memo.company} (memo v{memo.version})"]
    for key, s in memo.sections.items():
        lines.append(f"\n## {s.title} (section key: {key})")
        for c in s.claims:
            tag = " [unverified]" if c.unverified else ""
            lines.append(f"- {c.text}{tag} [{', '.join(c.evidence_ids)}]")
    lines.append("\n## Evidence")
    for i in ids:
        if r := ev.get(i):
            lines.append(f"[{i}] {r.title}{f', p. {r.page}' if r.page else ''} "
                         f"({r.confidence}{f', as of {r.as_of}' if r.as_of else ''}): \"{r.quote}\"")
    context = "\n".join(lines)

    run_id = f"qa-{uuid.uuid4().hex[:10]}"
    llm = LLM(cfg, ledger, RunContext(run_id=run_id, user=user, deal_id=crm_id))
    resp = await llm.create("qa", section="qa", system=SYSTEM, max_tokens=2000, schema=SCHEMA,
                            messages=[{"role": "user", "content":
                                       f"<memo>\n{context}\n</memo>\n\nQuestion: {question}"}])
    out = parse_json(resp)
    used = [e for e in out["evidence_ids"] if e in ev]
    numbered = [(n, ev[e]) for n, e in enumerate(used, start=1)]
    text = out["answer"]
    for n, e in enumerate(used, start=1):  # turn ids into [n] references
        text = text.replace(f"[{e}]", f"[{n}]").replace(e, f"[{n}]")
    sec = out["suggest_section"].strip()
    return Answer(text=text, confident=bool(out["confident"] and used),
                  suggest_section=sec if sec in cfg.sections else None,
                  seconds=time.monotonic() - t0, sources=numbered)
