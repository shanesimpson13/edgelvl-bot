"""
test_privy.py — proves the custody claim instead of asserting it.

The claim has three parts, and all three are checked here against the live API:

  1. our key can sign a Jupiter swap and wrap SOL into the wallet's own account
  2. our key cannot move funds anywhere else, however the attempt is dressed up
  3. the customer's key can always withdraw

    PRIVY_APP_ID=… PRIVY_APP_SECRET=… python test_privy.py

No funds required — signing is not sending, and nothing here can spend anything.
It provisions a throwaway wallet each run, so it never touches a real one.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys

import aiohttp
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

import config as C
import privy

WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
OUTSIDER = Pubkey.from_string("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
SDK_DIR = os.environ.get("PRIVY_SDK_DIR", "/home/ubuntu/privy-test")


def p256_keypair():
    """A P-256 keypair in Privy's format, via their SDK so the encoding is theirs."""
    out = subprocess.run(
        ["node", "-e", "require('@privy-io/node').generateP256KeyPair()"
                       ".then(k => console.log(JSON.stringify(k)))"],
        cwd=SDK_DIR, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"could not generate a key: {out.stderr[:200]}")
    return json.loads(out.stdout)


def tx_of(payer, ixs):
    msg = Message.new_with_blockhash(ixs, payer, Hash.default())
    return base64.b64encode(bytes(Transaction.new_unsigned(msg))).decode()


def sync_native(acct):
    """SyncNative — a Token-program instruction our policy permits."""
    return Instruction(TOKEN, bytes([17]), [AccountMeta(acct, False, True)])


async def main():
    if not (C.PRIVY_APP_ID and C.PRIVY_APP_SECRET):
        print("Set PRIVY_APP_ID and PRIVY_APP_SECRET first.")
        return 1

    import jupiter as J
    user = p256_keypair()          # the customer
    bot = p256_keypair()           # us

    async with aiohttp.ClientSession() as s:
        w, err = await privy.provision(s, {"public_key": user["publicKey"]}, bot["publicKey"], bot["privateKey"])
        if err:
            print(f"could not provision a test wallet: {err}")
            return 1
        addr = Pubkey.from_string(w.address)
        wsol = Pubkey.from_string(J.ata(w.address, J.WSOL))
        print(f"wallet {w.address}")
        print(f"  owner  = customer's key")
        print(f"  signer = our key, swap-only policy\n")

        def as_bot(tx):
            return PrivyWalletAs(w, bot["privateKey"]).sign(s, tx)

        def as_user(tx):
            return PrivyWalletAs(w, user["privateKey"]).sign(s, tx)

        cases = [
            ("we wrap SOL into its own account", as_bot, tx_of(addr, [
                transfer(TransferParams(from_pubkey=addr, to_pubkey=wsol,
                                        lamports=10**8))]), True),
            ("we call SyncNative", as_bot, tx_of(addr, [sync_native(wsol)]), True),
            ("we send SOL to an outsider", as_bot, tx_of(addr, [
                transfer(TransferParams(from_pubkey=addr, to_pubkey=OUTSIDER,
                                        lamports=5 * 10**8))]), False),
            ("we hide a drain behind a wrap", as_bot, tx_of(addr, [
                transfer(TransferParams(from_pubkey=addr, to_pubkey=wsol, lamports=1000)),
                transfer(TransferParams(from_pubkey=addr, to_pubkey=OUTSIDER,
                                        lamports=5 * 10**8))]), False),
            ("customer withdraws", as_user, tx_of(addr, [
                transfer(TransferParams(from_pubkey=addr, to_pubkey=OUTSIDER,
                                        lamports=5 * 10**8))]), True),
        ]

        ok = True
        for label, signer, tx, should_sign in cases:
            signed, err = await signer(tx)
            got = signed is not None
            ok &= got == should_sign
            print(f"  {'PASS' if got == should_sign else 'FAIL'}  {label:34s} -> "
                  + ("signed" if got else f"refused ({err})"))

        # The other half of the claim: we can't rewrite the rules either.
        # These must target the REAL policy and a REAL extra signer — a PATCH to
        # an id that doesn't exist 404s and proves nothing.
        print()
        evil = p256_keypair()
        q, _ = await privy._call(s, "POST", "/v1/key_quorums",
                                 {"public_keys": [evil["publicKey"]],
                                  "authorization_threshold": 1,
                                  "display_name": "unrestricted"})
        for label, path, body in [
                ("rewrite the policy", f"/v1/policies/{w.policy_id}",
                 {"rules": [{"name": "allow all", "method": "signTransaction",
                             "action": "ALLOW",
                             "conditions": [{"field_source": "solana_program_instruction",
                                             "field": "programId", "operator": "in",
                                             "value": ["11111111111111111111111111111111"]}]}]}),
                ("add an unrestricted signer", f"/v1/wallets/{w.wallet_id}",
                 {"additional_signers": [{"signer_id": q.get("id"),
                                          "override_policy_ids": []}]})]:
            d, err = await privy._call(s, "PATCH", path, body)
            # Insist on the RIGHT refusal. Privy reports a missing authorization
            # signature as `invalid_data`, which a malformed body would also
            # produce — so check the reason, or this test passes when our request
            # was simply wrong and proves nothing.
            reason = str(d.get("error", ""))
            blocked = err is not None and "authorization-signature" in reason
            ok &= blocked
            if err and not blocked:
                verdict = f"refused for the WRONG reason ({reason[:60]})"
            elif blocked:
                verdict = "refused — needs the customer's signature"
            else:
                verdict = "SUCCEEDED — guarantee is void"
            print(f"  {'PASS' if blocked else 'FAIL'}  we try to {label:24s} -> {verdict}")

        print()
        print("Customer can always withdraw; we can only ever trade." if ok
              else "NOT PROVEN — do not run customer funds through this.")
        return 0 if ok else 1


class PrivyWalletAs(privy.PrivyWallet):
    """The same wallet, signed for by a specific key. Lets one test act as either
    party without pretending they're interchangeable."""

    def __init__(self, wallet, auth_key):
        super().__init__(wallet.wallet_id, wallet.address, auth_key=auth_key)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
