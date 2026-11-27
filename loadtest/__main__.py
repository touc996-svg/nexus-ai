"""``python -m loadtest run ...`` (one scenario against any URL) and ``python -m loadtest suite ...`` (local Compose stack)."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loadtest.runner import run_load
from loadtest.scenarios import SCENARIOS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loadtest", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one scenario against a base URL")
    run.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    run.add_argument("--base-url", default="https://localhost:8443")
    run.add_argument("--api-key-file", required=True, help="file holding the operator key (never pass keys on the command line)")
    run.add_argument("--ca-cert", help="CA certificate to trust (for example Caddy's local root); verification stays on")
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--duration", type=float, default=30.0)
    run.add_argument("--warmup", type=float, default=0.0)
    run.add_argument("--users", type=int, default=8)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--host-header", help="Host header to send (for example localhost when targeting a service directly)")
    run.add_argument("--out", type=Path)
    suite = sub.add_parser("suite", help="scripted suite on the local Compose stack (needs Docker; see docs/LOAD_TESTING.md)")
    suite.add_argument("--duration", type=float, default=30.0, help="seconds per step")
    suite.add_argument("--sustained", type=float, default=60.0, help="seconds per sustained/recovery phase")
    suite.add_argument("--out", type=Path, default=Path("docs/loadtest-results/latest.json"))
    suite.add_argument("--only", nargs="*", help="subset: baseline rag llm concurrency mixed slow_llm llm_failures llm_timeouts "
                                                 "thread_starvation qdrant extraction ratelimit keepalive_ab sustained")
    suite.add_argument("--keep-stack", action="store_true", help="leave the stack running afterwards")
    suite.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        key = Path(args.api_key_file).read_text().strip()
        result = asyncio.run(run_load(args.scenario, args.base_url, key, args.concurrency, args.duration, args.warmup, args.users,
                                      args.seed, args.ca_cert, args.timeout, args.host_header))
        text = json.dumps(result, indent=1)
        if args.out:
            args.out.write_text(text)
        print(json.dumps({k: v for k, v in result.items() if k != "timeline"}, indent=1))
        return 0

    import tempfile

    from loadtest import stack
    from loadtest.suite import Suite

    ca_path = Path(tempfile.mkdtemp(prefix="nexus-loadtest-")) / "ca.crt"
    try:
        Suite(args.duration, args.sustained, args.out, ca_path, args.seed).run(set(args.only) if args.only else None)
    finally:
        if not args.keep_stack:
            stack.down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
