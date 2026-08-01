"""
privy.py — trading a wallet we do not own and cannot drain.

THE SHAPE
  The customer's key owns the wallet. We are an *additional signer* on it,
  carrying a policy that permits Jupiter swaps and wrapping SOL into the
  wallet's own WSOL account — nothing else. Privy evaluates each signer against
  its own policy, so:

    customer's key  ->  no policy       ->  can always withdraw
    our key         ->  swap-only       ->  can only ever trade

  The policy and the wallet are both owned by the customer's key, so we can't
  rewrite the policy or bolt on an unrestricted signer either. Verified: both
  attempts come back 401 for a missing authorization signature. This is not a
  promise we keep, it's one we cannot break.

  The whole guarantee rests on the customer's key living client-side, in their
  Privy session. If we ever generate or store it, all of the above evaporates.

REQUEST SIGNING
  Because we're a key-quorum signer, every request carries a
  `privy-authorization-signature`: a P-256 signature over the JCS-canonicalised
  request payload. Ported from Privy's own SDK rather than guessed at, and
  checked against the live API — see test_privy.py.
"""
import base64
import json

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import config as C

API = "https://api.privy.io"

# Programs a swap may touch. The System Program is deliberately absent: that
# omission is what makes an outbound transfer unrepresentable rather than merely
# disallowed. Wrapping SOL is permitted by a separate, tightly-scoped rule.
SWAP_PROGRAMS = [
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",   # associated token account
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL token
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
]


# ── request authorization ───────────────────────────────────────────────────
def _canonical(obj):
    """RFC 8785 (JCS), which is what Privy's SDK canonicalises with.

    Sorted keys, no whitespace. JCS orders by UTF-16 code unit; python sorts by
    code point. Identical for the ASCII keys these payloads use, and every key
    here is ASCII — field names, base64 transactions, base58 addresses.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_request(method, url, body, private_key_b64, app_id):
    """The `privy-authorization-signature` for one request.

    P-256 over SHA-256 of the canonical payload, DER-encoded, base64. An empty
    body serialises as "" rather than {} — a quirk of Privy's SDK that has to be
    matched exactly or every signature is silently rejected.
    """
    payload = {
        "version": 1,
        "method": method,
        "url": url,
        "body": "" if isinstance(body, dict) and not body else body,
        "headers": {"privy-app-id": app_id},
    }
    key = serialization.load_der_private_key(
        base64.b64decode(private_key_b64), password=None)
    sig = key.sign(_canonical(payload), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode()


def _headers(method=None, url=None, body=None, auth_key=None):
    tok = base64.b64encode(
        f"{C.PRIVY_APP_ID}:{C.PRIVY_APP_SECRET}".encode()).decode()
    h = {"Authorization": f"Basic {tok}",
         "privy-app-id": C.PRIVY_APP_ID,
         "Content-Type": "application/json"}
    if auth_key:
        h["privy-authorization-signature"] = sign_request(
            method, url, body if body is not None else {}, auth_key, C.PRIVY_APP_ID)
    return h


async def _call(s, method, path, body=None, auth_key=None):
    """One Privy API call. Returns (json, error_code)."""
    url = API + path
    try:
        async with s.request(method, url, json=body,
                             headers=_headers(method, url, body, auth_key),
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
            d = await r.json()
            return (d, None) if r.status == 200 else (d, d.get("code") or f"http_{r.status}")
    except Exception as e:
        return {}, f"unreachable:{type(e).__name__}"


# ── policy ──────────────────────────────────────────────────────────────────
def swap_policy_rules(wsol_account):
    """What our key is allowed to do with a customer's wallet.

    Two rules, and the split matters. The first permits the programs a swap
    touches, with the System Program left out. The second permits exactly one
    kind of System transfer: into this wallet's own WSOL account, so a plain SOL
    deposit can be wrapped and traded.

    Conditions are ANDed across every instruction in a transaction, so a drain
    smuggled in alongside a genuine wrap fails the whole thing. That's the case
    worth being sure about, and it's covered in test_privy.py.
    """
    rules = []
    for m in ("signTransaction", "signAndSendTransaction"):
        rules.append({
            "name": f"swap programs ({m})", "method": m, "action": "ALLOW",
            "conditions": [{"field_source": "solana_program_instruction",
                            "field": "programId", "operator": "in",
                            "value": SWAP_PROGRAMS}]})
        rules.append({
            "name": f"wrap into own WSOL only ({m})", "method": m, "action": "ALLOW",
            "conditions": [
                {"field_source": "solana_system_program_instruction",
                 "field": "instructionName", "operator": "eq", "value": "Transfer"},
                {"field_source": "solana_system_program_instruction",
                 "field": "Transfer.to", "operator": "eq", "value": wsol_account}]})
    return rules


class PrivyWallet:
    """A customer's wallet, as seen from our side: signable, not spendable."""

    kind = "privy"
    wrap_sol = False       # we build swaps unwrapped; see the module docstring

    def __init__(self, wallet_id, address, auth_key=None, policy_id=None):
        self.wallet_id = wallet_id
        self.address = address
        self.auth_key = auth_key or C.PRIVY_AUTH_PRIVATE_KEY
        # Kept so the guarantee can be re-checked against the real policy rather
        # than assumed. A test that can't name the policy can't test it.
        self.policy_id = policy_id

    def __repr__(self):
        return f"PrivyWallet({self.address[:8]}…, id={self.wallet_id[:8]}…)"

    @classmethod
    async def load(cls, s, wallet_id):
        d, err = await _call(s, "GET", f"/v1/wallets/{wallet_id}")
        if err:
            raise RuntimeError(f"privy load failed: {err} {str(d)[:160]}")
        return cls(d["id"], d["address"])

    async def sign(self, s, tx_b64):
        """Sign a transaction. Returns (signed_b64, error).

        A policy refusal is an expected outcome, not an exception: it means this
        trade doesn't happen, and retrying would only fail identically.
        """
        body = {"method": "signTransaction",
                "params": {"transaction": tx_b64, "encoding": "base64"}}
        d, err = await _call(s, "POST", f"/v1/wallets/{self.wallet_id}/rpc",
                             body, auth_key=self.auth_key)
        if err:
            if err == "policy_violation":
                # Either we've started building swaps differently, or something
                # tried to sign what isn't a swap. Both are worth seeing.
                print("PRIVY POLICY REFUSED a transaction — the guardrail "
                      "working. Nothing was signed.", flush=True)
            return None, f"privy_{err}"
        signed = (d.get("data") or {}).get("signed_transaction")
        return (signed, None) if signed else (None, "privy_no_signature")


