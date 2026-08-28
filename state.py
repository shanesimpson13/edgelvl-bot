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


def save(positions, seen, armed=()):
    """Write everything to disk. Called after every state change."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            # armed is a map now: key -> {"user", "wallet_id"}. A bare list
            # from an older build still loads, it just has nothing to re-arm
            # with. Written as a map so a restart can put the coin back on the
            # wallet it belonged to.
            armed_out = (armed if isinstance(armed, dict)
                         else {k: {} for k in armed})
            json.dump({"positions": positions, "seen": sorted(seen),
                       "armed": armed_out}, f, default=str)
        tmp.replace(STATE_FILE)          # atomic — a crash mid-write can't corrupt it
    except Exception as e:
        log.error(f"save failed: {e}")


def load():
    """Returns (positions, seen, armed). Empty on first run.

    `armed` is coins you greenlit that hadn't bought yet. Those sessions live in
    memory, so without this a restart drops them silently — you'd think the bot
    was watching a coin that nothing is watching.
    """
    if not STATE_FILE.exists():
        return {}, set(), {}
    try:
        d = json.load(open(STATE_FILE))
        pos = d.get("positions", {}) or {}
        seen = set(d.get("seen", []) or [])
        armed = d.get("armed") or {}
        if isinstance(armed, list):       # written by an older build
            armed = {k: {} for k in armed}
        if pos:
            log.info(f"restored {len(pos)} open position(s) from disk")
        return pos, seen, armed
    except Exception as e:
        log.error(f"load failed: {e}")
        return {}, set(), {}


async def reconcile(session, positions, get_balance):
    """Check saved positions against the wallet. The wallet is the truth.

    Three cases:
      - wallet matches   -> keep going
      - wallet has less  -> a sell landed that we didn't record; resync the amount
      - wallet has none  -> position is gone (sold, rugged, or dust); drop it

    Returns (notes, closed): human-readable notes about anything that changed,
    and the positions that vanished entirely. The caller needs the second one to
    journal an exit it did not make — dropping the position without recording
    it leaves the trade out of the history altogether.
    """
    notes, closed = [], []
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
            closed.append((mint, pos))
            positions.pop(mint, None)
        elif actual != saved:
            notes.append(f"{pos.get('name', mint[:8])}: wallet has {actual:,} not {saved:,} — resynced")
            pos["tokens_raw"] = actual
    return notes, closed
