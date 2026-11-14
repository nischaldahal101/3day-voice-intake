"""Stage E2: Ferguson cart inbox (carts_panel.py + the /inbound/cart and /carts
routes). Offline: no network, no Postgres, no R2, no Clerk (reuses the env scrub,
network block and module stubs from test_fieldpulse_sync; an in-memory FakeCartDB
with jobs.py's cart rules). All data is MADE UP. The real SQL is in
test_carts_db.py.

Run: /tmp/vi-f88/bin/python -m unittest test_carts
"""

import base64
import json
import os
import unittest
from contextlib import ExitStack
from unittest import mock

import test_fieldpulse_sync  # noqa: F401  (env scrub, network block, stubs)

import observability  # noqa: E402
import ferguson_cart  # noqa: E402
from test_ferguson_cart import cart_html, direct_email, gmail_forward  # noqa: E402

# carts_panel (and through it admin_panel / orchestrator) is imported when the tests
# RUN, not when this file is loaded: test_customer_source replaces sys.modules["jobs"]
# at load time, and importing them earlier would bind the other suites' modules to a
# stale stub (order-dependent failures in test_admin_panel / test_documents).
cp = None


def _load_panel():
    global cp
    if cp is None:
        import carts_panel
        cp = carts_panel
        # The stub storage module needs the cart names (used at call time).
        for n, v in (("CART_PREFIX", "ferguson-carts/"), ("put_bytes", lambda *a, **k: None)):
            if not hasattr(cp.storage, n):
                setattr(cp.storage, n, v)
    return cp

SECRET = "made-up-inbound-secret"
USER_A, USER_B = "user_madeupA", "user_madeupB"
PROFILES = {
    USER_A: {"first_name": "Pat", "primary_email": "pat@example.invalid",
             "emails": ["pat@example.invalid", "pat.work@example.invalid"]},
    USER_B: {"first_name": "Sam", "primary_email": "sam@example.invalid",
             "emails": ["sam@example.invalid"]},
}

def ar(spf=None, dkim=None, dmarc=None, servid="mx.cloudflare.net"):
    parts = [servid]
    if spf:
        parts.append(f"spf={spf[0]} (made-up comment; x=y) smtp.mailfrom={spf[1]}")
    if dkim:
        parts.append(f"dkim={dkim[0]} header.d={dkim[1]} header.s=s1 header.b=AbC")
    if dmarc:
        parts.append(f"dmarc={dmarc[0]} header.from={dmarc[1]}")
    return "Authentication-Results: " + ";\r\n\t".join(parts) + "\r\n"


def with_headers(raw, *headers):
    """Prepend headers (as our receiver would, topmost first)."""
    return "".join(headers).encode() + raw


FERGUSON_RAW = direct_email(cart_html(project="Testproject"))
FORWARD_RAW = gmail_forward(cart_html(project="Testproject"))
FERGUSON_AR = ar(dkim=("pass", "fergusonhome.com"), dmarc=("pass", "fergusonhome.com"))
FERGUSON_OK = with_headers(FERGUSON_RAW, FERGUSON_AR)
FORWARD_OK = with_headers(FORWARD_RAW, ar(spf=("pass", "pat@example.invalid")))


