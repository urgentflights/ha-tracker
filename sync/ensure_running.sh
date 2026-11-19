#!/bin/bash
# Idempotent watchdog for the HA Tracker box sync. Prints "OK ..." or "RESTARTED ..." with the last-success time.
# The wrapper (run_forever.sh) restarts sync.py if it crashes; this script revives the wrapper itself and
# detects a hung process via the heartbeat file. A stale heartbeat right after the box was suspended/resumed
# gets up to GRACE_S to recover before anything is killed.
cd "$(dirname "$0")" || exit 1
GRACE_S=${GRACE_S:-90}; HB_STALE_S=${HB_STALE_S:-60}
exec 9>ensure.lock; flock -w 30 9 || { echo "BUSY"; exit 1; }
now() { date +%s; }
hb_age() { local h=0; [ -f heartbeat ] && h=$(cat heartbeat 2>/dev/null); echo $(( $(now) - ${h:-0} )); }
wrapper_alive() { [ -f wrapper.pid ] && kill -0 "$(cat wrapper.pid)" 2>/dev/null && grep -q run_forever /proc/"$(cat wrapper.pid)"/cmdline 2>/dev/null; }
status=OK
if wrapper_alive && [ "$(hb_age)" -gt $HB_STALE_S ]; then
  # maybe the box was just resumed from suspension: give it a chance to produce a fresh heartbeat
  for _ in $(seq 1 $((GRACE_S/5))); do sleep 5; [ "$(hb_age)" -le $HB_STALE_S ] && break; done
  if [ "$(hb_age)" -gt $HB_STALE_S ]; then
    kill "$(cat wrapper.pid)" 2>/dev/null; [ -f sync.pid ] && kill "$(cat sync.pid)" 2>/dev/null; sleep 2
    [ -f sync.pid ] && kill -9 "$(cat sync.pid)" 2>/dev/null
    status=RESTARTED
  fi
fi
if ! wrapper_alive; then status=RESTARTED; fi
if [ $status = RESTARTED ] && ! wrapper_alive; then
  setsid nohup ./run_forever.sh > /dev/null 2>&1 9>&- < /dev/null &
  sleep 4
fi
ls_txt="never"; [ -f last_success ] && ls_txt=$(cut -d' ' -f2 last_success)
echo "$status last_success=$ls_txt heartbeat_age=$(hb_age)s"
