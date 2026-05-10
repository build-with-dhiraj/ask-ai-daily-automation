#!/bin/bash
# Daily Eval — scheduled 04:00 IST (22:30 UTC) via Daily Automation; self-hosted runner required
# Pulls from Metabase Q33193, judges via Azure gpt-4.1, writes scores to Langfuse, posts to Slack
set -a
source "$(dirname "$0")/.env"
set +a
cd "$(dirname "$0")"
caffeinate -u -t 4200 &   # keep awake until after digest fires
/usr/bin/python3 daily_eval.py >> /tmp/ask_ai_eval.log 2>&1
