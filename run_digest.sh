#!/bin/bash
# Daily Digest — runs at 09:30 IST (04:00 UTC) Mon–Fri
set -a
source "$(dirname "$0")/.env"
set +a
cd "$(dirname "$0")"
/usr/bin/python3 daily_digest.py >> /tmp/ask_ai_digest.log 2>&1
