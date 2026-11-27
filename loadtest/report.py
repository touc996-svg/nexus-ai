"""Turn a suite result JSON into Markdown tables: ``python -m loadtest.report docs/loadtest-results/<run>.json``."""

import json
import sys
from pathlib import Path


def fmt(value, suffix=""):
    return "n/a" if value is None else f"{value}{suffix}"


def step_row(s: dict) -> str:
    lat = s["latency_ms"]
    return (f"| {s['name']} | {s['scenario']} | {s['concurrency']} | {s['rps']} | {fmt(lat['p50'])} | {fmt(lat['p95'])} | "
            f"{fmt(lat['p99'])} | {round((s['error_rate'] or 0) * 100, 1)}% | {s['http_429']} | {s['http_503']} | "
            f"{s['http_5xx_other']} | {s['transport_errors']} |")


def resources(s: dict) -> str:
    r = s.get("resources", {})
    return ", ".join(f"{svc} {v['cpu_percent_avg']}/{v['cpu_percent_max']}% cpu, {v['mem_mb_max']} MB" for svc, v in r.items())


def render(data: dict) -> str:
    env = data.get("environment", {})
    steps = data["steps"]
    lines = ["## Environment", "", f"- Host: {env.get('host')}; Docker VM: {env.get('docker')}", f"- Started: {env.get('started')}; "
             f"{env.get('duration_per_step')} s per step; seed {env.get('seed')}", "", "## Results", "",
             "| step | scenario | conc. | req/s | p50 ms | p95 ms | p99 ms | errors | 429 | 503 | other 5xx | transport |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    lines += [step_row(s) for s in steps if "scenario" in s]
    lines += ["", "### Resource use per step (avg/max CPU %, max memory)", ""]
    lines += [f"- **{s['name']}**: {resources(s)}" for s in steps if s.get("resources")]
    lines += ["", "### PostgreSQL per step", ""]
    for s in steps:
        pg = s.get("postgres")
        if pg:
            lines.append(f"- **{s['name']}**: {pg['commits']} commits, {pg['tuples_inserted']} inserts, {pg['tuples_updated']} updates, "
                         f"cache hit {fmt(pg['cache_hit_ratio'])}, connections at end {pg['connections_at_end']}")
    lines += ["", "### Server-side latency per step (application histograms; p95 is a bucket upper bound)", ""]
    for s in steps:
        ss = s.get("server_side", {})
        parts = [f"{k.replace('_duration_ms', '')}: n={v['count']}, mean {v['mean_ms']} ms, p95<={v['p95_ms_bucket_upper_bound']} ms"
                 for k, v in ss.items() if k != "counters" and k in ("llm_duration_ms", "rag_duration_ms:search", "rag_duration_ms:ingest")]
        if parts:
            lines.append(f"- **{s['name']}**: " + "; ".join(parts))
    if "qdrant_direct" in data:
        lines += ["", "### Qdrant direct (from inside the API container, 40 calls each)", "", "```json", json.dumps(data["qdrant_direct"], indent=1), "```"]
    for s in steps:
        if s["name"] in {"qdrant_interruption", "thread_starvation"}:
            lines += ["", f"### {s['name']}", "", s.get("note", ""), "", "```json",
                      json.dumps({k: v for k, v in s.items() if k not in {"name", "note"}} if s["name"] == "thread_starvation" else
                                 {"marks": s["marks"], "server_side": s["server_side"],
                                  "search_status": s["search"]["status_counts"], "chat_status": s["chat"]["status_counts"],
                                  "search_timeline": s["search"]["timeline"], "chat_timeline": s["chat"]["timeline"]}, indent=1), "```"]
    if "recovery" in data:
        lines += ["", "### Recovery after a traffic spike", "", "```json", json.dumps(data["recovery"], indent=1), "```"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render(json.loads(Path(sys.argv[1]).read_text())))
