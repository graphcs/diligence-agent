"""Request shape per provider, checked offline with a capturing client."""

from types import SimpleNamespace

from diligence.llm import LLM, RunContext, canonical


class Capture:
    def __init__(self, cost=None):
        self.calls = []
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, server_tool_use=None, cost=cost)
        self.resp = lambda kw: SimpleNamespace(content=[], stop_reason="end_turn", usage=usage,
                                               model=kw["model"])
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))

    async def _create(self, **kw):
        self.calls.append(("messages", kw))
        return self.resp(kw)

    async def _beta(self, **kw):
        self.calls.append(("beta", kw))
        return self.resp(kw)


async def test_openrouter_request_shape_and_reported_cost(cfg, ledger):
    client = Capture(cost=0.0123)
    llm = LLM(cfg, ledger, RunContext("r", "vp_alice", "crm-003"), client=client)
    await llm.create("section", section="team", system="s", messages=[{"role": "user", "content": "x"}],
                     schema={"type": "object"})
    api, kw = client.calls[0]
    assert api == "messages" and "betas" not in kw
    assert kw["model"] == "anthropic/claude-sonnet-5.5"
    assert kw["output_config"]["effort"] == "medium"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["extra_body"]["fallbacks"] == [{"model": "anthropic/claude-opus-5"}]
    assert kw["extra_body"]["provider"] == {"data_collection": "deny"}
    assert kw["extra_body"]["user"] == "vp_alice"
    assert ledger.cost_by_section("r")[0][5] == 0.0123  # OpenRouter's own figure, not an estimate

    await llm.create("screen", section="team", system="s", messages=[])
    _, kw = client.calls[1]
    assert "fallbacks" not in kw["extra_body"] and "output_config" not in kw


async def test_anthropic_provider_uses_server_side_fallbacks(cfg, ledger):
    cfg.settings["provider"] = "anthropic"
    client = Capture()
    llm = LLM(cfg, ledger, RunContext("r2", "vp_alice", "crm-003"), client=client)
    await llm.create("editor", section="editor", system="s", messages=[])
    api, kw = client.calls[0]
    assert api == "beta" and kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert ledger.cost_by_section("r2")[0][5] > 0  # estimated from the price table


def test_canonical_model_names():
    assert canonical("anthropic/claude-sonnet-5.5") == "claude-sonnet-5-5"
    assert canonical("claude-opus-5-5") == "claude-opus-5-5"
