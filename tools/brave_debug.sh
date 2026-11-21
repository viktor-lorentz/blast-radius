#!/bin/bash
# Real Brave (not Chrome for Testing) with a saved profile and a debug port.
# Sign in once in the window this opens. Later submits attach to the port.
set -e
PROFILE="${HOME}/.buildathon-brave"
mkdir -p "$PROFILE"
exec "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "https://www.joinbuilders.org/buildathon/sign-in"
