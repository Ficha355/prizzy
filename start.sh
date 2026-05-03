#!/bin/bash
set -e

gunicorn app:app &
python3 run_bot.py
