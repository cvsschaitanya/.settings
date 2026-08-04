#!/usr/bin/env bash
# __#__~/.tmux/scripts/nth-line-matching.sh__#__
# Finds the Nth line from the bottom matching a regex and types it into the
# pane (does not execute it). N=1 is the most recent match.
set -euo pipefail

pane="$1"
n="$2"
regex="$3"

content=$(tmux capture-pane -p -t "$pane" -S -200)

target=$(printf '%s\n' "$content" | grep -E "$regex" | tail -n "$n" | head -n 1)

if [ -n "$target" ]; then
  tmux send-keys -t "$pane" -l "$target"
else
  tmux display-message -t "$pane" "nth-line-matching: no match"
fi
