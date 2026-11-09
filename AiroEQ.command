#!/bin/sh
# AiroEQ launcher for macOS - double-click in Finder (run "chmod +x AiroEQ.command" once).
cd "$(dirname "$0")" || exit 1
exec python3 airoeq.py
