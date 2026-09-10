#!/usr/bin/env bash
# Weekly drift check for the form registry.
#
# Two halves, because they need different things:
#   1. browser-free — live catalog + recently ingested answers. Also runs as a
#                     Cloud Run job on its own (see deploy/README.md).
#   2. wizard walk  — needs Playwright, so it runs here rather than in the
#                     deployed web image.
#
# Both are strictly read-only. The crawler asserts it never presses Submit, and
# nothing in this script files a request.
#
# Usage: scripts/weekly_drift.sh [--no-crawl]
# Exits non-zero when anything drifted, so cron or CI can alert.
set -uo pipefail

cd "$(dirname "$0")/.."
WORK="${TMPDIR:-/tmp}/alex311-drift"
mkdir -p "$WORK"
status=0

echo "== 1/2 catalog + submitted answers =="
# shellcheck disable=SC2086
uv run python -m alex311.registry_drift ${DRIFT_ARGS:-} || status=1

if [[ "${1:-}" == "--no-crawl" ]]; then
    echo "== 2/2 wizard walk skipped (--no-crawl) =="
    exit $status
fi

echo "== 2/2 wizard walk: 111 services, sequential and polite, takes about an hour =="
cp docs/data/wizard-rules.json "$WORK/baseline.json"
rm -f "$WORK/fresh.json"        # a leftover file would be resumed, not re-walked
ALEX311_RULES_OUT="$WORK/fresh.json" uv run python spike/wizard_rules.py all "$WORK" || status=1

uv run python spike/rules_diff.py "$WORK/baseline.json" "$WORK/fresh.json" || status=1

if [[ $status -ne 0 ]]; then
    cat <<EOF

Drift found. To adopt it:
  cp $WORK/fresh.json docs/data/wizard-rules.json
  uv run python spike/build_registry.py
  uv run python spike/wizard_rules_report.py
  uv run pytest -q
then commit the regenerated data.
EOF
fi
exit $status
