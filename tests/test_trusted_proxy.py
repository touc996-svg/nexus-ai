"""Client address resolution for the per-address rate limiter, through the real middleware."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.security.hardening import install_security, resolve_client_address

PEER = "10.0.0.5"  # the proxy
ENV = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
       "NEXUS_KEYSTORE_ENABLED", "NEXUS_RATE_LIMIT_PER_MINUTE", "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE",
       "NEXUS_TRUSTED_PROXY_HOPS")


def _request(*forwarded, peer=PEER):
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234) if peer else None,
             "query_string": b"", "server": ("t", 80), "scheme": "http"}
    return Request(scope)


# ---- unit: resolve_client_address --------------------------------------------------------------------------------

def test_zero_hops_never_reads_the_header():
    assert resolve_client_address(_request("1.2.3.4"), 0) == PEER


def test_one_hop_takes_the_rightmost_entry():
    assert resolve_client_address(_request("203.0.113.7"), 1) == "203.0.113.7"


def test_spoofed_prefix_on_the_left_is_ignored():
    # the client sent "9.9.9.9"; the one trusted proxy appended the real address it saw
    assert resolve_client_address(_request("9.9.9.9, 203.0.113.7"), 1) == "203.0.113.7"
    assert resolve_client_address(_request("8.8.8.8, 9.9.9.9, 203.0.113.7"), 1) == "203.0.113.7"


def test_two_hops_counts_from_the_right():
    # client -> proxyA -> proxyB -> app: "client, proxyA" ; spoofed prefix is still ignored
    assert resolve_client_address(_request("203.0.113.7, 198.51.100.2"), 2) == "203.0.113.7"
    assert resolve_client_address(_request("6.6.6.6, 203.0.113.7, 198.51.100.2"), 2) == "203.0.113.7"


def test_multiple_header_lines_are_combined_in_order():
    assert resolve_client_address(_request("6.6.6.6", "203.0.113.7, 198.51.100.2"), 2) == "203.0.113.7"


def test_ipv6_and_ports_are_normalised():
    assert resolve_client_address(_request("2001:0db8:0000:0000:0000:0000:0000:0001"), 1) == "2001:db8::1"
    assert resolve_client_address(_request("[2001:db8::1]:443"), 1) == "2001:db8::1"
    assert resolve_client_address(_request("203.0.113.7:51000"), 1) == "203.0.113.7"


@pytest.mark.parametrize("header", ["", "not-an-ip", "999.1.1.1", "1.2.3.4, garbage", "unknown", "1.2.3.4,,"])
def test_malformed_chain_falls_back_to_the_socket_peer(header):
    assert resolve_client_address(_request(header), 1) == PEER


def test_missing_header_or_too_short_chain_falls_back():
    assert resolve_client_address(_request(), 1) == PEER
    assert resolve_client_address(_request("203.0.113.7"), 2) == PEER  # fewer entries than trusted hops


def test_garbage_left_of_the_trusted_entries_does_not_matter():
    assert resolve_client_address(_request("not an ip, 203.0.113.7"), 1) == "203.0.113.7"


def test_no_socket_peer_gives_unknown():
    assert resolve_client_address(_request(peer=None), 0) == "unknown"


# ---- settings -----------------------------------------------------------------------------------------------------

def _app(monkeypatch, hops=None, per_address=2):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_MINUTE", str(per_address))
    if hops is not None:
        monkeypatch.setenv("NEXUS_TRUSTED_PROXY_HOPS", str(hops))
    api = FastAPI()

    @api.get("/ping")
    def ping():
        return {"ok": True}

    install_security(api)
    return api


def test_default_is_zero_hops(monkeypatch):
    assert _app(monkeypatch).state.security_settings.trusted_proxy_hops == 0


@pytest.mark.parametrize("value", ["-1", "11", "two"])
def test_invalid_hop_counts_are_rejected(monkeypatch, value):
    with pytest.raises(ValueError, match="NEXUS_TRUSTED_PROXY_HOPS"):
        _app(monkeypatch, hops=value)


# ---- integration: separate budgets behind one proxy ------------------------------------------------------------

def _get(client, forwarded):
    return client.get("/ping", headers={"X-Forwarded-For": forwarded}).status_code


def test_behaviour_is_unchanged_with_zero_hops_even_if_a_header_is_sent(monkeypatch):
    client = TestClient(_app(monkeypatch, hops=None), client=(PEER, 1))
    assert [_get(client, f"1.1.1.{i}") for i in range(3)] == [200, 200, 429]  # all share the proxy's budget


def test_clients_behind_the_same_proxy_get_separate_budgets(monkeypatch):
    client = TestClient(_app(monkeypatch, hops=1), client=(PEER, 1))
    assert [_get(client, "203.0.113.1") for _ in range(3)] == [200, 200, 429]
    assert _get(client, "203.0.113.2") == 200  # a different client is unaffected by the first one's exhaustion
    assert _get(client, "203.0.113.2") == 200
    assert _get(client, "203.0.113.2") == 429


def test_a_client_cannot_reset_its_budget_by_spoofing_the_header(monkeypatch):
    client = TestClient(_app(monkeypatch, hops=1), client=(PEER, 1))
    for _ in range(2):
        assert _get(client, "6.6.6.1, 203.0.113.9") == 200
    # same real client (rightmost entry), new fake prefixes each time: still limited
    assert _get(client, "6.6.6.2, 203.0.113.9") == 429
    assert _get(client, "6.6.6.3, 203.0.113.9") == 429


def test_malformed_header_shares_the_proxy_budget_not_a_chosen_one(monkeypatch):
    client = TestClient(_app(monkeypatch, hops=1), client=(PEER, 1))
    assert [_get(client, "junk") for _ in range(3)] == [200, 200, 429]
    assert _get(client, "junk2") == 429
