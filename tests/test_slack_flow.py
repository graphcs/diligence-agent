"""Slack flow offline: a fake Slack client records every call; Claude is the scripted fake."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from diligence import llm as llm_mod
from diligence import orchestrator, slack_app

from .test_end_to_end_fake import FakeClaude

ALICE_SLACK, BOB_SLACK, STRANGER = "UALICE", "UBOB", "USTRANGER"


class FakeSlack:
    def __init__(self, members=None):
        self.calls = []
        self.n = 0
        self.members = members or [ALICE_SLACK, "UBOT"]

    def _rec(self, name, kw):
        self.calls.append((name, kw))
        self.n += 1
        return {"ok": True, "ts": f"200.{self.n}", "channel": {"id": "DALICE"}}

    async def chat_postMessage(self, **kw):
        return self._rec("post", kw)

    async def chat_update(self, **kw):
        return self._rec("update", kw)

    async def chat_postEphemeral(self, **kw):
        return self._rec("ephemeral", kw)

    async def files_upload_v2(self, **kw):
        return self._rec("upload", kw)

    async def conversations_members(self, **kw):
        return {"members": self.members, "response_metadata": {"next_cursor": ""}}

    async def conversations_open(self, **kw):
        return self._rec("open_dm", kw)

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
def slack(cfg, monkeypatch):
    cfg.users["vp_alice"]["slack_ids"] = [ALICE_SLACK]
    cfg.users["analyst_bob"]["slack_ids"] = [BOB_SLACK]
    monkeypatch.setattr(slack_app, "cfg", cfg)
    monkeypatch.setattr(llm_mod, "make_client", lambda cfg: FakeClaude())
    slack_app.BOT_ID["id"] = "UBOT"
    return FakeSlack()


def start(thread, crm="crm-003", user="vp_alice", slack_user=ALICE_SLACK):
    slack_app.put_thread(thread, crm_id=crm, user=user, stage="brief", slack_user=slack_user, channel="C1")


async def test_ambiguous_dm_offers_company_buttons(slack):
    await slack_app.handle_request(slack, {"channel": "D1", "ts": "100.1", "user": ALICE_SLACK,
                                           "channel_type": "im",
                                           "text": "Help me conduct a diligence for Lattice"})
    (post,) = slack.named("post")
    assert post["thread_ts"] == "100.1"
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert "(`lattice.com`)" in post["blocks"][0]["text"]["text"]
    buttons = post["blocks"][1]["elements"]
    assert [b["action_id"] for b in buttons] == ["pick_crm-001", "pick_crm-002"]


async def test_unique_match_goes_straight_to_depth_choice(slack):
    await slack_app.handle_request(slack, {"channel": "C1", "ts": "100.2", "user": ALICE_SLACK,
                                           "text": "<@UBOT> brief on Northwind Robotics"})
    (post,) = slack.named("post")
    assert [b["action_id"] for b in post["blocks"][1]["elements"]] == ["run_stage_brief", "run_stage_deep_dive"]


async def test_unknown_company_and_strict_identity(slack, cfg):
    await slack_app.handle_request(slack, {"channel": "C1", "ts": "1", "user": ALICE_SLACK,
                                           "text": "diligence for Nonexistent Corp"})
    assert "couldn't find" in slack.named("post")[0]["text"]
    cfg.settings["slack"]["identity_mode"] = "strict"
    await slack_app.handle_request(slack, {"channel": "C1", "ts": "2", "user": STRANGER, "text": "Lattice"})
    assert "can't match your Slack account" in slack.named("post")[1]["text"]


async def test_full_run_posts_summary_with_review_button_and_file(slack):
    start("100.3")
    await slack_app.run_and_report(slack, "C1", "100.3")
    first = slack.named("post")[0]  # the progress card lists every planned section up front
    assert first["blocks"][1]["text"]["text"].count(":white_circle:") == 4
    assert any(":white_check_mark: Company snapshot" in str(u.get("blocks")) for u in slack.named("update"))
    assert "done in" in slack.named("update")[-1]["text"]
    summary = [p for p in slack.named("post") if p.get("text", "").startswith("Brief:")][-1]
    assert summary["blocks"][0]["text"]["text"] == "📄 Pre-meeting brief · Northwind Robotics"
    assert "AI draft, not yet reviewed · 0/4 sections reviewed" in summary["blocks"][1]["elements"][0]["text"]
    assert summary["blocks"][-1]["elements"][0]["action_id"] == "review_open"
    (upload,) = slack.named("upload")
    assert upload["filename"] == "Northwind_Robotics_brief_v1.docx" and upload["content"].startswith(b"PK")


async def test_thread_state_survives_restart_and_reruns_one_section(slack):
    start("100.4")
    await slack_app.run_and_report(slack, "C1", "100.4")
    # "Restart": nothing in memory; state comes back from SQLite.
    assert slack_app.get_thread("100.4")["crm_id"] == "crm-003"
    await slack_app.run_and_report(slack, "C1", "100.4", only=["competitors"])
    assert slack.named("upload")[1]["filename"] == "Northwind_Robotics_brief_v2.docx"


async def test_access_denied_is_reported_in_thread(slack):
    start("100.5", crm="crm-004", user="analyst_bob", slack_user=BOB_SLACK)
    await slack_app.run_and_report(slack, "C1", "100.5")
    assert slack.named("update")[-1]["text"].startswith(":lock: Access denied")
    assert not slack.named("upload")


async def test_wide_channel_gets_memo_by_dm_instead(slack):
    slack.members = [ALICE_SLACK, "UBOT", STRANGER]  # someone not cleared for the deal
    start("100.6")
    await slack_app.run_and_report(slack, "C1", "100.6")
    notice = [p for p in slack.named("post") if "Sent to" in p.get("text", "")]
    assert notice and notice[0]["channel"] == "C1"
    summary = [p for p in slack.named("post") if p.get("text", "").startswith("Brief:")][-1]
    assert summary["channel"] == "DALICE" and "thread_ts" not in summary
    assert slack.named("upload")[0]["channel"] == "DALICE"


async def test_bob_in_channel_blocks_data_room_memo(slack):
    slack.members = [ALICE_SLACK, BOB_SLACK, "UBOT"]  # Bob is on the deal but not the data room
    start("100.7")
    await slack_app.run_and_report(slack, "C1", "100.7")
    assert slack.named("upload")[0]["channel"] == "DALICE"


def _click(action_id, value, user=ALICE_SLACK, thread="100.8"):
    return {"actions": [{"action_id": action_id, "value": json.dumps(value)}],
            "user": {"id": user}, "channel": {"id": "C1"},
            "message": {"ts": "300.1", "thread_ts": thread}}


async def test_review_and_sign_off_through_buttons(slack, cfg):
    start("100.8")
    await slack_app.run_and_report(slack, "C1", "100.8")
    memo = orchestrator.load_memo(cfg, "crm-003", "vp_alice")
    for key in memo.sections:
        await slack_app.apply_review(slack, _click(f"rev_sec__{key}", {"thread": "100.8", "section": key}), "section")
    memo = orchestrator.load_memo(cfg, "crm-003", "vp_alice")
    assert set(memo.reviews) == set(memo.sections)
    for f in memo.figures:
        await slack_app.apply_review(slack, _click(f"fig_ok__{f.id}", {"thread": "100.8", "figure": f.id}), "ok")
    memo = orchestrator.load_memo(cfg, "crm-003", "vp_alice")
    from diligence import review
    assert review.signed_off_by(memo)
    card = slack_app.get_thread("100.8")["card"]
    card_updates = [u for u in slack.named("update") if u["ts"] == card["ts"]]
    assert "In review · 1/4 sections reviewed" in card_updates[0]["blocks"][1]["elements"][0]["text"]
    assert "Signed off by vp_alice" in card_updates[-1]["blocks"][1]["elements"][0]["text"]
    assert card_updates[-1]["blocks"][-1]["elements"][0]["text"]["text"] == "Open review"
    assert any("signed off" in p.get("text", "") for p in slack.named("post"))
    assert slack.named("upload")[-1]["filename"].endswith("_FINAL.docx")


async def test_bob_cannot_review_a_data_room_memo(slack, cfg):
    start("100.9")
    await slack_app.run_and_report(slack, "C1", "100.9")
    await slack_app.apply_review(slack, _click("rev_sec__team", {"thread": "100.9", "section": "team"},
                                               user=BOB_SLACK, thread="100.9"), "section")
    (eph,) = slack.named("ephemeral")
    assert "Not applied" in eph["text"] and "cannot review" in eph["text"]
    assert not orchestrator.load_memo(cfg, "crm-003", "vp_alice").reviews


async def test_question_in_thread_gets_cited_answer(slack, monkeypatch):
    start("101.0")
    await slack_app.run_and_report(slack, "C1", "101.0")

    async def fake_answer(cfg, *, user, crm_id, question):
        from diligence.qa import Answer
        return Answer(text="600 units/hour [1]", confident=True, suggest_section=None, seconds=1.2,
                      sources=[(1, SimpleNamespace(title="Series B pitch deck", page=3, confidence="verified"))])

    monkeypatch.setattr(slack_app.qa, "answer", fake_answer)
    await slack_app.handle_request(slack, {"channel": "C1", "ts": "101.1", "thread_ts": "101.0",
                                           "user": ALICE_SLACK, "text": "<@UBOT> what's the pick rate?"})
    holder = slack.named("post")[-1]  # a placeholder right away, then the answer in its place
    assert "Looking that up" in holder["text"]
    ans = slack.named("update")[-1]
    assert ans["ts"] == f"200.{slack.calls.index(('post', holder)) + 1}"
    assert "600 units/hour [1]" in ans["blocks"][0]["text"]["text"]
    assert "Series B pitch deck, p. 3" in ans["blocks"][1]["elements"][0]["text"]


async def test_question_without_company_gets_guidance(slack):
    await slack_app.handle_request(slack, {"channel": "D1", "ts": "102.1", "user": ALICE_SLACK,
                                           "channel_type": "im", "text": "how much have they raised so far?"})
    assert "Which company is that about?" in slack.named("post")[-1]["text"]


def test_disputed_figure_can_be_resolved_from_the_panel():
    from diligence.models import Figure, MemoState
    m = MemoState(deal_id="crm-003", company="N", stage="brief", requested_by="vp_alice",
                  figures=[Figure(id="snapshot-1", section="snapshot", text="ARR $4.8M", status="disputed", by="vp_alice")])
    ids = [e["action_id"] for b in slack_app.review_blocks(m, "t") if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["fig_ok__snapshot-1"]


async def test_reopening_review_retires_the_old_panel(slack):
    start("102.2")
    await slack_app.run_and_report(slack, "C1", "102.2")
    body = {"actions": [{"value": json.dumps({"thread": "102.2"})}], "channel": {"id": "C1"},
            "message": {"ts": "x", "thread_ts": "102.2"}}
    app = type("App", (), {})()
    handlers = {}
    app.event = lambda *a, **k: (lambda f: f)
    app.action = lambda name: (lambda f: handlers.setdefault(str(name), f))
    slack_app.register(app)
    ack = lambda: asyncio.sleep(0)
    await handlers["review_open"](ack, body, slack)
    first = slack_app.get_thread("102.2")["panel"]
    await handlers["review_open"](ack, body, slack)
    assert any(u["ts"] == first["ts"] and "moved" in u["text"] for u in slack.named("update"))
