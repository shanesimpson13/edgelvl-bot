"""Reconciliation must never close a position it did not actually verify.

This is the failure it exists to prevent: a restart closed two live positions
as "sold outside the bot", journaled the full stake as a loss, and stopped
managing the stop on coins still sitting in the wallets.
"""
import asyncio, sys
sys.path.insert(0, "/home/ubuntu/edgelvl-live")
import state as S

FAIL = []
def ok(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAIL.append(label)


async def main():
    # 1. an unreadable balance leaves the position alone
    pos = {"u1|MintA": {"name": "A", "tokens_raw": 100}}
    notes, closed = await S.reconcile(None, pos, lambda s, k: _none())
    ok("unreadable balance does NOT close the position", not closed and "u1|MintA" in pos,
       f"closed={closed}")

    # 2. a genuine zero does close it
    pos = {"u1|MintA": {"name": "A", "tokens_raw": 100}}
    notes, closed = await S.reconcile(None, pos, lambda s, k: _val(0))
    ok("a real zero closes it", len(closed) == 1 and "u1|MintA" not in pos)

    # 3. a different amount resyncs rather than closing
    pos = {"u1|MintA": {"name": "A", "tokens_raw": 100}}
    notes, closed = await S.reconcile(None, pos, lambda s, k: _val(60))
    ok("a smaller balance resyncs", not closed and pos["u1|MintA"]["tokens_raw"] == 60)

    # 4. the key handed to the balance reader is what the caller resolves —
    #    the whole composite arrives, and it is the caller's job to split it
    seen = []
    async def spy(s, k):
        seen.append(k)
        return 100
    pos = {"did:privy:abc|MintA": {"name": "A", "tokens_raw": 100}}
    await S.reconcile(None, pos, spy)
    ok("reader receives the full position key", seen == ["did:privy:abc|MintA"], str(seen))
    ok("the key really is composite, not a bare mint", "|" in seen[0])

    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILURES: {FAIL}"))
    return 1 if FAIL else 0


async def _none():
    return None
async def _val(v):
    return v

sys.exit(asyncio.run(main()))
