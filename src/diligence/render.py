"""Render the memo as Markdown and as Slack Block Kit."""

from __future__ import annotations

from typing import Any

from . import review
from .ledger import Ledger
from .models import EvidenceRecord, MemoState

BANNER = "AI-generated draft - not reviewed. Verify every figure against its source before use."
CONF_LABEL = {"verified": "Verified", "reported": "Reported", "inferred": "Inferred"}


class Citations:
    """Numbers sources in order of first appearance across the memo. Evidence recorded by
    different sections for the same source, page and quote shares one number."""

    def __init__(self, ledger: Ledger, memo: MemoState):
        ids = [e for p in memo.summary for e in p.evidence_ids]
        for s in memo.sections.values():
            ids += [e for c in s.claims for e in c.evidence_ids]
        ids += [e for p in memo.consistency_issues for e in p.evidence_ids]
        self.records: dict[str, EvidenceRecord] = ledger.get_evidence(sorted(set(ids)))
        self.order: dict[str, int] = {}      # evidence id -> citation number
        self.first: dict[int, str] = {}      # citation number -> representative evidence id
        keys: dict[tuple, int] = {}
        for e in ids:
            r = self.records.get(e)
            if r is None or e in self.order:
                continue
            key = (r.source_uri, r.page, " ".join(r.quote.lower().split()))
            if key not in keys:
                keys[key] = len(keys) + 1
                self.first[keys[key]] = e
            self.order[e] = keys[key]

    def refs(self, ids: list[str]) -> str:
        nums = sorted({self.order[e] for e in ids if e in self.order})
        return "".join(f"[{n}]" for n in nums)

    def sources(self) -> list[tuple[int, EvidenceRecord]]:
        return [(n, self.records[e]) for n, e in sorted(self.first.items())]


def banner(memo: MemoState) -> str:
    st = review.status(memo)
    if who := review.signed_off_by(memo):
        return f"Reviewed and signed off by {who} ({st.line()})."
    return f"{BANNER} Review status: {st.line()}."


def gaps(memo: MemoState) -> list[str]:
    out = list(memo.access_gaps)
    for s in memo.sections.values():
        if s.status != "complete":
            out.append(f"{s.title}: section is {s.status}.")
        out += [f"{s.title}: {g}" for g in s.gaps]
        n_unv = sum(c.unverified for c in s.claims)
        if n_unv:
            out.append(f"{s.title}: {n_unv} statement(s) had no recorded evidence and are marked unverified.")
        for fc in s.coverage:
            if not fc.complete:
                out.append(f"{s.title}: read {len(set(fc.pages_read))}/{fc.pages_total} pages of "
                           f"\"{fc.title}\" - the rest was not reviewed.")
        n_unsup = sum(c.support == "unsupported" for c in s.claims)
        if n_unsup:
            out.append(f"{s.title}: {n_unsup} statement(s) cite evidence that does not support them.")
        for page in s.ignored_doc_pages:
            out.append(f"{s.title}: withheld {page} - it contains instructions aimed at AI systems "
                       "(possible prompt injection). Review it manually.")
        if s.ignored_web_pages:
            out.append(f"{s.title}: {s.ignored_web_pages} web page(s) ignored because they "
                       "contained instructions aimed at AI systems.")
    return out


def to_markdown(ledger: Ledger, memo: MemoState) -> str:
    cites = Citations(ledger, memo)
    stage = "Pre-meeting brief" if memo.stage == "brief" else memo.stage.replace("_", " ").title()
    md = [f"# {stage}: {memo.company}", "", f"> **{banner(memo)}**  ",
          f"> Requested by {memo.requested_by} · version {memo.version} · CRM {memo.deal_id}", ""]
    if memo.summary:
        md += ["## Summary", ""] + [f"- {p.text} {cites.refs(p.evidence_ids)}" for p in memo.summary] + [""]
    g = gaps(memo)
    md += ["## Gaps and unverified", ""] + ([f"- {x}" for x in g] or ["- None reported."]) + [""]
    for s in memo.sections.values():
        md += [f"## {s.title}", ""]
        if not s.claims:
            md.append("_No supported findings._")
        for c in s.claims:
            tag = " *(unverified)*" if c.unverified else ""
            if c.support == "unsupported":
                tag = f" *(unverified: citation doesn't support this - {c.support_note})*"
            elif c.support == "partial":
                tag = f" *(partly supported: {c.support_note})*"
            md.append(f"- {c.text} {cites.refs(c.evidence_ids)}{tag}")
        md += ["", f"<sub>Drafted {s.generated_at} · run {s.run_id}</sub>", ""]
    if memo.figures:
        md += ["## Figures to verify", "",
               "Every financial figure must be checked against its source by a person on the deal team "
               "before this memo can be signed off.", ""]
        for f in memo.figures:
            mark = {"pending": "[ ]", "verified": "[x]", "disputed": "[!]"}[f.status]
            who = f" - {f.status} by {f.by}" + (f": {f.note}" if f.note else "") if f.by else ""
            md.append(f"- {mark} `{f.id}` {f.text} {cites.refs(f.evidence_ids)}{who}")
        md.append("")
    if memo.consistency_issues:
        md += ["## Consistency checks", ""]
        md += [f"- {p.text} {cites.refs(p.evidence_ids)}" for p in memo.consistency_issues] + [""]
    md += ["## Sources", ""]
    for n, r in cites.sources():
        where = f", p. {r.page}" if r.page else ""
        as_of = f" · as of {r.as_of}" if r.as_of else ""
        md.append(f"{n}. **{r.title}**{where} ({CONF_LABEL[r.confidence]}{as_of}) - "
                  f"{r.source_uri}  \n   > \"{r.quote}\"")
    if memo.changelog:
        md += ["", "## Changelog", ""] + [f"- {c}" for c in memo.changelog]
    return "\n".join(md) + "\n"


def slack_summary(ledger: Ledger, memo: MemoState, cost_usd: float, seconds: float) -> list[dict[str, Any]]:
    cites = Citations(ledger, memo)
    bullets = "\n".join(f"• {p.text} {cites.refs(p.evidence_ids)}" for p in memo.summary) \
        or "_No summary - see sections._"
    g = gaps(memo)
    gap_text = "\n".join(f"• {x}" for x in g[:8]) + (f"\n…and {len(g) - 8} more" if len(g) > 8 else "")
    sources = "\n".join(f"[{n}] {r.title} ({CONF_LABEL[r.confidence]})" for n, r in cites.sources()[:10])
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Brief: {memo.company}"[:150]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            (":white_check_mark: " if review.signed_off_by(memo) else ":warning: ") + banner(memo)}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{bullets}"[:2900]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Gaps and unverified*\n{gap_text or '• None reported.'}"[:2900]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sources*\n{sources or '—'}"[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{len(memo.sections)} sections · {seconds:.0f}s · ${cost_usd:.2f} · v{memo.version} · "
            "reply in this thread with e.g. `redo competitors` to refresh one section"}]},
    ]
