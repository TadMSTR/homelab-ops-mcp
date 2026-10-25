"""DNS-rebinding protection: the HTTP app refuses a non-loopback Host or Origin.

These drive the real Starlette app FastMCP builds, with the middleware main()
passes, rather than calling the guard in isolation, so a guard that is defined
but never wired in fails here.
"""

import asyncio
import logging
import sys

import pytest
from starlette.testclient import TestClient

from homelab_ops_mcp import server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("SYSTEM_OPS_ALLOWED_HOSTS", raising=False)

    def make():
        app = server.mcp.http_app(path="/mcp", middleware=server._http_middleware())
        return TestClient(app)

    return make


def _post(client, **headers):
    with client() as c:
        return c.post("/mcp", json=INIT, headers={**HEADERS, **headers})


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8282",
        "127.0.0.1:8282",
        "[::1]:8282",
        # A client behind an SSH port forward sends the forward's local port.
        "127.0.0.1:8290",
    ],
)
def test_loopback_host_is_served(client, host):
    r = _post(client, host=host)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8282", "192.0.2.10:8282"])
def test_foreign_host_is_refused(client, host):
    r = _post(client, host=host)
    assert r.status_code == 421


def test_foreign_origin_is_refused(client):
    r = _post(client, host="localhost:8282", origin="http://evil.example")
    assert r.status_code == 403


def test_loopback_origin_is_served(client):
    r = _post(client, host="localhost:8282", origin="http://localhost:8282")
    assert r.status_code == 200


def test_extra_host_from_env(client, monkeypatch):
    monkeypatch.setenv("SYSTEM_OPS_ALLOWED_HOSTS", " host.docker.internal:8282 ,")
    assert _post(client, host="host.docker.internal:8282").status_code == 200
    # Exact values: another port on that name is still refused.
    assert _post(client, host="host.docker.internal:9999").status_code == 421


def test_refusal_is_logged_without_the_header(client, recorder, caplog):
    """Neither our event nor any library record may carry the caller's header value.

    mcp's own check logs ``Invalid Host header: <value>``; a test that only
    replaced ``server.log`` could not see that, so stdlib records are checked too.
    """
    with caplog.at_level(logging.DEBUG):
        _post(client, host="evil.example", origin="http://evil-origin.example")
        _post(client, host="localhost:8282", origin="http://evil-origin.example")
    assert recorder.of("http.request_refused") == [
        {"status": 421, "scope_type": "http"},
        {"status": 403, "scope_type": "http"},
    ]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "evil.example" not in logged
    assert "evil-origin.example" not in logged


def _ws_scope(host):
    return {
        "type": "websocket",
        "path": "/mcp",
        "headers": [(b"host", host.encode())],
        "query_string": b"",
    }


async def _drive(guard, scope):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await guard(scope, receive, send)
    return sent


def test_websocket_with_a_foreign_host_is_closed_unserved():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    guard = server.HostOriginGuard(app, server._transport_security_settings())
    sent = asyncio.run(_drive(guard, _ws_scope("evil.example")))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == []


def test_websocket_with_a_loopback_host_reaches_the_app():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    guard = server.HostOriginGuard(app, server._transport_security_settings())
    asyncio.run(_drive(guard, _ws_scope("localhost:8282")))
    assert reached == ["websocket"]


def test_main_wires_the_guard(monkeypatch):
    """The guard only protects anything if main() hands it to mcp.run."""
    seen = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(server.telemetry, "init", lambda: None)
    monkeypatch.setattr(sys, "argv", ["homelab-ops-mcp", "--port", "8282"])
    server.main()
    [mw] = seen["middleware"]
    assert mw.cls is server.HostOriginGuard
    assert seen["transport"] == "streamable-http"
    assert seen["host"] == "127.0.0.1"


def test_main_shuts_telemetry_down_even_when_the_server_raises(monkeypatch):
    calls = []

    def crash(**kw):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(server.mcp, "run", crash)
    monkeypatch.setattr(server.telemetry, "init", lambda: None)
    monkeypatch.setattr(server.telemetry, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(sys, "argv", ["homelab-ops-mcp"])
    with pytest.raises(RuntimeError):
        server.main()
    assert calls == ["shutdown"]
