import json
from datetime import datetime, timedelta, timezone

from app.health.monitor import health_state, update_source_state


def test_one_failed_source_degrades_but_does_not_fail_closed(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({"sources": {}}), encoding="utf-8")
    update_source_state(settings.state_path, "kvca", success=True, seen_ids=["1"])
    update_source_state(settings.state_path, "vcs", success=False)
    assert health_state(settings.state_path) == "DEGRADED"


def test_enabled_source_freshness_thresholds(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    current = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    settings.state_path.write_text(json.dumps({"sources": {
        "kvca": {"last_success_at": (current - timedelta(hours=5, minutes=1)).isoformat(), "consecutive_failures": 0}
    }}), encoding="utf-8")
    assert health_state(settings.state_path, enabled_sources=("kvca",), as_of=current) == "DEGRADED"
    settings.state_path.write_text(json.dumps({"sources": {
        "kvca": {"last_success_at": (current - timedelta(hours=12, minutes=1)).isoformat(), "consecutive_failures": 0}
    }}), encoding="utf-8")
    assert health_state(settings.state_path, enabled_sources=("kvca",), as_of=current) == "FAILED"


def test_index_errors_degrade_health(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    update_source_state(settings.state_path, "kvca", success=True, seen_ids=["1"])
    errors = settings.radar_root / "_System" / "index_errors.json"
    errors.parent.mkdir(parents=True, exist_ok=True)
    errors.write_text(json.dumps({"count": 1, "malformed_notes": ["bad.md"]}), encoding="utf-8")
    assert health_state(settings.state_path, enabled_sources=("kvca",), index_errors_path=errors) == "DEGRADED"
