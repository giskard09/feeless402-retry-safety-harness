"""
settlement-retry-safety-v1 against Feeless402 v0.2.9 (c82e72a) — independent
re-verification of the three fixes glennquinting self-reported for
GHSA-cx37-j5vc-c967 (client double-charge on retry; 15-min replay window;
observer exhausting the 3-honors cap), plus a check for whether either new
mechanism (X-PAYMENT-PROOF, per-requester anonymous bucket) opens a new gap.

    FEELESS402_PATH=/path/to/feeless402-at-c82e72a python3 run_feeless402.py

Outcome classes per run:
  SAFE             ledger_len == 1 and the client was served the resource
  PAID_NOT_SERVED  ledger_len == 1, client never served (money moved, no resource)
  DOUBLE_CHARGE    ledger_len >= 2 for one purchase
See feeless402_backend.py for what is real and what is simulated.
"""

import base64
import json
import os
import sys
from unittest import mock

from feeless402_backend import Env, Feeless402Backend, server, verify  # noqa: F401  (sets sys.path)
from execute_payment import execute_payment
from pending_settlement_store import PendingSettlementStore, SETTLED
from retry_safety_backend import Challenge402, ServerError, TimeoutError_
from nano_pay.verify import block_hash

MAX_ROUNDS = 5
MODES = ["clean", "declared_safe", "accept_then_timeout", "5xx_after_settle",
         "slow_answer", "reconcile_unavailable", "double_402"]
EXTRA = ["late_restart", "late_restart_with_ref"]   # beyond the (now 24h) replay window


def classify(ledger_len, served):
    if ledger_len >= 2:
        return "DOUBLE_CHARGE"
    if ledger_len == 1 and served:
        return "SAFE"
    if ledger_len == 1:
        return "PAID_NOT_SERVED"
    return "NOT_PAID"


# --------------------------------------------------------------------------
# 1. battery: our correct client (execute_payment) vs. Feeless402's merchant
#    -- unchanged from the 0.2.8 run: exercises settled_replay/verify_block/
#    settle_block on the merchant side, which is where fix #2/#3 landed.
# --------------------------------------------------------------------------
def run_mode(mode):
    env = Env()
    be = Feeless402Backend(env, mode)
    store = PendingSettlementStore()
    key = f"idem-{mode}"
    rec, error = None, None
    for _ in range(MAX_ROUNDS):
        try:
            rec = execute_payment(store, be, key, resource="x402:feeless402/premium")
        except Challenge402 as e:
            error = f"Challenge402 twice: {e}"
            break
        if rec["verdict"] == SETTLED:
            break
    if mode == "declared_safe":
        rec2 = execute_payment(store, be, key, resource="x402:feeless402/premium")
        assert rec2["transaction_ref"] == rec["transaction_ref"]
    followup = None
    if be.served == 0 and be.blocks:
        followup = env.present(next(iter(be.blocks.values()))).status_code
        be.served += followup == 200
    if mode == "declared_safe":
        dup = env.present(next(iter(be.blocks.values()))).status_code
        assert dup == 200, dup
    return {"mode": mode, "verdict": rec["verdict"] if rec else None, "error": error,
            "ledger_len": env.ledger_len(), "served": be.served,
            "settle_calls": be.settle_calls, "reconcile_calls": be.reconcile_calls,
            "statuses": be.statuses, "followup": followup,
            "outcome": classify(env.ledger_len(), be.served)}


def run_mutation_control(mode="slow_answer"):
    """Broken client: signs a NEW block every round (new authorization)."""
    env = Env()
    be = Feeless402Backend(env, mode)
    for n in range(1, MAX_ROUNDS + 1):
        try:
            if be.settle({"signature": f"sig-mutation-round{n}"})["verdict"] == "settled":
                break
        except (TimeoutError_, ServerError, Challenge402):
            pass
    return {"ledger_len": env.ledger_len(), "served": be.served,
            "outcome": classify(env.ledger_len(), be.served)}


# --------------------------------------------------------------------------
# 2. replay-bound edges: merchant paid & settled, client never heard.
#    Ages/caps updated for 0.2.9 (24h window, 3/requester + 30/hash anon,
#    100/hash proven). New: proof-aware and requester-spoofing edges.
# --------------------------------------------------------------------------
def _paid_and_lost(env):
    block = env.sign_new_block()
    r = env.present(block)             # merchant settles; the reply is "lost"
    assert r.status_code == 200 and env.ledger_len() == 1
    return block, block_hash(block)


def edge_window(age_s):
    env = Env()
    block, h = _paid_and_lost(env)
    env.node.age(h, age_s)
    return env.present(block).status_code


def edge_cap_anonymous_same_requester():
    """Four re-presentations, no proof, same (spoofed-default) requester id."""
    env = Env()
    block, _ = _paid_and_lost(env)
    return [env.present(block).status_code for _ in range(4)]


