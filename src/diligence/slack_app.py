"""Slack front end (Socket Mode).

Flow: mention or DM -> instant CRM match -> pick company/depth -> live progress -> summary
card + memo file -> human review and sign-off buttons. In a brief's thread:
  - "redo competitors"            reruns one section
  - "what's their ARR?"           quick cited answer from the memo (no tools, a few seconds)
Top level: "Northwind Robotics: what's their ARR?" also gets a quick answer.

Before anything from a memo is posted in a channel, every channel member must be cleared
for the deal; otherwise it goes to the requester by DM. Thread state lives in SQLite, so
reruns, Q&A and reviews survive a restart.

Run: uv run diligence-slack   (needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from . import orchestrator, qa, render, review
from .config import load_config, load_env
from .data_access import AccessContext, AccessDenied, DataAccess
from .ledger import Ledger
from .models import MemoState

log = logging.getLogger("diligence.slack")
cfg = load_config()
BOT_ID: dict[str, str] = {}
MAX_FIGURE_ROWS = 20

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
ASK_RE = re.compile(r"(?:diligence|brief|research|prep|look into)\s+(?:for|on|into)?\s*(.+)", re.I)
RERUN_RE = re.compile(r"\b(?:redo|refresh|rerun|update)\s+(?:the\s+)?([a-z ]+?)(?:\s+section)?\s*$", re.I)
QA_RE = re.compile(r"^\s*(?P<company>[^:?\n]{2,60}):\s*(?P<question>.+)$", re.S)
QUESTION_RE = re.compile(r"\?\s*$|^\s*(what|who|when|where|why|how|is|are|does|do|did|can|which|has|have|was|were)\b", re.I)


# ---- helpers -----------------------------------------------------------------------------

def ledger() -> Ledger:
    return Ledger(cfg.path("db"))


def get_thread(thread: str | None) -> dict[str, Any] | None:
    return ledger().get_thread(thread)


def put_thread(thread: str, **data: Any) -> dict[str, Any]:
    st = {**(get_thread(thread) or {}), **data}
    ledger().put_thread(thread, st)
    return st


async def post(client, **kw) -> Any:
    """Post with link previews off: an unfurl fetches a URL on the bot's behalf and shows
    third-party content in the thread, so bot output never unfurls."""
    return await client.chat_postMessage(unfurl_links=False, unfurl_media=False, **kw)


def mapped_user(slack_user: str) -> str | None:
    for name, u in cfg.users.items():
        if slack_user in (u.get("slack_ids") or []):
            return name
    return None


def demo_user(slack_user: str) -> str | None:
    """Map a Slack member to a firm identity. In strict mode (production) unmapped members
    are refused; in demo mode they run as slack.default_user."""
    if user := mapped_user(slack_user):
        return user
    if cfg.settings["slack"].get("identity_mode", "demo") == "strict":
        return None
    return cfg.settings["slack"].get("default_user")


def company_from(text: str) -> str:
    text = MENTION_RE.sub("", text).strip()
    m = ASK_RE.search(text)
    name = (m.group(1) if m else text).strip()
    return re.sub(r"[?.!]+$", "", name).strip()


def section_from(text: str) -> str | None:
    m = RERUN_RE.search(MENTION_RE.sub("", text).strip())
    if not m:
        return None
    word = m.group(1).lower().strip()
    for key, spec in cfg.sections.items():
        if word == key or word in spec["title"].lower():
            return key
    return None


def is_question(text: str) -> bool:
    return bool(QUESTION_RE.search(MENTION_RE.sub("", text).strip()))


def button(text: str, action: str, value: dict[str, Any], style: str | None = None) -> dict[str, Any]:
    b = {"type": "button", "text": {"type": "plain_text", "text": text[:75]},
         "action_id": action, "value": json.dumps(value)}
    if style:
        b["style"] = style
    return b


def stage_blocks(company: str, crm_id: str, thread: str) -> list[dict[str, Any]]:
    stages = [button(f"{v['title']} ({len(v['sections'])} sections)", f"run_stage_{k}",
                     {"crm_id": crm_id, "stage": k, "thread": thread}, "primary" if k == "brief" else None)
              for k, v in cfg.stages.items()]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"How deep should I go on *{company}*?"}},
            {"type": "actions", "elements": stages}]


# ---- delivery: who may see memo content in this channel? -----------------------------------

def memo_uses_data_room(memo: MemoState) -> bool:
    ids = sorted({e for s in memo.sections.values() for c in s.claims for e in c.evidence_ids})
    return any(r.source_type == "doc" for r in ledger().get_evidence(ids).values())


async def channel_members(client, channel: str) -> list[str]:
    members, cursor = [], None
    while True:
        resp = await client.conversations_members(channel=channel, limit=200,
                                                  **({"cursor": cursor} if cursor else {}))
        members += resp["members"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return members


async def blocked_members(client, channel: str, requester_slack: str, memo: MemoState) -> list[str]:
    """Channel members who may not see this memo. DMs have no other audience."""
    if channel.startswith("D"):
        return []
    access = DataAccess(cfg, ledger())
    needs_room = memo_uses_data_room(memo)
    deal = next(c for c in cfg.crm if c["id"] == memo.deal_id)
    blocked = []
    for m in await channel_members(client, channel):
        if m in (requester_slack, BOT_ID.get("id")):
            continue
        user = mapped_user(m)  # never the demo default: unknown people are not cleared
        if user is None:
            blocked.append(m)
            continue
        ctx = AccessContext(user=user, deal_id=memo.deal_id, run_id="audience-check", actor="orchestrator")
        try:
            access.get_deal(ctx, memo.deal_id)
            if needs_room and access.data_room_access(ctx, deal):
                blocked.append(m)
        except AccessDenied:
            blocked.append(m)
    return blocked


async def delivery(client, channel: str, thread: str, st: dict[str, Any], memo: MemoState) -> tuple[str, str | None]:
    """Where to post memo content: the thread, or the requester's DM if the channel is wider
    than the people cleared for this deal."""
    blocked = await blocked_members(client, channel, st["slack_user"], memo)
    if not blocked:
        return channel, thread
    dm = (await client.conversations_open(users=st["slack_user"]))["channel"]["id"]
    await post(client, channel=channel, thread_ts=thread,
               text=f":lock: Sent to <@{st['slack_user']}> privately: {len(blocked)} member(s) of this "
                    "channel are not cleared for this deal's sources.")
    return dm, None


# ---- runs --------------------------------------------------------------------------------

async def rerun(client, channel: str, thread: str, key: str) -> None:
    await post(client, channel=channel, thread_ts=thread,
               text=f"Refreshing *{cfg.sections[key]['title']}* only…")
    asyncio.create_task(run_and_report(client, channel, thread, only=[key]))


async def run_and_report(client, channel: str, thread: str, only: list[str] | None = None) -> None:
    st = get_thread(thread)
    status: dict[str, str] = {}
    msg = await post(client, channel=channel, thread_ts=thread, text="Planning…")
    t0 = time.monotonic()

    async def progress(event: str, section: str | None) -> None:
        if event in ("start", "done") and section:
            status[section] = "✓" if event == "done" else "…"
        lines = " · ".join(f"{s} {cfg.sections[k]['title']}" for k, s in status.items())
        if event == "editor":
            lines += " · editor pass…"
        await client.chat_update(channel=channel, ts=msg["ts"],
                                 text=f"{lines or 'Planning…'}  ({time.monotonic() - t0:.0f}s)")

    try:
        result = await orchestrator.run(user=st["user"], crm_id=st["crm_id"], stage=st["stage"],
                                        only=only, progress=progress, cfg=cfg)
    except AccessDenied as e:
        await client.chat_update(channel=channel, ts=msg["ts"], text=f"Access denied: {e}")
        return
    except Exception as e:  # surface failures in the thread rather than going silent
        log.exception("run failed")
        await client.chat_update(channel=channel, ts=msg["ts"], text=f"Run failed: {e}")
        return

    memo = result.memo
    to_channel, to_thread = await delivery(client, channel, thread, st, memo)
    blocks = render.slack_summary(ledger(), memo, result.cost_usd, result.seconds)
    blocks.append({"type": "actions", "elements": [
        button("Review & sign off", "review_open", {"thread": thread}, "primary")]})
    await post(client, channel=to_channel, text=f"Brief: {memo.company}", blocks=blocks,
               **({"thread_ts": to_thread} if to_thread else {}))
    await upload_memo(client, to_channel, to_thread, memo, result.markdown)


async def upload_memo(client, channel: str, thread: str | None, memo: MemoState, md: str) -> None:
    final = review.signed_off_by(memo) is not None
    label = "signed off" if final else "AI draft"
    await client.files_upload_v2(
        channel=channel, content=md, **({"thread_ts": thread} if thread else {}),
        filename=f"{memo.company.replace(' ', '_')}_brief_v{memo.version}{'_FINAL' if final else ''}.md",
        title=f"{memo.company} - brief v{memo.version} ({label})")


# ---- review & sign-off ---------------------------------------------------------------------

def review_blocks(memo: MemoState, thread: str) -> list[dict[str, Any]]:
    st = review.status(memo)
    head = (f":white_check_mark: *Signed off by {review.signed_off_by(memo)}*" if st.signed_off
            else f"*Review & sign-off* - {st.line()}\nAI drafts, humans sign: mark each section "
                 "reviewed and check each financial figure against its source.")
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    rows = []
    for key, s in memo.sections.items():
        r = memo.reviews.get(key)
        rows.append(f"{'✅' if r else '⬜'} *{s.title}*" + (f" - {r.by}" if r else ""))
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}})
    pending_sections = [k for k in memo.sections if k not in memo.reviews]
    if pending_sections:
        blocks.append({"type": "actions", "elements": [
            button(f"✓ {cfg.sections[k]['title']}", f"rev_sec__{k}", {"thread": thread, "section": k})
            for k in pending_sections[:25]]})
    if memo.figures:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Financial figures*"}})
    for f in memo.figures[:MAX_FIGURE_ROWS]:
        icon = {"pending": "⬜", "verified": "✅", "disputed": "❗"}[f.status]
        who = f" - {f.status} by {f.by}" if f.by else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": f"{icon} `{f.id}` {f.text[:250]}{who}"}})
        if f.status == "pending":
            blocks.append({"type": "actions", "elements": [
                button("Verified", f"fig_ok__{f.id}", {"thread": thread, "figure": f.id}, "primary"),
                button("Dispute", f"fig_no__{f.id}", {"thread": thread, "figure": f.id}, "danger")]})
        elif f.status == "disputed":  # resolved after checking the source (or rerun the section)
            blocks.append({"type": "actions", "elements": [
                button("Resolved - mark verified", f"fig_ok__{f.id}", {"thread": thread, "figure": f.id})]})
    if len(memo.figures) > MAX_FIGURE_ROWS:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{len(memo.figures) - MAX_FIGURE_ROWS} more figures: use `diligence review` in the CLI."}]})
    return blocks


async def apply_review(client, body: dict[str, Any], action: str) -> None:
    v = json.loads(body["actions"][0]["value"])
    st = get_thread(v["thread"])
    clicker = body["user"]["id"]
    channel = body["channel"]["id"]
    reviewer = demo_user(clicker)
    memo = orchestrator.load_memo(cfg, st["crm_id"], st["user"]) if st else None
    if memo is None or reviewer is None:
        await client.chat_postEphemeral(channel=channel, user=clicker, text="Can't find that memo or your identity.")
        return
    was_signed = review.signed_off_by(memo) is not None
    try:
        r = review.Reviewer(cfg)
        if action == "section":
            r.review_section(memo, reviewer, v["section"])
        else:
            r.set_figure(memo, reviewer, v["figure"], "verified" if action == "ok" else "disputed")
    except (AccessDenied, KeyError, ValueError) as e:
        await client.chat_postEphemeral(channel=channel, user=clicker, text=f"Not applied: {e}")
        return
    _, md = orchestrator.save_memo(cfg, memo, st["user"])
    await client.chat_update(channel=channel, ts=body["message"]["ts"], text="Review & sign-off",
                             blocks=review_blocks(memo, v["thread"]))
    if not was_signed and review.signed_off_by(memo):
        thread = body["message"].get("thread_ts")
        await post(client, channel=channel, text=f":white_check_mark: {memo.company} brief signed off "
                   f"by {review.signed_off_by(memo)}. Final memo attached.",
                   **({"thread_ts": thread} if thread else {}))
        await upload_memo(client, channel, thread, memo, md)


# ---- quick Q&A -----------------------------------------------------------------------------

async def answer_question(client, channel: str, thread: str, question: str) -> None:
    st = get_thread(thread)
    t0 = time.monotonic()
    try:
        a = await qa.answer(cfg, user=st["user"], crm_id=st["crm_id"], question=question)
    except AccessDenied as e:
        await post(client, channel=channel, thread_ts=thread, text=f"Access denied: {e}")
        return
    if not a.has_memo:
        company = next(c["name"] for c in cfg.crm if c["id"] == st["crm_id"])
        await post(client, channel=channel, thread_ts=thread, text="No brief yet",
                   blocks=[{"type": "section", "text": {"type": "mrkdwn",
                            "text": f"There's no brief for *{company}* yet, so I can't answer from it."}},
                           *stage_blocks(company, st["crm_id"], thread)[1:]])
        return
    memo = orchestrator.load_memo(cfg, st["crm_id"], st["user"])
    to_channel, to_thread = await delivery(client, channel, thread, st, memo)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": a.markdown()[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{time.monotonic() - t0:.1f}s · from brief v{memo.version} · "
            + ("cited" if a.confident else "not covered by the brief")}]},
    ]
    if not a.confident and a.suggest_section:
        blocks.append({"type": "actions", "elements": [button(
            f"Refresh {cfg.sections[a.suggest_section]['title']}", "refresh_sec",
            {"thread": thread, "section": a.suggest_section})]})
    await post(client, channel=to_channel, text=a.text[:200], blocks=blocks,
               **({"thread_ts": to_thread} if to_thread else {}))


# ---- request routing -----------------------------------------------------------------------

async def handle_request(client, event: dict[str, Any]) -> None:
    """A new request (channel mention or DM): a question about a company, a rerun or Q&A in
    a known thread, or a new brief."""
    channel, text = event["channel"], MENTION_RE.sub("", event.get("text", "")).strip()
    thread = event.get("thread_ts") or event["ts"]
    user = demo_user(event["user"])
    if user is None:
        await post(client, channel=channel, thread_ts=thread,
                   text="Sorry, I can't match your Slack account to a firm identity.")
        return
    if get_thread(event.get("thread_ts")):
        if key := section_from(text):
            await rerun(client, channel, thread, key)
            return
        if is_question(text):
            await answer_question(client, channel, thread, text)
            return
    if (m := QA_RE.match(text)) and is_question(m.group("question")):
        cands = orchestrator.lookup(cfg, user, m.group("company"))
        if len(cands) == 1:
            memo = orchestrator.load_memo(cfg, cands[0]["id"], user)
            put_thread(thread, crm_id=cands[0]["id"], user=user, slack_user=event["user"],
                       stage=memo.stage if memo else "brief")
            await answer_question(client, channel, thread, m.group("question"))
            return
    company = company_from(text)
    if not company:
        await post(client, channel=channel, thread_ts=thread,
                   text="Which company? e.g. `Help me conduct a diligence for Lattice`")
        return
    t0 = time.monotonic()
    cands = orchestrator.lookup(cfg, user, company)
    if not cands and is_question(text):
        await post(client, channel=channel, thread_ts=thread, text=(
            "Which company is that about? Ask as `Company: question` (e.g. `Northwind Robotics: "
            "how much have they raised?`), or reply in the thread of an existing brief."))
    elif not cands:
        await post(client, channel=channel, thread_ts=thread, text=f"I couldn't find *{company}* in the CRM.")
    elif len(cands) == 1:
        await post(client, channel=channel, thread_ts=thread, text="Choose depth",
                   blocks=stage_blocks(cands[0]["name"], cands[0]["id"], thread))
    else:
        lines = "\n".join(f"• *{c['name']}* (`{c['domain']}`) - {c['description']} - CRM stage: {c['stage']}"
                          for c in cands)
        await post(client, channel=channel, thread_ts=thread, text="Which company?", blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"I found {len(cands)} matches for *{company}*:\n{lines}"}},
            {"type": "actions", "elements": [
                button(c["name"], f"pick_{c['id']}", {"crm_id": c["id"], "name": c["name"], "thread": thread})
                for c in cands]},
        ])
    log.info("first reply in %.2fs", time.monotonic() - t0)


def register(app: AsyncApp) -> None:
    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client) -> None:
        await handle_request(client, event)

    @app.action(re.compile(r"^pick_"))
    async def on_pick(ack, body, client) -> None:
        await ack()
        v = json.loads(body["actions"][0]["value"])
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                                 text=f"Got it: *{v['name']}*.", blocks=[])
        await post(client, channel=body["channel"]["id"], thread_ts=v["thread"],
                   text="Choose depth", blocks=stage_blocks(v["name"], v["crm_id"], v["thread"]))

    @app.action(re.compile(r"^run_stage_"))
    async def on_stage(ack, body, client) -> None:
        await ack()
        v = json.loads(body["actions"][0]["value"])
        user = demo_user(body["user"]["id"])
        channel = body["channel"]["id"]
        if user is None:
            await client.chat_postEphemeral(channel=channel, user=body["user"]["id"],
                                            text="Your Slack account isn't mapped to a firm identity.")
            return
        await client.chat_update(channel=channel, ts=body["message"]["ts"], blocks=[],
                                 text=f"Starting the {cfg.stages[v['stage']]['title'].lower()}…")
        put_thread(v["thread"], crm_id=v["crm_id"], user=user, stage=v["stage"],
                   slack_user=body["user"]["id"], channel=channel)
        asyncio.create_task(run_and_report(client, channel, v["thread"]))

    @app.action("review_open")
    async def on_review_open(ack, body, client) -> None:
        await ack()
        v = json.loads(body["actions"][0]["value"])
        st = get_thread(v["thread"])
        memo = orchestrator.load_memo(cfg, st["crm_id"], st["user"]) if st else None
        if memo is None:
            return
        thread = body["message"].get("thread_ts")
        await post(client, channel=body["channel"]["id"], text="Review & sign-off",
                   blocks=review_blocks(memo, v["thread"]), **({"thread_ts": thread} if thread else {}))

    @app.action(re.compile(r"^rev_sec__"))
    async def on_review_section(ack, body, client) -> None:
        await ack()
        await apply_review(client, body, "section")

    @app.action(re.compile(r"^fig_ok__"))
    async def on_figure_ok(ack, body, client) -> None:
        await ack()
        await apply_review(client, body, "ok")

    @app.action(re.compile(r"^fig_no__"))
    async def on_figure_no(ack, body, client) -> None:
        await ack()
        await apply_review(client, body, "no")

    @app.action("refresh_sec")
    async def on_refresh(ack, body, client) -> None:
        await ack()
        v = json.loads(body["actions"][0]["value"])
        await rerun(client, body["channel"]["id"], v["thread"], v["section"])

    @app.event("message")
    async def on_message(event: dict[str, Any], client) -> None:
        """DMs to the bot, and thread replies without a mention (reruns and questions)."""
        if event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text", "")
        if BOT_ID.get("id") and f"<@{BOT_ID['id']}>" in text:
            return  # channel mentions are handled by app_mention
        if event.get("channel_type") == "im":
            await handle_request(client, event)
        elif get_thread(thread := event.get("thread_ts")):
            if key := section_from(text):
                await rerun(client, event["channel"], thread, key)
            elif is_question(text):
                await answer_question(client, event["channel"], thread, text)


async def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO)
    if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
        raise SystemExit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN (see slack/manifest.yaml).")
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    register(app)
    me = await app.client.auth_test()
    BOT_ID["id"] = me["user_id"]
    mode = cfg.settings["slack"].get("identity_mode", "demo")
    who = "refused" if mode == "strict" else cfg.settings["slack"].get("default_user") or "refused"
    print(f"Connected to Slack workspace '{me['team']}' as @{me['user']} ({me['user_id']}). "
          f"LLM provider: {cfg.provider_name}. Identity mode: {mode}; unmapped Slack users: {who}. "
          "Ctrl+C to stop.", flush=True)
    await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
