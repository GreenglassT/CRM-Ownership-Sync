"""run_pipeline refuses to publish a queue built from a suspiciously small crawl."""
import json

import pytest

import run_pipeline
from pipeline import config


@pytest.fixture
def state(tmp_path, monkeypatch):
    for name in ("STATE_DIR",):
        monkeypatch.setattr(config, name, tmp_path)
    for name in ("SITE_SNAPSHOT", "CRM_SNAPSHOT", "PROPOSALS", "DECISIONS", "RUN_LOG"):
        monkeypatch.setattr(config, name, tmp_path / f"{name.lower()}.json")
    config.SITE_SNAPSHOT.write_text(json.dumps([{"slug": f"s{i}"} for i in range(35)]))
    return tmp_path


def test_empty_crawl_aborts_without_writing_queue(state, monkeypatch):
    monkeypatch.setattr(run_pipeline, "scrape", lambda: [])
    with pytest.raises(RuntimeError, match="35"):
        run_pipeline.run()
    assert not config.PROPOSALS.exists()
