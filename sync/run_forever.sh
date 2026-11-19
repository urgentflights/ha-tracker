#!/bin/bash
# Wrapper: keeps sync.py running, restarting it (after 5 s) whenever it exits for any reason.
cd "$(dirname "$0")" || exit 1
echo $$ > wrapper.pid
trap '' HUP
while true; do
  python3 sync.py >> sync.log 2>&1
  echo "$(date '+%Y-%m-%d %H:%M:%S') sync.py exited with code $? ; restarting in 5s" >> sync.log
  sleep 5
done
