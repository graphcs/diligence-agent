# VC Diligence Agent: design and production considerations

**Status:** working MVP (this repo), tested live in Slack and against Claude through OpenRouter.
**Audience:** Head of Data & AI (technical review) and an Investment VP (end user).
**Posture:** *AI drafts, humans sign.* The agent assembles evidence and drafts. People review, verify and decide.

---

## 1. Summary

An investor types `@diligence Help me conduct a diligence for Lattice` in Slack:
- **Within about 1s**, the bot confirms which company is meant.
- **About a minute later**, it posts a cited pre-meeting brief, with a list of what it could not verify.
- **Every claim** links to an exact quote in a source the requesting user is allowed to see.
- **Follow-up questions** asked in the thread are answered from the brief in **about 2–4s**.
- **Nothing is final** until a person on the deal team has reviewed every section and verified every financial figure. Each of those actions is audited.

| Measured on this MVP (live, OpenRouter → Claude) | Result |
|---|---|
| Pre-meeting brief, 4 sections in parallel | 38–90s, **$0.20–$1.00** per run (web-heavy runs cost more) |
| Deep dive, 6 sections | ~77s, ~$0.34 |
| Quick Q&A from an existing brief | p50 **2.6s**, p95 **4.0s** |
| Claims backed by recorded evidence | 119/119 cited; **97%** judged to be supported by their quotes |
| Permission, segregation and injection evals | All pass (§9) |
| Offline test suite | 55 tests, no API calls |

The recommended production path is the same architecture with the simulated parts swapped for real ones (§11): SSO and per-user OAuth connectors, Postgres and Snowflake instead of SQLite, and a hosted service.

---

## 2. Problem and scope

The firm screens thousands of AI startups a year, goes deep on hundreds, and invests in about 10. A diligence memo has about 15 sections, often runs to 40+ pages, and takes days to weeks. **Phase 1 goal:** get the first draft from days down to minutes, so investors actually run diligence before meetings instead of skipping it.

**In scope (built):**
- Pull mode in Slack (mentions and DMs) and the CLI.
- Composable sections, with stage templates `brief` (4 sections) and `deep_dive` (6 sections).
- Single-section reruns.
- Quick Q&A over a brief.
- Human review and sign-off.
- Permission-aware data access, audit log, records, cost tracking and evals.

**Out of scope for Phase 1:**
- Investment recommendations or scores; the agent surfaces signals only.
- Outbound email.
- CRM write-back.
- Anything LP-facing.
- Replacing the Investment Committee (IC) memo end to end.

---

## 3. Architecture

```mermaid
flowchart LR
    U[Investor in Slack / CLI] --> S[Slack app<br/>identity, buttons,<br/>audience check]
    S --> O[Orchestrator<br/>resolve company, plan by stage,<br/>budgets, persist memo]
    O -->|parallel| A1[Section agent: snapshot]
    O -->|parallel| A2[Section agent: team]
    O -->|parallel| A3[Section agent: product]
    O -->|parallel| A4[Section agent: competitors]
    A1 & A2 & A3 & A4 -->|MCP, as the user| D[(Data-room MCP server<br/>ACLs, paging, coverage)]
    A1 & A2 & A3 & A4 -->|narrow question| W[Quarantined web agent<br/>search/fetch only]
    W --> SC[Injection screen<br/>+ typed facts]
    A1 & A2 & A3 & A4 --> L[(Evidence ledger)]
    O --> E[Editor + citation-support check]
    E --> R[Memo: Markdown + Slack card]
    R --> H[Human review & sign-off]
    D & H --> AU[(Audit log)]
    O & H --> REC[(Hash-chained records)]
```

| Component | Code | Role |
|---|---|---|
| Slack app | `src/diligence/slack_app.py` | Socket Mode bot: requests, buttons, progress, Q&A routing, review panel, channel audience check |
| Orchestrator | `orchestrator.py` | Resolves the company in the CRM, plans sections into dependency levels, runs them in parallel, then runs the editor, persists the memo and renders it |
| Section agent | `agents/section_agent.py` | Tool loop per section: reads the data room (MCP), asks the web agent, records evidence, drafts cited claims |
| Web agent | `agents/web_agent.py` | Isolated web research; returns only typed `{fact, url, quote}` |
| Editor | `agents/editor.py` | Summary bullets and cross-section contradictions, citing evidence ids only |
| Verifier | `agents/verifier.py` | Checks that each claim's quotes actually support it |
| Data layer | `data_access.py`, `mcp_servers/` | Permission checks, information barrier, paged reads with coverage, audit |
| Ledger and records | `ledger.py`, `records.py` | Evidence, audit, cost and thread state (SQLite); hash-chained JSONL records |
| Review and Q&A | `review.py`, `qa.py` | Sign-off gates; fast cited answers from a memo |