def edge_cap_proven():
    """Payer attaches a real X-PAYMENT-PROOF every time: does REPLAY_MAX_PROVEN
    (100) actually let it through past the anonymous REPLAY_MAX (3)?"""
    env = Env()
    block, _ = _paid_and_lost(env)
    proof = env.payer_proof(block)
    return [env.present(block, proof=proof).status_code for _ in range(5)]


def edge_third_party_exhausts_no_proof():
    """Same as the 0.2.8 edge: observer and payer both re-present with NO
    proof and the SAME (default) requester id -- the realistic case of a
    payer whose client does not send X-PAYMENT-PROOF (e.g. hand re-presenting
    per the old README instructions, or an older client)."""
    env = Env()
    block, h = _paid_and_lost(env)
    public = env.node.call({"action": "block_info", "json_block": "true", "hash": h})["contents"]
    observer = [env.present(dict(public)).status_code for _ in range(3)]
    payer = env.present(block).status_code
    return {"observer": observer, "payer_no_proof": payer, "ledger_len": env.ledger_len()}


def edge_third_party_exhausts_payer_proves():
    """Same attack, but the payer's re-presentation carries a real
    X-PAYMENT-PROOF (what the new 0.2.9 client actually sends on retry).
    This is the scenario the fix is supposed to close."""
    env = Env()
    block, h = _paid_and_lost(env)
    public = env.node.call({"action": "block_info", "json_block": "true", "hash": h})["contents"]
    observer = [env.present(dict(public)).status_code for _ in range(3)]
    proof = env.payer_proof(block)
    payer = env.present(block, proof=proof).status_code
    return {"observer": observer, "payer_with_proof": payer, "ledger_len": env.ledger_len()}


def edge_anon_total_cap_via_ip_spoofing():
    """An observer with no proof, rotating a spoofed X-Forwarded-For per
    request, against the 30-per-hash anonymous total cap (REPLAY_MAX_ANON_TOTAL).
    _client_ip() in server.py reads X-Forwarded-For unconditionally (no
    trusted-proxy check) -- this checks whether that lets an unauthenticated
    observer burn through the WHOLE anonymous budget for a hash, not just
    the 3-per-requester slice, and whether it can then block a real payer
    who also has no proof."""
    env = Env()
    block, h = _paid_and_lost(env)
    public = env.node.call({"action": "block_info", "json_block": "true", "hash": h})["contents"]
    statuses = []
    for i in range(31):                       # one past REPLAY_MAX_ANON_TOTAL
        statuses.append(env.present(dict(public), requester_ip=f"10.0.0.{i}").status_code)
    payer_after = env.present(block).status_code   # payer, no proof, default requester id
    return {"spoofed_200_count": statuses.count(200), "spoofed_402_count": statuses.count(402),
            "payer_no_proof_after": payer_after, "ledger_len": env.ledger_len()}


def edge_no_local_timestamp(age_s=25 * 3600):
    env = Env()
    block, h = _paid_and_lost(env)
    env.node.age(h, age_s)
    env.node.omit_local_timestamp = True
    return env.present(block).status_code


def edge_restart_resets_cap():
    env = Env()
    block, _ = _paid_and_lost(env)
    first3 = [env.present(block).status_code for _ in range(3)]
    verify._replays.clear()            # == merchant process restart (state is in-memory)
    return {"first3": first3, "after_restart": env.present(block).status_code}


# --------------------------------------------------------------------------
# 3. Feeless402's own client (request_with_payment) -- now journal-backed.
#    C2 is the key check: does a retry re-present the journaled block, or
#    still sign a new one?
# --------------------------------------------------------------------------
class _Transport:
    def __init__(self, env):
        self.env = env
        self.lose_paid_reply = False

    def __call__(self, method, url, headers=None, timeout=None, **kw):
        import requests
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        r = self.env.client.request(method, path, headers=headers or {})
        if self.lose_paid_reply and headers and "PAYMENT-SIGNATURE" in headers:
            raise requests.ConnectionError("connection reset by peer")
        return r


