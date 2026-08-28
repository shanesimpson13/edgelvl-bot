"""Would a cross-user mix-up be caught? Prove it before this touches money.

Every function in the trade path is called with an explicit wallet and we
assert the RPC and the signature both went to that wallet — not to the
module-level default. A regression here means one customer's balance read
against another's signature, which is the worst failure this code can have.
"""
import asyncio, sys, types
import jupiter as J

FAIL = []
def ok(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond: FAIL.append(name)


class FakeWallet:
    kind = "privy"
    wrap_sol = False
    def __init__(self, name, address):
        self.name, self.address = name, address
        self.signed = []
    async def sign(self, s, tx_b64):
        self.signed.append(tx_b64)
        return f"signed-by-{self.name}", None


class SpyRPC:
    """Records which address each RPC was asked about."""
    def __init__(self): self.calls = []
    async def __call__(self, s, method, params):
        self.calls.append((method, params))
        if method == "getTokenAccountsByOwner":
            return {"result": {"value": [{"account": {"data": {"parsed": {"info": {
                "tokenAmount": {"amount": "123"}}}}}}]}}
        if method == "getTokenAccountBalance":
            return {"result": {"value": {"amount": "500000000"}}}
        return {"result": None}


async def main():
    alice = FakeWallet("alice", "A" * 43)
    bob = FakeWallet("bob", "B" * 43)
    # the module default is a THIRD wallet — if anything falls back to it, we see it
    J.WALLET = FakeWallet("default", "D" * 43)
    J.ME = J.WALLET.address

    spy = SpyRPC(); J.rpc = spy

    print("=== balance reads go to the wallet passed in ===")
    await J.token_balance(None, "MintX", wallet=alice)
    asked = spy.calls[-1][1][0]
    ok("token_balance uses alice", asked == alice.address, f"asked {asked[:8]}")

    await J.token_balance(None, "MintX", wallet=bob)
    asked = spy.calls[-1][1][0]
    ok("token_balance uses bob", asked == bob.address, f"asked {asked[:8]}")

    await J.trading_balance(None, wallet=alice)
    ok("trading_balance reads alice's own account, not the default's",
       spy.calls[-1][1][0] == alice.address)

    print("\n=== the default is used only when nothing is passed ===")
    await J.token_balance(None, "MintX")
    ok("falls back to the default", spy.calls[-1][1][0] == J.WALLET.address)

    print("\n=== the signature comes from the wallet passed in ===")
    # Jupiter builds and lands the transaction now, so the seam to hold is
    # ultra.swap: given a wallet, it must sign with THAT wallet and no other.
    import ultra as U
    async def fake_order(session, a, b, amt, taker, key, **kw):
        return {"transaction": "dGVzdA==", "requestId": "req"}, None
    async def fake_execute(session, rid, signed, key):
        return {"status": "Success", "signature": "sig",
                "totalOutputAmount": "1"}, None
    U.order, U.execute = fake_order, fake_execute
    U._signature_of = lambda b64: "sig"

    for w in (alice, bob):
        try:
            await U.swap(None, J.WSOL, "MintX", 1, w, "key")
        except Exception:
            pass
    ok("alice signed exactly once", len(alice.signed) == 1, f"got {len(alice.signed)}")
    ok("bob signed exactly once", len(bob.signed) == 1, f"got {len(bob.signed)}")
    ok("the default NEVER signed", len(J.WALLET.signed) == 0,
       f"default signed {len(J.WALLET.signed)} times")

    print("\n=== no wallet at all is refused, not defaulted ===")
    J.WALLET = None
    got, err, _gas = await J.execute_buy(None, "MintX", 0.02)
    ok("execute_buy refuses with no wallet", err == "no_wallet", f"err={err}")

    await price_sharing()

    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILURES: {FAIL}"))
    return 1 if FAIL else 0


# ── shared price feed ───────────────────────────────────────────────────────
async def price_sharing():
    print("\n=== one quote serves every session on that coin ===")
    calls = {"n": 0}

    async def counting_quote(s, a, b, amount, **kw):
        calls["n"] += 1
        await asyncio.sleep(0.02)          # make the race real
        return {"outAmount": "1000000"}

    J.quote = counting_quote
    J._px_cache.clear(); J._px_inflight.clear()

    # ten sessions ask for the same coin at the same instant
    got = await asyncio.gather(*[J.get_price(None, "SameMint") for _ in range(10)])
    ok2("10 concurrent readers -> 1 quote", calls["n"] == 1, f"made {calls['n']}")
    ok2("all got the same price", len(set(got)) == 1 and got[0] is not None)

    # within the TTL, still no new quote
    await J.get_price(None, "SameMint")
    ok2("cached inside the TTL", calls["n"] == 1, f"made {calls['n']}")

    # a different coin is a different fetch
    await J.get_price(None, "OtherMint")
    ok2("a second coin costs a second quote", calls["n"] == 2, f"made {calls['n']}")

    # after the TTL it refetches
    J._px_cache["SameMint"] = (0, 1.0)
    await J.get_price(None, "SameMint")
    ok2("refetches once the TTL lapses", calls["n"] == 3, f"made {calls['n']}")


def ok2(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond: FAIL.append(name)


sys.exit(asyncio.run(main()))
