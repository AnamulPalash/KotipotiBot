#!/usr/bin/env bash
# =============================================================================
# export_trades.sh — KotipotiBot Daily Trade Export
# =============================================================================
# Exports structured trade data from the running Freqtrade container.
# Output goes to user_data/exports/ so Hermes can read it.
#
# Exports:
#   1. Closed trades (JSON)
#   2. Open trades (JSON)
#   3. Per-pair summary (JSON)
#   4. Exit reason summary (JSON)
#   5. Error summary (from log)
#   6. Config snapshot hash
#   7. Strategy file hash
#
# Usage (run from project root on the Pi, ideally via cron at 23:50 UTC):
#   bash scripts/export_trades.sh
#
# Cron example (run at 23:50 UTC every day):
#   50 23 * * * cd /home/anam/KotipotiBot && bash scripts/export_trades.sh
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPORT_DIR="$PROJECT_ROOT/user_data/exports"
CONTAINER="kotipotibot_freqtrade"
TIMESTAMP=$(date -u +"%Y-%m-%d_%H%M")
DATE_STR=$(date -u +"%Y-%m-%d")
CONFIG_FILE="$PROJECT_ROOT/config.json"
STRATEGY_FILE="$PROJECT_ROOT/strategies/ShortScalper.py"
LOG_FILE="$PROJECT_ROOT/user_data/logs/freqtrade.log"

echo "=============================="
echo " KotipotiBot — Trade Exporter"
echo " $(date -u) UTC"
echo "=============================="

mkdir -p "$EXPORT_DIR"

# ---- Check container is running ----
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "ERROR: Container '${CONTAINER}' is not running."
    exit 1
fi

# ---- 1. Closed trades ----
echo "[1/7] Exporting closed trades..."
docker exec "$CONTAINER" freqtrade list-trades \
    --config /freqtrade/user_data/config.json \
    --print-json \
    --no-header \
    2>/dev/null \
    > "$EXPORT_DIR/trades_closed_${DATE_STR}.json" \
    || echo '[]' > "$EXPORT_DIR/trades_closed_${DATE_STR}.json"
echo "  → trades_closed_${DATE_STR}.json"

# ---- 2. Open trades ----
echo "[2/7] Exporting open trades..."
docker exec "$CONTAINER" freqtrade list-trades \
    --config /freqtrade/user_data/config.json \
    --print-json \
    --no-header \
    --open \
    2>/dev/null \
    > "$EXPORT_DIR/trades_open_${DATE_STR}.json" \
    || echo '[]' > "$EXPORT_DIR/trades_open_${DATE_STR}.json"
echo "  → trades_open_${DATE_STR}.json"

# ---- 3. Per-pair summary ----
echo "[3/7] Building per-pair summary..."
python3 - << 'PYEOF' "$EXPORT_DIR/trades_closed_${DATE_STR}.json" "$EXPORT_DIR/pair_summary_${DATE_STR}.json"
import json, sys
from collections import defaultdict

infile, outfile = sys.argv[1], sys.argv[2]
try:
    trades = json.loads(open(infile).read()) or []
except Exception:
    trades = []

pairs = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "exit_reasons": {}})
for t in trades:
    pair = t.get("pair", "unknown").split("/")[0]
    profit = t.get("profit_ratio", 0) or 0
    profit_abs = t.get("profit_abs", 0) or 0
    pairs[pair]["trades"] += 1
    pairs[pair]["pnl"] = round(pairs[pair]["pnl"] + profit_abs, 6)
    if profit >= 0:
        pairs[pair]["wins"] += 1
    else:
        pairs[pair]["losses"] += 1
    reason = t.get("exit_reason", "unknown")
    pairs[pair]["exit_reasons"][reason] = pairs[pair]["exit_reasons"].get(reason, 0) + 1

for p in pairs.values():
    t = p["trades"]
    p["win_rate"] = round(p["wins"] / t * 100, 1) if t > 0 else 0

json.dump(dict(pairs), open(outfile, "w"), indent=2)
print(f"  Pairs: {list(pairs.keys())}")
PYEOF
echo "  → pair_summary_${DATE_STR}.json"

