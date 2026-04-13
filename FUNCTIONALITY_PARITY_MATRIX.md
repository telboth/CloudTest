# CloudTest Functionality Parity Matrix (Phase 1)

## Goal

Bring `CloudTest/unified_app.py` to feature parity with the original 3 apps:

- `streamlit_apps/reporter_app.py`
- `streamlit_apps/assignee_app.py`
- `streamlit_apps/admin_app.py`
- shared behavior from `streamlit_apps/common.py`

## Locked scope decisions

- Keep: Microsoft Entra login (Streamlit OIDC).
- Keep: OpenAI-based AI features.
- Drop: Azure DevOps integration (UI + calls).
- Drop: Ollama/local LLM options.
- Keep: single unified app with role-based pages (Reporter/Assignee/Admin).

## Current baseline (CloudTest)

Already implemented in `CloudTest/unified_app.py`:

- Basic auth gate (OIDC + optional local fallback).
- Create/read/update/delete bugs.
- Basic comments/samtale.
- Basic status/severity/assignee updates.
- Basic role-based page navigation.

Missing from original apps:

- Most shared UI components from `common.py` (filter/sort/search/system-status/cache controls).
- Background jobs + async AI flows.
- Attachment pipeline.
- Similar bug search / semantic behavior.
- Duplicate candidate tooling.
- Sentiment analysis UI flow.
- Rich dashboard and work-queue widgets.

## Parity matrix

| Area | Reporter | Assignee | Admin | Current in CloudTest | Decision | Priority |
|---|---|---|---|---|---|---|
| Auth + role mapping | Yes | Yes | Yes | Partial | Keep | P0 |
| Shared styling/layout helpers | Yes | Yes | Yes | Minimal | Keep | P1 |
| Search + refresh + cache controls | Yes | Yes | Yes | No | Keep | P1 |
| Sort + filter (status/severity/age/closed/tags) | Yes | Yes | Yes | No | Keep | P1 |
| System/health panel in sidebar | Yes | Yes | Yes | No | Keep | P1 |
| Bug list controls (visible count/pagination-lite) | Yes | Yes | Yes | No | Keep | P1 |
| Attachment upload + rendering | Yes | Yes | Yes | No | Keep | P1 |
| Conversation thread + change-highlighting | Yes | Yes | Yes | Basic only | Keep | P1 |
| Tags/environment/notify fields | Yes | Yes | Yes | No | Keep | P1 |
| Reporter AI draft ("Bruk AI...") | Yes | No | No | No | Keep | P1 |
| Reporter typeahead + similar bugs | Yes | No | No | No | Keep | P1 |
| Doc/file text extraction support | Yes | No | No | No | Keep | P2 |
| Assignee AI "Foresla losning" | No | Yes | No | No | Keep | P1 |
| Sentiment analysis + indicator | No | Yes | Yes | No | Keep | P1 |
| Duplicate candidate scan + actions | No | Yes | Yes | No | Keep | P1 |
| Bug history timeline | No | Yes | Yes | Basic write only | Keep | P1 |
| Admin SLA/work queue summary cards | No | No | Yes | No | Keep | P2 |
| Background jobs (track/poll/apply result) | Yes | Yes | Yes | No | Keep | P1 |
| Azure DevOps sync/remove UI | No | Yes | Yes | No | Drop | N/A |
| Ollama/local model controls | Yes | Yes | Yes | No | Drop | N/A |

## Implementation waves (recommended)

1. Shared foundation wave  
   Port core helpers from `common.py` into `CloudTest`-local modules, but remove backend-only and dropped features (DevOps/Ollama).

2. Reporter parity wave  
   Implement create/edit flow parity first (AI draft, typeahead, similar bugs, attachments, tags, notify, search/sort/filter).

3. Assignee parity wave  
   Implement work queue, solution suggestion flow, sentiment, duplicate scan/actions, history timeline.

4. Admin parity wave  
   Implement dashboard cards, advanced filters, duplicate tooling, sentiment and history insights.

5. Hardening + parity test wave  
   End-to-end parity checklist per role and regression test script for key paths.

## Acceptance criteria for phase 1

- Scope decisions are explicit and agreed.
- All major feature groups are mapped to: Keep/Drop and priority.
- Next wave (phase 2) can start with a bounded work package.

## Progress log

- 2026-04-13: Phase 2 started.
  - Added `CloudTest/foundation.py` for shared style/cache/search/filter/sort/system-panel helpers.
  - Wired shared foundation into `CloudTest/unified_app.py` for all role pages.
- 2026-04-13: Phase 3 started (Reporter parity wave, del 1).
  - Added Reporter AI draft flow (`Bruk AI til å fylle ut felter`) with OpenAI and robust error handling.
  - Added similar-bug helper flow in Reporter.
  - Added metadata fields in create/edit flow (kategori, miljø, tagger, varsling, assignee).
  - Added attachment upload + download rendering in unified app pages.
- 2026-04-13: Phase 3 continued (Reporter parity wave, del 2).
  - Added local description typeahead flow (`Foreslå fortsettelse` + `Sett inn forslag`).
  - Added duplicate-check workflow before create (exact duplicate stop + candidate confirmation gate).
  - Added explicit form actions (`Finn lignende bugs`, `Sjekk duplikater`, `Tøm felter`) in reporter create flow.
