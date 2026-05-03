#!/bin/bash
set -e

python3 bot.py &
gunicorn app:app
