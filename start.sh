#!/bin/bash
set -e

gunicorn app:app --timeout 120 &
python3 run_bot.py