class FakeCartDB:
    """jobs.py's cart rules in memory: one alias per user, unique aliases, one cart per
    (user, raw_sha256), everything scoped by user."""

    def __init__(self):
        self.aliases = {}       # user -> alias
        self.carts = {}         # id -> row
        self.n = 0

    def get_cart_alias(self, user_id):
        return self.aliases.get(user_id)

    def ensure_cart_alias(self, user_id, candidates):
        if user_id in self.aliases:
            return self.aliases[user_id]
        for a in candidates:
            if a not in self.aliases.values():
                self.aliases[user_id] = a
                return a
        raise cp.jobs.JobError("No free cart address alias")

    def get_cart_alias_user(self, alias):
        return next((u for u, a in self.aliases.items() if a == alias), None)

    def _live(self, c):
        """jobs._LIVE_SUPERSEDED: a FINAL supersede (R4: the newer cart really wrote) counts
        while the cart is linked/sent to the customer it was finalized on; otherwise the
        stored superseded_by counts only while that cart exists for the same user and both
        are linked/sent to the same customer."""
        final = bool(c.get("superseded_by") and c.get("superseded_final_customer")
                     and c["superseded_final_customer"] == c["fieldpulse_customer_id"]
                     and c["status"] in ("linked", "sent"))
        if final:
            return {**c, "superseded_final": True}
        n = self.carts.get(c.get("superseded_by") or "")
        ok = (n is not None and n["id"] != c["id"] and n["clerk_user_id"] == c["clerk_user_id"]
              and n["status"] in ("linked", "sent") and c["status"] in ("linked", "sent")
              and n["fieldpulse_customer_id"] == c["fieldpulse_customer_id"])
        return {**c, "superseded_by": n["id"] if ok else None, "superseded_final": False}

    def find_cart_by_sha(self, user_id, sha):
        return next((self._live(c) for c in self.carts.values()
                     if c["clerk_user_id"] == user_id and c["raw_sha256"] == sha), None)

    def create_ferguson_cart(self, user_id, alias, sender, message_id, raw_key, raw_sha256,
                             parsed, ok, warnings, project_name):
        found = self.find_cart_by_sha(user_id, raw_sha256)
        if found:
            return found, False
        self.n += 1
        row = {"id": f"cart_{self.n:012x}", "clerk_user_id": user_id, "alias": alias,
               "received_at": f"2026-11-11T10:{self.n:02d}:00+00:00", "sender": sender,
               "message_id": message_id, "raw_key": raw_key, "raw_sha256": raw_sha256,
               "parsed": json.loads(json.dumps(parsed)), "ok": ok,
               "warnings": list(warnings), "project_name": project_name,
               "status": "needs_customer", "fieldpulse_customer_id": None,
               "customer": None, "customer_match": None, "fieldpulse": None,
               "fieldpulse_history": [], "superseded_by": None,
               "superseded_final_customer": None}
        self.carts[row["id"]] = row
        return dict(row), True

    def list_ferguson_carts(self, user_id, offset=0, limit=50):
        rows = sorted((c for c in self.carts.values() if c["clerk_user_id"] == user_id),
                      key=lambda c: c["received_at"], reverse=True)
        items = [{"id": c["id"], "received_at": c["received_at"],
                  "project_name": c["project_name"], "ok": c["ok"], "status": c["status"],
                  "item_count": len(c["parsed"]["items"]),
                  "subtotal": c["parsed"].get("subtotal"),
                  "warning_count": len(c["warnings"]),
                  "customer_name": (c["customer"] or {}).get("display_name"),
                  "match_state": (c["customer_match"] or {}).get("state"),
                  "fieldpulse_status": (c["fieldpulse"] or {}).get("status"),
                  "fieldpulse_at": (c["fieldpulse"] or {}).get("at"),
                  "superseded_by": self._live(c)["superseded_by"]}
                 for c in rows[offset:offset + limit]]
        return {"items": items, "total": len(rows)}

    def get_ferguson_cart(self, user_id, cart_id):
        c = self.carts.get(cart_id)
        return self._live(c) if c and c["clerk_user_id"] == user_id else None

    def delete_ferguson_cart(self, user_id, cart_id):
        c = self.get_ferguson_cart(user_id, cart_id)
        if c:
            del self.carts[cart_id]
        return c

    def set_cart_customer(self, user_id, cart_id, customer, match, only_from):
        c = self.carts.get(cart_id)
        if not c or c["clerk_user_id"] != user_id or c["status"] not in only_from:
            return None
        c.update(status="linked" if customer else "needs_customer",
                 fieldpulse_customer_id=str(customer["id"]) if customer else None,
                 customer=json.loads(json.dumps(customer)) if customer else None,
                 customer_match=dict(match))
        return self._live(c)

    def set_cart_item_types(self, user_id, cart_id, parsed):
        c = self.carts.get(cart_id)
        if (not c or c["clerk_user_id"] != user_id or c["parsed"] is None
                or "item_types" in c["parsed"]):
            return False
        c["parsed"] = json.loads(json.dumps(parsed))
        return True

    # --- E4: jobs.py's cart_fieldpulse_slots rules (claim takes the lease and consumes the
    # resync flags; busy flags resync with the newest asking cart; mark makes the cart
    # 'sent' only while linked to that customer and, for a POST, sets create_pending +
    # clears the id in one write; finish always releases the lease) ---------------------
    slots = None
    busy_keys = None
    marks = None

    def _slot(self, key):
        if self.slots is None:
            self.slots, self.busy_keys, self.marks = {}, set(), []
        return self.slots.setdefault(key, {
            "clerk_user_id": key[0], "customer_id": key[1], "project_key": key[2],
            "cart_id": None, "estimate_id": None, "estimate_status": None,
            "estimate_cart_id": None, "estimate_create_pending": False, "list_id": None,
            "list_status": None, "list_cart_id": None, "list_create_pending": False,
            "held": False, "mode": None, "resync": False, "resync_manual": False,
            "resync_cart_id": None, "writer_cart_id": None, "writer_received_at": None})

    def claim_cart_slot(self, user_id, customer_id, project_key, cart_id, manual=False):
        key = (user_id, customer_id, project_key)
        row = self._slot(key)
        if key in self.busy_keys:
            old = self.carts.get(row["resync_cart_id"] or "")
            new = self.carts.get(cart_id)
            if old is None or (new and new["received_at"] >= old["received_at"]):
                row["resync_cart_id"] = cart_id
            row["resync"] = True
            row["resync_manual"] = row["resync_manual"] or bool(manual)
            return None
        self.busy_keys.add(key)
        out = dict(row)
        row.update(resync=False, resync_manual=False, resync_cart_id=None)
        return out

    def mark_cart_sending(self, user_id, cart_id, customer_id, project_key, target, post,
                          request, mode):
        c = self.carts.get(cart_id)
        self.marks.append((cart_id, target, request["method"]))
        if (not c or c["clerk_user_id"] != user_id or c["fieldpulse_customer_id"] != customer_id
                or c["status"] not in ("linked", "sent")):
            return False
        slot = self._slot((user_id, customer_id, project_key))
        w = slot.get("writer_cart_id")
        if w and w != cart_id and (slot["writer_received_at"], w) > (c["received_at"], cart_id):
            return False                 # R4: a newer cart already wrote to this slot
        c["status"] = "sent"
        c["fieldpulse"] = {**(c["fieldpulse"] or {}), "status": "sending", "mode": mode}
        slot.update(writer_cart_id=cart_id, writer_received_at=c["received_at"], mode=mode)
        if post:
            slot.update({f"{target}_create_pending": True, f"{target}_id": None,
                         f"{target}_status": "sending"})
        for o in self.carts.values():   # R4: older carts of the slot -> replaced for good
            if (o["id"] != cart_id and o["clerk_user_id"] == user_id
                    and o["fieldpulse_customer_id"] == customer_id
                    and o["status"] in ("linked", "sent")
                    and (o["received_at"], o["id"]) < (c["received_at"], cart_id)
                    and ferguson_cart.project_key(o["project_name"]) == project_key):
                o.update(superseded_by=cart_id, superseded_final_customer=customer_id)
        return True

    def set_cart_fieldpulse(self, user_id, cart_id, record, history_entry=None):
        c = self.carts.get(cart_id)
        if not c or c["clerk_user_id"] != user_id:
            return False
        c["fieldpulse"] = json.loads(json.dumps(record))
        if history_entry:
            c["fieldpulse_history"] = ([history_entry] + c["fieldpulse_history"])[:10]
        return True

    def finish_cart_slot(self, user_id, customer_id, project_key, updates, cart_id, record,
                         history_entry=None, supersede=None):
        key = (user_id, customer_id, project_key)
        row = self._slot(key)
        row.update(updates or {})
        self.busy_keys.discard(key)
        self.set_cart_fieldpulse(user_id, cart_id, record, history_entry)
        if supersede and len(supersede) > 2 and supersede[2] == "final":
            old = self.carts.get(supersede[0])
            if (old and old["clerk_user_id"] == user_id and supersede[0] != supersede[1]
                    and str(old.get("fieldpulse_customer_id")) == str(customer_id)
                    and old.get("status") in ("linked", "sent")):
                old.update(superseded_by=supersede[1], superseded_final_customer=customer_id)
        elif supersede:
            old, new = self.carts.get(supersede[0]), self.carts.get(supersede[1])
            if (old and old["clerk_user_id"] == user_id and supersede[0] != supersede[1]
                    and str(old.get("fieldpulse_customer_id")) == str(customer_id)
                    and old.get("status") in ("linked", "sent")
                    and new and new["clerk_user_id"] == user_id
                    and str(new.get("fieldpulse_customer_id")) == str(customer_id)
                    and new.get("status") in ("linked", "sent")):
                old["superseded_by"] = supersede[1]
        return row["resync"], row["resync_manual"], row["resync_cart_id"]

    def patched(self):
        stack = ExitStack()
        for name in ("get_cart_alias", "ensure_cart_alias", "get_cart_alias_user",
                     "find_cart_by_sha", "create_ferguson_cart", "list_ferguson_carts",
                     "get_ferguson_cart", "delete_ferguson_cart", "set_cart_customer",
                     "claim_cart_slot", "mark_cart_sending", "set_cart_fieldpulse",
                     "finish_cart_slot", "set_cart_item_types"):
            stack.enter_context(mock.patch.object(cp.jobs, name, getattr(self, name),
                                                  create=True))
        return stack


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load_panel()
        import orchestrator
        cls.orch = orchestrator
        cls.client = orchestrator.app.test_client()

    def setUp(self):
        cp._profiles.clear()
        self.db = FakeCartDB()
        self.db.aliases = {USER_A: "pat", USER_B: "sam"}
        self.puts, self.removed = [], []
        self.profile_calls = []

        def load(uid):
            self.profile_calls.append(uid)
            if uid not in PROFILES:
                raise cp.ProfileError("UserNotFound")
            return PROFILES[uid]
        st = ExitStack()
        st.enter_context(self.db.patched())
        st.enter_context(mock.patch.object(cp, "_load_clerk_profile", load))
        st.enter_context(mock.patch.object(
            cp.storage, "put_bytes", lambda k, d, ct: self.puts.append((k, d, ct))))
        st.enter_context(mock.patch.object(cp.storage, "delete_audio",
                                           lambda k: self.removed.append(k)))
        self.queued = []
        st.enter_context(mock.patch.object(
            cp.queue_setup, "enqueue_cart_match",
            lambda u, c: self.queued.append((u, c)), create=True))
        self.sends = []                                   # E4 cart sends queued
        st.enter_context(mock.patch.object(
            cp.queue_setup, "enqueue_cart_send",
            lambda u, c, m=False: self.sends.append((u, c, m)), create=True))
        st.enter_context(mock.patch.object(
            cp.customer_source, "list_customers",
            side_effect=AssertionError("customer search outside a match test")))
        st.enter_context(mock.patch.dict(os.environ, {"INBOUND_EMAIL_SECRET": SECRET}))
        st.enter_context(mock.patch("requests.request", side_effect=AssertionError("no HTTP")))
        self.addCleanup(st.close)

    def unauthed(self):
        app = self.orch.app
        hooks = [f for f in app.before_request_funcs.get(None, [])
                 if f.__name__ != "_require_clerk_session"]
        return mock.patch.dict(app.before_request_funcs, {None: hooks})

    def inbound(self, raw, to="pat@voiceconsult.app", sender="support@fergusonhome.com",
                secret=SECRET, body=None):
        headers = {} if secret is None else {"X-Inbound-Secret": secret}
        if body is None:
            body = {"to": to, "from": sender, "raw_b64": base64.b64encode(raw).decode()}
        # /inbound/cart is public: the real Clerk hook stays installed and must skip it.
        r = self.client.post("/inbound/cart", json=body, headers=headers)
        return r.status_code, r.get_json()

    def as_user(self, user_id):
        return ExitStack() if user_id is None else _combined(
            self.unauthed(), mock.patch.object(self.orch, "_user_id", return_value=user_id))


