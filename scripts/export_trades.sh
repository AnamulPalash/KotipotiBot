#!/usr/bin/env bash
# =============================================================================
# export_trades.sh — KotipotiBot Trade Data Export Script
# =============================================================================
# Exports Freqtrade trade data, logs, and performance summary from the
# running Docker container to a timestamped local archive.
#
# Usage (run from project root on the Pi):
#   bash scripts/export_trades.sh
#
# Output: ./exports/trades_YYYY-MM-DD_HH-MM.tar.gz
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPORT_DIR="$PROJECT_ROOT/exports"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
ARCHIVE_NAME="trades_${TIMESTAMP}.tar.gz"
CONTAINER="kotipotibot_freqtrade"

echo "=============================="
echo " KotipotiBot — Trade Exporter"
echo " $(date)"
echo "=============================="

# ---- Create export directory ----
mkdir -p "$EXPORT_DIR"

# ---- Check container is running ----
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "ERROR: Container '${CONTAINER}' is not running."
  echo "Start it with: docker compose up -d"
  exit 1
fi

# ---- Export trade history via freqtrade CLI ----
echo "[1/4] Exporting trade list from freqtrade..."
docker exec "$CONTAINER" freqtrade list-trades \
  --config /freqtrade/user_data/config.json \
  --print-json 2>/dev/null \
  > "$EXPORT_DIR/trades_${TIMESTAMP}.json" || echo "  (No trades yet — skipping JSON export)"

# ---- Copy logs ----
echo "[2/4] Copying logs..."
cp -r "$PROJECT_ROOT/user_data/logs" "$EXPORT_DIR/logs_${TIMESTAMP}" 2>/dev/null || true

# ---- Copy data directory ----
echo "[3/4] Copying candle data..."
cp -r "$PROJECT_ROOT/user_data/data" "$EXPORT_DIR/data_${TIMESTAMP}" 2>/dev/null || true

# ---- Create archive ----
echo "[4/4] Creating archive: $ARCHIVE_NAME"
cd "$EXPORT_DIR"
tar -czf "$ARCHIVE_NAME" \
  "trades_${TIMESTAMP}.json" \
  "logs_${TIMESTAMP}/" \
  "data_${TIMESTAMP}/" \
  2>/dev/null || true

# ---- Cleanup raw export dirs ----
rm -rf "trades_${TIMESTAMP}.json" "logs_${TIMESTAMP}/" "data_${TIMESTAMP}/"

echo ""
echo "✅ Export complete: $EXPORT_DIR/$ARCHIVE_NAME"
echo ""

# ---- Summary stats (if trades exist) ----
if docker exec "$CONTAINER" freqtrade show-trades \
    --config /freqtrade/user_data/config.json 2>/dev/null | \
    grep -q "Total"; then
  echo "---- Trade Summary ----"
  docker exec "$CONTAINER" freqtrade show-trades \
    --config /freqtrade/user_data/config.json 2>/dev/null || true
fi
