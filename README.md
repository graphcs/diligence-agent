# Diligence agent: demo MVP

A working prototype of the VC diligence agent design. You ask in Slack, or on the command line, for a brief on a company. It drafts a **cited pre-meeting brief**, and every claim traces to a source. Several production guarantees can be watched working in the demo:

| Design claim | Where to see it |
| --- | --- |
| Permissions enforced at the data layer, not by filtering output | `src/diligence/data_access.py` and the MCP servers in `mcp_servers/`; `diligence audit` |
| Truncation-aware reads (no silent partial answers) | `read_file` returns `coverage`; the memo's *Gaps* section lists partial reads |
| Information barrier between portfolio companies and other deals | The `portco_x` folder is tagged `portfolio`; the canary string never reaches a competitor's memo |
| Quarantined web research | `agents/web_agent.py`: server-side search/fetch only, screened, typed facts |
| Grounded citations | `record_evidence` rejects quotes that aren't in text the agent actually read |
| Composable memo, single-section reruns | `--section competitors`, or reply "redo competitors" in the Slack thread |
| Cost attribution and caps | Per-call cost (actual, from OpenRouter) by user, deal and section; per-section tool budget; per-run token cap |
| **AI drafts, humans sign** | Every section must be marked reviewed and every financial figure verified by a deal-team member before sign-off (Slack buttons or `diligence review`); all actions audited |
| **Pull-mode Q&A in seconds** | Ask a question in a brief's thread or with `diligence ask` and get a cited answer from the memo in about 2–4s (no tools, no web) |
| **Data-room prompt-injection screening** | Pages addressing AI systems are withheld before any agent sees them and listed under Gaps |
| **Complete reads, not just disclosure** | `analyze_document` reads a whole file (map-reduce); sections with `min_coverage` cannot cite a partly read file |
| **Citations that actually support the claim** | A second check flags claims whose quotes don't back them |
| **Books and records** | Every model call and human action goes to a hash-chained log per run; `diligence records verify <run>` detects tampering |
| **Channel audience check** | Memo content goes to the thread only if every channel member is cleared for the deal; otherwise to the requester by DM |

**Design doc:** [docs/DESIGN.md](docs/DESIGN.md) covers architecture, how each requirement in the brief is met, trade-offs, eval results, and the path to production.

All internal data is **synthetic**. Northwind Robotics and Ferrous Dynamics are fictional. Lattice is researched from the public web only, and its CRM record is a mock.

## Setup

```bash
uv sync
uv run python scripts/make_dataroom.py   # writes the synthetic data room
cp .env.example .env                     # add OPENROUTER_API_KEY
uv run pytest                            # 52 offline tests, no API calls
uv run diligence check-provider          # live probes of the provider (a few cents)
```

### LLM provider

Claude models are called **through OpenRouter** by default, using OpenRouter's Anthropic-compatible Messages endpoint (`https://openrouter.ai/api/v1/messages`). The code uses the official Anthropic SDK pointed at that endpoint, so the request format is the same as calling Anthropic directly. Everything is set in `config/settings.yaml` under `providers.openrouter`:

- **Model IDs:** `anthropic/claude-opus-5.5` (editor), `anthropic/claude-sonnet-5.5` (sections and web), `anthropic/claude-haiku-4.5` (screening).
- **Fallbacks:** if a call fails or is declined, OpenRouter retries it on `anthropic/claude-opus-5`.
- **Routing:** `provider: {data_collection: deny}` goes with every request. Confirm the ZDR terms with compliance before using real deal data.
- **Web research:** uses OpenRouter's server tools `openrouter:web_search` and `openrouter:web_fetch`.
- **Cost:** taken from OpenRouter's `usage.cost` (the actual charge), not estimated. Each call is also tagged with the demo user for OpenRouter's dashboard.

To call Anthropic directly instead, set `provider: anthropic` and `ANTHROPIC_API_KEY`. That path uses Anthropic's own `web_search_20260209` and `web_fetch_20260209` tools and its server-side refusal fallbacks. After switching providers, run `diligence check-provider`.

## Command line

```bash
uv run diligence run "Lattice"                                # asks which Lattice; web-sourced brief
uv run diligence run "Lattice" --pick crm-001
uv run diligence run "Northwind Robotics" --user vp_alice     # data room + web
uv run diligence run "Northwind Robotics" --user analyst_bob  # same deal, no data-room access
uv run diligence run "Northwind Robotics" --section competitors   # rerun one section
uv run diligence run "Northwind Robotics" --stage deep_dive       # 6 sections, with dependencies
uv run diligence audit --run <run id>                         # every tool call, allowed or denied
uv run diligence ask "Northwind Robotics" "How much have they raised?"   # cited answer in ~2-4s
uv run diligence review "Northwind Robotics"                  # review status: sections + figures to verify
uv run diligence review "Northwind Robotics" --all-sections --verify snapshot-5
uv run diligence records verify <run id>                      # check the run's hash chain
```

