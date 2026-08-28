#!/usr/bin/env bash
# Start exactly one bot. Killing by `pgrep | head -1` left duplicates running,
# and each one answered the same greenlight — one arming it, the stale ones
# replying "couldn't load that coin".
cd /home/ubuntu/edgelvl-live
for pid in $(pgrep -f "edgelvl-live/bot.py|python3 -u bot.py"); do
  [ "$pid" != "$$" ] && kill "$pid" 2>/dev/null
done
sleep 3
pkill -9 -f "python3 -u bot.py" 2>/dev/null
sleep 1
set -a; . ./.env; set +a
nohup /home/ubuntu/dsb-gmgn/venv/bin/python3 -u bot.py >> run.log 2>&1 < /dev/null &
echo "started pid $!"
