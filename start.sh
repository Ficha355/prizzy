#!/bin/bash
set -e

gunicorn app:app --timeout 120 &
python3 bot_legit.py &
python3 run_bot.py
