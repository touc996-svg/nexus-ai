"""The scripted load-test suite against the local production-like stack (see docs/LOAD_TESTING.md).

Every step records client-side numbers (rps, p50/p95/p99, error classes, 429/503), container CPU/memory, PostgreSQL counters
and the application's own latency histograms (LLM and RAG) as deltas. Production defaults are never changed in the repository:
the *capacity* profile raises the per-address and per-identity limits through environment variables on the test stack only,
and the *default-limits* profile exercises the shipped limits (rate-limit scenario).
"""

import asyncio
import json
import os
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from loadtest import stack
from loadtest.runner import run_load

CAPACITY = {"NEXUS_RATE_LIMIT_PER_MINUTE": "1000000", "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": "1000000"}
DEFAULT_LIMITS = {"NEXUS_RATE_LIMIT_PER_MINUTE": "120", "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE": "120"}


class Suite:
    def __init__(self, duration: float, sustained: float, out: Path, ca_path: Path, seed: int = 1):
        self.duration, self.sustained, self.out, self.ca_path, self.seed = duration, sustained, out, ca_path, seed
        self.results: dict = {"steps": []}
        self.ca = ""
        self.key = ""

    # ---- plumbing --------------------------------------------------------------------------------------------------

    def log(self, message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    def load(self, scenario: str, concurrency: int, duration: float | None = None, **kw) -> dict:
        return asyncio.run(run_load(scenario, stack.BASE_URL, self.key, concurrency, duration or self.duration, ca_cert=self.ca,
                                    seed=self.seed, **kw))

    def measured(self, name: str, scenario: str, concurrency: int, duration: float | None = None, note: str = "",
                 light: bool = False, **kw) -> dict:
        """One scenario with resource, database and server-side latency measurement around it (``light``: client-side figures
        and container resources only, no metric/database snapshots, so no flush waits)."""
        self.log(f"{name}: {scenario} c={concurrency}")
        if not light:
            time.sleep(12)  # let every worker flush before the 'before' snapshot
            before_metrics, before_pg = stack.metric_snapshot(self.ca), stack.pg_stats()
        with stack.ResourceSampler() as sampler:
            result = self.load(scenario, concurrency, duration, **kw)
        step = {"name": name, "note": note, **result, "resources": sampler.summary()}
        if not light:
            time.sleep(12)
            step["postgres"] = stack.pg_delta(before_pg, stack.pg_stats())
            step["server_side"] = stack.metric_delta(before_metrics, stack.metric_snapshot(self.ca))
        self.results["steps"].append(step)
        lat = step["latency_ms"]
        self.log(f"  -> {step['rps']} rps, ok={step['success_rate']}, p50={lat['p50']} p95={lat['p95']} p99={lat['p99']} "
                 f"429={step['http_429']} 503={step['http_503']} other5xx={step['http_5xx_other']} err={step['transport_errors']}")
        return step

    def restart_api(self, profile: dict, extra: dict | None = None, legacy: bool = False) -> None:
        stack.compose("up", "-d", "--force-recreate", "--no-deps", "--wait", "--wait-timeout", "120", "api",
                      env={**profile, **(extra or {})}, legacy=legacy)
        stack.wait_ready(self.ca)

    # ---- the suite -------------------------------------------------------------------------------------------------

    def run(self, only: set[str] | None = None) -> dict:
        want = (lambda n: only is None or n in only)
        info = subprocess.run(["docker", "info", "--format", "{{.NCPU}} CPUs, {{.MemTotal}} bytes, {{.OperatingSystem}}, {{.Architecture}}"],
                              capture_output=True, text=True).stdout.strip()
        others = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
        foreign = [n for n in others if not n.startswith(f"{stack.PROJECT}-")]
        self.results["environment"] = {"load_average_at_start": os.getloadavg(), "other_running_containers": len(foreign),"started": datetime.now(UTC).isoformat(timespec="seconds"), "docker": info,
                                       "host": f"{platform.system()} {platform.machine()}", "duration_per_step": self.duration,
                                       "seed": self.seed, "profile_note": "capacity profile raises rate limits on the test stack only"}
        self.log("starting stack")
        stack.up(env=CAPACITY, build=True)
        self.ca = stack.fetch_ca(self.ca_path)
        self.key = stack.api_key()
        stack.wait_ready(self.ca)
        stack.fake_llm(reset=True)
        try:
            if want("baseline"):
                self.measured("health", "health", 64, note="liveness only: the framework/proxy ceiling")
                self.measured("ready", "ready", 8, note="readiness touches PostgreSQL, Qdrant and the (fake) model server")
            if want("rag"):
                self.measured("rag_ingest", "ingest", 8, note="TXT upload -> extraction subprocess -> chunk -> embed -> Qdrant upsert")
                self.results["qdrant_direct"] = stack.qdrant_probe()
                self.measured("rag_search", "search", 16, note="embed (fake, ~10 ms) + Qdrant query")
            if want("llm"):
                stack.fake_llm(reset=True, latency_ms=200, jitter_ms=50)
                self.measured("chat", "chat", 16, note="fake LLM 200 ms: intent call + profile-learning call per request")
                self.measured("agent", "agent", 16, note="single agent call (fake LLM 200 ms)")
                self.measured("team", "team", 8, note="three sequential agent calls + decision engine (fake LLM 200 ms)")
            if want("concurrency"):
                for c in (1, 4, 16, 64):
                    self.measured(f"chat_c{c}", "chat", c, note="concurrency sweep, fake LLM 200 ms")
            if want("mixed"):
                self.measured("mixed", "mixed", 24, note="20% health, 30% search, 30% chat, 10% ingest, 5% agent, 5% team")
            if want("slow_llm"):
                stack.fake_llm(reset=True, latency_ms=3000, jitter_ms=300)
                self.measured("slow_llm", "chat", 16, note="fake LLM 3 s per call")
                stack.fake_llm(reset=True)
            if want("llm_failures"):
                stack.fake_llm(reset=True, failure_rate=0.3, failure_status=500)
                self.measured("llm_failures_30pct", "chat", 16, note="30% of model calls return 500 (client retries once)")
                stack.fake_llm(reset=True)
            if want("llm_timeouts"):
                self.llm_timeouts()
            if want("thread_starvation"):
                self.thread_starvation()
            if want("qdrant"):
                self.qdrant_interruption()
            if want("extraction"):
                self.measured("extraction_docx", "ingest_docx", 16, note="400-paragraph DOCX; extraction slots 2/process x 2 workers, 5 s queue")
            if want("ratelimit"):
                self.restart_api(DEFAULT_LIMITS)
                self.measured("rate_limit_default", "ratelimit", 10,
                              note="shipped limits (120/min per identity and per address): 429s are the expected, correct outcome")
                self.restart_api(CAPACITY)
            if want("keepalive_ab"):
                self.keepalive_ab()
            if want("sustained"):
                self.sustained_and_recovery()
            self.results["fake_llm_stats"] = stack.fake_llm_stats()
        finally:
            stack.fake_llm(reset=True)
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(json.dumps(self.results, indent=1))
            self.log(f"results written to {self.out}")
        return self.results

    # ---- compound scenarios ----------------------------------------------------------------------------------------

    def llm_timeouts(self) -> None:
        stack.fake_llm(reset=True, hang_rate=1.0, hang_seconds=30)
        self.restart_api(CAPACITY, {"OLLAMA_TIMEOUT_SECONDS": "5"})
        step = self.measured("llm_timeouts", "chat", 8, duration=max(self.duration, 45), timeout=90,
                             note="every model call hangs 30 s, client timeout 5 s (+1 retry): requests should fail, not hang")
        stack.fake_llm(reset=True)
        self.restart_api(CAPACITY)
        step["note"] += " (api restarted afterwards with the default 10 s timeout)"

    def thread_starvation(self) -> None:
        """Slow model calls occupy worker threads; does a cheap liveness probe still answer quickly meanwhile?"""
        stack.fake_llm(reset=True, latency_ms=8000, jitter_ms=0)
        self.log("thread_starvation: 8 s model calls with 120 concurrent chats, probing /health/live")

        async def both():
            chat = asyncio.create_task(run_load("chat", stack.BASE_URL, self.key, 120, 40, ca_cert=self.ca, seed=self.seed, timeout=90))
            await asyncio.sleep(10)  # let the pool fill
            probe = await run_load("health", stack.BASE_URL, self.key, 2, 20, ca_cert=self.ca, seed=self.seed, timeout=30)
            return await chat, probe

        chat, probe = asyncio.run(both())
        stack.fake_llm(reset=True)
        self.results["steps"].append({"name": "thread_starvation", "note": "FastAPI runs sync handlers in a 40-thread pool per worker (2 workers = 80)."
                                      " 120 chats each holding a thread for ~16 s (two 8 s model calls) saturate it.",
                                      "chat": {k: chat[k] for k in ("requests", "rps", "success_rate", "latency_ms", "status_counts")},
                                      "liveness_probe_during_saturation": {k: probe[k] for k in ("requests", "success_rate", "latency_ms", "status_counts")}})
        self.log(f"  -> chat p95={chat['latency_ms']['p95']} ms; liveness during saturation p50={probe['latency_ms']['p50']} "
                 f"p95={probe['latency_ms']['p95']} max={probe['latency_ms']['max']} ok={probe['success_rate']}")
        time.sleep(15)

    def qdrant_interruption(self) -> None:
        self.log("qdrant_interruption: search + chat load; Qdrant stopped at t=15 s, started at t=35 s")
        marks: dict = {}

        def interrupt(origin_wait: float) -> None:
            time.sleep(15)
            marks["stopped_at"] = time.perf_counter()
            stack.compose("stop", "qdrant")
            time.sleep(20)
            stack.compose("start", "qdrant")
            marks["started_at"] = time.perf_counter()
            marks["ready_after_start_s"] = round(stack.wait_ready(self.ca), 1)

        async def go():
            loop = asyncio.get_running_loop()
            killer = loop.run_in_executor(None, interrupt, 0)
            s = asyncio.create_task(run_load("search", stack.BASE_URL, self.key, 8, 75, ca_cert=self.ca, seed=self.seed))
            c = asyncio.create_task(run_load("chat", stack.BASE_URL, self.key, 8, 75, ca_cert=self.ca, seed=self.seed))
            await killer
            return await s, await c

        before = stack.metric_snapshot(self.ca)
        search, chat = asyncio.run(go())
        time.sleep(12)
        delta = stack.metric_delta(before, stack.metric_snapshot(self.ca))
        for r in (search, chat):
            r["timeline"] = r["timeline"]
        self.results["steps"].append({
            "name": "qdrant_interruption", "note": "Qdrant stopped for 20 s under search+chat load; chat must keep answering without retrieval",
            "marks": {k: marks[k] for k in marks if k.endswith("_s")}, "search": search, "chat": chat, "server_side": delta})
        self.log(f"  -> ready {marks.get('ready_after_start_s')} s after start; search ok={search['success_rate']} chat ok={chat['success_rate']}")

    def keepalive_ab(self) -> None:
        """A/B/A/B: the pre-fix command (plain multi-worker uvicorn) against app.serve (TCP_NODELAY on the shared listener).
        Each variant gets a fresh API container, a warm-up, a loopback keep-alive microbenchmark and four proxied scenarios."""
        stack.fake_llm(reset=True, latency_ms=200, jitter_ms=50)
        results = []
        for round_ in (1, 2):
            for variant, legacy in (("legacy_uvicorn_cli", True), ("app_serve", False)):
                self.restart_api(CAPACITY, legacy=legacy)
                time.sleep(15)  # warm-up: imports, connection pools, first Qdrant/DB connections
                probe = stack.keepalive_probe()
                self.log(f"keepalive_ab round {round_} {variant}: loopback keep-alive {probe}")
                steps = {}
                for name, scenario, c in (("health", "health", 64), ("search", "search", 16), ("chat", "chat", 16), ("mixed", "mixed", 24)):
                    step = self.measured(f"ab{round_}_{variant}_{name}", scenario, c, warmup=10, light=True,
                                         note=f"A/B round {round_}, variant {variant}")
                    steps[name] = {"rps": step["rps"], "p50": step["latency_ms"]["p50"], "p95": step["latency_ms"]["p95"],
                                   "p99": step["latency_ms"]["p99"], "error_rate": step["error_rate"]}
                results.append({"round": round_, "variant": variant, "loopback_keepalive": probe, "scenarios": steps})
        self.results["keepalive_ab"] = results
        self.restart_api(CAPACITY)

    def sustained_and_recovery(self) -> None:
        phases = [("steady_before", 24, self.sustained), ("spike", 96, max(20.0, self.sustained / 3)), ("steady_after", 24, self.sustained)]
        self.log("sustained_and_recovery: " + ", ".join(f"{n} c={c} {d:.0f}s" for n, c, d in phases))
        summaries = []
        for name, concurrency, duration in phases:
            summaries.append(self.measured(f"sustained_{name}", "mixed", concurrency, duration=duration,
                                           note="recovery: latency/error rate after the spike should return to the 'before' level"))
        before, after = summaries[0]["latency_ms"]["p95"], summaries[2]["latency_ms"]["p95"]
        self.results["recovery"] = {"p95_before_ms": before, "p95_after_ms": after,
                                    "success_before": summaries[0]["success_rate"], "success_after": summaries[2]["success_rate"]}