def _combined(*cms):
    st = ExitStack()
    for c in cms:
        st.enter_context(c)
    return st


# --- aliases ---------------------------------------------------------------------------

class TestAliases(Base):
    def test_alias_base(self):
        f = cp.alias_base
        self.assertEqual(f("Pat"), "pat")
        self.assertEqual(f("José María"), "josemaria")
        self.assertEqual(f("  ..Mary--Ann..  "), "mary-ann")
        self.assertEqual(f("a..b"), "a.b")
        self.assertEqual(f("名前"), "")
        self.assertEqual(f(None), "")
        self.assertEqual(len(f("x" * 80)), 40)

    def test_candidates_reserved_fallback_and_suffixes(self):
        first = lambda *a: list(cp.alias_candidates(*a))[:3]   # noqa: E731
        self.assertEqual(first("Pat", "x@y.z"), ["pat", "pat2", "pat3"])
        self.assertEqual(first("Admin", None), ["admin2", "admin3", "admin4"])
        self.assertEqual(first("No-Reply", None)[0], "no-reply2")
        self.assertEqual(first(None, "Pat.Example+cart@example.invalid")[0],
                         "pat.examplecart")
        self.assertEqual(first("", ""), ["user", "user2", "user3"])
        long = list(cp.alias_candidates("y" * 60, None))
        self.assertTrue(all(cp.ALIAS_RE.match(a) for a in long))
        self.assertEqual(long[1], "y" * 39 + "2")
        for a in long:
            self.assertNotIn(a, cp.RESERVED_ALIASES)
        for word in cp.RESERVED_ALIASES:
            self.assertNotIn(word, list(cp.alias_candidates(word, word + "@x.y")))

    def test_address_created_once_with_collisions(self):
        self.db.aliases = {"user_other": "pat"}
        with mock.patch.dict(PROFILES, {"user_new": {"first_name": "Pat",
                                                     "primary_email": "p@x.invalid",
                                                     "emails": []}}):
            with self.as_user("user_new"):
                r = self.client.get("/carts/address")
                self.assertEqual((r.status_code, r.get_json()),
                                 (200, {"address": "pat2@voiceconsult.app"}))
                r = self.client.get("/carts/address")
                self.assertEqual(r.get_json(), {"address": "pat2@voiceconsult.app"})
        self.assertEqual(self.db.aliases["user_new"], "pat2")

    def test_domain_env(self):
        with mock.patch.dict(os.environ, {"INBOUND_EMAIL_DOMAIN": "Example.Invalid"}), \
                self.as_user(USER_A):
            r = self.client.get("/carts/address")
        self.assertEqual(r.get_json(), {"address": "pat@example.invalid"})

    def test_clerk_down_means_503_and_no_alias(self):
        with self.as_user("user_unknown"):
            r = self.client.get("/carts/address")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("user_unknown", self.db.aliases)


# --- auth ------------------------------------------------------------------------------

class TestRouteAuth(Base):
    def test_carts_routes_need_a_clerk_session(self):
        with mock.patch.object(cp, "jobs", side_effect=AssertionError("no auth")):
            for method, path in (("get", "/carts"), ("get", "/carts/address"),
                                 ("get", "/carts/cart_000000000001"),
                                 ("delete", "/carts/cart_000000000001")):
                for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
                    with self.subTest(path=path, headers=bool(headers)):
                        r = getattr(self.client, method)(path, headers=headers)
                        self.assertEqual(r.status_code, 401)
        self.assertIn("/inbound/cart", self.orch._PUBLIC_PATHS)
        for p in ("/carts", "/carts/address"):
            self.assertNotIn(p, self.orch._PUBLIC_PATHS)

    def test_no_user_id_is_401(self):
        with self.as_user(None), self.unauthed():
            self.assertEqual(self.client.get("/carts").status_code, 401)
            self.assertEqual(self.client.get("/carts/address").status_code, 401)


# --- inbound webhook ---------------------------------------------------------------------

class TestInboundSecret(Base):
    def test_secret_missing_wrong_right(self):
        for secret, want in ((None, 401), ("", 401), ("wrong", 401), (SECRET + "x", 401),
                             (SECRET.upper(), 401)):
            with self.subTest(secret=secret):
                code, _ = self.inbound(FERGUSON_OK, secret=secret)
                self.assertEqual(code, want)
        self.assertEqual((self.db.carts, self.puts), ({}, []))
        code, out = self.inbound(FERGUSON_OK)
        self.assertEqual((code, out["status"]), (202, "stored"))

    def test_env_unset_answers_503_and_accepts_nothing(self):
        for value in ("", "   "):
            with mock.patch.dict(os.environ, {"INBOUND_EMAIL_SECRET": value}):
                for secret in (SECRET, "", None):
                    code, _ = self.inbound(FERGUSON_OK, secret=secret)
                    self.assertEqual(code, 503)
        with mock.patch.dict(os.environ):
            os.environ.pop("INBOUND_EMAIL_SECRET")
            self.assertEqual(self.inbound(FERGUSON_OK)[0], 503)
        self.assertEqual((self.db.carts, self.puts), ({}, []))

    def test_secret_is_compared_in_constant_time(self):
        with mock.patch.object(cp.hmac, "compare_digest", wraps=cp.hmac.compare_digest) as cd:
            self.inbound(FERGUSON_OK, secret="nope")
        cd.assert_called_once()


class TestInboundBody(Base):
    def test_size_caps(self):
        read = mock.Mock(side_effect=AssertionError("body must not be read"))
        self.assertEqual(cp.receive_inbound(SECRET, cp.MAX_BODY_BYTES + 1, read)[0], 413)
        self.assertEqual(cp.receive_inbound(SECRET, None, read)[0], 411)
        with mock.patch.object(cp, "MAX_RAW_BYTES", 100):
            code, _ = self.inbound(b"x" * 101)
        self.assertEqual(code, 413)
        self.assertEqual((self.db.carts, self.puts), ({}, []))

    def test_bad_bodies(self):
        for body in ({}, {"to": "pat@voiceconsult.app"}, [], "x",
                     {"to": 1, "from": "a@b.c", "raw_b64": "eA=="},
                     {"to": "pat@voiceconsult.app", "from": "a@b.c", "raw_b64": "not base64!"},
                     {"to": "pat@voiceconsult.app", "from": "a@b.c", "raw_b64": ""}):
            with self.subTest(body=body):
                self.assertEqual(self.inbound(b"", body=body)[0], 400)
        code, _ = cp.receive_inbound(SECRET, 5, lambda: b"{nope")
        self.assertEqual(code, 400)
        self.assertEqual(self.db.carts, {})

    def test_unknown_alias_wrong_domain_bad_address_are_silent_drops(self):
        cases = (("nobody@voiceconsult.app", "unknown_alias"),
                 ("PAT@example.invalid", "wrong_domain"),
                 ("pat@voiceconsult.app.evil.invalid", "wrong_domain"),
                 ("p+at@voiceconsult.app", "unknown_alias"),
                 ("not an address", "bad_address"))
        for to, reason in cases:
            with self.subTest(to=to):
                self.assertEqual(self.inbound(FERGUSON_OK, to=to),
                                 (202, {"status": "dropped", "reason": reason}))
        self.assertEqual((self.db.carts, self.puts), ({}, []))

    def test_alias_and_addresses_are_case_insensitive(self):
        code, out = self.inbound(FERGUSON_OK, to="Pat <PAT@VoiceConsult.App>",
                                 sender="Support@FergusonHome.com")
        self.assertEqual((code, out["status"]), (202, "stored"))


