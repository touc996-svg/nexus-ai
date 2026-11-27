# Client address behind a reverse proxy

The per-address rate limiter (before authentication) keys on a client address. By default that is the TCP peer. Behind a load
balancer the peer is the **proxy**, so every user would share one budget.

## Setting
`NEXUS_TRUSTED_PROXY_HOPS` (default `0`, range 0-10).
- `0`: `X-Forwarded-For` is **never read**. Behaviour is unchanged from before.
- `N > 0`: the entry N places from the **right** of `X-Forwarded-For` (all header lines combined, in order) is the client
  address. Entries to its left are client-supplied and ignored, so spoofed prefixes cannot change the bucket.
- Missing header, fewer entries than N, or an unparsable entry among the N rightmost: fall back to the socket peer (never to
  an attacker-chosen value). IPv6 is normalised; ports in `ip:port` and `[v6]:port` are stripped.

Set N to the number of proxies **you control or trust** that each append the address they received the request from.
Too high: the value comes from a client-controlled entry (spoofable). Too low: you key on a proxy's address (shared budget).

## Render: do not guess the value
Render's public documentation states that its load balancer terminates TLS and forwards requests over HTTP, and that
Cloudflare provides DDoS protection. It does **not** document the number of proxy hops, the contents of `X-Forwarded-For`, or
whether a client-supplied `X-Forwarded-For` is kept or overwritten (checked 2026-11-27). So no production value is
recommended here. Verify on your own service before setting it:

1. In a **non-production** service on Render, add a temporary admin-only endpoint that returns `request.client.host` and the raw
   `X-Forwarded-For` header (not committed).
2. Call it from a known address with no header, then with a forged `X-Forwarded-For: 9.9.9.9`.
3. Count the entries: the rightmost entry should be your address and the forged one should appear to its left. The number of
   trusted proxies that appended entries is N. If a forged header is not preserved, or your address is not in the chain, do
   not enable this setting; ask Render support.
4. Set `NEXUS_TRUSTED_PROXY_HOPS=N` and confirm two different clients get separate budgets.

A custom domain behind your own Cloudflare proxy adds a hop in front of Render's chain; re-measure after any topology change.

## Limitations
- Limits stay per process and are not shared across instances (use a shared store for that).
- The identity (per-credential) limiter is unchanged.
- IPv6 clients are bucketed per full address, so one user with a /64 can rotate addresses.
- `X-Forwarded-For` is the only header read; `Forwarded`, `X-Real-IP`, `True-Client-IP` and `CF-Connecting-IP` are not.
