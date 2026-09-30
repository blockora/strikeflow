# BLOCKORA — Option Strike Selector

Production-oriented decision-support system for NIFTY option strike selection.

## Features
- Angel One SmartAPI as primary source
- Jugaad/NSE option-chain as fallback/enrichment
- 1-minute synchronized cycle engine
- 10 candidate strike generation
- Regime detection
- Scoring + confidence
- Entry / SL / Target / R:R
- SQLite history
- Previous-cycle memory
- No auto-trading

## Setup on Android 14 / Termux

```bash
pkg update && pkg upgrade
pkg install python git wget
pip install -r requirements.txt
cp .env.example .env
nano .env
python -m app.main
