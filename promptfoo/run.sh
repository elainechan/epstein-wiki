#!/usr/bin/env bash
# Run all Promptfoo prompt regression suites.
# Usage: ./promptfoo/run.sh [--suite qa|entity|routing]
set -euo pipefail
cd "$(dirname "$0")"

SUITE="${1:-all}"

run_suite() {
  local name="$1" file="$2"
  echo ""
  echo "══ Suite: $name ══════════════════════════════════════"
  promptfoo eval --config "$file" --no-cache 2>&1
}

case "$SUITE" in
  --suite)
    case "${2:-}" in
      qa)       run_suite "Q&A Grounding"       suite-qa.yaml ;;
      entity)   run_suite "Entity Extraction"   suite-entity.yaml ;;
      routing)  run_suite "Query Routing"        suite-routing.yaml ;;
      *) echo "Unknown suite. Use: qa | entity | routing"; exit 1 ;;
    esac
    ;;
  *)
    run_suite "Q&A Grounding"     suite-qa.yaml
    run_suite "Entity Extraction" suite-entity.yaml
    run_suite "Query Routing"     suite-routing.yaml
    echo ""
    echo "All suites complete."
    ;;
esac
