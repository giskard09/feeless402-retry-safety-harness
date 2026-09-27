"""
Feeless402Backend — SettlementBackend implementation that drives Feeless402's
real merchant route (GET /premium in nano_pay/server.py, commit c82e72a /
tag v0.2.9) through FastAPI's TestClient, over FakeNanoNode.

Adapted from the 0.2.8 (a7a8154) version of this file for the 0.2.9 API:
settled_replay() gained requester/proof/method/path kwargs and server.py's
route now passes X-PAYMENT-PROOF / a spoofable X-Real-IP-derived requester
id. env.present() below gained optional `proof` and `requester_ip` kwargs
so the battery can exercise both paths; every other line is unchanged from
the 0.2.8 version.

Real (Feeless402's code, imported unmodified):
  server.create_app()/premium route, _extract_block, settled_replay,
  verify_block, settle_block, block_hash, build_payment_header, parse_quote,
  pick_nano_offer.
Simulated:
  - the Nano node (fake_nano_node.py);
  - the HTTP hop between client and merchant (TestClient; transport faults
    are applied to the reply AFTER the merchant has fully handled the
    request, i.e. the merchant did its work and the client never heard);
  - the payer's signing is nanopy with a zero work value, as in
    Feeless402's own tests/test_payment_path.py (verify_block checks work
    format only; difficulty is enforced by the real network, not modelled).

Setup requirement: FEELESS402_PATH points at a checkout of
github.com/Feeless402/feeless402 at c82e72a, with `nanopy`, `fastapi`,
`httpx`, `anyio`, `requests` installed. NANO_PAY_HOME is forced to a temp
dir so Feeless402's wallets never touch ~/.nano-pay.

Nano-specific ledger semantics that matter for the result:
  one signed block can land at most once (the node answers "Old block"),
  so re-presenting the SAME authorization can never charge twice. A second
  charge needs a second block, i.e. a client that signs again. ledger_len
  counts confirmed sends payer -> merchant treasury in FakeNanoNode.
"""

import base64
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_F402 = os.environ.get("FEELESS402_PATH")
if not _F402:
    raise SystemExit("set FEELESS402_PATH to a feeless402 checkout at c82e72a")
sys.path.insert(0, _F402)
os.environ["NANO_PAY_HOME"] = tempfile.mkdtemp(prefix="f402home029-")

import nanopy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from nano_pay import server, verify  # noqa: E402
from nano_pay.x402 import build_payment_header, parse_quote, pick_nano_offer, replay_proof  # noqa: E402
from nano_pay.verify import block_hash  # noqa: E402

from retry_safety_backend import (  # noqa: E402
    Challenge402,
    ReconcileUnavailable,
    ServerError,
    SettlementBackend,
    TimeoutError_,
)
from fake_nano_node import FakeNanoNode, REP  # noqa: E402

NET = nanopy.Network()
PAYER_SEED = "7" * 64          # test-only, same seed style as Feeless402's own tests
GENESIS_FRONTIER = "A" * 64
PAYER_BALANCE = 10**30

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = TestClient(server.create_app(), raise_server_exceptions=False)
    return _APP


class Env:
    """One fresh world: empty ledger, empty merchant replay/seen state."""

    def __init__(self, payer_index=0):
        self.node = FakeNanoNode()
        server.rpc = self.node            # the name every paid route resolves at call time
        verify._replays.clear()
        verify._seen_previous.clear()
        self.client = _app()
        self.treasury = server.server_wallet.address
        self.payer = nanopy.Account(sk=nanopy.deterministic_key(PAYER_SEED, payer_index))
        self.node.open_account(self.payer.addr, GENESIS_FRONTIER, PAYER_BALANCE)
        q = self.client.get("/premium")
        assert q.status_code == 402, q.status_code
        self.quote = parse_quote(q)
        self.offer = pick_nano_offer(self.quote)
        self.amount = int(self.offer["amount"])

    def sign_new_block(self):
        """Sign a send from the payer's CURRENT ledger frontier."""
        info = self.node.account_info(self.payer.addr)
        acct = nanopy.Account(sk=nanopy.deterministic_key(PAYER_SEED, 0))
        acct.frontier, acct.raw_bal = info["frontier"], int(info["balance"])
        acct.rep = nanopy.Account(addr=REP)
        return acct.send(nanopy.Account(addr=self.treasury), self.amount,
                         work="0000000000000000").dict_

    def present(self, block, proof=None, requester_ip=None):
        """Present a block. `proof` sets X-PAYMENT-PROOF (payer-signature
        replay proof, real nano_pay.x402.replay_proof output). `requester_ip`
        sets X-Forwarded-For — real server.py's _client_ip() reads it
        unconditionally, no trusted-proxy check, so this also doubles as the
        harness's tool for testing whether that field is spoofable."""
        hdr = build_payment_header(self.quote, self.offer, block, self.treasury)
        headers = {"PAYMENT-SIGNATURE": hdr, "X-PAYMENT": hdr}
        if proof is not None:
            headers["X-PAYMENT-PROOF"] = proof
        if requester_ip is not None:
            headers["X-Forwarded-For"] = requester_ip
        return self.client.get("/premium", headers=headers)

    def payer_proof(self, block, method="GET", path="/premium"):
        """Real proof a genuine payer's 0.2.9 client would attach for this block."""
        return replay_proof(self.payer, block_hash(block), method, path)

    def ledger_len(self):
        return len(self.node.sends_to(self.treasury, self.payer.addr))


