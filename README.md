# Korea Finance & Content Recruiting Radar

Dependencies are locked in `uv.lock`. CI runs the full test suite, byte-compilation,
and a production Docker build for every push and pull request.

An Obsidian-canonical recruiting intelligence service for Korean finance and content-industry opportunities. Collectors normalize source facts, conservative dedupe joins only strong matches, deterministic rules remain available when OpenAI classification fails, and generated Markdown is committed to a dedicated Vault checkout.

## Canonical data model

`Career/Recruiting_Radar/Jobs/**/*.md` is canonical. `_System/index.json` and dashboards are rebuildable projections; malformed notes are skipped and listed in `_System/index_errors.json`. No database is required.

## Safe local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python -m app.cli bootstrap-vault
python -m pytest --basetemp=.pytest-tmp/test
```

`DRY_RUN=true` is the default. It writes only to `.runtime/dry-run-vault`, does not send Telegram, and never mutates production Git.

## Production Vault checkout

Production is explicit: set `DRY_RUN=false`, a writable absolute `VAULT_ROOT`, `VAULT_GIT_URL`, and `VAULT_BRANCH`. On startup the service:

1. clones the configured branch when `VAULT_ROOT` is empty;
2. otherwise validates the Git worktree, repairs `origin` when configured, fetches, checks out the branch, and pulls with rebase;
3. fails closed before the scheduler starts if checkout or readiness fails.

For private repositories, prefer a repository-scoped write-enabled SSH deploy key via `SSH_DEPLOY_KEY` and an SSH `VAULT_GIT_URL`. A fine-grained `GITHUB_TOKEN` limited to the Vault repository is also supported for HTTPS. Authentication is passed through process environment config; never embed credentials in `VAULT_GIT_URL`. Automation commits use `git commit --only` scoped to `VAULT_RELATIVE_PATH`, so unrelated staged files are not included. A dedicated server-side clone is still strongly recommended.

Collection operations also use a cross-process file lock keyed by `VAULT_ROOT`. This prevents the HTTP scheduler and an operator CLI command in the same container from mutating the checkout concurrently; a second operation fails safely instead of corrupting Git state.

Run exactly one Railway replica while the built-in scheduler is enabled. The file lock coordinates processes that share one filesystem, but separate replicas do not share that lock or checkout. An external scheduler may invoke the CLI commands with `SCHEDULER_ENABLED=false` when horizontal web replicas are required.

Railway example:

```env
DRY_RUN=false
VAULT_ROOT=/data/obsidian
VAULT_RELATIVE_PATH=Career/Recruiting_Radar
VAULT_GIT_URL=https://github.com/OWNER/VAULT.git
VAULT_BRANCH=master
GITHUB_TOKEN=secret-value
TZ=Asia/Seoul
SCHEDULER_ENABLED=true
```

Mount persistent storage at `/data`. The app listens on Railway's `PORT` and does not require a manually configured port.

## Collectors and Saramin quota

KVCA, VCS, KOFIA, and Saramin remain isolated sources. Known IDs are source-scoped. Saramin publication and update watermarks run as separate query streams and union by job ID. Page indices begin at zero, publication sorting uses `pd`, update sorting uses `ud`, and usage is reserved atomically in `_System/api_usage.json` immediately before each attempt.

`SARAMIN_DAILY_LIMIT` defaults to 470. `SARAMIN_KEYWORDS_PER_RUN` defaults to 8 and rotates the keyword window between runs. Each keyword and publication/update stream has its own watermark and resumable page cursor. A capped or partial query keeps its original watermark, resumes at the next page, and records a degraded run.

Canonical notes carry a parser version. The nightly active refresh automatically includes notes written by an older parser, incomplete detail fallbacks, and pending classifications. This repairs affected existing records after a parser rollout instead of leaving them permanently hidden behind the known-ID cache.

`ENABLED_SOURCES` defaults to `kvca,vcs,kofia,saramin`, preserving the existing finance deployment. Add `company` to activate official content-company collectors. Disable a source explicitly when its production credential is unavailable; for example, use `kvca,vcs,kofia,company` until a Saramin key is provisioned. Readiness fails closed when Saramin is enabled without `SARAMIN_ACCESS_KEY`.

## Content radar

`config/content_watchlist.yaml` is the runtime source of truth for content companies, aliases, official URLs, collector adapters, and fallback status. The first release provides official collectors for NAVER WEBTOON, Kakao Entertainment, SM Entertainment, HYBE, JYP Entertainment, MUNPIA, and Wavve. Other watchlist companies use Saramin fallback queries and are explicitly marked `fallback` rather than represented as official collectors.

Content postings share the canonical Markdown database and are separated with `category: Content`. They retain lower-priority and currently ineligible internships for historical analysis. Deterministic classification records content subcategory, student eligibility evidence, graduation requirements, normalized internship duration, and `summer_fit` (`HIGH`, `POSSIBLE`, `LOW`, `INELIGIBLE`, or `UNKNOWN`). Finance scoring remains unchanged.

Safe commands:

```bash
ENABLED_SOURCES=kvca,vcs,kofia,saramin,company python -m app.cli collect-all
python -m app.cli collect --source company
python -m app.cli preview-content
```

`preview-content` fetches official postings and prints Telegram-formatted previews without writing jobs or sending messages.

## Broad discovery: Linkareer and JobKorea

`linkareer` and `jobkorea` are independently selectable sources. Neither is added to the default `ENABLED_SOURCES`, so existing deployments keep their current behavior. The rotating search terms live in `config/discovery_keywords.yaml` and cover Finance, Content, Beauty / Consumer, and Gaming / Consumer Internet. New categories use deterministic scoring; Finance and Content retain their existing scoring paths. Canonical notes keep every discovery URL and source ID, while confirmed official facts take precedence during a merge.

Linkareer reads the public search page's Next/Apollo data and fetches detail pages only for unseen, relevant junior listings. JobKorea parses the public job list and its JSON-LD detail pages, with its POST-backed pager. Both check `robots.txt` on each run, limit pages and detail requests, delay requests, and record blocked or structurally changed responses in source health. The discovery cursor and per-keyword watermarks are stored under `_System/discovery_state.json` in the selected Vault; failed runs retain the cursor.

JobKorea's service terms restrict copying information obtained through the service without prior consent. For that reason `JOBKOREA_LICENSED_ACCESS=false` blocks automated requests by default; set it to `true` only after obtaining permission for this use. The collector is fixture-tested but should not be enabled on an unlicensed deployment. Linkareer also requires low-rate use and must not collect its restricted STEM section. Review both sites' current terms before changing collection volume.

For an isolated Linkareer trial, set `DRY_RUN=true`, `ENABLED_SOURCES=linkareer`, and a disposable `DRY_RUN_VAULT_ROOT`, then run `python -m app.cli collect --source linkareer`. The default is two keywords, two pages per keyword, at most eight detail requests, and a two-second delay between requests. Tune `LINKAREER_KEYWORDS_PER_RUN`, `JOBKOREA_KEYWORDS_PER_RUN`, `DISCOVERY_MAX_PAGES`, `DISCOVERY_MAX_DETAILS`, and `DISCOVERY_DELAY_SECONDS` conservatively. Beauty and gaming jobs appear in their own generated dashboard sections and views.

## Telegram

Outgoing delivery requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Immediate A alerts use `_System/notification_outbox.json`: a durable `sending` reservation is pushed before delivery, explicit failures remain retryable even when a later collection returns no items, and delivered material fingerprints are not emitted again after restart. Daily B digest and deadline reminder receipts are also persisted; long digests are chunked without dropping entries.

For commands and callback buttons, set a random `TELEGRAM_WEBHOOK_SECRET` and register:

```text
https://YOUR_DOMAIN/telegram/webhook
```

Pass the same value to Telegram as `secret_token`. Never put tokens in a URL saved to Markdown or logs.

## Scheduling and commands

The built-in APScheduler is optional (`SCHEDULER_ENABLED=true`) and runs in `TZ`:

- collect all sources every two hours;
- refresh active jobs at 03:00;
- deadline reminders at 09:00;
- daily digest at 20:30.

The same business actions are independent CLI commands suitable for Cloud Run Jobs and Cloud Scheduler:

```bash
python -m app.cli collect-all
python -m app.cli refresh-active
python -m app.cli digest
python -m app.cli deadline
python -m app.cli collect --source kvca
python -m app.cli backfill --source kofia --from 2025-01-01
```

Set `SCHEDULER_ENABLED=false` when an external scheduler invokes these commands.

## Health endpoints

- `GET /livez`: process liveness only.
- `GET /readyz`: writable runtime and, in production, valid Vault checkout, state, and Git configuration. Returns 503 when unsafe.
- `GET /health`: detailed radar state, readiness reasons, source failures, and dry-run status.

Zero successful source runs, stale collection watermarks, malformed canonical notes, pending classifications, persisted Git/Telegram failures, or a stopped in-process scheduler are not reported as healthy. Refresh timestamps are tracked separately, so a successful refresh cannot mask a stalled collection job.

## Environment variables

| Variable | Purpose |
|---|---|
| `DRY_RUN` | Safe mode; production mutation requires explicit `false`. |
| `DRY_RUN_VAULT_ROOT` | Optional disposable Vault path for dry-run. |
| `VAULT_ROOT` | Writable production checkout path. |
| `VAULT_RELATIVE_PATH` | Managed path; default `Career/Recruiting_Radar`. |
| `VAULT_GIT_URL` | Credential-free canonical Vault remote URL. |
| `VAULT_BRANCH` | Canonical Vault branch. |
| `GITHUB_TOKEN` | Optional private HTTPS Git authentication secret. |
| `SSH_DEPLOY_KEY` | Preferred repository-scoped private SSH deploy key. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot secret. |
| `TELEGRAM_CHAT_ID` | Authorized destination chat. |
| `TELEGRAM_WEBHOOK_SECRET` | Telegram webhook request verification secret. |
| `SARAMIN_ACCESS_KEY` | Saramin Open API credential. |
| `SARAMIN_DAILY_LIMIT` | Persistent daily safety ceiling; default 470. |
| `SARAMIN_KEYWORDS_PER_RUN` | Rotating keyword window size; default 8. |
| `SARAMIN_CONTENT_KEYWORDS_PER_RUN` | Additional rotating content-company fallback queries; default 3. Finance query capacity is preserved. |
| `ENABLED_SOURCES` | Comma-separated scheduled sources; add `company` or `linkareer` as needed. `jobkorea` requires licensed access. |
| `LINKAREER_KEYWORDS_PER_RUN`, `JOBKOREA_KEYWORDS_PER_RUN` | Rotating search terms per run; default 2 each. |
| `DISCOVERY_MAX_PAGES`, `DISCOVERY_MAX_DETAILS`, `DISCOVERY_DELAY_SECONDS` | Discovery request caps and inter-request delay; defaults 2, 8, and 2 seconds. |
| `JOBKOREA_LICENSED_ACCESS` | Fail-closed JobKorea collection gate; default `false`. |
| `OPENAI_API_KEY` | Optional Responses API key. |
| `OPENAI_MODEL_CLASSIFIER` | Optional classifier model; deterministic fallback remains enabled. |
| `TZ` | Application timezone; default `Asia/Seoul`. |
| `SCHEDULER_ENABLED` | Enable the in-process scheduler. |
| source URL variables | `KVCA_LIST_URL`, `VCS_LIST_URL`, `KOFIA_LIST_URL`, `SARAMIN_API_URL`. |

All third-party exceptions pass through central redaction before health notes, run logs, or returned diagnostics are written.

## Configuration ownership

`config/scoring.yaml` remains the finance scoring source. `config/content_scoring.yaml` and `config/content_watchlist.yaml` are loaded by the content runtime. `config/taxonomy.yaml` documents the vocabulary whose executable schema is canonical in `app/models.py`. Candidate preference and example watchlist files remain operator templates.

## Swiss Korea Event Radar

The Event domain lives in `app/events`, separately from Job models, scoring,
indexes and delivery fingerprints. Set `EVENT_RADAR_ENABLED=true` to register
its scheduler and Telegram routes; the default is false. Existing Job source
settings and schedules continue to apply independently. Event times and digest
use `Europe/Zurich`, including daylight saving time.

Canonical notes are under `Career/Recruiting_Radar/Events/<discovery-year>/`.
Event dates changing do not move the note or change its ID. Facts, source
observations, field provenance, before/after changes, user status and notification
intents are stored in Markdown frontmatter. Edit personal content outside the
`event:auto` markers; automatic refresh preserves it. Event indexes and seven
views are rebuildable. Source state, discovery queues and outbox receipts under
`_System/Events` are durable operational data, not disposable projections.

`config/events/sources.yaml` owns URLs, parser type, source trust, polling cadence,
request caps, parser versions and validation status. Only verified sources with
public-read policy can be enabled. Currently enabled: Friends of Korea upcoming
events, the Bern embassy activities board, Geneva mission activities board and
Startupticker's calendar. Administrative notices are excluded. Retrospective
events and low scoring events remain stored. S-GE is disabled pending Load More
pagination validation; its real JSON-LD detail parser is tested. KOTRA's legacy
Zurich landing URL returned 404. Other IR and organizer sources remain explicitly
pending rather than being counted as working collectors.

Collectors support official HTML, JSON-LD Event, RSS/Atom discovery links and
explicit ICS VEVENT occurrences. ICS recurrence rules are retained as evidence;
the collector does not invent/expand recurrence dates. Add a source by configuring
an adapter and committing real list/detail/empty/pagination fixtures. Newly found
organizers do not automatically become trusted or broadly crawled sources.

Discovery collects outside the Vault lock and writes a local persistent spool
beside the production Vault volume. Applying a batch synchronizes Git and
rechecks identity under the existing operation lock. A failed Git push retains
the spool. Detail failures retain list facts plus a durable retry URL. Every scan
checks the head as well as a historical continuation, so older pages do not hide
new announcements. Robots rules, Crawl-Delay, bounded retries and redirect URL
validation apply to every public page request. Public search snippets are only
discovery signals; they cannot establish verified dates, participants or priority.

Search uses an optional Brave API key. Without it, official sources still run and
health reports `not_configured`. `EVENT_SEARCH_FREE_VERIFIED=true` and a positive
`EVENT_SEARCH_FREE_REMAINING` are required. Configure a zero paid-spend cap at the
provider before enabling it. The local ledger reserves requests durably before
network calls and enforces the smaller of the verified free allowance, 20/day
and 600/month. Usage reservations are not refunded on failure. No Event LLM calls
are made. Search-result descriptions are not retained as canonical Event text.

Event and registration states are separate. Date-only events finish on the next
local day. Cancellation/postponement requires source evidence; a disappeared
listing or HTTP failure is insufficient. Conservative cross-source matching uses
dates, city, organizer and title; uncertain matches stay separate and appear in
the dedupe review state. Official facts win, supplemental fields fill gaps and
conflicts remain visible. Reviewed duplicates can be merged with alias notes
preserving the original user's notes/status history.

Scoring weights are in `config/events/scoring.yaml`, organizations in
`organizers.yaml`, multilingual queries in `queries.yaml`, and Geneva-based
geographic preferences in `preferences.yaml`. A is 75+, B is 55+. An A alert also
requires verified Korea relevance and Swiss location. Invitation-only events can
be A; eligibility, price and participants are never assumed. Unknown access is
reported explicitly. Scores are retained after an event completes or cancels.

Telegram: `/events`, `/events_high`, `/events_geneva`, `/events_zurich`,
`/events_saved`, `/events_status`. Messages show dates, place, organizer,
participants, reasons, access, registration and scores. Buttons record interest,
registration and attendance; they do not register with an external organizer.
A and material changes are immediate, B is included in the 18:30 digest once per
meaningful version, and saved events have 7/1-day reminders. New alerts for past
events are suppressed. A 5-minute worker drains durable intents.

Delivery deliberately favors avoiding duplicates. Reserve in Git before sending;
store Telegram message IDs on success. Definite connection failures and rate
limits retry. Read/write timeout, ambiguous server failures or interrupted sending
become `delivery_unknown` and never automatically resend. Health exposes them;
an operator must explicitly request a retry. Telegram and Git cannot provide an
atomic exactly-once transaction, so this policy can leave an uncertain message
undelivered. Job delivery behavior is unchanged.

Useful commands (global `DRY_RUN=true` selects an isolated local Vault):

```powershell
$env:DRY_RUN='true'
$env:DRY_RUN_VAULT_ROOT='.runtime/event-verification'
python -m app.cli events collect --force
python -m app.cli events collect --source mission_geneva --force --backfill
python -m app.cli events refresh
python -m app.cli events preview
python -m app.cli events health
python -m app.cli events rebuild
python -m app.cli events migrate             # validation / dry-run report
python -m app.cli events migrate --apply     # exact backups before migration
python -m app.cli events merge --from-id evt-OLD --into-id evt-CANONICAL
python -m app.cli events resend --delivery-key 'evt-ID:update:FINGERPRINT'
```

`events ingest --fixture FILE --backfill` imports EventSourceItem JSON without
creating alert intents. Preview never sends Telegram. In production all mutation
commands use the same operation/Git locks as Job operations. Roll back by disabling
`EVENT_RADAR_ENABLED`; preserve the Event notes and operational state.

Event health is added under `/health.events` without changing Job readiness.
Source age thresholds follow its configured cadence (2x degraded, 4x failed).
Monthly JSONL logs, malformed-note diagnostics, queue backlog and uncertain
deliveries are under `_System/Events`. CI runs the existing Job suite plus Event
fixture, lifecycle, dedupe, persistence, scheduler and delivery failure tests.
