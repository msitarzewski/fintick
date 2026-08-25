#!/usr/bin/env bash
# FinTick v2 demo: one stream, aggregated events, validation-first board.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8137}"
DB="${DB:-data/fintick-demo.db}"

python3 -m fintick ingest \
  --fixture reference/nvda_repost_cluster.json \
  --database "$DB"

echo "Aggregating the six-hour stream window with local Qwen..."
python3 -m fintick aggregate --database "$DB" --limit 200

echo ""
echo "FinTick Edge Board: http://127.0.0.1:${PORT}/  (Ctrl-C to stop)"
echo ""
exec python3 -m fintick serve --database "$DB" --host 127.0.0.1 --port "$PORT"