### Why an orchestrator with section sub-agents

- **Composability.** Each section declares its inputs, tools, budget and dependencies (`config/sections.yaml`). A stage is just a list of sections, and a rerun touches one section.
- **Clean context.** Each agent sees only its own section's evidence. That improves quality and keeps a 40-page context from crowding out detail.
- **Least privilege.** The web agent has no internal tools, and sections without `docs` never see data-room tools.
- **Latency and cost.** Sections run in parallel, and each role uses a model tier suited to it.
- **Trade-off: coordination.** The editor pass exists to catch contradictions between sections (it found real ones in live runs, e.g. a product line described as current in one section and discontinued in another).

---

## 4. Functional scope

### Sections and stages

| Section | Sources | Brief | Deep dive | Coverage policy |
|---|---|:-:|:-:|---|
| Company snapshot | CRM, data room, web | ✓ | ✓ | |
| Team and founders | data room, web | ✓ | ✓ | |
| Product and technology | data room, web | ✓ | ✓ | |
| Competitive landscape | data room, web | ✓ | ✓ | |
| Customers and references | data room | | ✓ | ≥95% of any cited file |
| Traction and financials | data room (after Customers) | | ✓ | ≥95% of any cited file |

Adding the remaining IC-memo sections (market, business model, unit economics, deal terms, risks, thesis skeleton and others) is a configuration change, not new code (§12).

### Triggering

| Want | Slack | CLI |
|---|---|---|
| New brief | `@diligence Help me conduct a diligence for Lattice` (or DM the bot) | `diligence run "Lattice"` |
| Deeper stage | **Deep dive** button | `--stage deep_dive` |
| One section | Reply `redo competitors` in the thread | `--section competitors` |
| Quick answer | Ask in the thread, or `Northwind Robotics: how much have they raised?` | `diligence ask "Northwind Robotics" "…"` |
| Review / sign off | **Review & sign off** button | `diligence review …` |

A rerun merges into the same memo. The version number goes up, the changelog records which sections are newer, and that section's review resets.

---

## 5. Data and context