class Feeless402Backend(SettlementBackend):
    """Battery modes mapped to faults that exist on this rail.

    | battery mode          | fault injected (never inside Feeless402's code)          |
    |-----------------------|-----------------------------------------------------------|
    | clean                 | none                                                      |
    | declared_safe         | none; the runner then re-presents the same block          |
    | accept_then_timeout   | node applies the block, its RPC reply to the merchant is lost |
    | 5xx_after_settle      | merchant settles and answers 200; client receives a 500   |
    | slow_answer           | merchant settles and answers 200; client times out        |
    | reconcile_unavailable | as slow_answer, client holds the hash; first ledger query fails |
    | double_402            | as slow_answer; client re-presents 14 min later           |
    | late_restart (extra)  | as slow_answer; client re-presents 25h later (now beyond the 24h window) |
    | late_restart_with_ref | as late_restart, client holds the hash and reconciles     |

    double_402 note: on Nano the merchant can only answer a correct client's
    re-presented, valid block with a fresh 402 once the block has already
    landed (its frontier moved) and settled_replay declines it. So "reissue
    instead of resume" on this rail is exactly settled_replay's refusal;
    the 14-minute case checks it resumes inside the (now 24h) window, and
    the edge runs in run_feeless402.py probe past it.
    """

    def __init__(self, env, mode):
        self.env = env
        self.mode = mode
        self.blocks = {}          # authorization["signature"] -> signed block
        self.settle_calls = 0
        self.reconcile_calls = 0
        self.served = 0           # 200s actually delivered to this client
        self.statuses = []

    @property
    def ledger(self):
        return self.env.node.sends_to(self.env.treasury, self.env.payer.addr)

    def block_for(self, authorization):
        key = authorization["signature"]
        if key not in self.blocks:
            self.blocks[key] = self.env.sign_new_block()
        return self.blocks[key]

    def settle(self, authorization):
        self.settle_calls += 1
        block = self.block_for(authorization)
        h = block_hash(block)
        first = self.settle_calls == 1
        node = self.env.node

        if self.mode == "accept_then_timeout" and first:
            node.lose_process_reply = 1

        r = self.env.present(block)
        self.statuses.append(r.status_code)

        if first and self.mode in ("5xx_after_settle", "slow_answer", "reconcile_unavailable",
                                   "double_402", "late_restart", "late_restart_with_ref"):
            if self.mode == "5xx_after_settle":
                raise ServerError(f"502 from proxy (merchant answered {r.status_code})")
            if self.mode == "reconcile_unavailable":
                node.block_info_unavailable = 1
                raise TimeoutError_("reply lost", broadcast_ref=h)
            if self.mode == "double_402":
                node.age(h, 14 * 60)
            if self.mode == "late_restart":          # extra row, not one of the 7
                node.age(h, 25 * 3600)
            if self.mode == "late_restart_with_ref":  # extra row, not one of the 7
                node.age(h, 25 * 3600)
                raise TimeoutError_("reply lost", broadcast_ref=h)
            raise TimeoutError_("reply lost")

        if r.status_code == 200:
            self.served += 1
            receipt = json.loads(base64.b64decode(r.headers["payment-response"]))
            return {"verdict": "settled", "transaction_ref": receipt["hash"],
                    "declared_safe": bool(receipt.get("replay"))}
        if r.status_code == 402:
            raise Challenge402(r.json().get("error", "402"))
        raise ServerError(f"HTTP {r.status_code}")

    def reconcile(self, ref):
        """Out-of-band: ask the ledger (Feeless402's own source of truth)."""
        self.reconcile_calls += 1
        try:
            info = self.env.node.call({"action": "block_info", "json_block": "true", "hash": ref})
        except Exception as e:
            if "not found" in str(e).lower():
                return {"verdict": "unknown"}
            raise ReconcileUnavailable(str(e))
        if str(info.get("confirmed")).lower() == "true":
            return {"verdict": "settled", "transaction_ref": ref.upper()}
        return {"verdict": "unknown"}
