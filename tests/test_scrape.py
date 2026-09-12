"""Scraper: pagination termination, tolerant parsing, retry. No network."""
import urllib.error

import pytest

from pipeline import scrape


def card(slug):
    return f'<div class="card"><h3><a href="/communities/{slug}">{slug}</a></h3></div>'


def detail(name, h1='<h1>{}</h1>'):
    return (h1.format(name) + '<dl class="detail"><dt>Address</dt><dd>1 Main St<br>Xtown, OH 44000</dd>'
            '<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span></dd><dt>Phone</dt><dd>(555) 000-0000</dd></dl>')


def fake_site(monkeypatch, pages):
    calls = []
    def _get(path):
        calls.append(path)
        return pages[path]
    monkeypatch.setattr(scrape, "_get", _get)
    return calls


def test_pagination_survives_homepage_overlap(monkeypatch):
    pages = {"/": 'href="/communities/a" href="/communities/b"',
             "/communities?page=1": card("a") + card("b"),
             "/communities?page=2": card("c") + card("d"),
             "/communities?page=3": card("c") + card("d")}      # site clamps to the last page
    fake_site(monkeypatch, pages)
    assert [s.rsplit("/", 1)[1] for s in scrape.discover_slugs()] == ["a", "b", "c", "d"]


def test_detail_h1_with_attributes_parses(monkeypatch):
    loc = scrape.parse_detail("/communities/x", detail("Bellhaven of X", h1='<h1 class="title">{}</h1>'))
    assert loc["name"] == "Bellhaven of X" and loc["zip"] == "44000"


def test_detail_without_h1_falls_back_to_slug():
    loc = scrape.parse_detail("/communities/bellhaven-of-x", detail("", h1="{}"))
    assert loc["name"] == "Bellhaven Of X"


def test_get_retries_transient_errors(monkeypatch):
    attempts = []
    def urlopen(req, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("reset")
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def read(self): return b"ok"
        return R()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(scrape.time, "sleep", lambda s: None)
    assert scrape._get("/") == "ok" and len(attempts) == 3
