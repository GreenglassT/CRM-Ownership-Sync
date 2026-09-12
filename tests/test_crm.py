"""API client: retry policy and exception translation. No network."""
import http.client
import io
import json
import urllib.error

import pytest

from pipeline.crm import CRM, CRMError


class Fake:
    """Stand-in for urllib.request.urlopen: each call pops the next outcome."""
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, req, timeout=None):
        self.calls.append(req.get_method())
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        body = json.dumps(o).encode()
        r = io.BytesIO(body)
        r.__enter__ = lambda s=r: s
        r.__exit__ = lambda *a: None
        return r


def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b"upstream"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("pipeline.crm.time.sleep", lambda s: None)
    c = CRM(base="http://x", token="t")
    def use(*outcomes):
        f = Fake(outcomes)
        monkeypatch.setattr("urllib.request.urlopen", f)
        return f
    c.use = use
    return c


def test_get_retries_on_5xx(client):
    f = client.use(http_error(502), http_error(503), {"ok": 1})
    assert client._request("GET", "/x") == {"ok": 1}
    assert f.calls == ["GET", "GET", "GET"]


def test_post_is_never_retried(client):
    f = client.use(http_error(502), {"account_id": "NEW"})
    with pytest.raises(CRMError):
        client.create_account({"name": "n"})
    assert f.calls == ["POST"]


def test_read_side_timeout_becomes_crmerror(client):
    f = client.use(TimeoutError("read timed out"), TimeoutError("again"), TimeoutError("again"))
    with pytest.raises(CRMError):
        client._request("GET", "/x")
    assert f.calls == ["GET", "GET", "GET"]        # retried, then surfaced as CRMError


def test_remote_disconnected_becomes_crmerror(client):
    client.use(http.client.RemoteDisconnected("gone"))
    with pytest.raises(CRMError):
        client.create_account({"name": "n"})
