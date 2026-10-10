"""Grounding checks in the section executor, using the real data layer and no model."""

import json
from types import SimpleNamespace

import pytest

from diligence.agents.section_agent import SectionContext, SectionExecutor
from diligence.llm import LLM, BudgetExceeded, RunContext, ToolFailure, run_tool_loop

from .conftest import ctx


class FakeDocs:
    """Mimics an MCP ClientSession backed by DataAccess."""

    def __init__(self, access, c):
        self.access, self.c = access, c

    async def call_tool(self, name, args):
        try:
            data = getattr(self.access, name)(self.c, **args)
            return SimpleNamespace(is_error=False, content=[SimpleNamespace(text=json.dumps(data))])
        except Exception as e:  # noqa: BLE001
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=f"Access denied: {e}")])


def make_executor(cfg, ledger, access, user="vp_alice"):
    deal = access.get_deal(ctx(user), "crm-003")
    sc = SectionContext(key="product", spec=cfg.sections["product"], company="Northwind Robotics",
                        deal=deal, run_id="testrun12345", docs=FakeDocs(access, ctx(user)),
                        docs_tools=[], data_room_note=None)
    llm = LLM(cfg, ledger, RunContext("testrun12345", user, "crm-003"), client=SimpleNamespace())
    return SectionExecutor(cfg, ledger, llm, sc)


async def test_evidence_requires_a_source_that_was_actually_read(cfg, ledger, access):
    ex = make_executor(cfg, ledger, access)
    with pytest.raises(ToolFailure, match="Unknown source_id"):
        await ex.execute("record_evidence", {"claim": "x", "source_id": "northwind/pitch_deck.md",
                                             "quote": "Picks 600 units/hour", "confidence": "verified"})
    await ex.execute("read_file", {"file_id": "northwind/pitch_deck.md"})
    with pytest.raises(ToolFailure, match="Quote not found"):
        await ex.execute("record_evidence", {"claim": "x", "source_id": "northwind/pitch_deck.md",
                                             "quote": "Picks 900 units/hour", "confidence": "verified"})
    out = json.loads(await ex.execute("record_evidence", {
        "claim": "Pick rate", "source_id": "northwind/pitch_deck.md",
        "quote": "Picks 600 units/hour on mixed SKUs", "confidence": "verified"}))
    rec = ledger.get_evidence([out["evidence_id"]])[out["evidence_id"]]
    assert rec.page == 3 and rec.source_type == "doc"


async def test_crm_record_is_a_citable_source(cfg, ledger, access):
    ex = make_executor(cfg, ledger, access)
    out = json.loads(await ex.execute("record_evidence", {
        "claim": "Stage", "source_id": "crm:crm-003", "quote": "Deep dive", "confidence": "verified"}))
    assert out["evidence_id"]


async def test_coverage_tracks_partial_reads(cfg, ledger, access):
    ex = make_executor(cfg, ledger, access)
    await ex.execute("get_file_manifest", {"file_id": "northwind/market_study_2026.txt"})
    await ex.execute("read_file", {"file_id": "northwind/market_study_2026.txt", "start_page": 1})
    fc = ex.coverage["northwind/market_study_2026.txt"]
    assert fc.pages_total == 180 and len(fc.pages_read) == 20 and not fc.complete


async def test_denied_reads_surface_as_tool_failures(cfg, ledger, access):
    ex = make_executor(cfg, ledger, access)
    with pytest.raises(ToolFailure, match="information barrier"):
        await ex.execute("read_file", {"file_id": "portco_x/board_deck_q3.md"})


class ScriptedClient:
    """Returns a tool call every turn until the loop's budget forces an answer."""

    def __init__(self):
        self.calls = 0
        usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, server_tool_use=None)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)
        self.usage = usage

    async def _create(self, **kw):
        self.calls += 1
        last = kw["messages"][-1]["content"]
        budget_hit = isinstance(last, list) and any(r.get("is_error") for r in last)
        if budget_hit:
            content = [SimpleNamespace(type="text", text='{"claims": [], "gaps": ["budget"]}')]
            return SimpleNamespace(content=content, stop_reason="end_turn", usage=self.usage,
                                   model=kw["model"])
        tool = SimpleNamespace(type="tool_use", id=f"t{self.calls}", name="noop", input={})
        return SimpleNamespace(content=[tool], stop_reason="tool_use", usage=self.usage,
                               model=kw["model"])


async def test_tool_loop_enforces_per_section_tool_budget(cfg, ledger):
    client = ScriptedClient()
    llm = LLM(cfg, ledger, RunContext("r1", "vp_alice", "crm-003"), client=client)

    async def execute(name, args):
        return "ok"

    resp = await run_tool_loop(llm, "section", section="product", system="s",
                               messages=[{"role": "user", "content": "go"}], tools=[],
                               execute=execute, max_tool_calls=3)
    assert resp.stop_reason == "end_turn" and client.calls == 5
    assert ledger.cost_by_section("r1")[0][2] == 500  # input tokens attributed to section


async def test_run_token_cap_stops_new_calls(cfg, ledger):
    cfg.settings["budgets"]["run_token_cap"] = 100
    llm = LLM(cfg, ledger, RunContext("r2", "vp_alice", "crm-003"), client=ScriptedClient())
    llm.run.tokens_used = 100
    with pytest.raises(BudgetExceeded):
        await llm.create("section", section="x", system="s", messages=[])
