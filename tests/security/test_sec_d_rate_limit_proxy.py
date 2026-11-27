"""D. Rate limiting and trusted proxies over HTTP, through the real middleware chain."""

import time

import pytest
from fastapi.testclient import TestClient

from tests.security.helpers import ALICE_KEY, BOB_KEY, OPERATOR_KEY, PROXY_PEER, auth


def peer(ctx, address):
    return TestClient(ctx.api, client=(address, 6000), raise_server_exceptions=False)


def hit(client, headers=None, path="/agents/enterprise"):
    return client.get(path, headers=headers or {}).status_code


def test_requests_beyond_the_limit_are_refused_with_retry_after(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 3})
    codes = [hit(ctx.client, auth(ALICE_KEY)) for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    refused = ctx.client.get("/agents/enterprise", headers=auth(ALICE_KEY))
    assert refused.headers["retry-after"].isdigit() and refused.headers["x-ratelimit-remaining"] == "0"
    assert refused.headers["x-content-type-options"] == "nosniff"


def test_unauthenticated_floods_are_limited_before_authentication_runs(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 3})
    assert [hit(ctx.client) for _ in range(5)] == [401, 401, 401, 429, 429]


def test_health_endpoints_are_not_rate_limited(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2})
    assert {hit(ctx.client, path="/health/live") for _ in range(10)} == {200}


def test_different_addresses_have_separate_budgets(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2})
    a, b = peer(ctx, "203.0.113.1"), peer(ctx, "203.0.113.2")
    assert [hit(a, auth(ALICE_KEY)) for _ in range(3)] == [200, 200, 429]
    assert hit(b, auth(ALICE_KEY)) == 200


# ---- proxy headers: default (0 hops) -----------------------------------------------------------------------------------

@pytest.mark.parametrize("forwarded", ["1.1.1.1", "2.2.2.2, 3.3.3.3", "garbage", "", "::1", "1.1.1.1:8080"])
def test_forwarded_headers_are_ignored_when_no_proxy_is_trusted(make_app, forwarded):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2}, peer=PROXY_PEER)
    codes = [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": f"{forwarded}{i}" if forwarded[:1].isdigit() and i else forwarded})
             for i in range(4)]
    assert codes == [200, 200, 429, 429]  # rotating the header never resets the budget


def test_rotating_spoofed_addresses_cannot_bypass_the_limit_by_default(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 3}, peer=PROXY_PEER)
    codes = [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": f"198.51.100.{i}", "X-Real-IP": f"192.0.2.{i}",
                              "Forwarded": f"for=203.0.113.{i}", "True-Client-IP": f"203.0.113.{i}",
                              "CF-Connecting-IP": f"203.0.113.{i}"}) for i in range(6)]
    assert codes[3:] == [429, 429, 429]


# ---- proxy headers: one trusted proxy ----------------------------------------------------------------------------------

def test_clients_behind_the_same_proxy_get_separate_budgets(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    assert [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": "203.0.113.1"}) for _ in range(3)] == [200, 200, 429]
    assert hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": "203.0.113.2"}) == 200


def test_a_spoofed_left_prefix_cannot_reset_the_budget_behind_a_trusted_proxy(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    codes = [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": f"10.9.9.{i}, 203.0.113.7"}) for i in range(5)]
    assert codes == [200, 200, 429, 429, 429]


@pytest.mark.parametrize("forwarded", ["", "unknown", "not-an-ip", "999.1.1.1", "1.2.3.4,,", ",", "<script>", "1.2.3.4 5.6.7.8"])
def test_malformed_chains_fall_back_to_the_proxy_address_not_to_a_chosen_value(make_app, forwarded):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    assert [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": forwarded}) for _ in range(3)] == [200, 200, 429]
    other = peer(ctx, "203.0.113.50")  # a different socket peer is unaffected
    assert hit(other, {**auth(ALICE_KEY), "X-Forwarded-For": forwarded}) == 200


def test_a_missing_header_falls_back_to_the_socket_peer_when_a_proxy_is_trusted(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    assert [hit(ctx.client, auth(ALICE_KEY)) for _ in range(3)] == [200, 200, 429]


def test_a_chain_shorter_than_the_trusted_hops_falls_back(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 2}, peer=PROXY_PEER)
    assert [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": f"203.0.113.{i}"}) for i in range(3)] == [200, 200, 429]


def test_two_trusted_hops_count_from_the_right(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 2, "NEXUS_TRUSTED_PROXY_HOPS": 2}, peer=PROXY_PEER)
    chain = lambda i: f"66.6.6.{i}, 203.0.113.9, 198.51.100.2"  # spoofed prefix varies, real client is fixed
    assert [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": chain(i)}) for i in range(4)] == [200, 200, 429, 429]


def test_ipv6_clients_behind_a_trusted_proxy_are_distinguished(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 1, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    assert hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": "2001:db8::1"}) == 200
    assert hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": "2001:0db8:0:0:0:0:0:1"}) == 429  # same address, other spelling
    assert hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": "2001:db8::2"}) == 200


def test_an_enormous_forwarding_header_is_handled_quickly(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 5, "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    header = ", ".join(f"10.{i // 256}.{i % 256}.1" for i in range(2000)) + ", 203.0.113.5"
    started = time.monotonic()
    assert hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": header}) == 200
    assert time.monotonic() - started < 2


# ---- per-credential limit -------------------------------------------------------------------------------------------

def test_one_credential_shares_one_budget_across_addresses(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 1000, "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": 2})
    codes = [hit(peer(ctx, f"203.0.113.{i}"), auth(ALICE_KEY)) for i in range(4)]
    assert codes == [200, 200, 429, 429]
    assert hit(peer(ctx, "203.0.113.99"), auth(BOB_KEY)) == 200  # another user's budget is untouched


def test_changing_client_identity_headers_does_not_reset_the_credential_budget(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 1000, "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": 2,
                        "NEXUS_TRUSTED_PROXY_HOPS": 1}, peer=PROXY_PEER)
    codes = [hit(ctx.client, {**auth(ALICE_KEY), "X-Forwarded-For": f"203.0.113.{i}"}) for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_operator_credentials_share_one_admin_budget(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 1000, "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": 2})
    assert [hit(peer(ctx, f"203.0.113.{i}"), auth(OPERATOR_KEY)) for i in range(3)] == [200, 200, 429]


def test_failed_authentication_attempts_do_not_consume_the_identity_budget_of_a_victim(make_app):
    ctx = make_app(env={"NEXUS_RATE_LIMIT_PER_MINUTE": 1000, "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": 2})
    for _ in range(10):
        hit(ctx.client, auth("wrong-" + ALICE_KEY))
    assert hit(ctx.client, auth(ALICE_KEY)) == 200
