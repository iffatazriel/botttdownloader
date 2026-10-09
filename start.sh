#!/bin/bash
# Start script for Termux (Android) / Linux
# Usage:
#   export TELEGRAM_TOKEN="123456:ABC..."
#   bash start.sh
set -e
cd "$(dirname "$0")"

if [ -z "$TELEGRAM_TOKEN" ]; then
  echo "ERROR: TELEGRAM_TOKEN env var is not set."
  echo "Run: export TELEGRAM_TOKEN='your-bot-token-here'"
  exit 1
fi

pip install -r requirements.txt
python bot.py