class TestInboundSender(Base):
    def test_ferguson_needs_no_clerk_lookup(self):
        code, out = self.inbound(FERGUSON_OK)
        self.assertEqual((code, out["status"]), (202, "stored"))
        self.assertEqual(self.profile_calls, [])

    def test_users_own_verified_email(self):
        code, out = self.inbound(FORWARD_OK, sender="pat@example.invalid")
        self.assertEqual((code, out["status"]), (202, "stored"))
        cart = self.db.carts[out["cart_id"]]
        self.assertEqual((cart["clerk_user_id"], cart["alias"], cart["sender"]),
                         (USER_A, "pat", "pat@example.invalid"))
        # cached: a second forward does not ask Clerk again
        self.inbound(with_headers(FORWARD_RAW, ar(spf=("pass", "pat@example.invalid")),
                                  "X-Made-Up: 2\r\n"), sender="pat@example.invalid")
        self.assertEqual(self.profile_calls, [USER_A])

    def test_strangers_and_other_users_emails_are_dropped(self):
        for sender in ("stranger@example.invalid", "sam@example.invalid",
                       "support@fergusonhome.com.evil.invalid", "pat@example.invalid.evil"):
            with self.subTest(sender=sender):
                raw = with_headers(FORWARD_RAW, ar(spf=("pass", sender), dkim=("pass",
                                                   sender.split("@")[1])))
                self.assertEqual(self.inbound(raw, sender=sender),
                                 (202, {"status": "dropped", "reason": "sender_not_allowed"}))
        self.assertEqual(self.db.carts, {})

    def test_unverified_email_of_the_user_is_dropped(self):
        prof = dict(PROFILES[USER_A], emails=["pat.work@example.invalid"])
        with mock.patch.dict(PROFILES, {USER_A: prof}):
            code, out = self.inbound(FORWARD_OK, sender="pat@example.invalid")
        self.assertEqual(out, {"status": "dropped", "reason": "sender_not_allowed"})

    def forward_from(self, from_, header, sender="pat@example.invalid"):
        raw = gmail_forward(cart_html(project="Testproject"))
        raw = raw.replace(b"From: Pat Example <pat@example.invalid>", f"From: {from_}".encode(), 1)
        return self.inbound(with_headers(raw, header), sender=sender)

    def test_f108_same_domain_other_mailbox_replay_is_dropped(self):
        # A DKIM/DMARC-valid message from ANOTHER mailbox at the user's provider,
        # replayed with the victim's envelope address (SPF fails).
        replay = ar(spf=("fail", "pat@example.invalid"), dkim=("pass", "example.invalid"),
                    dmarc=("pass", "example.invalid"))
        for from_ in ("Attacker <attacker@example.invalid>", "sam@example.invalid",
                      "pat@example.invalid, attacker@example.invalid", ""):
            with self.subTest(from_=from_):
                self.assertEqual(self.forward_from(from_, replay),
                                 (202, {"status": "dropped", "reason": "auth_failed"}))
        self.assertEqual(self.db.carts, {})

    def test_f108_from_must_be_the_users_and_aligned(self):
        # Verified From + aligned DKIM/DMARC: accepted; the From mailbox is stored.
        ok = ar(spf=("fail", "pat@example.invalid"), dkim=("pass", "example.invalid"),
                dmarc=("pass", "example.invalid"))
        code, out = self.forward_from("Pat <PAT@example.invalid>", ok)
        self.assertEqual((code, out["status"]), (202, "stored"))
        self.assertEqual(self.db.carts[out["cart_id"]]["sender"], "pat@example.invalid")
        self.db.carts.clear()
        # The user's OTHER verified address in From with the envelope one: accepted.
        code, out = self.forward_from("pat.work@example.invalid",
                                      ar(spf=("pass", "pat@example.invalid")))
        self.assertEqual(out["status"], "stored")
        self.assertEqual(self.db.carts[out["cart_id"]]["sender"], "pat.work@example.invalid")
        self.db.carts.clear()
        # Authentication for a different domain than the From's: dropped.
        for header in (ar(dkim=("pass", "evil.invalid"), dmarc=("pass", "evil.invalid")),
                       ar(spf=("pass", "x@evil.invalid")),
                       # dkim passes but DMARC says the From domain failed
                       ar(dkim=("pass", "example.invalid"), dmarc=("fail", "example.invalid"))):
            with self.subTest(header=header):
                self.assertEqual(self.forward_from("pat@example.invalid", header),
                                 (202, {"status": "dropped", "reason": "auth_failed"}))
        # A verified From with a stranger envelope is still not allowed.
        self.assertEqual(self.forward_from("pat@example.invalid", ok,
                                           sender="stranger@example.invalid"),
                         (202, {"status": "dropped", "reason": "sender_not_allowed"}))
        self.assertEqual(self.db.carts, {})

    def test_clerk_failure_is_503_retry(self):
        def boom(uid):
            raise cp.ProfileError("ConnectError")
        with mock.patch.object(cp, "_load_clerk_profile", boom):
            code, out = self.inbound(FORWARD_OK, sender="pat@example.invalid")
        self.assertEqual((code, out["reason"]), (503, "user_lookup_failed"))
        self.assertEqual(self.db.carts, {})


class TestSenderAuthentication(Base):
    def check(self, raw, sender="pat@example.invalid", servid="mx.cloudflare.net"):
        import email.parser
        import email.policy
        h = email.parser.BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
        return cp.sender_authenticated(h, sender, servid)

    def test_pass_variants(self):
        for header in (ar(spf=("pass", "pat@example.invalid")),
                       ar(spf=("pass", "example.invalid")),
                       ar(dkim=("pass", "example.invalid")),
                       ar(dkim=("pass", "EXAMPLE.invalid")),
                       ar(dmarc=("pass", "example.invalid")),
                       ar(spf=("softfail", "pat@example.invalid"), dkim=("pass", "example.invalid")),
                       ar(spf=("pass", "pat@example.invalid"), servid="MX.Cloudflare.NET 1")):
            with self.subTest(header=header):
                self.assertTrue(self.check(with_headers(FORWARD_RAW, header)))
        # Ferguson via a sending subdomain: parent-domain DKIM counts.
        self.assertTrue(self.check(with_headers(FERGUSON_RAW, ar(dkim=("pass", "fergusonhome.com"))),
                                   sender="support@mail.fergusonhome.com"))

    def test_fail_variants(self):
        for header in ("",
                       ar(spf=("fail", "pat@example.invalid")),
                       ar(spf=("softfail", "pat@example.invalid"), dkim=("fail", "example.invalid")),
                       ar(spf=("pass", "other@evil.invalid")),
                       ar(dkim=("pass", "evil.invalid")),
                       ar(dkim=("pass", "invalid")),                  # a bare TLD never counts
                       ar(dkim=("pass", "sub.example.invalid")),      # child of the sender domain
                       ar(dmarc=("pass", "evil.invalid")),
                       ar(spf=("pass", "pat@example.invalid"), servid="mx.evil.invalid"),
                       "Authentication-Results: mx.cloudflare.net; none\r\n",
                       "Authentication-Results: garbage\r\n"):
            with self.subTest(header=header):
                self.assertFalse(self.check(with_headers(FORWARD_RAW, header)))
        self.assertFalse(self.check(FORWARD_OK, sender="nodomain"))

    def test_only_the_topmost_trusted_header_counts(self):
        ours = ar(spf=("fail", "pat@example.invalid"))
        forged = ar(spf=("pass", "pat@example.invalid"), dkim=("pass", "example.invalid"))
        self.assertFalse(self.check(with_headers(FORWARD_RAW, ours, "X-Made-Up: 1\r\n", forged)))
        # an untrusted id above ours is skipped, ours still decides
        self.assertTrue(self.check(with_headers(
            FORWARD_RAW, ar(spf=("fail", "x@y.invalid"), servid="other.invalid"),
            ar(spf=("pass", "pat@example.invalid")), ours)))

    def test_forged_lower_header_is_dropped_end_to_end(self):
        ours = ar(spf=("fail", "support@fergusonhome.com"))
        forged = ar(dkim=("pass", "fergusonhome.com"))
        code, out = self.inbound(with_headers(FERGUSON_RAW, ours, forged))
        self.assertEqual(out, {"status": "dropped", "reason": "auth_failed"})
        code, out = self.inbound(FERGUSON_RAW)                 # no header at all
        self.assertEqual(out, {"status": "dropped", "reason": "auth_failed"})
        self.assertEqual(self.db.carts, {})

    def test_authserv_id_env(self):
        raw = with_headers(FERGUSON_RAW, ar(dkim=("pass", "fergusonhome.com"),
                                             dmarc=("pass", "fergusonhome.com"),
                                             servid="mx.example.invalid"))
        self.assertEqual(self.inbound(raw)[1]["reason"], "auth_failed")
        with mock.patch.dict(os.environ, {"INBOUND_AUTHSERV_ID": "mx.example.invalid"}):
            self.assertEqual(self.inbound(raw)[1]["status"], "stored")

    def test_comment_cannot_smuggle_a_pass(self):
        h = ("Authentication-Results: mx.cloudflare.net; spf=fail (spf=pass "
             "smtp.mailfrom=pat@example.invalid) smtp.mailfrom=pat@example.invalid\r\n")
        self.assertFalse(self.check(with_headers(FORWARD_RAW, h)))