# ---- 4. Exit reason summary ----
echo "[4/7] Building exit reason summary..."
python3 - << 'PYEOF' "$EXPORT_DIR/trades_closed_${DATE_STR}.json" "$EXPORT_DIR/exit_summary_${DATE_STR}.json"
import json, sys
from collections import defaultdict

infile, outfile = sys.argv[1], sys.argv[2]
try:
    trades = json.loads(open(infile).read()) or []
except Exception:
    trades = []

reasons = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
for t in trades:
    reason = t.get("exit_reason", "unknown")
    profit = t.get("profit_ratio", 0) or 0
    profit_abs = t.get("profit_abs", 0) or 0
    reasons[reason]["count"] += 1
    reasons[reason]["pnl"] = round(reasons[reason]["pnl"] + profit_abs, 6)
    if profit >= 0:
        reasons[reason]["wins"] += 1

json.dump(dict(reasons), open(outfile, "w"), indent=2)
PYEOF
echo "  → exit_summary_${DATE_STR}.json"

# ---- 5. Error summary ----
echo "[5/7] Extracting error summary from logs..."
python3 - << 'PYEOF' "$LOG_FILE" "$EXPORT_DIR/error_summary_${DATE_STR}.json" "$DATE_STR"
import json, sys, re
from datetime import datetime

logfile, outfile, date_str = sys.argv[1], sys.argv[2], sys.argv[3]
result = {"date": date_str, "errors": [], "warnings": [], "stale_count": 0, "api_errors": 0, "restarts": 0}

try:
    with open(logfile) as f:
        for line in f:
            if date_str not in line[:10]:
                continue
            lower = line.lower()
            if " - error" in lower or "fatal" in lower:
                result["errors"].append(line.strip()[:200])
            if " - warning" in lower:
                result["warnings"].append(line.strip()[:150])
            if "stale" in lower or "old candle" in lower:
                result["stale_count"] += 1
            if "api" in lower and ("error" in lower or "failed" in lower):
                result["api_errors"] += 1
            if "starting worker" in lower:
                result["restarts"] += 1
    # Cap lists
    result["errors"] = result["errors"][-10:]
    result["warnings"] = result["warnings"][-10:]
except Exception as e:
    result["parse_error"] = str(e)

json.dump(result, open(outfile, "w"), indent=2)
PYEOF
echo "  → error_summary_${DATE_STR}.json"

# ---- 6. Config snapshot hash ----
echo "[6/7] Hashing config snapshot..."
if [ -f "$CONFIG_FILE" ]; then
    CONFIG_HASH=$(sha256sum "$CONFIG_FILE" | awk '{print $1}')
    echo "{\"date\": \"$DATE_STR\", \"config_sha256\": \"$CONFIG_HASH\", \"file\": \"config.json\"}" \
        > "$EXPORT_DIR/config_hash_${DATE_STR}.json"
    echo "  → config_hash_${DATE_STR}.json (sha256: ${CONFIG_HASH:0:16}...)"
else
    echo "  WARNING: config.json not found"
fi

# ---- 7. Strategy file hash ----
echo "[7/7] Hashing strategy file..."
if [ -f "$STRATEGY_FILE" ]; then
    STRATEGY_HASH=$(sha256sum "$STRATEGY_FILE" | awk '{print $1}')
    echo "{\"date\": \"$DATE_STR\", \"strategy_sha256\": \"$STRATEGY_HASH\", \"file\": \"ShortScalper.py\"}" \
        > "$EXPORT_DIR/strategy_hash_${DATE_STR}.json"
    echo "  → strategy_hash_${DATE_STR}.json (sha256: ${STRATEGY_HASH:0:16}...)"
else
    echo "  WARNING: ShortScalper.py not found"
fi

# ---- Cleanup old exports (keep last 30 days) ----
echo ""
echo "Cleaning up exports older than 30 days..."
find "$EXPORT_DIR" -name "*.json" -mtime +30 -delete 2>/dev/null || true

echo ""
echo "✅ Export complete: $EXPORT_DIR"
ls -lh "$EXPORT_DIR/"*"${DATE_STR}"* 2>/dev/null || true
