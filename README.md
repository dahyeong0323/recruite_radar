# Korea Finance Recruiting Radar

An Obsidian-canonical recruiting intelligence service for Korean VC, PE, and IB opportunities. Collectors normalize source facts, conservative dedupe joins only strong matches, deterministic rules remain available when OpenAI classification fails, and generated Markdown is committed to a dedicated Vault checkout.

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

`SARAMIN_DAILY_LIMIT` defaults to 470. `SARAMIN_KEYWORDS_PER_RUN` defaults to 8 and rotates the keyword window between runs to reduce repeated broad queries while preserving coverage. A partial stream or item failure records a degraded run and does not advance the source watermark.

## Telegram

Outgoing delivery requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Immediate A alerts use `_System/notification_outbox.json`: a durable `sending` reservation is pushed before delivery, explicit failures remain retryable, and delivered IDs are not emitted again after restart. Daily B digest receipts live in `_System/digest_state.json`, so unchanged jobs are sent once.

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

Zero successful source runs are `DEGRADED`, not healthy.

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
| `OPENAI_API_KEY` | Optional Responses API key. |
| `OPENAI_MODEL_CLASSIFIER` | Optional classifier model; deterministic fallback remains enabled. |
| `TZ` | Application timezone; default `Asia/Seoul`. |
| `SCHEDULER_ENABLED` | Enable the in-process scheduler. |
| source URL variables | `KVCA_LIST_URL`, `VCS_LIST_URL`, `KOFIA_LIST_URL`, `SARAMIN_API_URL`. |

All third-party exceptions pass through central redaction before health notes, run logs, or returned diagnostics are written.

## Configuration ownership

`config/scoring.yaml` is loaded by the deterministic scoring runtime. `config/taxonomy.yaml` documents and validates the vocabulary whose executable schema is canonical in `app/models.py`. The candidate preference and watchlist example files are operator templates only and are intentionally not loaded; copying them does not change runtime behavior.
