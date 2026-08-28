#!/usr/bin/env python3
"""Sweep the retired gas-sponsor wallet to an address you name.

The sponsor paid network fees back when we assembled swap transactions
ourselves. Swap v2 does that now, nothing in the live path touches this wallet,
and the balance is just sitting there.

RUN THIS YOURSELF. It moves your money, so it is yours to execute — I wrote it,
I have not run it, and it will not run without --go.

    python3 sweep_sponsor.py                 # show what it would do
    python3 sweep_sponsor.py --go            # actually send

It leaves nothing behind: the whole balance minus the network fee goes to the
destination, and the account is left empty rather than dusty.
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

DEST = "AgreJUkzjMgZNKZqYR3mXoMCx2TkpddqcWNkioHj557V"
ENV = "/home/ubuntu/edgelvl-live/.env"
FEE_LAMPORTS = 5000          # one signature


def rpc(url, method, params):
    req = urllib.request.Request(
        url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": params}).encode(),
        {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def env(name):
    """Read one value out of the bot's .env without importing its config.

    The file mixes `export KEY=` and bare `KEY=`, so both are accepted.
    """
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually send it")
    ap.add_argument("--to", default=DEST, help="destination address")
    args = ap.parse_args()

    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.message import Message
    from solders.transaction import Transaction
    from solders.hash import Hash
    import base58

    secret = env("SPONSOR_SECRET")
    if not secret:
        sys.exit("SPONSOR_SECRET is not in the .env — nothing to sweep.")
    kp = Keypair.from_bytes(base58.b58decode(secret))
    src = kp.pubkey()
    # Same fallback the bot's config uses when RPC_URL isn't set.
    url = env("RPC_URL") or "https://api.mainnet-beta.solana.com"

    lamports = rpc(url, "getBalance", [str(src)])["result"]["value"]
    send = lamports - FEE_LAMPORTS
    print(f"  from   {src}")
    print(f"  to     {args.to}")
    print(f"  holds  {lamports / 1e9:.6f} SOL")
    print(f"  sends  {send / 1e9:.6f} SOL   (leaving the {FEE_LAMPORTS/1e9:.6f} fee)")

    if send <= 0:
        sys.exit("  nothing to send.")
    if not args.go:
        print("\n  DRY RUN. Re-run with --go to send it.")
        return

    bh = rpc(url, "getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["result"]["value"]["blockhash"])
    ix = transfer(TransferParams(from_pubkey=src,
                                 to_pubkey=Pubkey.from_string(args.to),
                                 lamports=send))
    tx = Transaction([kp], Message.new_with_blockhash([ix], src, blockhash), blockhash)
    sig = rpc(url, "sendTransaction",
              [base64.b64encode(bytes(tx)).decode(),
               {"encoding": "base64", "skipPreflight": False, "maxRetries": 5}])
    if "error" in sig:
        sys.exit(f"  send failed: {sig['error']}")
    print(f"\n  sent: {sig['result']}")
    print(f"  https://solscan.io/tx/{sig['result']}")


if __name__ == "__main__":
    main()