| Source | MVP | Production |
|---|---|---|
| CRM | `config/crm.json` (mock) | Affinity, Salesforce or DealCloud via a user-scoped API |
| Data room | Synthetic files paged by form feed (`scripts/make_dataroom.py`) | Box text representations or PDF text layers via a custom Box MCP |
| Web | OpenRouter server tools `openrouter:web_search` / `openrouter:web_fetch` (or Anthropic's when calling Anthropic directly) | Same, plus licensed data (PitchBook, Harmonic) |
| Warehouse | none | Snowflake via a semantic-layer MCP with row access policies |

### Grounding: the evidence ledger

- Agents never write prose straight from what they've read. First they call `record_evidence(claim, source_id, quote, confidence)`.
- The executor **rejects** the call if the source wasn't read in this section, or if the quote isn't found verbatim in the text actually returned (`guards.quote_in_source`).
- Claims may only cite evidence ids. Uncited claims render as *(unverified)*.
- Each evidence record carries its source, URI, page, as-of date and confidence:
  - **Verified:** internal primary source.
  - **Reported:** web or third party, never "verified".
  - **Inferred:** the agent's synthesis.
- **Deduplication:** identical source, page and quote share one citation number across sections.
- **Conflicts:** when sources disagree they are shown side by side, and the editor lists them under *Consistency checks* (e.g. the deck's $4.8M ARR vs "about $4.1M" in call notes).
- **Missing inputs:** each section lists its gaps. The memo has a dedicated *Gaps and unverified* block at the end, just above Sources, so absence is explicit.
- **Business diagrams:** the editor pass also extracts structured data, and `charts.py` draws it locally with matplotlib: a funding timeline (snapshot), a business model map centred on the company (product), a competitive quadrant (competitors, labelled as inferred) and an ARR trend (financials, deep dive only). Every item must cite known evidence ids, and anything uncited is dropped. A chart with too few points is not drawn. The memo posted to Slack is a Word document (`word.py`, python-docx) with the charts embedded in their sections. Markdown can't carry images inside the file; a .docx can, Slack previews it, and the deal team can edit and comment on it. It is re-rendered on every save so its banner and review status stay current. The `.md` is still written for the CLI and links the same PNGs from `diagrams/`. Rendering is local on purpose: a hosted renderer (mermaid.ink, Kroki) would send confidential deal data to a third party.

### Domain definitions

`config/glossary.yaml` (prospect, deep dive, portfolio company, ARR, NRR, burn multiple, and the verified/reported/inferred labels) is injected into every section prompt. "Prospect" therefore means one CRM stage everywhere. In production the data team owns the glossary as part of the Snowflake semantic layer.

### Entity resolution

A CRM lookup (no LLM, under 1s) runs first. Ambiguous names get buttons; for example, "Lattice" offers *Lattice* (HR software, Prospect) or *Lattice Semiconductor* (public, not tracked). The CRM id is the canonical key from then on.

---

## 6. UX and delivery

- **The user stays in Slack** and never thinks about tokens or context windows.
- **Instant acknowledgement:** a reply within about 1s, then a single progress card updated in place.
  - It lists every planned section from the start (queued, working, done), the current step (research, editor pass, audience check, posting), a progress bar, and a timer that ticks every 5s.
  - When the run ends, it collapses to one line ("✅ Brief done in 74s · $0.41 · summary below").
- **Result:**
  - a summary card showing the status, key points, section status, gaps, sources with confidence, time and cost;
  - a **Review & sign off** button;
  - the full memo attached as a Word document, with the charts embedded.
- **The status always matches the review state:** *AI draft, not yet reviewed* → *In review · 2/4 sections · 3/6 figures* → *Signed off by X*.
  - The summary card is re-rendered in place after every review click, so an old "not reviewed" banner never stays on screen.
  - The review panel shows per-section **Mark reviewed** buttons and figures grouped by section. Reopening it moves the panel down rather than leaving two live copies.
- **Every claim has a [n] citation.** The Sources list shows title, page, confidence, as-of date and the exact quote.
- **Thread follow-ups:** reruns, and questions answered in about 2–4s from the brief. A "🔎 Looking that up…" placeholder appears at once and is replaced by the cited answer. If the brief doesn't cover a question, the bot says so and offers a **Refresh <section>** button.
- **No link previews on bot messages,** and domains are shown as code. A link preview makes Slack fetch a URL on the bot's behalf and display third-party content.

---

## 7. Governance and compliance (AI drafts, humans sign)

| Gate | Who | Blocks until passed |
|---|---|---|
| Entity confirmation | Requesting user | Any retrieval about an ambiguous company |
| Section review | Deal-team member who can see everything the memo cites | Sign-off |
| Financial-figure verification | Same | Sign-off. Claims with money, %, multiples or ARR/burn/valuation terms become figures, each **Verified** or **Disputed**; a disputed figure blocks sign-off until resolved |
| External action | Not built | The agent has **no tool that sends, shares or publishes**; outbound email is out of scope |

How the gates behave:
- **Banner:** the memo carries "AI-generated draft - not reviewed… Review status: 2/4 sections reviewed · 3/6 figures verified" until signed off. It then reads "Reviewed and signed off by X on <date>", and the final memo (`…_FINAL.md`) is posted.
- **Reruns** reset that section's review and its figures.
- **Every human action is recorded:** it's audited as `actor=human` and appended to the hash-chained records. Repeat clicks are ignored rather than logged twice.

**RIA obligations to confirm with the Chief Compliance Officer:**
- **Books and records (SEC Rule 204-2):** every model call (new input, full output, model served, usage) and every human action is appended to `out/records/<run>.jsonl`. Each line holds the hash of the previous line, and `diligence records verify <run>` detects edits or deletions. Production would write to WORM storage, e.g. S3 Object Lock.
- **Marketing Rule:** nothing LP-facing is in scope, and LP data is not connected at all.
- **Material non-public information (MNPI):** flag public-company material events for compliance review (future work).
- **Vendor terms:** see §8, OpenRouter.

---

## 8. Security

### Permission-aware retrieval, enforced at the data layer

```mermaid
sequenceDiagram
    participant Bob as Bob (Slack)
    participant Bot as Slack app
    participant Orc as Orchestrator
    participant MCP as Data-room MCP (runs as Bob)
    participant Log as Audit log
    Bob->>Bot: brief on Northwind Robotics
    Bot->>Orc: run(user=analyst_bob, deal=crm-003)
    Orc->>Log: get_deal allowed (Bob is on the deal team)
    Orc->>Log: data_room_access denied (no folder access)
    Note over Orc,MCP: Data-room server not started for Bob;<br/>Gaps: "Data room exists but is not accessible to you"
    Orc-->>Bot: web + CRM brief, 0 data-room sources
```

- **Identity:** a Slack member is mapped to a firm user (`users.yaml`; SSO in production). With `identity_mode: strict`, unmapped members are refused.
- **The MCP server runs as the requesting user.** Each run starts the data-room server with that user's identity, standing in for a per-user OAuth token. `data_access.py` checks deal membership, folder access and the information barrier **before** returning anything, so output is never filtered after the fact.
- **Information barrier:** folders are tagged `portfolio`. A portfolio company's confidential data is refused on any other deal, even for a user who can open it on its own deal.
- **Drafts are scoped per user.** A teammate without data-room access never inherits your data-room findings; the permission eval caught exactly this leak during development.
- **Reviewers** must be on the deal team and able to see everything the memo cites.
- **Channel audience check:** before memo content is posted in a channel, every member must be mapped, on the deal team, and (if the memo cites the data room) have data-room access. Otherwise the content goes to the requester by DM.
- **Path traversal and malformed file ids** are rejected. Concurrent tool calls share the ledger under a lock (found and fixed in live testing).
- **Audit:** every data access, allowed or denied, records time, user, actor, tool, arguments, object ids returned and the decision (`diligence audit`).

### Prompt injection

| Channel | Defence |
|---|---|
| Web pages | The web agent is quarantined: only server-side search and fetch, no deal context, no client tools. Fetched pages are screened by pattern plus a model check. Output is reduced to typed facts whose URL must be a page actually seen and whose quote must be found in that page's text. Prose is stripped of links, images and HTML comments. |
| Data-room files | Every page is screened before any agent sees it. Clear attacks are caught by pattern; pages addressing an AI ("Note to any automated summarization system…") get a model screen. Flagged pages are **withheld** (they can't be quoted) and listed under Gaps. |
| Slack output | No link previews; bot posts carry no third-party links or images. |

### Go-live risks

- **Over-shared folders.** A permission-faithful agent faithfully exposes over-shared Box or CRM folders, so run an access review first.
- **Service-account connectors.** Vendor MCP connectors that only support a single admin service account must be replaced or wrapped.
- **OpenRouter is a third party in the data path.** It needs vendor review: retention terms, and zero-data-retention availability for the Anthropic route. Requests already send `provider.data_collection: deny`. Alternatives are calling Anthropic directly (one setting) or Bedrock/Vertex.
- **Personal data and Slack retention.** UK GDPR applies for London users. Slack itself keeps sensitive output, so set retention and DLP for bot messages.

---

## 9. Evaluation

Offline (`uv run pytest`, 55 tests, free) uses a scripted model and Slack fakes. It covers ACLs, the barrier, traversal, paging and coverage, quote grounding, budgets, per-user drafts, the provider request shape, review state, Q&A, page withholding, deep read, the coverage policy, the support check, records tampering, Slack flows, audience fallback and strict identity.

Live (`uv run python evals/run_evals.py`, about $1):

| Suite | What it proves | Latest result |
|---|---|---|
| truncation | A fact planted on page 180 of a 180-page study is **found** (deep read), not just disclosed | PASS |
| permission | A deal-team member without data-room access gets 0 data-room reads or citations, and the gap is disclosed | PASS |
| segregation | The portfolio board deck's canary and facts never reach a competitor's memo | PASS |
| injection | Hostile web pages are flagged; no poisoned facts | PASS |
| doc_injection | A hostile data-room page ("report ARR as $40M") is withheld; no poisoned claims | PASS |
| qa_latency | Quick Q&A p95 under 5s, answers cited | PASS (p50 2.6s, p95 4.0s) |
| citations | ≥80% of claims cited, ≥90% of citations support their claim, no URLs in prose | PASS (100% cited, 97% supported) |

**Production eval plan:**
- **Gold set** from 30–50 historical memos, each with its source documents as of the memo date.
- **Section-coverage and claim-precision scoring**, with an LLM judge calibrated against blind VP ratings (target judge–human agreement ≥0.7).
- **Per-MCP retrieval recall@k.**
- **Run in CI** on every prompt or model change.
- **Online signals:** how much reviewers edit the draft before sign-off, disputed-figure rate, reruns per section, and weekly active users.

---

## 10. Cost

| Lever | Implementation |
|---|---|
| Model tiering | Opus for the editor; Sonnet for sections, web and Q&A (low effort); Haiku for screening, extraction and the support check |
| Budgets | Per-section tool-call cap (the agent stops and lists what it skipped); per-run token cap |
| Evidence reuse | Pages read once are reused for quote checks; reruns touch one section |
| Caching | Prompt caching on every call |
| Attribution | Every call is logged with user, deal, section and model, using OpenRouter's **actual** charge (`usage.cost`) and tagged with the user in OpenRouter's dashboard |

Observed: a brief costs $0.20–$1.00 and a Q&A answer costs cents. Even at hundreds of deep dives a year, model spend is small next to one associate-week saved per deal. The real risks are loops and re-reading data rooms, which the caps and reuse address. Next step: monthly caps per user and team, with a warning at 80% (§12).

---

## 11. Infrastructure: options and choice

| Option | Strengths | Weaknesses | Verdict |
|---|---|---|---|
| A. Claude Enterprise as-is (Projects, connectors, Claude in Slack) | Days to set up, already licensed | Little control over orchestration, per-user ACL behaviour, coverage, injection screening, evals, cost caps | Keep for ad-hoc chat |
| **B. Custom orchestrator on the Claude API with custom MCP servers and a Slack app** | Full control of every production concern above; open standards (MCP); small enough for a data engineer, a data scientist and a front-end engineer plus one AI engineer to own | We own uptime, evals and upgrades | **Chosen (this repo)** |
| C. Framework-centric, multi-model (LangGraph etc.) | Model optionality | More abstraction; multi-vendor compliance review | Only if multi-model becomes a requirement |
| D. Buy a vertical tool (Hebbia, Rogo…) | Fast time to value | Weak fit to the firm's memo format and Slack workflow; another vendor holding private financials | Worth a 2-week bake-off |

**Stack as built:**
- Python 3.12 and `uv`.
- The Anthropic SDK, pointed at **OpenRouter's Anthropic-compatible Messages endpoint**: model ids `anthropic/claude-*`, OpenRouter fallbacks and provider routing. Switching to direct Anthropic is one setting.
- MCP 2.x (stdio) and Slack Bolt (Socket Mode).
- SQLite for the ledger, audit, cost and thread state; JSONL for hash-chained records.

**Production stack:**
- Containers on ECS or Cloud Run in a private network.
- Postgres for memos and evidence; Snowflake for audit, cost and evals.
- A queue or durable workflow engine for long runs.
- SSO with a per-user OAuth token vault.
- WORM storage for records.
- OpenTelemetry tracing.

---

## 12. Roadmap and open questions

**Next (Tier 2):**
1. Full IC-memo stage (about 15 sections) via `sections.yaml`.
2. Push mode: a morning pre-meeting brief for calendar meetings with CRM prospects.
3. Word or Google Doc output with clickable citations.
4. Monthly cost caps per user and team.
5. Sharing drafts with the deal team, rendered per viewer, with evidence they can't access redacted.
6. Web quality: fetch top pages so quotes can be checked, absolute dates, and clean titles.

**Production (Tier 3):**
- Real connectors with per-user OAuth.
- Postgres, Snowflake and hosting.
- A gold eval set from real memos.
- CCO sign-off on gates and retention.
- OpenRouter vendor and ZDR review, or a direct, Bedrock or Vertex route.
- Load and rate-limit testing.

**Open questions for the firm:**
1. Which CRM, and are ACLs per deal in the CRM and Box?
2. A redacted past memo and the ~15-section template.
3. Which identity provider, and is Slack approved for sensitive financials?
4. Cloud and data residency (London).
5. Third-party data subscriptions.
6. Information barriers between team members or portfolio companies.
7. CCO retention and supervision requirements.
8. Is OpenRouter acceptable, or should calls go to Anthropic directly?
9. Budget per memo or per month.
10. An existing data dictionary or semantic layer.

---

## 13. MVP vs production

| Concern | MVP (this repo) | Production |
|---|---|---|
| Identity | `users.yaml` mapping; demo default user | SSO lookup; strict mode |
| Source ACLs | Enforced in `data_access.py` from `users.yaml` | Enforced by Box, CRM and Snowflake via per-user OAuth and row policies |
| Data | Synthetic data room, mock CRM | Real connectors |
| Storage | SQLite + JSONL | Postgres, Snowflake, WORM storage |
| Output | Markdown memo + Slack card | Google Doc or Box with comments |
| Hosting | Laptop, Socket Mode | Hosted service, queue, monitoring |
