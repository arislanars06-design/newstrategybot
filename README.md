# newstrategybot

Binance Futures **grid-block** trading bot driven by a Telegram interface.

## Strategy

Each trade is a **block** of 8 chained limit orders on the same symbol.

- Every order has its own entry, take-profit (TP), and stop-loss (SL).
- Order N's SL price equals order N+1's entry price, so at most one
  position is open at a time (a rolling ladder).
- Each block has a single **Cancel Price**. If the market reaches it
  *before any order triggers*, every pending order is cancelled and the
  block is marked **INVALID**.

### Block lifecycle

| Status   | When                                                                  |
|----------|-----------------------------------------------------------------------|
| ACTIVE   | Block created, orders placed, watching the market.                    |
| WIN      | Any one order reached its TP. Remaining pending orders are cancelled. |
| LOSS     | All 8 orders ended at SL (no TP).                                     |
| INVALID  | Cancel Price hit before any order was triggered.                      |

Open-position policy after a TP win: **Variant A** — already-open
positions keep running on their own TP/SL; only *pending* orders are
cancelled. With the chain layout, in practice this means the winning
position is the only open one.

## Tech stack

- Python 3.11+
- [`python-binance`](https://github.com/sammchardy/python-binance) for REST + WebSocket
- [`aiogram`](https://github.com/aiogram/aiogram) v3 for Telegram
- SQLAlchemy 2.x (async) + SQLite (or PostgreSQL on a VPS)
- `pydantic-settings` for typed configuration
- `loguru` for logging

## Project layout

```
newstrategybot/
├── src/
│   ├── bot/          # Telegram interface (handlers, FSM, keyboards)
│   ├── core/         # Block engine, state machine, rules
│   ├── exchange/     # Binance REST + WebSocket adapters
│   ├── db/           # SQLAlchemy models, repositories, migrations
│   ├── config.py     # pydantic-settings
│   ├── logging_setup.py
│   └── main.py       # application entrypoint
├── tests/
├── pyproject.toml
└── .env.example
```

## Setup

### 1. Prerequisites

- Python 3.11 or newer
- A Binance Futures account (testnet recommended at first)
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))

### 2. Install

```bash
git clone https://github.com/arislanars06-design/newstrategybot.git
cd newstrategybot
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and fill in real values.
```

Required settings:
- `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- `BINANCE_TESTNET=true` (recommended for first run)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS` (your Telegram numeric ID)
- `TELEGRAM_NOTIFY_CHAT_ID` (where the bot sends notifications)

### 4. Binance Futures position mode

The bot expects **Hedge Mode** so that long and short blocks on the same
symbol can coexist. The bot will check this on startup and refuse to run
otherwise.

To set it manually:
```
Binance Futures → Preferences → Position Mode → Hedge Mode
```

### 5. Run

```bash
python -m src.main
```

### 6. VPS deployment

Two supported paths — pick one.

#### A. Docker (simplest)

```bash
# On the VPS, after cloning the repo and writing your .env
docker compose up -d --build

# Logs
docker compose logs -f bot

# Update later
git pull && docker compose up -d --build
```

The `data/` and `logs/` directories on the host are mounted into the
container, so the SQLite database and log files persist across rebuilds.

#### B. systemd (no Docker)

A one-shot installer is provided for fresh Ubuntu/Debian VPS hosts:

```bash
sudo bash deploy/install_vps.sh
```

The installer:
1. Installs Python and Git.
2. Creates a `botuser` system user.
3. Clones the repo to `/opt/newstrategybot`.
4. Creates a virtualenv and installs the package.
5. Drops a `.env` template at `/opt/newstrategybot/.env` (mode 0600).
6. Installs `deploy/newstrategybot.service` into systemd.

Then:

```bash
sudo -u botuser nano /opt/newstrategybot/.env   # fill in secrets
sudo systemctl enable --now newstrategybot
sudo journalctl -u newstrategybot -f            # follow logs
```

The systemd unit restarts on failure with a 10-restarts-per-minute
ceiling and applies basic hardening (`NoNewPrivileges`, `ProtectSystem`,
read-only `$HOME`, writable only `data/` and `logs/`).

## Telegram commands

| Command       | Description                              |
|---------------|------------------------------------------|
| `/newblock`   | Interactive flow to create a new block.  |
| `/list`       | Show all currently active blocks.        |
| `/block <id>` | Show details of a specific block.        |
| `/cancel <id>`| Manually close an active block.          |
| `/stats`      | Aggregated win-rate and net P&L.         |
| `/balance`    | Show Binance Futures wallet balance.     |

## Status

MVP complete. The bot can place 8-rung blocks on Binance Futures, react
to entry/TP/SL events, enforce the cancel-price rule, and report through
Telegram.

Known limitations:
- Tested only on the smoke-test level — no live-fire testnet runs in
  this repository's CI yet. Run on the Binance testnet first.
- Multi-position gap scenarios (where multiple entries fill in a single
  tick due to a price gap) follow Variant A: extra open positions run
  on their own TP/SL and are not market-closed.
- Net P&amp;L recorded at WIN time may understate later P&amp;L from
  positions that were already open. In normal chain flow this is a
  non-issue; in gap scenarios it is a known trade-off.

## License

MIT
