"""Render the memo as Markdown and as Slack Block Kit."""

from __future__ import annotations

from typing import Any

from . import review
from .ledger import Ledger
from .models import EvidenceRecord, MemoState

BANNER = "AI-generated draft - not reviewed. Verify every figure against its source before use."
CONF_LABEL = {"verified": "Verified", "reported": "Reported", "inferred": "Inferred"}
CONF_ICON = {"verified": ":large_green_circle:", "reported": ":large_yellow_circle:", "inferred": ":white_circle:"}
STATE_ICON = {"draft": ":warning:", "in_review": ":hourglass_flowing_sand:", "signed_off": ":white_check_mark:"}
SECTION_ICON = {"complete": ":white_check_mark:", "partial": ":large_orange_circle:", "failed": ":red_circle:"}
STAGE_LABEL = {"brief": "Pre-meeting brief", "deep_dive": "Deep dive"}


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


def review_state(memo: MemoState) -> str:
    """'draft' (no human action yet), 'in_review' or 'signed_off'."""
    st = review.status(memo)
    if st.signed_off:
        return "signed_off"
    touched = st.sections_reviewed or st.figures_verified or st.figures_disputed
    return "in_review" if touched else "draft"


def status_line(memo: MemoState) -> str:
    """Review state in one line, e.g. 'In review · 2/4 sections reviewed · 3/6 figures verified'."""
    st = review.status(memo)
    state = review_state(memo)
    if state == "signed_off":
        return f"Signed off by {review.signed_off_by(memo)} · {st.line()}"
    return f"{'In review' if state == 'in_review' else 'AI draft, not yet reviewed'} · {st.line()}"


def banner(memo: MemoState) -> str:
    if review_state(memo) == "signed_off":
        return f"Reviewed and signed off by {review.signed_off_by(memo)} ({review.status(memo).line()})."
    if review_state(memo) == "in_review":
        return f"AI-generated draft - review in progress. Review status: {review.status(memo).line()}."
    return f"{BANNER} Review status: {review.status(memo).line()}."


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


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
    """The summary card. Re-rendered in place after every review action, so its status line
    always matches the memo's review state."""
    cites = Citations(ledger, memo)
    st = review.status(memo)
    state = review_state(memo)
    bullets = "\n".join(f"• {p.text} {cites.refs(p.evidence_ids)}" for p in memo.summary) \
        or "_No summary - see the sections in the memo file._"
    sections = []
    for key, s in memo.sections.items():
        r = memo.reviews.get(key)
        tail = f"  _reviewed by {r.by}_" if r else ""
        partial = "" if s.status == "complete" else f" _({s.status})_"
        sections.append(f"{SECTION_ICON.get(s.status, ':white_circle:')} {s.title}{partial}{tail}")
    g = gaps(memo)
    gap_text = "\n".join(f"• {x}" for x in g[:5]) + (f"\n_+{len(g) - 5} more in the memo file_" if len(g) > 5 else "")
    srcs = cites.sources()
    sources = "\n".join(f"{CONF_ICON[r.confidence]} [{n}] {r.title}" + (f", p. {r.page}" if r.page else "")
                        for n, r in srcs[:6])
    if len(srcs) > 6:
        sources += f"\n_+{len(srcs) - 6} more in the memo file_"
    figures = f"{st.figures_verified}/{st.figures_total} verified" if st.figures_total else "none"
    stage = STAGE_LABEL.get(memo.stage, memo.stage.replace("_", " ").title())
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"📄 {stage} · {memo.company}"[:150]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{STATE_ICON[state]} *{status_line(memo)}*"}]},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Sections reviewed*\n{progress_bar(st.sections_reviewed, st.sections_total, 8)} "
                                       f"{st.sections_reviewed}/{st.sections_total}"},
            {"type": "mrkdwn", "text": f"*Financial figures*\n{figures}"
                                       + (f" · {st.figures_disputed} disputed" if st.figures_disputed else "")},
            {"type": "mrkdwn", "text": f"*Version*\nv{memo.version} · requested by {memo.requested_by}"},
            {"type": "mrkdwn", "text": f"*Run*\n{seconds:.0f}s · ${cost_usd:.2f}"},
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Key points*\n{bullets}"[:2900]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Sections*\n" + "\n".join(sections)}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*:mag: Gaps and unverified*\n{gap_text or '• None reported.'}"[:2900]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sources*\n{sources or '—'}"[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":speech_balloon: Reply in this thread to ask a question (e.g. _what's their ARR?_) "
            "or refresh one section (e.g. `redo competitors`)."}]},
    ]
