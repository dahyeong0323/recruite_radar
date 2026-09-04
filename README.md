# Korea Finance Recruiting Radar

Obsidian-canonical recruiting intelligence system for Korean VC, PE and IB opportunities.

The application code lives here, while the canonical records are Markdown files in the configured Obsidian vault. `index.json` is a rebuildable cache only. The default mode is dry-run; production writes, Telegram delivery and Git push require explicit configuration.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` only for local configuration. Never place tokens in the vault.

## Commands

```powershell
python -m app.cli bootstrap-vault
python -m app.cli ingest-fixtures --fixture tests/fixtures/sample_items.json
python -m app.cli rebuild-index
python -m app.cli health
python -m pytest
```

For a real collection run, configure the source endpoints and credentials, then use `collect --source kvca|vcs|kofia|saramin`. Keep `DRY_RUN=true` until the fixture and regression suite are green.

## Railway

Railway can deploy this repository directly from the root `Dockerfile`. The container listens on Railway's `PORT` and exposes `GET /health` for health checks. Start with `DRY_RUN=true`, then configure the variables in `.env.example` in Railway's environment settings before enabling production writes.

Production Git synchronization requires a writable `VAULT_ROOT` checkout with an authenticated Git remote. Keep all tokens and deploy credentials in Railway variables or a secret manager; never commit `.env`.

## Current implementation boundary

Phase 0 and the VC core are implemented first: shared `SourceItem` model, source adapters, normalization, deterministic classification/scoring fallback, cross-source dedupe, Markdown note generation, rebuildable index, generated dashboards, health state and tests. Telegram, scheduler, AI classification, Git synchronization and Saramin are wired as isolated components so they can be enabled without making an unavailable credential or source fail closed.

Source endpoints are deliberately configurable because the official sites change their HTML and routing. The adapters fail loudly on structural drift and preserve the source item when classification or detail parsing fails.
