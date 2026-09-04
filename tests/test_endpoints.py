import asyncio
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

import app.main as main
from app.service import RadarService


def test_livez_reports_process_liveness():
    assert asyncio.run(main.livez()) == {"status": "alive"}


def test_readyz_reports_dry_run_runtime_ready(settings, monkeypatch):
    settings.radar_root.mkdir(parents=True)
    monkeypatch.setattr(main, "settings", settings)
    assert asyncio.run(main.readyz())["status"] == "ready"


def test_readyz_fails_for_unprepared_production_vault(settings, monkeypatch):
    prod = replace(settings, dry_run=False, git_url="https://example.test/vault.git")
    prod.radar_root.mkdir(parents=True)
    monkeypatch.setattr(main, "settings", prod)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.readyz())
    assert caught.value.status_code == 503


def test_health_is_detailed_and_not_healthy_with_zero_sources(settings, monkeypatch):
    settings.state_path.parent.mkdir(parents=True)
    settings.state_path.write_text(json.dumps({"sources": {}}), encoding="utf-8")
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "service", RadarService(settings))
    result = asyncio.run(main.health())
    assert result["status"] == "DEGRADED"
    assert result["ready"] is True
    assert result["sources"] == []
