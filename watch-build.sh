#!/bin/bash
# Live view of the corridor build: progress on the left, store state on the right.
LOG=/tmp/claude-1000/-home-ppv-Projects/ffa47c91-a181-483c-9294-d88433cba1a2/scratchpad/build.log
STORE=/home/ppv/Projects/Wheatear/foundry-output/store
while true; do
  clear
  echo "=== STORE ============================================================"
  for plat in copilot-studio orchestrate; do
    n=$(ls "$STORE/corpora/$plat" 2>/dev/null | wc -l)
    a=$(find "$STORE/adapters/$plat" -name artifact.json 2>/dev/null | wc -l)
    printf "  %-16s corpora:%-3s adapters built:%-3s\n" "$plat" "$n" "$a"
  done
  echo "  assets shipped : $(find /home/ppv/Projects/Wheatear/engine/assets -name artifact.json 2>/dev/null | wc -l) adapter(s)"
  echo
  echo "=== BUILD LOG (last 30 lines) ========================================"
  tail -n 30 "$LOG" 2>/dev/null
  echo
  echo "--- refreshing every 5s, Ctrl-C to stop watching (build keeps running)"
  sleep 5
done
