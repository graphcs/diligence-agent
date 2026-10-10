"""Review gates and quick Q&A, offline."""

import json
from types import SimpleNamespace

import pytest

from diligence import llm as llm_mod
from diligence import orchestrator, qa, review
from diligence.data_access import AccessDenied
from diligence.models import Claim, EvidenceRecord, MemoState, SectionDraft


def memo_with_figures() -> MemoState:
    m = MemoState(deal_id="crm-003", company="Northwind Robotics", stage="brief", requested_by="vp_alice")
    m.sections["snapshot"] = SectionDraft(key="snapshot", title="Company snapshot", claims=[
        Claim(text="ARR was $4.8M in August 2026", evidence_ids=["e1"]),
        Claim(text="Founded in 2022 by Maya Okafor", evidence_ids=["e2"]),
        Claim(text="Gross margin is 41%", evidence_ids=["e3"])])
    m.sections["team"] = SectionDraft(key="team", title="Team", claims=[Claim(text="CTO holds 3 patents")])
    return m


def test_only_financial_claims_become_figures():
    m = memo_with_figures()
    review.refresh_figures(m, ["snapshot", "team"])
    assert [f.id for f in m.figures] == ["snapshot-1", "snapshot-3"]
    assert review.status(m).line() == "0/2 sections reviewed · 0/2 figures verified"


def test_sign_off_needs_every_section_and_figure(cfg):
    m = memo_with_figures()
    review.refresh_figures(m, ["snapshot", "team"])
    r = review.Reviewer(cfg)
    r.review_section(m, "vp_alice", "snapshot")
    r.review_section(m, "vp_alice", "team")
    r.set_figure(m, "vp_alice", "snapshot-1", "verified")
    assert review.signed_off_by(m) is None
    r.set_figure(m, "vp_alice", "snapshot-3", "disputed", note="deck says 41%, KPIs say 41% only in Aug")
    assert review.signed_off_by(m) is None  # a disputed figure blocks sign-off
    r.set_figure(m, "vp_alice", "snapshot-3", "verified")
    assert review.signed_off_by(m).startswith("vp_alice on ")
    rows = r.ledger.audit_rows("review")
    actions = [x for x in rows if x[5] in ("review_section", "verify_figure")]
    assert {x[4] for x in rows} == {"human"} and len(actions) == 5


def test_rerun_resets_that_sections_review_and_figures(cfg):
    m = memo_with_figures()
    review.refresh_figures(m, ["snapshot", "team"])
    r = review.Reviewer(cfg)
    r.review_section(m, "vp_alice", "snapshot")
    r.set_figure(m, "vp_alice", "snapshot-1", "verified")
    review.refresh_figures(m, ["snapshot"])
    assert "snapshot" not in m.reviews and all(f.status == "pending" for f in m.figures)


def test_non_deal_team_member_cannot_review(cfg):
    m = memo_with_figures()
    m.deal_id = "crm-004"
    with pytest.raises(AccessDenied):
        review.Reviewer(cfg).review_section(m, "analyst_bob", "team")


class QAClient:
    def __init__(self, out):
        self.out = out
        self.messages = SimpleNamespace(create=self.create)
        self.seen = None

    async def create(self, **kw):
        self.seen = kw["messages"][0]["content"]
        usage = SimpleNamespace(input_tokens=500, output_tokens=40, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, server_tool_use=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.out))],
                               stop_reason="end_turn", usage=usage, model="qa")


async def test_qa_answers_from_memo_with_numbered_citations(cfg, ledger, monkeypatch):
    m = memo_with_figures()
    ledger.add_evidence(EvidenceRecord(id="e1", run_id="r", deal_id="crm-003", section="snapshot",
                                       claim="ARR", quote="ARR $4.8M (August 2026)", source_type="doc",
                                       source_id="northwind/pitch_deck.md", source_uri="dataroom://x",
                                       title="Series B pitch deck", page=5, confidence="verified"))
    orchestrator.save_memo(cfg, m, "vp_alice")
    client = QAClient({"answer": "ARR was $4.8M in August 2026 [e1].", "evidence_ids": ["e1"],
                       "confident": True, "suggest_section": ""})
    monkeypatch.setattr(llm_mod, "make_client", lambda c: client)
    a = await qa.answer(cfg, user="vp_alice", crm_id="crm-003", question="What is ARR?")
    assert a.text == "ARR was $4.8M in August 2026 [1]." and a.confident
    assert a.markdown().endswith("[1] Series B pitch deck, p. 5 (verified)")
    assert '"ARR $4.8M (August 2026)"' in client.seen  # the model saw the exact evidence


async def test_qa_without_memo_or_access(cfg, monkeypatch):
    monkeypatch.setattr(llm_mod, "make_client", lambda c: QAClient({}))
    a = await qa.answer(cfg, user="analyst_bob", crm_id="crm-001", question="Who founded it?")
    assert not a.has_memo
    with pytest.raises(AccessDenied):
        await qa.answer(cfg, user="analyst_bob", crm_id="crm-004", question="ARR?")


async def test_qa_not_covered_suggests_section(cfg, monkeypatch):
    orchestrator.save_memo(cfg, memo_with_figures(), "vp_alice")
    monkeypatch.setattr(llm_mod, "make_client", lambda c: QAClient(
        {"answer": "The brief doesn't cover burn.", "evidence_ids": [], "confident": False,
         "suggest_section": "financials"}))
    a = await qa.answer(cfg, user="vp_alice", crm_id="crm-003", question="What's the burn?")
    assert not a.confident and a.suggest_section == "financials"


def test_repeat_clicks_are_not_logged_twice(cfg):
    m = memo_with_figures()
    review.refresh_figures(m, ["snapshot", "team"])
    r = review.Reviewer(cfg)
    for _ in range(3):
        r.review_section(m, "vp_alice", "team")
        r.set_figure(m, "vp_alice", "snapshot-1", "verified")
    actions = [x for x in r.ledger.audit_rows("review") if x[5] in ("review_section", "verify_figure")]
    assert len(actions) == 2
