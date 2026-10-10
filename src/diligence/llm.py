"""Claude API wrapper (direct or via OpenRouter): model-per-role, run budget, cost
attribution, tool loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import anthropic

from .config import Config
from .ledger import Ledger
from .records import Records

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(Exception):
    """The run's hard token cap was reached; no further model calls are made."""


class RefusalError(Exception):
    """The model (and its fallback) declined the request."""


class ToolFailure(Exception):
    """A tool call failed; the message is returned to the model as an error result."""


@dataclass
class RunContext:
    run_id: str
    user: str
    deal_id: str
    tokens_used: int = 0


def canonical(model: str) -> str:
    """'anthropic/claude-sonnet-5.5' -> 'claude-sonnet-5-5' (pricing keys)."""
    return model.split("/", 1)[-1].replace(".", "-")


def make_client(cfg: Config) -> anthropic.AsyncAnthropic:
    """Anthropic SDK client for the configured provider. OpenRouter serves the Anthropic
    Messages API at /api/v1/messages and takes its key as a Bearer token."""
    p = cfg.provider
    key = os.environ.get(p["api_key_env"])
    if not key:
        raise SystemExit(f"Set {p['api_key_env']} (provider: {cfg.provider_name}) in .env")
    if cfg.provider_name == "anthropic":
        return anthropic.AsyncAnthropic(api_key=key)
    return anthropic.AsyncAnthropic(base_url=p["base_url"], auth_token=key, api_key=None,
                                    default_headers=p.get("headers") or None)


class LLM:
    def __init__(self, cfg: Config, ledger: Ledger, run: RunContext,
                 client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self.records = Records(cfg.path("records"))
        self._systems_logged: set[str] = set()
        self.run = run
        self.client = client or make_client(cfg)
        self.cap = cfg.settings["budgets"]["run_token_cap"]

    def _usd(self, model: str, usage: Any) -> tuple[float, int]:
        server = getattr(usage, "server_tool_use", None)
        searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
        reported = getattr(usage, "cost", None)  # OpenRouter reports the actual charge
        if reported is None and getattr(usage, "model_extra", None):
            reported = usage.model_extra.get("cost")
        if reported is not None:
            return float(reported), searches
        prices = self.cfg.settings["pricing"]
        p = prices.get(canonical(model)) or prices[canonical(self.cfg.model("editor"))]
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        usd = (usage.input_tokens * p["input"] + cache_write * p["input"] * 1.25
               + cache_read * p["cache_read"] + usage.output_tokens * p["output"]) / 1e6
        usd += searches * prices["web_search_per_1k"] / 1000
        return usd, searches

    def _record(self, resp: Any, section: str) -> None:
        u = resp.usage
        self.run.tokens_used += (u.input_tokens + (u.cache_read_input_tokens or 0)
                                 + (u.cache_creation_input_tokens or 0) + u.output_tokens)
        usd, searches = self._usd(resp.model, u)
        self.ledger.log_cost(run_id=self.run.run_id, user=self.run.user,
                             deal_id=self.run.deal_id, section=section, model=resp.model,
                             input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                             cache_read_tokens=u.cache_read_input_tokens or 0,
                             web_searches=searches, usd=usd)

    def _log_call(self, section: str, model: str, system: str, messages: list[dict[str, Any]],
                  tools: list[dict[str, Any]] | None, output_config: dict[str, Any], resp: Any) -> None:
        """Books-and-records: log each call's new input and full output. The system prompt is
        logged once per run (by hash) instead of on every call."""
        sha = hashlib.sha256(system.encode("utf-8")).hexdigest()
        if sha not in self._systems_logged:
            self._systems_logged.add(sha)
            self.records.append(self.run.run_id, "system_prompt", {"sha256": sha, "text": system})
        self.records.append(self.run.run_id, "model_call", {
            "section": section, "user": self.run.user, "deal_id": self.run.deal_id,
            "model_requested": model, "model_served": getattr(resp, "model", None),
            "system_sha256": sha, "tools": [t.get("name") or t.get("type") for t in tools or []],
            "output_config": output_config, "input": messages[-1] if messages else None,
            "output": getattr(resp, "content", None), "stop_reason": getattr(resp, "stop_reason", None),
            "usage": getattr(resp, "usage", None),
        })

    async def create(self, role: str, *, section: str, system: str,
                     messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                     max_tokens: int = 16000, schema: dict[str, Any] | None = None) -> Any:
        if self.run.tokens_used >= self.cap:
            raise BudgetExceeded(f"run token cap of {self.cap:,} reached")
        model = self.cfg.model(role)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                                  "system": system, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        output_config: dict[str, Any] = {}
        if effort := self.cfg.settings.get("effort", {}).get(role):
            output_config["effort"] = effort
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        is_haiku = "haiku" in model

        if self.cfg.provider_name == "openrouter":
            # OpenRouter extensions ride in the body: model fallbacks and provider routing.
            extra: dict[str, Any] = {}
            if (fb := self.cfg.provider.get("fallback_models")) and not is_haiku:
                extra["fallbacks"] = [{"model": m} for m in fb[:3]]
            if routing := self.cfg.provider.get("routing"):
                extra["provider"] = routing
            extra["user"] = self.run.user  # per-user attribution in OpenRouter's dashboard
            resp = await self.client.messages.create(
                **kwargs, cache_control={"type": "ephemeral"}, extra_body=extra)
        elif is_haiku:
            resp = await self.client.messages.create(**kwargs)
        else:
            # Server-side refusal fallback: a declined request is retried on a fallback
            # model inside the same call instead of failing the section.
            resp = await self.client.beta.messages.create(
                **kwargs, betas=[FALLBACK_BETA], fallbacks="default",
                cache_control={"type": "ephemeral"},
            )
        self._record(resp, section)
        self._log_call(section, model, system, messages, tools, output_config, resp)
        if resp.stop_reason == "refusal":
            raise RefusalError(f"{model} declined ({getattr(resp, 'stop_details', None)})")
        return resp


def final_text(resp: Any) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def parse_json(resp: Any) -> dict[str, Any]:
    return json.loads(final_text(resp))


Executor = Callable[[str, dict[str, Any]], Awaitable[str]]

BUDGET_NOTE = ("Tool budget for this section is used up. Do not call more tools; write your "
               "final answer now from what you have and list what you could not check in gaps.")


async def run_tool_loop(llm: LLM, role: str, *, section: str, system: str,
                        messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                        execute: Executor, max_tool_calls: int, max_tokens: int = 16000,
                        schema: dict[str, Any] | None = None, max_turns: int = 40) -> Any:
    """Manual agent loop: runs client tools in parallel, resumes `pause_turn`, and caps
    tool calls per section so a run ends gracefully instead of looping."""
    calls = 0
    for _ in range(max_turns):
        resp = await llm.create(role, section=section, system=system, messages=messages,
                                tools=tools, max_tokens=max_tokens, schema=schema)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            return resp

        async def one(block: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls > max_tool_calls:
                return {"type": "tool_result", "tool_use_id": block.id,
                        "content": BUDGET_NOTE, "is_error": True}
            try:
                content = await execute(block.name, block.input)
                return {"type": "tool_result", "tool_use_id": block.id, "content": content}
            except ToolFailure as e:
                return {"type": "tool_result", "tool_use_id": block.id,
                        "content": str(e), "is_error": True}

        results = await asyncio.gather(*(one(b) for b in uses))
        messages.append({"role": "user", "content": list(results)})
    raise RuntimeError(f"{section}: agent loop exceeded {max_turns} turns")
