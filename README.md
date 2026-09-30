# BLOCKORA — Option Strike Selector

Production-oriented decision-support system for NIFTY option strike selection,
built for Android 14 + Termux + Python. Master specification: `BLOCKORA.md`
(read-only source of truth).

## Features
- Angel One SmartAPI as primary source (SmartAPI v2 WebSocket, TOTP auth, Scrip Master)
- NSE/Jugaad option-chain as secondary/fallback/enrichment with source tagging
- 1-minute synchronized cycle engine (minute-boundary aligned, no blind sleep)
- Exactly 10 candidate strikes when sufficient data exists, balanced CE/PE around ATM
- Regime detection with NO_CLEAR_REGIME honesty
- 0–100 scoring per BLOCKORA §15 weight architecture
- Confidence separate from score, gated by real historical sample sizes
- Entry zone / SL / Target / R:R, NO VALID TARGET rejection
- Hard minimum gates -> NO CLEAR STRIKE when evidence is insufficient
- SQLite history: cycles, market/option snapshots, candidate scores, signals,
  signal state history, outcomes, system events
- Previous-cycle memory and §35 signal state machine
- Backtesting and walk-forward validation over stored historical data only
  (no look-ahead, costs + slippage; reports BACKTEST NOT READY until real
  historical option-chain snapshots exist)
- No automatic trading: no order placement, modification or cancellation

## Setup on Android 14 / Termux

```bash
pkg update && pkg upgrade
pkg install python git wget
pip install -r requirements.txt
cp .env.example .env
nano .env            # fill Angel credentials; never commit .env
python -m app.main
```

Notes:
- All credentials live only in `.env` (§52). `.env` is git-ignored.
- Database: `data/history.db`; logs: `data/logs/`. Both survive restarts (§74).
- Screen-off: Android may suspend background processes. For continuous
  operation use a Termux wake-lock (`termux-wake-lock`) and disable battery
  optimization for Termux. The engine itself has no GUI dependency (§49).
- Historical validation accumulates automatically while the engine runs during
  market hours; backtesting remains honest (NOT READY) until real snapshots
  exist.

## Decision-support only

This system never places, modifies, or cancels orders (§54, §88). The user
makes the final trading decision.
