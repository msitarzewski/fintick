#!/usr/bin/env bash
# FinTick offline demo: ingest the captured fixture and serve the dashboard.
# No external network needed. If the local Ollama model is reachable, the
# demo also enriches the fixture so the cards show AI analysis; otherwise it
# still works and shows headlines as they arrive (analysis pending). Stop with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8090}"
DB="data/fintick.db"

python3 -m fintick ingest --fixture reference/feed_sample.json --database "$DB"

if curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Local model detected — enriching fixture (best effort)..."
    # One bounded pass; failures stay retryable and never block the demo.
    python3 -m fintick enrich --database "$DB" --limit 60 > /dev/null 2>&1 || true
fi

echo ""
echo "FinTick demo live at:  http://127.0.0.1:${PORT}/  (Ctrl-C to stop)"
echo ""
exec python3 -m fintick serve --database "$DB" --port "$PORT"
