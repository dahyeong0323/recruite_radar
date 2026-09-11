import pytest

from app.config import SUPPORTED_SOURCES, _enabled_sources_env


def test_enabled_sources_default_to_all_supported(monkeypatch):
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    assert _enabled_sources_env() == SUPPORTED_SOURCES


def test_enabled_sources_are_normalized_and_deduplicated(monkeypatch):
    monkeypatch.setenv("ENABLED_SOURCES", " KVCA, vcs,kvca ")
    assert _enabled_sources_env() == ("kvca", "vcs")


@pytest.mark.parametrize("value", ["", "kvca,unknown"])
def test_enabled_sources_reject_empty_or_unknown_values(monkeypatch, value):
    monkeypatch.setenv("ENABLED_SOURCES", value)
    with pytest.raises(ValueError, match="ENABLED_SOURCES"):
        _enabled_sources_env()