def client_leg():
    from nano_pay import x402
    from nano_pay.wallet import Wallet
    env = Env()
    wallet_path = os.path.join(os.environ["NANO_PAY_HOME"], "payer-wallet.json")
    # Hygiene: a stale pending-payments.json from an earlier call in this same
    # interpreter (NANO_PAY_HOME is set once at import time) would make C1
    # re-present a block from a previous run instead of signing a fresh one.
    journal_path = os.path.join(os.environ["NANO_PAY_HOME"], "pending-payments.json")
    if os.path.exists(journal_path):
        os.remove(journal_path)
    w = Wallet(wallet_path).create(seed="7" * 64)
    assert w.address == env.payer.addr
    t = _Transport(env)
    url = "http://merchant.test/premium"
    out = {}
    with mock.patch.object(x402.requests, "request", t), \
         mock.patch.object(Wallet, "_work_valid", staticmethod(lambda *a: True)):
        # C1: reply lost after the merchant settled
        t.lose_paid_reply = True
        try:
            x402.request_with_payment("GET", url, w, env.node, max_raw=env.amount)
            out["C1"] = "no exception"
        except x402.PaidRequestFailed as e:
            out["C1"] = {"settled": e.receipt["settled"], "ledger": e.receipt["ledger"],
                         "receipt_keys": sorted(e.receipt), "note": e.receipt["note"],
                         "will_re_present": e.receipt.get("will_re_present"),
                         "ledger_len": env.ledger_len()}
            lost_hash = e.receipt["block"]
        out["journal_written_after_C1"] = os.path.exists(journal_path)
        t.lose_paid_reply = False
        # C2: a caller that retries the same call (the only paying entry point).
        # THE key check: does the journal make this re-present the SAME block?
        r, rec = x402.request_with_payment("GET", url, w, env.node, max_raw=env.amount)
        out["C2"] = {"status": r.status_code, "new_block": rec["block"] != lost_hash,
                     "block_matches_journal": rec["block"] == lost_hash,
                     "ledger_len": env.ledger_len()}
        out["journal_cleared_after_C2"] = not os.path.exists(journal_path) or json.loads(
            open(journal_path).read() or "{}") == {}

    # C3: a caller that follows the OLD README ("re-present the same one" by
    # hand, no X-PAYMENT-PROOF -- what a pre-0.2.9 client, or a manual retry,
    # would do). Receipt carries only the hash; block fetched back from the
    # ledger and re-sent by hand; once inside the (now 24h) window, once
    # 1s after it.
    env = Env()
    block, h = _paid_and_lost(env)
    inside = env.present(env.node.call({"action": "block_info", "hash": h})["contents"]).status_code
    env.node.age(h, 24 * 3600 + 1)
    after = env.present(block).status_code
    settled_after, ledger_after = x402._settle_outcome(env.node, h, after)
    out["C3"] = {"inside_window": inside, "after_window": after,
                 "client_verdict_after_402": [settled_after, ledger_after],
                 "note_shown": x402._OUTCOME_NOTE[settled_after],
                 "ledger_len": env.ledger_len()}
    return out


def main():
    ok = True
    print("== 1. battery (execute_payment vs Feeless402 merchant, unchanged shape) ==")
    for m in MODES:
        r = run_mode(m)
        ok &= r["outcome"] == "SAFE"
        print(f"  {r['outcome']:<16} {m:<22} verdict={r['verdict']} ledger_len={r['ledger_len']} "
              f"served={r['served']} settle={r['settle_calls']} reconcile={r['reconcile_calls']} "
              f"statuses={r['statuses']} followup={r['followup']} err={r['error']}")
    print("  -- extra rows: client re-presents 25h later, past the 24h window --")
    for m in EXTRA:
        r = run_mode(m)
        print(f"  {r['outcome']:<16} {m:<22} verdict={r['verdict']} ledger_len={r['ledger_len']} "
              f"served={r['served']} settle={r['settle_calls']} reconcile={r['reconcile_calls']} "
              f"statuses={r['statuses']} followup={r['followup']} err={r['error']}")
    print("  -- merchant mutation: settled_replay disabled (pre-0.2.7 behaviour) --")
    with mock.patch.object(server, "settled_replay", lambda *a, **k: None):
        for m in ("5xx_after_settle", "slow_answer", "double_402"):
            r = run_mode(m)
            print(f"  {r['outcome']:<16} {m:<22} statuses={r['statuses']} followup={r['followup']} "
                  f"err={r['error']}")
            ok &= r["outcome"] != "SAFE"
    mc = run_mutation_control()
    disc = mc["outcome"] == "DOUBLE_CHARGE"
    ok &= disc
    print(f"  mutation control (new block per round, slow_answer): {mc} -> "
          f"{'discriminates' if disc else 'DOES NOT DISCRIMINATE'}")

    print("\n== 2. replay-bound edges (paid, settled, reply lost) ==")
    edges = {
        "E1a re-present at 23:59:59h": edge_window(24 * 3600 - 1),
        "E1b re-present at 24:00:01h": edge_window(24 * 3600 + 1),
        "E2 four re-presentations, no proof, same requester": edge_cap_anonymous_same_requester(),
        "E2p five re-presentations WITH proof (cap should be 100, not 3)": edge_cap_proven(),
        "E3a observer(no proof) exhausts 3, payer ALSO no proof": edge_third_party_exhausts_no_proof(),
        "E3b observer(no proof) exhausts 3, payer PROVES (the real 0.2.9 client path)":
            edge_third_party_exhausts_payer_proves(),
        "E3c observer spoofs 31 X-Forwarded-For values (no proof) vs 30-cap":
            edge_anon_total_cap_via_ip_spoofing(),
        "E4 no local_timestamp, 25h old (was the 0.2.8 window-skip bug)": edge_no_local_timestamp(),
        "E5 merchant restart resets cap (in-memory _replays, unchanged limit)": edge_restart_resets_cap(),
    }
    for k, v in edges.items():
        print(f"  {k:<62} {v}")

    print("\n== 3. Feeless402 client (request_with_payment, now journal-backed) ==")
    for k, v in client_leg().items():
        print(f"  {k}: {json.dumps(v)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