# The shape of a REAL Ferguson direct email's header (E5, 2026-11-12); hosts, IP, selector
# token and signature are made up.
REAL_AR = (
    "Authentication-Results: mx.cloudflare.net; dkim=pass header.d=fergusonhome.com "
    "header.s=s1 header.b=Ab/Cd+eF; dmarc={dmarc} header.from={hfrom} policy.dmarc=quarantine; "
    "spf=none (mx.cloudflare.net: no SPF records found for postmaster@o2.sgmail.example.com) "
    "smtp.helo=o2.sgmail.example.com; spf=pass (mx.cloudflare.net: domain of "
    "bounces+1234-abcd-test=voiceconsult.app@em9668.fergusonhome.com designates 192.0.2.1 as "
    "permitted sender) smtp.mailfrom=bounces+1234-abcd-test=voiceconsult.app@em9668.fergusonhome.com; "
    "arc=none smtp.remote-ip=192.0.2.1\r\n")
BOUNCE = "bounces+1234-abcd-test=voiceconsult.app@em9668.fergusonhome.com"


def real_ar(dmarc="pass", hfrom="fergusonhome.com"):
    return REAL_AR.format(dmarc=dmarc, hfrom=hfrom)


class TestFergusonDirect(Base):
    def direct(self, header=None, from_="Ferguson Home <support@fergusonhome.com>", extra=()):
        raw = with_headers(direct_email(cart_html(project="Testproject"), sender=from_),
                           real_ar() if header is None else header, *extra)
        return self.inbound(raw, sender=BOUNCE)

    def test_parser_reads_the_real_shape(self):
        servid, results = cp._parse_auth_results(real_ar()[len("Authentication-Results: "):])
        self.assertEqual(servid, "mx.cloudflare.net")
        self.assertEqual([(m, r) for m, r, _ in results],
                         [("dkim", "pass"), ("dmarc", "pass"), ("spf", "none"), ("spf", "pass"),
                          ("arc", "none")])
        self.assertEqual(results[0][2]["header.b"], "ab/cd+ef")
        self.assertEqual(results[1][2]["header.from"], "fergusonhome.com")
        self.assertEqual(results[3][2]["smtp.mailfrom"], BOUNCE)

    def test_real_direct_email_is_accepted_without_clerk(self):
        code, out = self.direct()
        self.assertEqual((code, out["status"]), (202, "stored"))
        self.assertEqual(self.db.carts[out["cart_id"]]["sender"], "support@fergusonhome.com")
        self.assertEqual(self.profile_calls, [])

    def test_from_with_display_name_variants(self):
        for from_ in ("support@fergusonhome.com", '"Ferguson, Home" <Support@FergusonHome.com>',
                      "Ferguson Home <orders@fergusonhome.com>"):
            with self.subTest(from_=from_):
                self.assertEqual(self.direct(from_=from_)[1]["status"], "stored")
                self.db.carts.clear()

    def test_dmarc_fail_or_other_domain_is_dropped(self):
        for header in (real_ar(dmarc="fail"), real_ar(dmarc="none"),
                       real_ar(hfrom="evil.example.invalid"),
                       real_ar(hfrom="em9668.fergusonhome.com"),
                       real_ar().replace("mx.cloudflare.net;", "mx.evil.invalid;", 1), ""):
            with self.subTest(header=header):
                self.assertEqual(self.direct(header),
                                 (202, {"status": "dropped", "reason": "auth_failed"}))
        self.assertEqual(self.db.carts, {})

    def test_forged_lower_header_does_not_count(self):
        out = self.direct(real_ar(dmarc="fail"), extra=("X-Made-Up: 1\r\n", real_ar()))
        self.assertEqual(out, (202, {"status": "dropped", "reason": "auth_failed"}))
        self.assertEqual(self.db.carts, {})

    def test_look_alike_from_domains_are_dropped(self):
        for dom in ("evil-fergusonhome.com", "fergusonhome.com.evil.com", "mail.fergusonhome.com"):
            with self.subTest(dom=dom):
                code, out = self.direct(real_ar(hfrom=dom), from_=f"Ferguson <support@{dom}>")
                self.assertEqual((code, out["status"]), (202, "dropped"))
                # header.from says fergusonhome.com but From is the look-alike: also dropped
                code, out = self.direct(from_=f"Ferguson <support@{dom}>")
                self.assertEqual((code, out["status"]), (202, "dropped"))
        self.assertEqual(self.db.carts, {})

    def test_two_from_addresses_or_headers_are_dropped(self):
        out = self.direct(from_="support@fergusonhome.com, other@example.invalid")
        self.assertEqual(out[1]["status"], "dropped")
        raw = with_headers(direct_email(cart_html(project="Testproject")),
                           real_ar(), "From: support@fergusonhome.com\r\n")
        self.assertEqual(self.inbound(raw, sender=BOUNCE)[1]["status"], "dropped")
        self.assertEqual(self.db.carts, {})

    def test_forward_from_verified_user_still_accepted(self):
        code, out = self.inbound(FORWARD_OK, sender="pat@example.invalid")
        self.assertEqual((code, out["status"]), (202, "stored"))
        self.assertEqual(self.db.carts[out["cart_id"]]["sender"], "pat@example.invalid")
        # a user's email that fails authentication
        raw = with_headers(FORWARD_RAW, ar(spf=("fail", "pat@example.invalid")))
        self.assertEqual(self.inbound(raw, sender="pat@example.invalid"),
                         (202, {"status": "dropped", "reason": "auth_failed"}))
        # a stranger envelope with a non-Ferguson From
        self.assertEqual(self.inbound(FORWARD_OK, sender="stranger@example.invalid"),
                         (202, {"status": "dropped", "reason": "sender_not_allowed"}))


