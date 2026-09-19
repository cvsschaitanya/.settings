#!/usr/bin/env bash
# __#__~/.tmux/scripts/paste-line-above-prompt.sh__#__
# Grabs the last non-empty line above the current shell prompt block and
# types it into the pane (does not execute it).
#
# Assumes the two-line zsh prompt format from ~/.zshrc:
#   <dot> [hh:mm:ss] path (branch)
#   $
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

# tmux's run-shell provides no LANG/LC_*; awk needs a UTF-8 locale to treat the
# multi-byte prompt marker below as one character rather than three bytes.
export LANG="C.UTF-8"
export LC_ALL="C.UTF-8"

pane="${1:-$TMUX_PANE}"

content=$(tmux capture-pane -p -t "$pane" -S -200)

target=$(printf '%s\n' "$content" | awk '
  /^[^[:space:]][[:space:]]*\[[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\]/ { last_prompt = NR }
  { lines[NR] = $0 }
  END {
    for (i = last_prompt - 1; i >= 1; i--) {
      if (lines[i] !~ /^[[:space:]]*$/) { print lines[i]; exit }
    }
  }
')

if [ -n "$target" ]; then
  tmux send-keys -t "$pane" -l "$target"
  printf '%s' "$target" | pbcopy
  tmux display-message -t "$pane" "Copied: $target"
else
  tmux display-message -t "$pane" "paste-line-above-prompt: nothing found"
fi
