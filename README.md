# 🤖 KotipotiBot

A Freqtrade-based crypto **short scalping bot** running in futures dry-run mode on Bybit, deployed on a Raspberry Pi via Docker.

Includes **Hermes**, a nightly analysis agent that reads trade logs and sends a Telegram summary every evening.

---

## Architecture

```
Mac (dev)  ──git push──►  GitHub  ──git pull──►  Raspberry Pi (prod)
                                                      │
                                              Docker Compose
                                            ┌──────┴──────────┐
                                            │  freqtrade      │  port 8090
                                            │  (ShortScalper) │
                                            └────────┬────────┘
                                                     │
                                            ┌────────┴────────┐
                                            │  Hermes Agent   │  nightly
                                            │  (Telegram rpt) │
                                            └─────────────────┘
```

| Setting | Value |
|---|---|
| Exchange | Bybit (futures) |
| Mode | Dry run (paper trading) |
| Wallet | $2,000 simulated USDT |
| Strategy | ShortScalper |
| Timeframe | 5m |
| Leverage | 5x |
| Web UI | http://\<pi-ip\>:8090 |

---

## Project Structure

```
KotipotiBot/
├── strategies/
│   └── ShortScalper.py       # Short-only scalping strategy
├── hermes/
│   ├── hermes_agent.py       # Nightly analysis + Telegram reports
│   └── hermes_context.json   # Agent identity/context file
├── scripts/
│   └── export_trades.sh      # Export trade data from container
├── user_data/
│   ├── logs/                 # Freqtrade logs (git-ignored)
│   └── data/                 # Candle data (git-ignored)
├── config.json               # ← REAL config, NOT in git (create on Pi)
├── config.json.example       # Template committed to git
├── .env                      # ← REAL env vars, NOT in git (create on Pi)
├── .env.example              # Template committed to git
├── docker-compose.yml        # Freqtrade + Hermes services
└── .gitignore
```

---

## One-Time Pi Setup

SSH into the Pi and run these commands once:

```bash
# 1. Clone the repo
git clone https://github.com/AnamulPalash/KotipotiBot.git
cd KotipotiBot

# 2. Create real config from example
cp config.json.example config.json
nano config.json
# → Fill in: Bybit API key/secret, UI password, JWT secret
# → Telegram token and chat_id are already set via .env (see step 3)

# 3. Create .env with your Telegram credentials
cat > .env << 'EOF'
TELEGRAM_TOKEN=8799522287:AAEGT3-xnc7PdIEv3vBcTc3p6_qubVZfkJQ
TELEGRAM_CHAT_ID=8007399291
EOF

# 4. Make export script executable
chmod +x scripts/export_trades.sh

# 5. Start the bot
docker compose up -d

# 6. View logs
docker compose logs -f freqtrade
```

The **Freqtrade Web UI** will be available at:
```
http://192.146.20.146:8090
```

---

## config.json Key Fields to Fill In

| Field | Where to get it |
|---|---|
| `exchange.key` | Bybit → API Management → Create API key |
| `exchange.secret` | Same as above |
| `telegram.token` | @BotFather on Telegram |
| `telegram.chat_id` | @userinfobot on Telegram |
| `api_server.jwt_secret_key` | Any random 32+ char string |
| `api_server.ws_token` | Any random string |
| `api_server.password` | Your chosen UI login password |

> **Note:** `dry_run: true` means no real money is used. The bot simulates trades against a $2,000 virtual wallet.

---

## Ongoing Workflow (Mac → Pi)

After making any changes on your Mac:

```bash
# On Mac — commit and push
git add .
git commit -m "describe your change"
git push

# Then SSH into the Pi and pull + restart
ssh pi@192.146.20.146
cd KotipotiBot
git pull
docker compose restart
```

Or use the one-liner from Mac:

```bash
git push && ssh pi@192.146.20.146 "cd KotipotiBot && git pull && docker compose restart"
```

---

## Hermes — Nightly Reports

Hermes runs as a sidecar container and sends a Telegram report every night at **23:55 UTC** covering:

- Number of entries and exits
- Win / loss count and win rate
- Estimated total P&L
- Any errors detected in logs

To view Hermes logs:
```bash
docker compose logs -f hermes
```

---

## Export Trade Data

To export trade history and logs to a local archive:

```bash
# On the Pi, from project root
bash scripts/export_trades.sh
# → Creates exports/trades_YYYY-MM-DD_HH-MM.tar.gz
```

---

## Stopping the Bot

```bash
# Graceful stop
docker compose stop

# Full teardown (removes containers, keeps data)
docker compose down
```

---

## Strategy: ShortScalper

Short-only momentum reversal strategy.

**Entry (short):** Price closes above Bollinger Band upper band + RSI > 68 + bearish EMA alignment + above-average volume.

**Exit (short):** Price reverts to BB mid-band + RSI < 45.

**Risk management:** 2.5% hard stop loss, trailing stop activates at 1% profit.

---

## License

Private project — not for redistribution.
