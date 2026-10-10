"""End-to-end through the real orchestrator, MCP server subprocess, executor, editor and
renderer - with a scripted stand-in for the Claude API, so it runs offline and free."""

import json
from types import SimpleNamespace

import pytest

from diligence import llm as llm_mod
from diligence import orchestrator
from diligence.data_access import AccessDenied
from diligence.ledger import Ledger

USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0, server_tool_use=None)


def _resp(content, stop, model):
    return SimpleNamespace(content=content, stop_reason=stop, usage=USAGE, model=model)


def _text(s):
    return SimpleNamespace(type="text", text=s, citations=None)


def _tool(i, name, args):
    return SimpleNamespace(type="tool_use", id=i, name=name, input=args)


class FakeClaude:
    """Section agents: read the deck, record one quote, answer. Editor: one bullet."""

    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kw):
        model, msgs = kw["model"], kw["messages"]
        if "editor" in kw["system"][:60].lower():
            ids = sorted(set(json.loads("[" + ",".join(
                f'"{x.strip()}"' for line in msgs[0]["content"].splitlines() if "[" in line
                for x in line.rsplit("[", 1)[1].rstrip("]").split(",")) + "]")))
            out = {"summary": [{"text": "Northwind sells picking robots as a service.", "evidence_ids": ids[:1]}],
                   "consistency_issues": []}
            return _resp([_text(json.dumps(out))], "end_turn", model)
        turn = sum(1 for m in msgs if m["role"] == "assistant")
        names = {t["name"] for t in kw.get("tools", [])}
        if turn == 0 and "read_file" in names:
            return _resp([_tool("t1", "read_file", {"file_id": "northwind/pitch_deck.md"})], "tool_use", model)
        if turn == 1 and "read_file" in names:
            return _resp([_tool("t2", "record_evidence", {
                "claim": "Pick rate", "source_id": "northwind/pitch_deck.md",
                "quote": "Picks 600 units/hour on mixed SKUs", "confidence": "verified"})], "tool_use", model)
        eid = None
        for m in msgs:
            if m["role"] == "user" and isinstance(m["content"], list):
                for r in m["content"]:
                    if "evidence_id" in str(r.get("content", "")):
                        eid = json.loads(r["content"])["evidence_id"]
        claims = [{"text": "The robot picks 600 units/hour.", "evidence_ids": [eid] if eid else []},
                  {"text": "Market leader in Europe.", "evidence_ids": []}]
        return _resp([_text(json.dumps({"claims": claims, "gaps": ["No benchmark data."]}))], "end_turn", model)


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(llm_mod, "make_client", lambda cfg: FakeClaude())


async def test_brief_for_alice_is_cited_and_persisted(cfg, fake_claude):
    events = []

    async def progress(e, s):
        events.append((e, s))

    r = await orchestrator.run(user="vp_alice", crm_id="crm-003", stage="brief", progress=progress, cfg=cfg)
    assert set(r.memo.sections) == {"snapshot", "team", "product", "competitors"}
    assert "The robot picks 600 units/hour. [" in r.markdown
    assert "*(unverified)*" in r.markdown  # the uncited claim is labelled, not hidden
    assert "Series B pitch deck**, p. 3" in r.markdown
    assert ("done", "product") in events and r.memo_path.exists()
    assert r.cost_usd > 0 and r.memo.version == 1
    # The MCP server (a separate process) audited its reads into this run's ledger.
    audit = Ledger(cfg.path("db")).audit_rows(r.run_id)
    assert any(row[5] == "read_file" and row[8] == "allowed" for row in audit)


async def test_single_section_rerun_keeps_other_sections(cfg, fake_claude):
    first = await orchestrator.run(user="vp_alice", crm_id="crm-003", cfg=cfg)
    team_run = first.memo.sections["team"].run_id
    second = await orchestrator.run(user="vp_alice", crm_id="crm-003", only=["competitors"], cfg=cfg)
    assert second.memo.version == 2
    assert second.memo.sections["team"].run_id == team_run
    assert second.memo.sections["competitors"].run_id == second.run_id
    assert list(second.memo.sections) == ["snapshot", "team", "product", "competitors"]


async def test_rerun_never_inherits_another_users_draft(cfg, fake_claude):
    await orchestrator.run(user="vp_alice", crm_id="crm-003", cfg=cfg)  # cites the data room
    r = await orchestrator.run(user="analyst_bob", crm_id="crm-003", only=["product"], cfg=cfg)
    assert list(r.memo.sections) == ["product"] and r.memo.version == 1
    assert "dataroom://" not in r.markdown


async def test_bob_gets_access_gap_and_no_data_room_tools(cfg, fake_claude):
    r = await orchestrator.run(user="analyst_bob", crm_id="crm-003", only=["product"], cfg=cfg)
    assert "not accessible to you" in r.markdown
    assert "dataroom://" not in r.markdown


async def test_users_off_the_deal_team_are_refused(cfg, fake_claude):
    with pytest.raises(AccessDenied):
        await orchestrator.run(user="analyst_bob", crm_id="crm-004", cfg=cfg)
