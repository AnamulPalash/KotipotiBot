# 🤖 Kotipoti Trading Bot

A Freqtrade-based crypto **short scalping bot** running in futures dry-run mode on Bybit, deployed on a Raspberry Pi via Docker.

**Hermes** is a read-only nightly analysis agent that sends a daily and weekly Telegram digest.

---

## Architecture

```
Bybit public market data (no API keys in dry-run)
        │
        ▼
┌─────────────────────────────────────────┐
│  kotipotibot_freqtrade (Docker)         │
│  Strategy: ShortScalper v2              │
│  Pairs: BTC, ETH, SOL, DOGE, XRP only  │
│  Timeframe: 5m │ Leverage: 5x           │
│  Mode: dry_run │ Wallet: $2,000 sim     │
│                                         │
│  Writes → user_data/logs/               │
│  Writes → tradesv3.dryrun.sqlite        │
│  Writes → user_data/exports/            │
│  Serves Web UI on :8090                 │
│  Sends live Telegram alerts             │
└─────────────┬───────────────────────────┘
              │ shared Docker volume (read-only for Hermes)
              ▼
┌─────────────────────────────────────────┐
│  kotipotibot_hermes (Docker)            │
│  Read-only analyst — no trading control │
│                                         │
│  Reads ← SQLite DB (primary)            │
│  Reads ← exports/ (fallback)            │
│  Reads ← logs (errors only)             │
│  Writes → hermes/reports/               │
│  Sends daily digest 23:55 UTC           │
│  Sends weekly report Sunday 22:00 UTC   │
└─────────────────────────────────────────┘
```

| Setting | Value |
|---|---|
| Exchange | Bybit (futures, public data) |
| Mode | Dry run — no real money |
| Wallet | $2,000 simulated USDT |
| Pairs | BTC, ETH, SOL, DOGE, XRP only |
| Strategy | ShortScalper v2 |
| Timeframe | 5m (+ 1h, 4h informative) |
| Leverage | 5x |
| Max open trades | 3 |
| Stake per trade | $200 |
| Web UI | http://192.168.20.146:8090 |

---

## Project Structure

```
KotipotiBot/
├── strategies/
│   └── ShortScalper.py       # Short-only strategy with full filters
├── hermes/
│   ├── hermes_agent.py       # Daily + weekly Telegram analyst
│   ├── hermes_context.json   # Agent identity file
│   └── reports/              # Hermes report archive (auto-created)
├── scripts/
│   ├── export_trades.sh      # Daily trade data export (run at 23:50 UTC)
│   ├── setup_cron.sh         # Install cron job on Pi
│   ├── backtest.sh           # Run all required backtests
├── user_data/
│   ├── logs/                 # Freqtrade + regime logs (git-ignored)
│   ├── data/                 # Candle data (git-ignored)
│   └── exports/              # Daily trade exports for Hermes (git-ignored)
├── config.json               # ← REAL config, NOT in git
├── config.json.example       # Template committed to git
├── .env                      # ← REAL secrets, NOT in git
├── .env.example              # Template committed to git
├── docker-compose.yml        # Freqtrade + Hermes services
└── .gitignore
```

---

## Strategy: ShortScalper v2

**Direction:** Short only. No longs.

**Entry conditions (all must pass):**
- Price closes above Bollinger Band upper (20,2)
- RSI 14 > 68
- EMA 8 < EMA 21 (bearish alignment)
- Volume > 20-period average × 1.2

**1h trend filter:** Block short if pair's own 1h is strongly bullish (price above EMA21 AND RSI > 60 AND higher highs). Override allowed if RSI 1h > 80 AND price > 2σ above BB upper.

**BTC regime filter (alts only):** Block ETH/SOL/DOGE/XRP shorts if BTC 1h is strongly bullish. Same overextension override applies.

**Exit conditions:**
- Price reverts below BB midline AND RSI < 45
- ROI target, trailing stop, or hard stoploss

**Safety controls:**
- 10% daily wallet loss → halt new entries until next UTC day
- 4 consecutive losses → halt for 2 hours
- Per-pair cooldown: 3 candles (15m) after a losing trade
- Max 6 trades per pair per day
- Halt if >5 API errors in one hour
- Halt if candle data is stale beyond 2 candles
- Halt if DB write fails

**Protections:** StoplossGuard, MaxDrawdown, CooldownPeriod, LowProfitPairs

---

## One-Time Pi Setup

```bash
# SSH in
ssh anam@192.168.20.146

# Clone
git clone https://github.com/AnamulPalash/KotipotiBot.git
cd KotipotiBot

# Create real config
cp config.json.example config.json
nano config.json
# Fill in: jwt_secret_key, ws_token, api_server.password

# Create .env with Telegram credentials
cat > .env << 'EOF'
TELEGRAM_TOKEN=8799522287:AAEGT3-xnc7PdIEv3vBcTc3p6_qubVZfkJQ
TELEGRAM_CHAT_ID=8007399291
EOF

# Create required directories
mkdir -p user_data/logs user_data/data user_data/exports hermes/reports

# Fix permissions for Freqtrade container (runs as uid 1000)
sudo chown -R 1000:1000 user_data/
sudo chmod -R 755 user_data/

# Make scripts executable
chmod +x scripts/*.sh

# Install daily export cron job (runs at 23:50 UTC)
bash scripts/setup_cron.sh

# Start the bot
docker compose up -d
docker compose logs -f freqtrade
```