async def provision(s, owner, bot_public_key=None, bot_private_key=None):
    """Create a wallet the customer owns and we can only trade.

    `owner` is how the customer is identified to Privy, and there are two forms.
    In production it's {"user_id": "did:privy:…"} — they signed in with an email
    and Privy holds their key against that login, so losing a device means
    logging in again rather than losing the wallet. {"public_key": …} is the
    raw-key form, used by the tests.

    Ordering is forced by a chicken-and-egg: the wrap rule has to name the
    wallet's WSOL account, which can't be derived until the wallet exists. So we
    hold ownership just long enough to finish configuring, then hand both the
    wallet and its policy to the customer's key and lose the ability to change
    either. The wallet is empty for that whole window.

    Returns (PrivyWallet, error).
    """
    import jupiter as J          # local import: jupiter imports this module

    if isinstance(owner, str):
        owner = ({"user_id": owner} if owner.startswith("did:privy:")
                 else {"public_key": owner})
    bot_public_key = bot_public_key or C.PRIVY_AUTH_PUBLIC_KEY
    bot_private_key = bot_private_key or C.PRIVY_AUTH_PRIVATE_KEY
    if not (owner and bot_public_key and bot_private_key):
        # Without our private key we can't authorise steps 4-6, and the wallet
        # would be left half-configured and owned by nobody useful.
        return None, "missing_keys"

    # 1. our signing key, as a quorum — additional_signers takes an id, not a key
    q, err = await _call(s, "POST", "/v1/key_quorums",
                         {"public_keys": [bot_public_key],
                          "authorization_threshold": 1,
                          "display_name": "edgelvl bot"})
    if err:
        return None, f"quorum:{err}"

    # 2. a placeholder policy, owned by us for now so we can finish setting up
    pol, err = await _call(s, "POST", "/v1/policies",
                           {"version": "1.0", "name": "edgelvl: swaps only",
                            "chain_type": "solana",
                            "owner": {"public_key": bot_public_key},
                            "rules": swap_policy_rules(
                                "So11111111111111111111111111111111111111112")})
    if err:
        return None, f"policy:{err}"

    # 3. the wallet, with us as a restricted extra signer
    w, err = await _call(s, "POST", "/v1/wallets",
                         {"chain_type": "solana",
                          "owner": {"public_key": bot_public_key},
                          "additional_signers": [
                              {"signer_id": q["id"],
                               "override_policy_ids": [pol["id"]]}]})
    if err:
        return None, f"wallet:{err}"

    # 4. now the address exists, point the wrap rule at its real WSOL account
    wsol = J.ata(w["address"], J.WSOL)
    _, err = await _call(s, "PATCH", f"/v1/policies/{pol['id']}",
                         {"rules": swap_policy_rules(wsol)},
                         auth_key=bot_private_key)
    if err:
        return None, f"policy_rules:{err}"

    # 5. hand both to the customer. After this we cannot change either, which is
    #    the entire point — so it goes last, and a failure here leaves a wallet
    #    we still control rather than one nobody does.
    _, err = await _call(s, "PATCH", f"/v1/policies/{pol['id']}",
                         {"owner": owner}, auth_key=bot_private_key)
    if err:
        return None, f"policy_handover:{err}"
    _, err = await _call(s, "PATCH", f"/v1/wallets/{w['id']}",
                         {"owner": owner}, auth_key=bot_private_key)
    if err:
        return None, f"wallet_handover:{err}"

    return PrivyWallet(w["id"], w["address"], policy_id=pol["id"]), None


class LocalWallet:
    """The original signer: a private key in the bot's own environment.

    Still here because it's what the self-hosted repo uses — you run the bot,
    you hold your key, nobody else is involved. Privy is for the hosted
    terminal, where holding a customer's key is the thing we refuse to do.
    """

    kind = "local"
    wrap_sol = True        # a local key can wrap freely; there's no policy to satisfy

    def __init__(self, keypair):
        self.kp = keypair
        self.address = str(keypair.pubkey())

    def __repr__(self):
        return f"LocalWallet({self.address[:8]}…)"

    async def sign(self, s, tx_b64):
        from solders.transaction import VersionedTransaction
        try:
            raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
            signed = VersionedTransaction(raw.message, [self.kp])
            return base64.b64encode(bytes(signed)).decode(), None
        except Exception as e:
            return None, f"sign_failed:{type(e).__name__}"


def build(keypair=None):
    """Pick a signer from the environment.

    Privy wins when configured, because that's a deliberate choice; a stray
    PRIVATE_KEY left in a .env shouldn't quietly take over.
    """
    if C.PRIVY_APP_ID and C.PRIVY_APP_SECRET and C.PRIVY_WALLET_ID:
        return PrivyWallet(C.PRIVY_WALLET_ID, C.PRIVY_WALLET_ADDRESS)
    if keypair is not None:
        return LocalWallet(keypair)
    return None