Memos are written to `out/<crm id>/<user>/memo.md` and `memo.docx` (drafts are per user, so a teammate without data-room access never inherits your data-room findings). Each run is also saved as `memo_<run id>.md`.

## Slack (sandbox workspace)

1. Create an app from `slack/manifest.yaml`, then install it to a **sandbox** workspace.
2. Put `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env`.
3. Add your Slack member ID to `slack_ids` for a user in `config/users.yaml`. Until you do, unmapped users run as `slack.default_user` in `config/settings.yaml`.
4. Run `uv run python -m diligence.slack_app`. In a channel, invite the bot and then type `@diligence Help me conduct a diligence for Lattice`.

**In a brief's Slack thread:** `redo competitors` reruns one section, and a question (e.g. `what's their uptime?`) gets a quick cited answer. **Review & sign off** opens per-section and per-figure buttons; the final memo is posted when everything is signed off. You can also ask at the top level: `Northwind Robotics: how much have they raised?`.

> After updating the manifest (new scopes `channels:read`, `groups:read`, `im:write`), **reinstall the app** in Slack. Set `slack.identity_mode: strict` in `config/settings.yaml` to refuse unmapped Slack users.

## Evals (live; cost a few dollars)

```bash
uv run python evals/run_evals.py          # truncation, permission, segregation, injection,
                                          # doc_injection, qa_latency, citations
uv run python evals/run_evals.py injection
```

## 3-minute debrief demo script

1. **Ask:** in Slack, type `@diligence Help me conduct a diligence for Lattice`. The bot replies within about a second with two CRM matches. Pick **Lattice**, then **Pre-meeting brief**.
2. **Progress:** a single message updates as each section finishes; the sections run in parallel.
3. **Trust:** open the memo file. Every bullet has a [n] citation, and each source shows its type (Verified or Reported), its as-of date and the exact quote. Uncited statements are labeled *(unverified)*. Funding, business model, competitive positioning and ARR are drawn as charts inside their sections. Slack gets the memo as a Word document with the charts embedded (`out/<crm id>/<user>/memo.docx`; the `.md` next to it links the same PNGs in `diagrams/`). *Gaps and unverified* is at the end, above Sources.
4. **Truncation:** run Northwind with `--stage deep_dive`. Gaps shows "read 20/180 pages of the market study", or the agent reads on to page 180 and finds the customer-termination risk.
5. **Permissions:** run Northwind as `analyst_bob`. Gaps says the data room is not accessible to him, and `diligence audit` shows the denials in red.
6. **Information barrier:** Alice sits on Ferrous Dynamics' board. Northwind's competitor section still never uses Ferrous' board deck. Run the `segregation` eval to show this.
7. **Rerun:** reply `redo competitors` in the thread. Only that section reruns, and the memo version increments.
8. **Cost:** the CLI prints cost per section and model at the end of every run.

## Layout

```
config/            settings (models, budgets, prices), users + ACLs, mock CRM, glossary, section registry
data/dataroom/     synthetic data room (generated)        data/fixtures/injection/  hostile web pages
src/diligence/
  data_access.py   permission checks, paging + coverage, audit   (the "data layer")
  mcp_servers/     docs + CRM MCP servers, launched per run as the requesting user
  orchestrator.py  resolve -> plan -> parallel sections -> editor -> persist memo
  agents/          section_agent (evidence ledger), web_agent (quarantined), editor
  llm.py           Claude client (OpenRouter or direct): model per role, fallbacks, budgets, cost log, tool loop
  provider_check.py  `diligence check-provider` live probes
  guards.py        deterministic grounding / sanitising / injection checks
  render.py        Markdown memo + Slack blocks          slack_app.py  Socket Mode app
evals/run_evals.py live eval suites     tests/  offline tests (incl. a scripted end-to-end run)
```

## What is simulated vs production

| Demo | Production |
| --- | --- |
| `users.yaml` ACLs, user passed via env to the MCP server | SSO identity plus per-user OAuth tokens; Box/CRM/Snowflake enforce their own ACLs |
| Text files paged by form feeds | Box API text representations or PDF text layers |
| SQLite ledger and audit log | Postgres (ledger) and Snowflake (audit, cost), immutable retention |
| Memo as Markdown file plus Slack upload | Google Doc or Box file with comments and an approval workflow |
