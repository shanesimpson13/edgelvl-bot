"""
state.py — surviving a restart.

The single most expensive bug in a trading bot is forgetting that you're holding
something. Kill the process mid-trade without this and the position sits in your
wallet with no take-profit and no stop, quietly bleeding, until you notice.

So: every position is written to disk the moment anything changes, and on startup
we reload it and check it against what the wallet actually holds. The wallet is
the source of truth — if they disagree, the wallet wins.
"""
import json
import logging
from pathlib import Path

import config as C

log = logging.getLogger("state")

STATE_FILE = Path(C.STATE_FILE)


def save(positions, seen):
    """Write everything to disk. Called after every state change."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"positions": positions, "seen": sorted(seen)}, f, default=str)
        tmp.replace(STATE_FILE)          # atomic — a crash mid-write can't corrupt it
    except Exception as e:
        log.error(f"save failed: {e}")


def load():
    """Returns (positions, seen). Empty on first run."""
    if not STATE_FILE.exists():
        return {}, set()
    try:
        d = json.load(open(STATE_FILE))
        pos = d.get("positions", {}) or {}
        seen = set(d.get("seen", []) or [])
        if pos:
            log.info(f"restored {len(pos)} open position(s) from disk")
        return pos, seen
    except Exception as e:
        log.error(f"load failed: {e}")
        return {}, set()


async def reconcile(session, positions, get_balance):
    """Check saved positions against the wallet. The wallet is the truth.

    Three cases:
      - wallet matches   -> keep going
      - wallet has less  -> a sell landed that we didn't record; resync the amount
      - wallet has none  -> position is gone (sold, rugged, or dust); drop it

    Returns a list of human-readable notes about anything that changed, so the
    bot can tell you rather than silently fixing it behind your back.
    """
    notes = []
    for mint in list(positions.keys()):
        pos = positions[mint]
        saved = int(pos.get("tokens_raw", 0) or 0)
        try:
            actual = await get_balance(session, mint)
        except Exception as e:
            log.warning(f"reconcile {mint[:8]}: balance check failed ({e}) — keeping as-is")
            continue

        if actual is None:
            continue

        if actual == 0:
            notes.append(f"{pos.get('name', mint[:8])}: no longer in wallet — closing it out")
            positions.pop(mint, None)
        elif actual != saved:
            notes.append(f"{pos.get('name', mint[:8])}: wallet has {actual:,} not {saved:,} — resynced")
            pos["tokens_raw"] = actual
    return notes
