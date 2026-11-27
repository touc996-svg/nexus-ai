"""Helpers that drive the local Docker Compose stack and sample it: start/stop services, resource usage, PostgreSQL and
application-metric deltas. Only used by the suite; the runner itself needs nothing but httpx."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx

from app.core.metrics import BUCKETS_MS

ROOT = Path(__file__).resolve().parents[1]
FILES = ["-f", str(ROOT / "deploy/compose.yaml"), "-f", str(ROOT / "deploy/compose.loadtest.yaml")]
PROJECT = "nexus-local"
SERVICES = ["caddy", "api", "db", "qdrant", "fake-llm"]
CONTROL = f"http://127.0.0.1:{os.getenv('FAKE_LLM_PORT', '18434')}"
BASE_URL = f"https://localhost:{os.getenv('NEXUS_HTTPS_PORT', '8443')}"


LEGACY_OVERLAY = ["-f", str(ROOT / "loadtest/compose.legacy-cmd.yaml")]


def compose(*args: str, env: dict | None = None, check: bool = True, capture: bool = True,
            legacy: bool = False) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "NEXUS_READINESS_REQUIRE_LLM": "false", **(env or {})}
    return subprocess.run(["docker", "compose", *FILES, *(LEGACY_OVERLAY if legacy else []), *args], env=full_env, cwd=ROOT, check=check,
                          capture_output=capture, text=True)


def api_key() -> str:
    return (ROOT / "deploy/secrets/nexus_api_key").read_text().strip()


def fetch_ca(path: Path) -> str:
    path.write_text(compose("exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt").stdout)
    return str(path)


def up(env: dict | None = None, build: bool = False) -> None:
    subprocess.run([str(ROOT / "scripts/deploy_local_secrets.sh")], cwd=ROOT, check=True, capture_output=True)
    args = ["up", "-d", "--wait", "--wait-timeout", "240"] + (["--build"] if build else [])
    compose(*args, env=env)


def down() -> None:
    compose("down", "-v", "--remove-orphans", check=False)


def wait_ready(ca: str, timeout: float = 120.0) -> float:
    """Seconds until ``/health/ready`` answers 200 (raises on timeout)."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        try:
            if httpx.get(f"{BASE_URL}/health/ready", verify=ca, timeout=3).status_code == 200:
                return time.perf_counter() - started
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("stack did not become ready")


def fake_llm(**settings) -> dict:
    return httpx.post(f"{CONTROL}/_control", json=settings, timeout=5).json()["state"]


def fake_llm_stats() -> dict:
    return httpx.get(f"{CONTROL}/_stats", timeout=5).json()


def _to_mb(text: str) -> float:
    number = float("".join(ch for ch in text if ch.isdigit() or ch == "."))
    unit = text.strip().lstrip("0123456789.").strip()
    return number * {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "kB": 1 / 1024, "MB": 0.95367, "GB": 953.67}.get(unit, 1)


class ResourceSampler:
    """Samples ``docker stats`` for the stack's containers every ``interval`` seconds while active."""

    def __init__(self, interval: float = 3.0):
        self.interval, self._stop, self._rows = interval, threading.Event(), []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        names = [f"{PROJECT}-{s}-1" for s in SERVICES]
        while not self._stop.is_set():
            out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                try:
                    row = json.loads(line)
                    service = row["Name"].removeprefix(f"{PROJECT}-").removesuffix("-1")
                    self._rows.append((service, float(row["CPUPerc"].rstrip("%")), _to_mb(row["MemUsage"].split("/")[0])))
                except (ValueError, KeyError):
                    continue
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=20)

    def summary(self) -> dict:
        out: dict[str, dict] = {}
        for service in SERVICES:
            rows = [r for r in self._rows if r[0] == service]
            if rows:
                out[service] = {"cpu_percent_avg": round(sum(r[1] for r in rows) / len(rows), 1),
                                "cpu_percent_max": round(max(r[1] for r in rows), 1),
                                "mem_mb_max": round(max(r[2] for r in rows), 1), "samples": len(rows)}
        return out


PG_QUERY = ("SELECT xact_commit, xact_rollback, tup_returned, tup_fetched, tup_inserted, tup_updated, blks_hit, blks_read, "
            "(SELECT count(*) FROM pg_stat_activity WHERE datname = 'nexus') FROM pg_stat_database WHERE datname = 'nexus'")
PG_FIELDS = ["commits", "rollbacks", "tuples_returned", "tuples_fetched", "tuples_inserted", "tuples_updated",
             "blocks_hit", "blocks_read", "connections"]


def pg_stats() -> dict:
    out = compose("exec", "-T", "db", "psql", "-U", "nexus", "-d", "nexus", "-At", "-F", ",", "-c", PG_QUERY).stdout.strip()
    return dict(zip(PG_FIELDS, (int(v) for v in out.split(",")), strict=True))


def pg_delta(before: dict, after: dict) -> dict:
    delta = {k: after[k] - before[k] for k in PG_FIELDS if k != "connections"}
    reads = delta["blocks_hit"] + delta["blocks_read"]
    delta["cache_hit_ratio"] = round(delta["blocks_hit"] / reads, 4) if reads else None
    delta["connections_at_end"] = after["connections"]
    return delta


