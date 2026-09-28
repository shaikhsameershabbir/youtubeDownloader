#!/bin/sh
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
if [ ! -x bin/ffmpeg ] || [ ! -x bin/ffprobe ]; then
  echo "ffmpeg was not found in bin/. Install ffmpeg to save the best quality." >&2
fi
exec .venv/bin/python app.py