class TestInboundStore(Base):
    def test_stored_cart_shape(self):
        code, out = self.inbound(FERGUSON_OK)
        self.assertEqual((code, out["status"]), (202, "stored"))
        cart = self.db.carts[out["cart_id"]]
        self.assertRegex(cart["id"], r"^cart_[0-9a-f]{12}$")
        self.assertEqual(len(self.puts), 1)
        key, data, ct = self.puts[0]
        self.assertRegex(key, r"^ferguson-carts/user_madeupA/[0-9a-f-]{36}\.eml$")
        self.assertEqual((data, ct, cart["raw_key"]), (FERGUSON_OK, "message/rfc822", key))
        p = cart["parsed"]
        self.assertTrue(cart["ok"], cart["warnings"])
        self.assertEqual((p["subtotal"], p["items"][0]["unit_price"], p["items"][1]["quantity"]),
                         ("1502.76", "1409.42", 2))     # Decimals stored as strings
        self.assertEqual((cart["project_name"], cart["status"], cart["warnings"]),
                         ("Testproject", "needs_customer", []))
        self.assertIsNone(cart["fieldpulse_customer_id"])

    def test_duplicate_forward_is_a_no_op(self):
        _, first = self.inbound(FERGUSON_OK)
        code, again = self.inbound(FERGUSON_OK)
        self.assertEqual((code, again), (202, {"status": "duplicate",
                                               "cart_id": first["cart_id"]}))
        self.assertEqual((len(self.db.carts), len(self.puts)), (1, 1))

    def test_duplicate_race_removes_the_second_upload(self):
        _, first = self.inbound(FERGUSON_OK)
        with mock.patch.object(cp.jobs, "find_cart_by_sha", return_value=None):
            code, again = self.inbound(FERGUSON_OK)
        self.assertEqual(again, {"status": "duplicate", "cart_id": first["cart_id"]})
        self.assertEqual(self.removed, [self.puts[1][0]])

    def test_parse_failure_stored_as_not_ok(self):
        with mock.patch.object(cp, "parse_cart_email", side_effect=RuntimeError("boom")):
            code, out = self.inbound(FERGUSON_OK)
        cart = self.db.carts[out["cart_id"]]
        self.assertEqual((code, cart["ok"], cart["warnings"], cart["project_name"]),
                         (202, False, [cp.CANT_READ], None))

    def test_not_a_cart_is_stored_with_the_reason(self):
        import ferguson_cart
        raw = with_headers(direct_email("<p>Hello, no cart here.</p>"), FERGUSON_AR)
        code, out = self.inbound(raw)
        cart = self.db.carts[out["cart_id"]]
        self.assertEqual((cart["ok"], cart["warnings"]), (False, [ferguson_cart.NOT_A_CART]))

    def test_r2_failure_is_503_and_nothing_stored(self):
        def fail(*a):
            raise cp.storage.StorageError("Failed to upload bytes to r2://b/k: boom")
        with mock.patch.object(cp.storage, "put_bytes", fail):
            code, out = self.inbound(FERGUSON_OK)
        self.assertEqual((code, out["reason"]), (503, "storage_unavailable"))
        self.assertEqual(self.db.carts, {})

    def test_logs_hold_no_addresses_or_content(self):
        with self.assertLogs("voice_intake.carts_panel", level="INFO") as logs:
            self.inbound(FERGUSON_OK, to="nobody@voiceconsult.app")
            self.inbound(FORWARD_OK, sender="stranger@example.invalid")
            self.inbound(with_headers(FERGUSON_RAW, ar(spf=("fail", "x@y.invalid"))))
            self.inbound(FERGUSON_OK)
            self.inbound(FERGUSON_OK, secret="wrong")
        text = "\n".join(logs.output)
        for leak in ("nobody", "stranger", "example.invalid", "fergusonhome", "Testproject",
                     "pat", SECRET, "voiceconsult"):
            self.assertNotIn(leak, text.lower() if leak.islower() else text)
        for code in ("unknown_alias", "sender_not_allowed", "auth_failed", "bad secret"):
            self.assertIn(code, text)


# --- the user's carts ----------------------------------------------------------------------