def metric_snapshot(ca: str, attempts: int = 8) -> dict:
    """The persisted metrics, retried because the proxy may answer 5xx briefly right after heavy load; ``{}`` if it never works
    (the step then simply has no server-side figures instead of aborting the suite)."""
    for _ in range(attempts):
        try:
            response = httpx.get(f"{BASE_URL}/system/metrics", headers={"X-API-Key": api_key()}, verify=ca, timeout=30)
            if response.status_code == 200:
                return response.json()
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    return {}


def _p95(buckets: list[int]) -> float | None:
    total = sum(buckets)
    if not total:
        return None
    running = 0
    for index, count in enumerate(buckets):
        running += count
        if running >= 0.95 * total:
            return float(BUCKETS_MS[index]) if index < len(BUCKETS_MS) else float(BUCKETS_MS[-1] * 2)
    return None


def metric_delta(before: dict, after: dict, names=("llm_duration_ms", "rag_duration_ms", "http_request_duration_ms")) -> dict:
    """Server-side latency per histogram (all labels merged, plus per-label for rag ops): count, mean, bucket-based p95."""
    def collect(snap):
        merged: dict[str, dict] = {}
        for h in snap.get("histograms", []):
            if h["name"] not in names:
                continue
            keys = [h["name"]] + ([f"{h['name']}:{h['labels'].get('op')}"] if h["name"] == "rag_duration_ms" else [])
            order = [str(b) for b in BUCKETS_MS] + ["+Inf"]
            counts = [h["buckets_ms"].get(k, 0) for k in order]
            for key in keys:
                entry = merged.setdefault(key, {"count": 0, "sum": 0.0, "buckets": [0] * len(order)})
                entry["count"] += h["count"]
                entry["sum"] += h["sum_ms"]
                entry["buckets"] = [a + b for a, b in zip(entry["buckets"], counts, strict=True)]
        return merged

    b, a, out = collect(before), collect(after), {}
    for key, entry in a.items():
        prior = b.get(key, {"count": 0, "sum": 0.0, "buckets": [0] * len(entry["buckets"])})
        count = entry["count"] - prior["count"]
        if count > 0:
            out[key] = {"count": count, "mean_ms": round((entry["sum"] - prior["sum"]) / count, 1),
                        "p95_ms_bucket_upper_bound": _p95([x - y for x, y in zip(entry["buckets"], prior["buckets"], strict=True)])}
    counters = {}
    for snap, sign in ((before, -1), (after, 1)):
        for c in snap.get("counters", []):
            if c["name"] in {"qdrant_unavailable_total", "llm_failures_total", "http_throttled_total", "alerts_fired_total"}:
                key = c["name"] + (":" + ",".join(f"{k}={v}" for k, v in sorted(c["labels"].items())) if c["labels"] else "")
                counters[key] = counters.get(key, 0) + sign * c["value"]
    out["counters"] = {k: v for k, v in counters.items() if v}
    return out


QDRANT_PROBE = r"""
import json, time, statistics
from qdrant_client import QdrantClient
key = open('/run/secrets/qdrant_api_key').read().strip()
c = QdrantClient(url='http://qdrant:6333', api_key=key, timeout=10, check_compatibility=False)
name = [x.name for x in c.get_collections().collections][0]
def t(fn, n=40):
    xs = []
    for _ in range(n):
        s = time.perf_counter(); fn(); xs.append((time.perf_counter() - s) * 1000)
    xs.sort(); return {'p50_ms': round(statistics.median(xs), 2), 'p95_ms': round(xs[int(.95 * (n - 1))], 2)}
info = c.get_collection(name)
print(json.dumps({'collection': name, 'points': info.points_count,
                  'get_collection': t(lambda: c.get_collection(name)),
                  'scroll_5': t(lambda: c.scroll(name, limit=5)),
                  'query_vector': t(lambda: c.query_points(name, query=[0.01] * 768, limit=5))}))
"""


def qdrant_probe() -> dict:
    out = compose("exec", "-T", "api", "python", "-c", QDRANT_PROBE, check=False)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (out.stderr or out.stdout)[-300:]}


KEEPALIVE_PROBE = r"""
import http.client, json, statistics, time
out = []
for _ in range(3):
    c = http.client.HTTPConnection("127.0.0.1", 8010)
    c.request("GET", "/health/live"); c.getresponse().read()
    for _ in range(40):
        a = time.perf_counter(); c.request("GET", "/health/live"); c.getresponse().read(); out.append((time.perf_counter() - a) * 1000)
    c.close()
out.sort()
print(json.dumps({"requests": len(out), "p50_ms": round(statistics.median(out), 2), "p95_ms": round(out[int(.95 * (len(out) - 1))], 2)}))
"""


def keepalive_probe() -> dict:
    """Sequential keep-alive requests over loopback inside the API container (no proxy, no load generator in the way)."""
    out = compose("exec", "-T", "api", "python", "-c", KEEPALIVE_PROBE, check=False)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (out.stderr or out.stdout)[-200:]}
