#!/usr/bin/env bash
# Staged evaluation of the real NexusRAG systems, one local model at a time (8 GB machines).
#
#   evals/run_nexus_staged.sh dev        # development split
#   evals/run_nexus_staged.sh heldout    # held-out split: run once, never tune against it
#
# Needs Ollama running with nomic-embed-text, qwen3:1.7b and qwen3:4b when the cache has gaps.
# With a complete cache it needs no model at all. Reports go to evals/reports/, and the peak
# Ollama memory observed during the run is written to evals/reports/<split>_ram.txt.
set -euo pipefail

SPLIT="${1:-dev}"
PY="${PYTHON:-python3}"
OUT="evals/reports"
mkdir -p "$OUT"
RSS="$OUT/.rss_$SPLIT"
: > "$RSS"

stop_models() {
  command -v ollama >/dev/null 2>&1 || return 0
  ollama ps 2>/dev/null | awk 'NR>1 {print $1}' | xargs -n1 ollama stop >/dev/null 2>&1 || true
}

( while true; do ps -axo rss=,command= | awk '/[o]llama/ {s+=$1} END {print s+0}' >> "$RSS"; sleep 2; done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

run() {  # run <system> <answerer>
  echo "== $SPLIT | $1 | $2"
  /usr/bin/time -l "$PY" -m evals.run --split "$SPLIT" --system "$1" --answerer "$2" \
      --output "$OUT/${SPLIT}_$1_$2.json" 2>"$OUT/.time_${SPLIT}_$1_$2" >/dev/null
  stop_models
}

stop_models
# Stage 1: retrieval only (extractive answerer). Loads nomic-embed-text, then qwen3:1.7b for rerank.
run nexus_dense extractive
run nexus_hybrid extractive
run nexus_rerank extractive
# Stage 2: grounded answers with the structured-output generator (loads qwen3:4b only).
run nexus_dense llm
run nexus_hybrid llm
run nexus_rerank llm

kill "$SAMPLER" 2>/dev/null || true
PEAK_KB=$(sort -n "$RSS" | tail -1)
PY_PEAK=$(grep -h "maximum resident set size" "$OUT"/.time_"${SPLIT}"_* | awk '{print $1}' | sort -n | tail -1)
{
  echo "split=$SPLIT"
  echo "peak_ollama_rss_mb=$((PEAK_KB / 1024))"
  echo "peak_python_rss_mb=$((PY_PEAK / 1048576))"
} | tee "$OUT/${SPLIT}_ram.txt"