class TestCartsForUsers(Base):
    def setUp(self):
        super().setUp()
        self.a1 = self.inbound(FERGUSON_OK)[1]["cart_id"]
        self.a2 = self.inbound(FORWARD_OK, sender="pat.work@example.invalid")[1]["cart_id"]
        # Forwarding the same bytes to the other user is a separate cart for them.
        self.b1 = self.inbound(FERGUSON_OK, to="sam@voiceconsult.app")[1]["cart_id"]
        self.assertEqual(len({self.a1, self.a2, self.b1}), 3)

    def test_list_is_own_newest_first(self):
        with self.as_user(USER_A):
            r = self.client.get("/carts")
        body = r.get_json()
        self.assertEqual([i["id"] for i in body["items"]], [self.a2, self.a1])
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["items"][1], {
            "id": self.a1, "received_at": self.db.carts[self.a1]["received_at"],
            "project_name": "Testproject", "item_count": 2, "subtotal": "1502.76", "ok": True,
            "warning_count": 0, "status": "needs_customer", "customer_name": None,
            "match_state": "pending", "fieldpulse_state": "not_sent", "superseded_by": None})
        with self.as_user(USER_B):
            ids = [i["id"] for i in self.client.get("/carts").get_json()["items"]]
        self.assertEqual(ids, [self.b1])
        with self.as_user(USER_A):
            self.assertEqual(self.client.get("/carts?limit=1&offset=1").get_json()["items"][0]["id"],
                             self.a1)
            self.assertEqual(self.client.get("/carts?limit=x").status_code, 400)

    def test_detail_own_only(self):
        with self.as_user(USER_A):
            d = self.client.get(f"/carts/{self.a1}").get_json()
        self.assertEqual((d["project_name"], d["status"], d["ok"], d["warnings"]),
                         ("Testproject", "needs_customer", True, []))
        self.assertEqual(d["cart"]["subtotal"], "1502.76")
        self.assertEqual(d["cart"]["pricing_expires"], "2026-12-10")
        self.assertEqual(d["cart"]["items"][1], {
            "room": "Kitchen", "brand": "Otherbrand Co", "model": "OB-200-NODRAIN",
            "finish": "Polished White", "quantity": 2, "unit_price": "46.67",
            "product_url": "https://www.fergusonhome.com/otherbrand-ob-200-nodrain/p1000002",
            "image_url": "https://images.example.invalid/madeup/otherbrand-ob-200-nodrain.jpg",
            "item_type": None})
        self.assertIsNone(d["cart"]["item_types"])      # not decided yet (worker, before send)
        for key in ("raw_key", "sender", "clerk_user_id", "raw_sha256", "message_id"):
            self.assertNotIn(key, d)
        with self.as_user(USER_B):
            r = self.client.get(f"/carts/{self.a1}")
        self.assertEqual(r.status_code, 404)
        with self.as_user(USER_A):
            self.assertEqual(self.client.get("/carts/cart_ffffffffffff").status_code, 404)
            self.assertEqual(self.client.get("/carts/../admin").status_code, 404)

    def test_delete_own_only_row_then_raw(self):
        with self.as_user(USER_B):
            self.assertEqual(self.client.delete(f"/carts/{self.a1}").status_code, 404)
        self.assertIn(self.a1, self.db.carts)
        key = self.db.carts[self.a1]["raw_key"]
        with self.as_user(USER_A):
            r = self.client.delete(f"/carts/{self.a1}")
            self.assertEqual(r.get_json(), {"deleted": True, "id": self.a1, "file": "deleted"})
            self.assertEqual(self.client.delete(f"/carts/{self.a1}").status_code, 404)
        self.assertEqual(self.removed, [key])
        self.assertNotIn(self.a1, self.db.carts)
        self.assertIn(self.b1, self.db.carts)

    def test_delete_r2_failure_keeps_db_delete(self):
        def rm(k):
            raise cp.storage.StorageError("Failed to delete r2://b/k: boom")
        with mock.patch.object(cp.storage, "delete_audio", rm), \
                mock.patch.object(cp, "report") as rep, self.as_user(USER_A):
            r = self.client.delete(f"/carts/{self.a1}")
        self.assertEqual(r.get_json()["file"], "failed")
        self.assertNotIn(self.a1, self.db.carts)
        self.assertEqual(rep.call_args[0][1], "R2 cart email delete failed")

    def test_delete_preflight_allowed(self):
        app = self.orch.app
        with mock.patch.dict(app.before_request_funcs,
                             {None: [self.orch._require_clerk_session]}):
            r = self.client.open(f"/carts/{self.a1}", method="OPTIONS", headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization"})
        self.assertIn(r.status_code, (200, 204))


# --- E3: customer match + link -------------------------------------------------------------

def cust(cid, display=None, first=None, last=None, **kw):
    """A customer as customer_source.list_customers returns it (made up)."""
    return {"id": cid, "display_name": display, "first_name": first, "last_name": last,
            "phone": kw.get("phone"), "email": kw.get("email"), "company_name": None,
            "address": kw.get("address"), "created_at": None}


CUSTOMERS = [cust(101, "Testproject", phone="555-0101", address="1 Madeup St"),
             cust(102, "Other Person"), cust("cust_mock3", None, "Robin", "O'Madeup")]


class TestCartMatch(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.inbound(FERGUSON_OK)[1]["cart_id"]
        self.other = self.inbound(FERGUSON_OK, to="sam@voiceconsult.app")[1]["cart_id"]

    def run_match(self, customers=CUSTOMERS, cart_id=None, user=USER_A):
        kw = ({"side_effect": customers} if isinstance(customers, Exception)
              else {"return_value": customers})
        with mock.patch.object(cp.customer_source, "list_customers", **kw) as lc:
            out = cp.match_cart(user, cart_id or self.cid)
        return out, lc

    def cart(self, cid=None):
        return self.db.carts[cid or self.cid]

    def detail(self, user=USER_A, cid=None):
        with self.as_user(user):
            r = self.client.get(f"/carts/{cid or self.cid}")
        return r.status_code, r.get_json()

    def post(self, path, user=USER_A, body=None, customers=CUSTOMERS):
        kw = ({"side_effect": customers} if isinstance(customers, Exception)
              else {"return_value": customers})
        with self.as_user(user), \
                mock.patch.object(cp.customer_source, "list_customers", **kw):
            r = self.client.post(path, json=body)
        return r.status_code, r.get_json()

    def test_inbound_queues_a_match_and_marks_it_pending(self):
        self.assertEqual(self.queued, [(USER_A, self.cid), (USER_B, self.other)])
        c = self.cart()
        self.assertEqual((c["status"], c["customer_match"]["state"],
                          c["customer_match"]["match"]), ("needs_customer", "pending", None))
        # A duplicate forward queues nothing new.
        self.inbound(FERGUSON_OK)
        self.assertEqual(len(self.queued), 2)

    def test_exact_match_links(self):
        out, lc = self.run_match()
        self.assertEqual((out, lc.call_count), ("linked", 1))
        c = self.cart()
        self.assertEqual((c["status"], c["fieldpulse_customer_id"]), ("linked", "101"))
        self.assertEqual(c["customer"]["display_name"], "Testproject")
        self.assertEqual((c["customer_match"]["match"], c["customer_match"]["state"]),
                         ("auto", "done"))
        code, d = self.detail()
        self.assertEqual((code, d["status"], d["match"], d["customer"]["id"],
                          d["customer"]["phone"], d["can_link"], d["can_match"]),
                         (200, "linked", "auto", 101, "555-0101", True, False))
        self.assertIn("Testproject", d["match_reason"])
        with self.as_user(USER_A):
            item = self.client.get("/carts").get_json()["items"][0]
        self.assertEqual((item["status"], item["customer_name"], item["match_state"]),
                         ("linked", "Testproject", "done"))

    def test_normalization_case_spaces_punctuation(self):
        n = cp.normalize_name
        self.assertEqual(n("  TEST   Project. "), "test project")
        self.assertEqual(n("O\u2019Madeup-Smith,  Robin"), "omadeup smith robin")
        self.assertEqual(n("Pat & Sam"), n("pat sam"))
        self.assertEqual(n("Pat\tExample"), "pat example")
        self.assertEqual(n("Ｐａｔ"), "pat")                     # full-width (NFKC)
        self.assertEqual((n(None), n("  ...  "), n(7)), ("", "", ""))
        self.assertNotEqual(n("José"), n("Jose"))              # accents stay distinct
        for variant in ("testproject", "  TESTPROJECT  ", "Testproject!", "testproject."):
            hit, _ = cp.find_by_name([cust(1, variant)], "Testproject")
            self.assertEqual(hit and hit["id"], 1, variant)
        hit, _ = cp.find_by_name([cust(9, None, "Robin", "O'Madeup")], "robin omadeup")
        self.assertEqual(hit["id"], 9)                          # first + last when no display
        # End to end with a messy FieldPulse name.
        out, _ = self.run_match([cust(201, "  TESTPROJECT. "), cust(202, "Testproject 2")])
        self.assertEqual((out, self.cart()["fieldpulse_customer_id"]), ("linked", "201"))

    def test_never_fuzzy(self):
        for near in ("Test project", "Testproject2", "Testprojects", "Testprojekt",
                     "The Testproject"):
            hit, reason = cp.find_by_name([cust(1, near)], "Testproject")
            self.assertIsNone(hit, near)
            self.assertEqual(reason, "No FieldPulse customer named \u201cTestproject\u201d.")

    def test_no_match_stays_needs_customer(self):
        out, _ = self.run_match([cust(102, "Other Person")])
        c = self.cart()
        self.assertEqual((out, c["status"], c["customer"], c["fieldpulse_customer_id"]),
                         ("needs_customer", "needs_customer", None, None))
        self.assertEqual(c["customer_match"]["reason"],
                         "No FieldPulse customer named \u201cTestproject\u201d.")
        _, d = self.detail()
        self.assertEqual((d["can_match"], d["can_link"], d["match"]), (True, True, None))

    def test_two_matches_stays_needs_customer(self):
        out, _ = self.run_match([cust(1, "Testproject"), cust(2, "TESTPROJECT"),
                                 cust(3, "Other")])
        c = self.cart()
        self.assertEqual((out, c["status"]), ("needs_customer", "needs_customer"))
        self.assertTrue(c["customer_match"]["reason"].startswith(
            "2 customers match \u201cTestproject\u201d."))

    def test_search_error_then_retry(self):
        err = cp.customer_source.CustomerSourceError("FieldPulse returned HTTP 500.")
        with mock.patch.object(cp, "report") as rep:
            out, _ = self.run_match(err)
        c = self.cart()
        self.assertEqual((out, c["status"], c["customer_match"]["state"],
                          c["customer_match"]["reason"]),
                         ("search_failed", "needs_customer", "error", cp.SEARCH_FAILED))
        self.assertEqual(rep.call_args[0][1], "Cart match: customer search failed")
        # Any other exception is a search failure too (never escapes the worker).
        out, _ = self.run_match(OSError("boom"))
        self.assertEqual(out, "search_failed")
        # Retry: queued, pending, then the worker links it.
        self.queued.clear()
        code, body = self.post(f"/carts/{self.cid}/match",
                               customers=AssertionError("no search in the request"))
        self.assertEqual((code, body), (202, {"status": "queued", "id": self.cid}))
        self.assertEqual(self.queued, [(USER_A, self.cid)])
        self.assertEqual(self.cart()["customer_match"]["state"], "pending")
        self.assertEqual(self.run_match()[0], "linked")
        # Retry on a linked cart is refused.
        code, _ = self.post(f"/carts/{self.cid}/match")
        self.assertEqual(code, 409)

    def test_manual_link(self):
        self.run_match([])                                     # no match
        code, d = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 102})
        self.assertEqual((code, d["status"], d["match"], d["customer"]["display_name"]),
                         (200, "linked", "manual", "Other Person"))
        self.assertEqual(d["match_reason"], cp.LINKED_BY_HAND)
        self.assertEqual(self.cart()["fieldpulse_customer_id"], "102")
        # A mock-style string id, and first+last used as the snapshot's display name.
        code, d = self.post(f"/carts/{self.cid}/customer", body={"customer_id": "cust_mock3"})
        self.assertEqual((code, d["customer"]["display_name"]), (200, "Robin O'Madeup"))

    def test_snapshot_comes_from_fieldpulse_not_the_browser(self):
        code, d = self.post(f"/carts/{self.cid}/customer",
                            body={"customer_id": "101", "display_name": "Injected"})
        self.assertEqual((code, d["customer"]["display_name"]), (200, "Testproject"))

    def test_relink_and_stale_auto_match_never_overrides(self):
        self.assertEqual(self.run_match()[0], "linked")        # auto -> 101
        code, d = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 102})
        self.assertEqual((code, d["match"], d["customer"]["id"]), (200, "manual", 102))
        code, d = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 101})
        self.assertEqual((code, d["customer"]["id"]), (200, 101))
        # A late auto-match job changes nothing.
        self.assertEqual(self.run_match([cust(102, "Testproject")])[0], "skipped")
        self.assertEqual((self.cart()["fieldpulse_customer_id"],
                          self.cart()["customer_match"]["match"]), ("101", "manual"))
        # E4 states (simulated) can't be relinked.
        self.db.carts[self.cid]["status"] = "sent"
        code, body = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 102})
        self.assertEqual(code, 409)
        _, d = self.detail()
        self.assertEqual((d["can_link"], d["can_match"]), (False, False))

    def test_other_users_cart_is_refused(self):
        for path, body in ((f"/carts/{self.other}/customer", {"customer_id": 101}),
                           (f"/carts/{self.other}/match", None)):
            code, _ = self.post(path, user=USER_A, body=body,
                                customers=AssertionError("no search for a foreign cart"))
            self.assertEqual(code, 404, path)
        self.assertEqual(cp.match_cart(USER_A, self.other), "skipped")
        o = self.cart(self.other)
        self.assertEqual((o["status"], o["customer"]), ("needs_customer", None))
        for path in ("/carts/cart_ffffffffffff/customer", "/carts/nope/customer"):
            self.assertEqual(self.post(path, body={"customer_id": 101})[0], 404)
        with self.as_user(None):
            r = self.client.post(f"/carts/{self.cid}/customer", json={"customer_id": 101})
        self.assertEqual(r.status_code, 401)                  # real Clerk hook, no session

    def test_invalid_customer_id(self):
        for body in (None, {}, {"customer_id": None}, {"customer_id": True},
                     {"customer_id": 0}, {"customer_id": -5}, {"customer_id": 1.5},
                     {"customer_id": ""}, {"customer_id": "../admin"},
                     {"customer_id": "x" * 65}, {"customer_id": ["101"]}, ["101"]):
            code, out = self.post(f"/carts/{self.cid}/customer", body=body,
                                  customers=AssertionError("validated before any search"))
            self.assertEqual((code, out["error"]),
                             (400, "'customer_id' must be a FieldPulse customer id."), body)
        code, out = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 999})
        self.assertEqual((code, out["error"]), (400, "That FieldPulse customer wasn't found."))
        self.assertEqual(self.cart()["status"], "needs_customer")

    def test_link_search_error_is_502_and_changes_nothing(self):
        err = cp.customer_source.CustomerSourceError("Timed out.")
        with mock.patch.object(cp, "report"):
            code, out = self.post(f"/carts/{self.cid}/customer", body={"customer_id": 101},
                                  customers=err)
        self.assertEqual(code, 502)
        self.assertEqual(self.cart()["status"], "needs_customer")

    def test_enqueue_failure_keeps_the_cart(self):
        def boom(u, c):
            raise ConnectionError("redis down")
        with mock.patch.object(cp.queue_setup, "enqueue_cart_match", boom), \
                mock.patch.object(cp, "report") as rep:
            code, out = self.inbound(FORWARD_OK, sender="pat.work@example.invalid")
            self.assertEqual((code, out["status"]), (202, "stored"))
            c = self.db.carts[out["cart_id"]]
            self.assertEqual((c["status"], c["customer_match"]["state"],
                              c["customer_match"]["reason"]),
                             ("needs_customer", "error", cp.QUEUE_FAILED))
            self.assertEqual(rep.call_args[0][1], "Could not queue cart match")
            code, body = self.post(f"/carts/{out['cart_id']}/match")
            self.assertEqual(code, 503)
        # Even a DB failure while starting the match keeps the stored cart + 202.
        with mock.patch.object(self.db, "set_cart_customer", side_effect=RuntimeError("db")), \
                mock.patch.object(cp.jobs, "set_cart_customer",
                                  side_effect=RuntimeError("db")), \
                mock.patch.object(cp, "report"):
            raw = with_headers(gmail_forward(cart_html(project="Another")),
                               ar(spf=("pass", "pat@example.invalid")))
            code, out = self.inbound(raw, sender="pat@example.invalid")
        self.assertEqual((code, out["status"]), (202, "stored"))
        self.assertIn(out["cart_id"], self.db.carts)

    def test_not_ok_cart_is_matched_but_flagged(self):
        with mock.patch.object(cp, "parse_cart_email", return_value={
                "project_name": "Testproject", "items": [], "checks": ["Made-up check."],
                "ok": False}):
            raw = with_headers(gmail_forward(cart_html(project="Notok")),
                               ar(spf=("pass", "pat@example.invalid")))
            cid = self.inbound(raw, sender="pat@example.invalid")[1]["cart_id"]
        self.assertEqual(self.run_match(cart_id=cid)[0], "linked")
        _, d = self.detail(cid=cid)
        self.assertEqual((d["ok"], d["status"]), (False, "linked"))
        self.assertIn("couldn't be read completely", d["match_reason"])

    def test_unreadable_cart_without_project_name(self):
        with mock.patch.object(cp, "parse_cart_email", side_effect=RuntimeError("boom")):
            raw = with_headers(gmail_forward(cart_html(project="X1")),
                               ar(spf=("pass", "pat@example.invalid")))
            cid = self.inbound(raw, sender="pat@example.invalid")[1]["cart_id"]
        out, lc = self.run_match(cart_id=cid)
        self.assertEqual((out, lc.call_count), ("needs_customer", 0))   # no search at all
        self.assertTrue(self.cart(cid)["customer_match"]["reason"].startswith(cp.NO_PROJECT))

    def test_logs_hold_no_names(self):
        with self.assertLogs("voice_intake.carts_panel", level="INFO") as logs:
            self.run_match([])
            self.post(f"/carts/{self.cid}/customer", body={"customer_id": 102})
        text = "\n".join(logs.output)
        for leak in ("Testproject", "Other Person", "102"):
            self.assertNotIn(leak, text)

    def test_worker_job_delegates(self):
        import worker
        with mock.patch.object(cp, "match_cart", return_value="linked") as m:
            self.assertEqual(worker.match_cart_customer(USER_A, self.cid), "linked")
        m.assert_called_once_with(USER_A, self.cid)


class TestObservability(unittest.TestCase):
    def test_cart_tags_and_messages(self):
        rule = observability._TAG_RULES["job_id"]
        self.assertTrue(observability._rule(rule, "cart_0123456789ab"))
        self.assertIsNone(observability._rule(rule, "cart_Pat Example"))
        for msg in ("No free cart address alias", "ensure_cart_alias needs a user id"):
            self.assertEqual(observability._safe_value(msg, "JobError", "jobs", None), msg)
        _load_panel()
        self.assertEqual(observability._safe_value("UserNotFound", "ProfileError",
                                                   "carts_panel", None), "UserNotFound")

    def test_routes_are_templates(self):
        import orchestrator
        rules = {r.rule for r in orchestrator.app.url_map.iter_rules()}
        for r in ("/inbound/cart", "/carts", "/carts/address", "/carts/<cart_id>",
                  "/carts/<cart_id>/match", "/carts/<cart_id>/customer",
                  "/carts/<cart_id>/send"):
            self.assertIn(r, rules)


if __name__ == "__main__":
    unittest.main()
