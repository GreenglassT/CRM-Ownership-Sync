"""Pin the runtime toggles so a developer's .env cannot change what the fixtures assert."""
import pytest

from pipeline import config


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    monkeypatch.setattr(config, "PROPOSE_PHONE_UPDATES", False)
    monkeypatch.setattr(config, "PARENT_ID_OVERRIDE", "")
