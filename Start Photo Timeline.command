#!/bin/bash
# Double-click on a Mac (or run in a terminal on Linux).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed. Opening the download page — install it, then run this again."
  open https://www.python.org/downloads/ 2>/dev/null || xdg-open https://www.python.org/downloads/
  read -n 1 -s -r -p "Press any key to close."; exit 1
fi
if [ ! -f library/.ready ]; then
  echo "First start: installing what Photo Timeline needs. This takes a minute…"
  python3 -m pip install --user -q -r requirements.txt || python3 -m pip install --user --break-system-packages -q -r requirements.txt || { echo "Install failed."; read -n 1; exit 1; }
  mkdir -p library && touch library/.ready
  command -v ffmpeg >/dev/null 2>&1 || echo "Tip: for video thumbnails install ffmpeg (Mac: brew install ffmpeg)."
fi
python3 timeline.py
