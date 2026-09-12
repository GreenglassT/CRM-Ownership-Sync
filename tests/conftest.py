"""Pin the runtime toggles and keep every test away from the real state/ directory."""
import pytest

from pipeline import config


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROPOSE_PHONE_UPDATES", False)
    monkeypatch.setattr(config, "PARENT_ID_OVERRIDE", "")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    for name in ("SITE_SNAPSHOT", "CRM_SNAPSHOT", "PROPOSALS", "DECISIONS", "RUN_LOG"):
        monkeypatch.setattr(config, name, tmp_path / f"{name.lower()}.json")
