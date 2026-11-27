"""Closed-loop asyncio load generator on httpx: ``concurrency`` virtual users each send requests back to back for
``duration`` seconds (after an optional warm-up whose samples are discarded). Deterministic given ``seed``."""

import asyncio
import random
import time
from collections.abc import Callable

import httpx

from loadtest.scenarios import SCENARIOS, Ctx
from loadtest.stats import summarize, timeline

Sample = tuple[float, float, int, str]


def make_client(base_url: str, api_key: str, ca_cert: str | None, timeout: float, concurrency: int,
                host_header: str | None = None) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10)
    return httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": api_key, **({"Host": host_header} if host_header else {})}, verify=ca_cert or True,
                             timeout=httpx.Timeout(timeout), limits=limits)


async def _worker(index: int, step: Callable, client: httpx.AsyncClient, users: int, seed: int, stop_at: float,
                  origin: float, samples: list[Sample], record_after: float) -> None:
    ctx = Ctx(client=client, user=f"lt-user-{index % users}", session=f"lt-s-{seed}-{index}", rng=random.Random(seed + index))
    while time.perf_counter() < stop_at:
        started = time.perf_counter()
        status, error = 0, ""
        try:
            response = await step(ctx)
            status = response.status_code
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        finished = time.perf_counter()
        if started >= record_after:
            samples.append((started - record_after, (finished - started) * 1000.0, status, error))


async def run_load(scenario: str, base_url: str, api_key: str, concurrency: int, duration: float, warmup: float = 0.0,
                   users: int = 8, seed: int = 1, ca_cert: str | None = None, timeout: float = 60.0,
                   host_header: str | None = None, on_start: Callable | None = None) -> dict:
    step = SCENARIOS[scenario]
    samples: list[Sample] = []
    async with make_client(base_url, api_key, ca_cert, timeout, concurrency, host_header) as client:
        origin = time.perf_counter()
        record_after = origin + warmup
        stop_at = record_after + duration
        if on_start:
            on_start(record_after)
        workers = [asyncio.create_task(_worker(i, step, client, users, seed, stop_at, origin, samples, record_after))
                   for i in range(concurrency)]
        await asyncio.gather(*workers)
    result = summarize(samples, duration)
    result.update(scenario=scenario, concurrency=concurrency, users=users, warmup_seconds=warmup, seed=seed,
                  timeline=timeline(samples))
    return result
