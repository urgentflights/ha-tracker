#!/bin/bash
# Idempotent: starts the HA Tracker box sync if it is not running (or has gone stale).
# Prints "OK last_success=<time>" or "RESTARTED last_success=<time>".
cd "$(dirname "$0")" || exit 1
STALE_S=${STALE_S:-900}
exec 9>ensure.lock; flock -w 30 9 || { echo "BUSY"; exit 1; }
alive=0
if [ -f sync.pid ] && kill -0 "$(cat sync.pid)" 2>/dev/null && grep -q "sync.py" /proc/"$(cat sync.pid)"/cmdline 2>/dev/null; then alive=1; fi
last_epoch=0; [ -f last_success ] && last_epoch=$(cut -d' ' -f1 last_success)
age=$(( $(date +%s) - last_epoch ))
status=OK
if [ $alive -eq 1 ] && [ $age -gt $STALE_S ] && [ "${NO_STALE_RESTART:-0}" != 1 ]; then
  # running but no successful push for a while -> restart it
  kill "$(cat sync.pid)" 2>/dev/null; sleep 2; kill -9 "$(cat sync.pid)" 2>/dev/null; alive=0
fi
if [ $alive -eq 0 ]; then
  status=RESTARTED
  nohup setsid python3 sync.py >> sync.log 2>&1 9>&- < /dev/null &
  sleep 3
fi
ls_txt="never"; [ -f last_success ] && ls_txt=$(cut -d' ' -f2 last_success)
echo "$status last_success=$ls_txt"
