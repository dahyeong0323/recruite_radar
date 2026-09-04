import json

from app.health.monitor import health_state, update_source_state


def test_one_failed_source_degrades_but_does_not_fail_closed(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({"sources": {}}), encoding="utf-8")
    update_source_state(settings.state_path, "kvca", success=True, seen_ids=["1"])
    update_source_state(settings.state_path, "vcs", success=False)
    assert health_state(settings.state_path) == "DEGRADED"
