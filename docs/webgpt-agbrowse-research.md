# WebGPT active research scout

The `WEB_SCOUT` lane is an evidence-gathering component of the Research Plane. It is
not a news summarizer, an order generator, a reduce-only policy controller, or a
strategy-approval authority. Its output is a provenance-bound
`ResearchEvidenceBundleV1` consumed by a separately selected Research Commander.

## Runtime boundary

The supported provider path is exactly:

```text
headed Chrome -> local CDP -> AGBrowse -> ChatGPT web UI
             -> GPT-5.6 Sol Pro / xhigh
```

There is no API-model fallback and no automatic fallback to another ChatGPT model or
reasoning profile. The host verifies the actual model, reasoning profile, browser
session, request binding, and conversation binding immediately around the send and
after completion. An incomplete response, stopped reasoning, interruption, binding
change, model mismatch, or reasoning mismatch invalidates the entire result.

Each `WEB_SCOUT` request is single-use and sends only after the bridge has prepared and
verified a blank Web Search tab. The resulting conversation ID must not appear in the
browser's pre-send conversation set or in the request's `prior_conversation_ids`. A
separate `RESEARCH_COMMANDER` invocation uses the same fail-closed transport but must
create another fresh conversation; free-form conversation history is never forwarded
between roles.

All executable and storage paths are supplied locally through environment variables:

| Variable | Purpose |
| --- | --- |
| `TRADING_RESEARCH_NODE_EXECUTABLE` | Node executable used for the local runner |
| `TRADING_RESEARCH_AGBROWSE_ENTRY` | Absolute path to the AGBrowse entry point |
| `TRADING_RESEARCH_AGBROWSE_ROOT` | Absolute path to the AGBrowse installation |
| `TRADING_RESEARCH_WEBGPT_BRIDGE` | Absolute path to the strict browser-state bridge |
| `TRADING_RESEARCH_CDP_ENDPOINT` | Credential-free loopback CDP endpoint |
| `TRADING_RESEARCH_ARTIFACT_ROOT` | Absolute local path for structured run artifacts |
| `TRADING_RESEARCH_RAW_OBJECT_ROOT` | Absolute gitignored raw-object storage path |
| `TRADING_RESEARCH_POLL_TIMEOUT_SECONDS` | Bounded AGBrowse response timeout |
| `TRADING_RESEARCH_COMMAND_TIMEOUT_SECONDS` | Bounded individual command timeout |
| `TRADING_RESEARCH_REBIND_TIMEOUT_SECONDS` | Bounded fresh-conversation rebind timeout |

The bridge and AGBrowse status payloads must prove that Chrome is headed and connected
over CDP. These values are deployment inputs and are never embedded in public source.

Execute one single-use Scout request with:

```powershell
$env:TRADING_REAL_LLM_ENABLED = "true"
uv run python -m trading.cli research scout `
  --request .local/research/input/web-scout-request.json `
  --output .local/research/evidence/evidence-bundle.json
```

The request and optional normalized output are strict UTF-8 JSON. Repository-local
output must stay under `.local/`, `artifacts/`, `runs/`, or `data/raw/`; raw
browser material remains under the configured local raw-object root. The command
prints only the request/cycle IDs, evidence hash, source/claim counts, verified
model/reasoning, and `real_order_routing=false`.

## WebGPT Research Commander

When the append-only Commander selection bound to a prepared
`ResearchRequestV1` is `WEBGPT_SOL_PRO`, execute that request with:

```powershell
$env:TRADING_REAL_LLM_ENABLED = "true"
uv run python -m trading.cli research commander-run `
  --request .local/research/runs/<cycle-id>/request/research_request.json `
  --bundle-root .local/research/runs
```

Use `--prior-conversation-id <id>` repeatedly when a deployment has additional
role-conversation IDs that are not visible in the current browser conversation set.
The transport always rejects the current conversation, every pre-send conversation,
and every explicitly supplied prior ID. It verifies GPT-5.6 Sol Pro, Pro access,
`xhigh`, headed Chrome/CDP, request ID, role, browser session, conversation binding,
completion, non-interruption, and active Web Search both before and after the response.
There is no API execution path.

The prompt contains only the exact hash-bound `ResearchRequestV1` and the
`ResearchDecisionV1` output schema. New facts discovered while browsing cannot be
smuggled into a decision as evidence; the Commander must return
`REQUEST_MORE_EVIDENCE` so a new Scout bundle and Research request can bind them.
Host-owned timestamps and proposal/decision hashes use explicit placeholders and are
computed only after the response passes schema and binding validation.

The selected Commander is checked before send and again after completion. Expiry,
selection changes, model/profile drift, conversation reuse, invalid JSON, binding
mismatch, or an incomplete response fail closed. No validated result is copied into
the cycle output on failure. A successful run atomically creates:

```text
.local/research/runs/<cycle-id>/output/research_decision.json
```

The separate transport audit remains under the configured local
`TRADING_RESEARCH_ARTIFACT_ROOT`.

## Active research scope

Every request carries a versioned, hashed `available_data_catalog`. It may contain any
eligible US-listed equity or ETF. The scout may investigate company filings and IR,
SEC records, central banks, governments, regulators, exchanges, primary datasets,
reputable news, industry research, X, Reddit, and new sources discovered during the
browse cycle.

Research questions distinguish:

- durable alpha discovery;
- current-strategy failure analysis;
- economic mechanisms;
- factor and regime behavior;
- hypothesis falsification;
- data feasibility;
- execution cost and capacity.

The universe is never hard-coded to semiconductors or leveraged ETFs. SOXL and SOXS
are optional high-risk instruments only when explicitly present in the catalog and
relevant to a research question.

## Provenance contract

Each source records its URL, title, publisher, publication time,
`first_available_at`, capture time, source tier, deterministic content hash, short
lawful excerpt, license note, instrument and factor tags, corroboration status, and
contradiction status.

Source tiers are:

1. `TIER_1_OFFICIAL`
2. `TIER_2_PRIMARY_DATA`
3. `TIER_3_REPUTABLE_NEWS`
4. `TIER_4_INDUSTRY_ANALYSIS`
5. `TIER_5_SOCIAL`
6. `TIER_6_UNVERIFIED`

X and Reddit can document narratives, rumor diffusion, sentiment, or investigation
leads. Social-only evidence cannot become a corroborated factual claim. Corroborated
claims require an official source or at least two independent non-social publishers.
Contradicted claims must cite a source explicitly marked as contradiction evidence.

The structured source `content_hash` covers the canonical URL, title, publisher,
publication time, first-availability time, and retained excerpt. Full page bodies,
browser captures, cookies, profiles, and raw payloads are excluded from structured
artifacts. If a deployment retains raw objects for licensed local use, they stay only
under the external `TRADING_RESEARCH_RAW_OBJECT_ROOT`, which must be excluded from Git.

## Point-in-time and failure behavior

Every source must satisfy:

```text
published_at <= first_available_at <= data_available_cutoff
first_available_at <= captured_at <= bundle.captured_at
```

Every stored source must be attributable to an actively completed browse query. Query,
source, claim, model, and browser bindings are schema-validated. Symbols not present in
the versioned catalog are rejected. Failures produce no usable evidence bundle, no
strategy change, no challenger, and no trading-plane side effect.
