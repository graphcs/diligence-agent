"""Human-in-the-loop gates: AI drafts, humans sign.

- Every section must be marked reviewed by a person on the deal team.
- Every financial figure (money, %, ARR/burn/margin/valuation...) must be verified or
  disputed by a person against its source.
- Only when both are complete is the memo "signed off"; until then it carries the AI-draft
  banner. Rerunning a section resets its review and its figures.
- Every human action is written to the audit log (actor=human) and the run records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .data_access import AccessContext, AccessDenied, DataAccess
from .ledger import Ledger, now
from .models import Figure, MemoState, SectionReview
from .records import Records

FINANCIAL_RE = re.compile(
    r"\$\s?\d|€\s?\d|£\s?\d|\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s?x\b"
    r"|\b(arr|revenue|burn|runway|margin|valuation|nrr|retention|raised|funding|round|ebitda|cash)\b[^.]{0,60}\d",
    re.IGNORECASE)


def is_financial(text: str) -> bool:
    return bool(FINANCIAL_RE.search(text))


def refresh_figures(memo: MemoState, sections: list[str]) -> None:
    """(Re)detect figures for sections that were just drafted and reset their reviews."""
    memo.figures = [f for f in memo.figures if f.section not in sections]
    for key in sections:
        memo.reviews.pop(key, None)
        sec = memo.sections.get(key)
        if not sec:
            continue
        for i, c in enumerate(sec.claims):
            if is_financial(c.text):
                memo.figures.append(Figure(id=f"{key}-{i + 1}", section=key, text=c.text,
                                           evidence_ids=c.evidence_ids))


@dataclass
class Status:
    sections_total: int
    sections_reviewed: int
    figures_total: int
    figures_verified: int
    figures_disputed: int

    @property
    def figures_pending(self) -> int:
        return self.figures_total - self.figures_verified - self.figures_disputed

    @property
    def signed_off(self) -> bool:
        return (self.sections_total > 0 and self.sections_reviewed == self.sections_total
                and self.figures_pending == 0 and self.figures_disputed == 0)

    def line(self) -> str:
        parts = [f"{self.sections_reviewed}/{self.sections_total} sections reviewed"]
        if self.figures_total:
            parts.append(f"{self.figures_verified}/{self.figures_total} figures verified")
        if self.figures_disputed:
            parts.append(f"{self.figures_disputed} disputed")
        return " · ".join(parts)


def status(memo: MemoState) -> Status:
    return Status(
        sections_total=len(memo.sections),
        sections_reviewed=sum(k in memo.reviews for k in memo.sections),
        figures_total=len(memo.figures),
        figures_verified=sum(f.status == "verified" for f in memo.figures),
        figures_disputed=sum(f.status == "disputed" for f in memo.figures),
    )


def signed_off_by(memo: MemoState) -> str | None:
    if not status(memo).signed_off:
        return None
    last = max(memo.reviews.values(), key=lambda r: r.at)
    return f"{last.by} on {last.at[:10]}"


class Reviewer:
    """Applies human review actions with permission checks, audit and records."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ledger = Ledger(cfg.path("db"))
        self.access = DataAccess(cfg, self.ledger)
        self.records = Records(cfg.path("records"))

    def _authorize(self, reviewer: str, memo: MemoState) -> None:
        """Reviewer must be on the deal team and able to see everything the memo cites."""
        ctx = AccessContext(user=reviewer, deal_id=memo.deal_id, run_id="review", actor="human")
        deal = self.access.get_deal(ctx, memo.deal_id)  # raises AccessDenied
        uses_room = any(e.startswith("dataroom://") for e in self._cited_uris(memo))
        if uses_room and (reason := self.access.data_room_access(ctx, deal)):
            raise AccessDenied(f"{reviewer} cannot review: {reason}")

    def _cited_uris(self, memo: MemoState) -> list[str]:
        ids = [e for s in memo.sections.values() for c in s.claims for e in c.evidence_ids]
        return [r.source_uri for r in self.ledger.get_evidence(sorted(set(ids))).values()]

    def _log(self, reviewer: str, memo: MemoState, tool: str, args: dict, decision: str) -> None:
        self.ledger.audit(run_id="review", user=reviewer, deal_id=memo.deal_id, actor="human",
                          tool=tool, args=args, returned=[], decision=decision)
        self.records.append(f"review-{memo.deal_id}-{memo.requested_by}", "human_action",
                            {"reviewer": reviewer, "action": tool, "args": args,
                             "decision": decision, "memo_version": memo.version})

    def review_section(self, memo: MemoState, reviewer: str, section: str) -> None:
        if section not in memo.sections:
            raise KeyError(f"no section {section!r} in this memo")
        self._authorize(reviewer, memo)
        if section in memo.reviews:
            return  # already reviewed (e.g. a double-click): nothing to change or log
        memo.reviews[section] = SectionReview(by=reviewer, at=now())
        self._log(reviewer, memo, "review_section", {"section": section}, "reviewed")

    def set_figure(self, memo: MemoState, reviewer: str, figure_id: str, verdict: str,
                   note: str = "") -> Figure:
        if verdict not in ("verified", "disputed"):
            raise ValueError("verdict must be verified or disputed")
        fig = next((f for f in memo.figures if f.id == figure_id), None)
        if fig is None:
            raise KeyError(f"no figure {figure_id!r}")
        self._authorize(reviewer, memo)
        if fig.status == verdict:
            return fig  # no change (e.g. a double-click)
        fig.status, fig.by, fig.at, fig.note = verdict, reviewer, now(), note
        self._log(reviewer, memo, "verify_figure", {"figure": figure_id, "note": note}, verdict)
        return fig
