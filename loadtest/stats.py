"""Percentiles and summaries for load-test samples."""

import math


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0-100) of ``values``; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: list[tuple[float, float, int, str]], duration: float) -> dict:
    """``samples`` are ``(start_offset_s, latency_ms, status, error_type)``; status 0 means a transport error."""
    total = len(samples)
    latencies = [s[1] for s in samples]
    ok_latencies = [s[1] for s in samples if 200 <= s[2] < 300]
    statuses: dict[str, int] = {}
    for _, _, status, error in samples:
        key = str(status) if status else f"error:{error or 'unknown'}"
        statuses[key] = statuses.get(key, 0) + 1
    ok = sum(1 for s in samples if 200 <= s[2] < 300)
    round_ = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {
        "requests": total,
        "duration_seconds": round(duration, 2),
        "rps": round(total / duration, 2) if duration > 0 else 0.0,
        "success_rps": round(ok / duration, 2) if duration > 0 else 0.0,
        "ok": ok,
        "success_rate": round(ok / total, 4) if total else None,
        "error_rate": round(1 - ok / total, 4) if total else None,
        "http_429": statuses.get("429", 0),
        "http_503": statuses.get("503", 0),
        "http_5xx_other": sum(n for k, n in statuses.items() if k.isdigit() and int(k) >= 500 and k != "503"),
        "transport_errors": sum(n for k, n in statuses.items() if k.startswith("error:")),
        "status_counts": dict(sorted(statuses.items())),
        "latency_ms": {
            "min": round_(min(latencies)) if latencies else None,
            "p50": round_(percentile(latencies, 50)), "p95": round_(percentile(latencies, 95)),
            "p99": round_(percentile(latencies, 99)), "max": round_(max(latencies)) if latencies else None,
            "mean": round_(sum(latencies) / total) if total else None,
        },
        "ok_latency_ms": {"p50": round_(percentile(ok_latencies, 50)), "p95": round_(percentile(ok_latencies, 95)),
                          "p99": round_(percentile(ok_latencies, 99))},
    }


def timeline(samples: list[tuple[float, float, int, str]], bucket_seconds: float = 5.0) -> list[dict]:
    """Per-bucket request count, success rate and p95 latency, for spotting outages and recovery."""
    buckets: dict[int, list[tuple[float, float, int, str]]] = {}
    for sample in samples:
        buckets.setdefault(int(sample[0] // bucket_seconds), []).append(sample)
    out = []
    for index in sorted(buckets):
        group = buckets[index]
        ok = sum(1 for s in group if 200 <= s[2] < 300)
        out.append({"t": round(index * bucket_seconds, 1), "requests": len(group), "success_rate": round(ok / len(group), 3),
                    "p95_ms": round(percentile([s[1] for s in group], 95) or 0, 1)})
    return out