---

## Ongoing Workflow (Mac → Pi)

```bash
# On Mac — commit and push
git add .
git commit -m "describe change"
git push

# One-liner: push + pull on Pi + restart
git push && ssh anam@192.168.20.146 "cd KotipotiBot && git pull && \
  sudo chown -R 1000:1000 user_data/ && docker compose restart"
```

---

## Verification Checklist

### ✅ Dry-run only (no real trades)
```bash
docker exec kotipotibot_freqtrade grep -i "dry_run" /freqtrade/user_data/logs/freqtrade.log | head -5
# Expect: "Dry run is enabled" and "All trades are simulated"
```

### ✅ Only 5 pairs active
```bash
docker exec kotipotibot_freqtrade freqtrade list-trades \
  --config /freqtrade/user_data/config.json --print-json 2>/dev/null | \
  python3 -c "import json,sys; [print(t['pair']) for t in json.load(sys.stdin)]" | sort -u
# Expect: only BTC/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT, DOGE/USDT:USDT, XRP/USDT:USDT
```

### ✅ Hermes is read-only (no exchange keys)
```bash
docker inspect kotipotibot_hermes | grep -A5 "Env"
# Expect: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID only — no exchange keys
docker inspect kotipotibot_hermes | grep Mounts -A40 | grep "ro"
# Expect: user_data, logs, exports all mounted :ro
```

### ✅ Daily and weekly reports
```bash
# Trigger a test daily report manually
docker exec kotipotibot_hermes python3 -c "
from hermes_agent import run_daily_report
run_daily_report()
"
# Check reports folder
ls hermes/reports/
```

### ✅ Telegram alerts work
```bash
# Check Freqtrade Telegram is connected
docker compose logs freqtrade | grep -i "telegram"
# Expect: "rpc.telegram is listening for following commands"
```

### ✅ Web UI on port 8090
```bash
curl -s http://192.168.20.146:8090/api/v1/ping | python3 -m json.tool
# Expect: {"status": "pong"}
```

### ✅ Both containers restart after Pi reboot
```bash
sudo reboot
# After reboot — wait ~60s then:
ssh anam@192.168.20.146 "docker ps --format 'table {{.Names}}\t{{.Status}}'"
# Expect: both kotipotibot_freqtrade and kotipotibot_hermes show "Up X seconds"
```

### ✅ Regime log is writing
```bash
tail -5 user_data/logs/regime_log.jsonl
# Expect: JSON lines with pair, timestamp, session, btc_1h_trend, etc.
```

---

## Backtesting

Download data and run all required date ranges:

```bash
bash scripts/backtest.sh
```

This covers:
- Last 30 days
- Last 90 days
- Strong BTC uptrend: 2024-10-01 → 2024-12-15
- Strong BTC downtrend: 2024-05-01 → 2024-08-01
- Sideways/choppy: 2024-08-01 → 2024-09-30
- High-volatility dump: 2024-04-01 → 2024-05-01

View results:
```bash
docker exec kotipotibot_freqtrade freqtrade backtesting-show
```

---

## Hermes Reports

**Daily (23:55 UTC):** Wallet balance, net P&L, fees, trade count, win rate, profit factor, best/worst pair and trade, exit reason breakdown, error count, suggested follow-up.

**Weekly (Sunday 22:00 UTC):** Pair ranking, session analysis, regime analysis, skipped signal review, loss patterns, overtrading check, structured recommendations.

**Recommendation format:**
- Recommendation
- Evidence
- Expected benefit
- Risk
- Test method
- Status: `Review required` / `Paper-test only` / `Reject`

Hermes will never say "Apply this live", "Increase leverage", or "Remove stoploss".

---

## Daily Export

Export runs automatically via cron at 23:50 UTC (before Hermes report):
```bash
bash scripts/export_trades.sh
```
Outputs to `user_data/exports/`:
- `trades_closed_YYYY-MM-DD.json`
- `trades_open_YYYY-MM-DD.json`
- `pair_summary_YYYY-MM-DD.json`
- `exit_summary_YYYY-MM-DD.json`
- `error_summary_YYYY-MM-DD.json`
- `config_hash_YYYY-MM-DD.json`
- `strategy_hash_YYYY-MM-DD.json`

---

## Stopping / Restarting

```bash
docker compose stop          # graceful stop (data preserved)
docker compose down          # remove containers (data preserved)
docker compose up -d         # start fresh
docker compose restart       # restart both containers
docker compose restart freqtrade  # restart only freqtrade
```

---

## Security Notes

- No Bybit API keys in dry-run mode — public market data only
- Hermes has no exchange keys and no write access to Freqtrade config
- `.env` and `config.json` are git-ignored — never commit real credentials
- Web UI is LAN-only — do not port-forward port 8090 to the internet
- Secrets are never printed in Hermes reports or logs

---

## License

Private project — not for redistribution.