- 2026-04-13: Phase 3 continued (Reporter parity wave, del 3).
  - Added reporter bug refresh action (`Oppdater`) per bug.
  - Added reporter history rendering (`Endringshistorikk`) under each bug.
  - Switched reporter assignee edit to selectable assignable list (with fallback option).
  - Added safe queue/rerun flow for clearing "Ny oppdatering fra rapportør" input after save.
- 2026-04-13: Phase 3 continued (Reporter parity wave, del 4).
  - Added `Avanserte AI-detaljer` panel in CloudTest reporter AI section.
  - Added AI file text extraction (text/PDF) and extraction summary for draft generation.
  - Added stricter reporter create validation (minimum title/description, assignee whitelist, notify email checks).
- 2026-04-13: Phase 4 started (Assignee parity wave, del 1).
  - Added `AI: Foreslå løsning` for assignee bugs with short suggestion output based on bug context + conversation.
  - Added safe queue/rerun flow for `Sett inn forslag` into arbeidsnotater (avoids Streamlit session-state widget mutation errors).
  - Added direct `Sentiment - analyse` action in CloudTest assignee with persisted `sentiment_label` and `sentiment_summary`.
- 2026-04-13: Phase 4 continued (Assignee parity wave, del 2).
  - Added sidebar `Arbeidskø` summary for assignee view (open/in_progress/resolved/critical/negative sentiment counts).
  - Added sidebar `Mulige duplikater` tool with `Se etter duplikater` scan on current assignee view.
  - Added duplicate candidate actions in sidebar (`Slett`/`Skjul`) with cache refresh and rerun handling.
- 2026-04-13: Phase 4 continued (Assignee parity wave, del 3).
  - Expanded assignee bug editor with metadata updates (`Tildel bug til`, `Miljø`, `Tagger`, `Varsle e-post(er)`).
  - Added attachment upload on existing bugs in assignee flow.
  - Added per-bug refresh action and moved history to the bottom of the bug card flow.
- 2026-04-13: Phase 5 started (Admin parity wave, del 1).
  - Added admin sidebar `Arbeidskø` summary and `Mulige duplikater` scanner/actions (`Se etter duplikater`, `Slett`, `Skjul`).
  - Expanded admin bug card with richer metadata editing (assignee/status/severity/environment/tags/notify).
  - Added admin sentiment analysis action, attachment upload on existing bugs, and history rendering at the bottom.
- 2026-04-13: Phase 5 continued (Admin parity wave, del 2).
  - Added admin-specific sidebar filters (`Opprettet fra`, `Sentiment`, `Kun uten ansvarlig`, `Rapportør inneholder`, `Tilfredshet`).
  - Added admin dashboard KPI cards in main view (open/in-progress/unassigned/critical/negative sentiment/stale/feedback).
  - Added `Prioriter nå` list for stale bugs to support faster admin triage.
- 2026-04-13: Phase 5 continued (Admin parity wave, del 3).
  - Added quick status actions in admin bug cards (`Lukk bug`, `Gjenåpne bug`).
  - Added admin editing support for `Kategori`, `Rapportør-tilfredshet` and optional description update.
  - Extended update logic to persist `reporter_satisfaction` in unified local workflow.
- 2026-04-13: Phase 6 started (Hardening + parity test wave).
  - Added `CloudTest/parity_smoke_test.py` with assertions for core parity helpers (sentiment, email parsing, duplicate detection, admin date parsing, stale/aging logic).
  - Added `CloudTest/run_hardening_checks.ps1` to run compile + smoke checks in one command.
  - Updated `CloudTest/README.md` with a dedicated hardening-check command.
- 2026-04-13: Phase 6 continued (Hardening + parity test wave, del 2).
  - Added `CloudTest/UI_REGRESSION_CHECKLIST.md` with role-based manual regression checks for Reporter/Assignee/Admin.
  - Updated `CloudTest/README.md` with a direct reference to the UI regression checklist.
- 2026-04-13: Phase 6 continued (Hardening + parity test wave, del 3).
  - Added `CloudTest/UI_REGRESSION_REPORT.md` for per-run tracking of automated checks and manual UI verification status.
  - Updated `CloudTest/README.md` with report-file reference to standardize regression sign-off.
- 2026-04-13: Phase 6 continued (Hardening + parity wave, del 4).
  - Added local background job framework in `unified_app.py` (start/track/poll/finalize) and moved key AI actions to async-capable flow.
  - Added bug summary generation flow (`Oppsummer bug`) for Assignee/Admin with persisted `bug_summary`.
  - Added `changes since last view` support using `BugViewState` with manual `Marker som lest`.
  - Added sidebar work-queue filters for Assignee/Admin and wired them into page filtering.
  - Added delete confirmation flow for bug and duplicate deletions in Assignee/Admin.
  - Improved conversation/history rendering options (collapsed/compact and dedupe of consecutive duplicate comments).
- 2026-04-13: Phase 7 started (modularisering og robusthet).
  - Moved auth UI flow into `CloudTest/auth_ui.py` and replaced in-app auth implementation with a single wrapper in `unified_app.py`.
  - Moved OpenAI/JSON AI client logic into `CloudTest/ai_client.py` and kept thin wrappers in `unified_app.py`.
  - Added shared user-facing error formatting in `CloudTest/error_utils.py` for consistent fallback messages.
  - Moved background job runtime internals into `CloudTest/job_runtime.py` with stable wrapper API in `unified_app.py`.

