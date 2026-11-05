"""Privacy regressions for observability.py (F-31): nothing personal reaches Sentry.

Real sentry_sdk + our init_sentry + an in-memory transport (no network, the real
DSN is never used). Every test plants customer names/phones/addresses, transcript
text, credentials, signed URL queries and psycopg DETAIL rows, then asserts none of
them appear ANYWHERE in the serialized outgoing envelopes, and that the useful
parts (exception type, stack frames, our fixed text) still do.

Run: python -m unittest test_observability
"""
import json
import logging
import os
import socket
import _socket
import unittest
from unittest import mock

import sentry_sdk

import observability
from observability import init_sentry, report, traced_job

NAME = "PlantedCustomerZelda"
PHONE = "+15555550199"
ADDRESS = "4242 Planted Synthetic Lane"
TRANSCRIPT = "PLANTED_PRIVATE_TRANSCRIPT_SENTENCE"
TOKEN = "PLANTED_BEARER_SECRET"
COOKIE = "PLANTED_COOKIE_SECRET"
INTAKE = "PLANTED_INTAKE_SECRET"
SIGNATURE = "PLANTEDSIGNATURE"
SIGNED_URL = (f"https://acct.r2.cloudflarestorage.com/bucket/consultations/a.webm"
              f"?X-Amz-Signature={SIGNATURE}&X-Amz-Credential=PLANTEDCRED")
PLANTED = (NAME, PHONE, ADDRESS, TRANSCRIPT, TOKEN, COOKIE, INTAKE, SIGNATURE, "PLANTEDCRED")
CUSTOMER_TEXT = f"{NAME} {PHONE} {ADDRESS} {TRANSCRIPT}"
PG_TEXT = ('null value in column "status" of relation "consultation_jobs" violates not-null '
           f'constraint\nDETAIL:  Failing row contains (job_1a2b3c4d5e6f, null, {NAME}, '
           f'{PHONE}, {ADDRESS}, {TRANSCRIPT}).')


def _memory_transport(options):
    """observability's real SafeTransport (sanitizer included); only the final
    HTTP delivery is replaced by an in-memory list."""
    base = observability._safe_transport_class()

    class _MemoryTransport(base):
        def __init__(self, opts):
            super().__init__(opts)
            self.envelopes = []

        def _deliver(self, envelope):
            self.envelopes.append(envelope)

        def flush(self, timeout, callback=None):
            return None

    return _MemoryTransport(options)


def _payloads(transport):
    out = []
    for env in transport.envelopes:
        for item in env.items:
            if item.type in ("event", "transaction"):
                out.append(item.payload.json)
    return out


def _loopback_allowed():
    """Other suites (test_customer_source) block socket.connect process-wide; these
    two tests need a real loopback server, so restore the C-level connect for them."""
    return mock.patch.object(socket.socket, "connect", _socket.socket.connect)


class ObservabilityPrivacyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {"SENTRY_DSN": "http://public@127.0.0.1:9/1", "SENTRY_ENVIRONMENT": "unit-test",
               "SENTRY_RELEASE": "unit-release",
               "CLERK_AUTHORIZED_PARTIES": "http://localhost:5173,https://app.example.com",
               "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY") or "sk-ant-unit-test-dummy"}
        cls._env = mock.patch.dict(os.environ, env)
        cls._env.start()
        # An earlier suite (e.g. orchestrator import with SENTRY_DSN set) may have left a
        # client running; init_sentry keeps an active client, so drop it first.
        sentry_sdk.get_client().close()
        for _scope in (sentry_sdk.get_global_scope(), sentry_sdk.get_isolation_scope(),
                       sentry_sdk.get_current_scope()):
            _scope.set_client(None)
        observability._enabled = False
        assert init_sentry("worker")
        assert type(sentry_sdk.get_client().transport).__name__ == "SafeTransport"
        cls.transport = _memory_transport(sentry_sdk.get_client().options)
        sentry_sdk.get_client().transport = cls.transport

    @classmethod
    def tearDownClass(cls):
        sentry_sdk.get_client().close()
        sentry_sdk.init()                    # inactive client for anything after us
        observability._enabled = False
        cls._env.stop()

    def setUp(self):
        self.transport.envelopes.clear()
        sentry_sdk.get_isolation_scope().clear()
        sentry_sdk.get_current_scope().clear()

    # --- helpers ---------------------------------------------------------

    def sent(self):
        sentry_sdk.flush()
        return _payloads(self.transport)

    def assert_clean(self, payloads):
        self.assertTrue(payloads, "nothing was sent")
        blob = json.dumps(payloads)
        for secret in PLANTED:
            self.assertNotIn(secret, blob, f"{secret!r} leaked")
        self.assertNotIn("DETAIL", blob)
        return blob

    def errors(self, payloads):
        return [p for p in payloads if p.get("type") != "transaction"]

    # --- exceptions ------------------------------------------------------

    def test_worker_upstream_body_redacted_but_type_and_frames_kept(self):
        from transcriber import TranscriptionError

        @traced_job
        def process_consultation_job(job_id):
            logging.getLogger("voice_intake.worker").warning("[%s] customer %s", job_id, NAME)
            raise TranscriptionError(f"Deepgram returned 400: {CUSTOMER_TEXT}")

        with self.assertRaises(TranscriptionError) as ctx:
            process_consultation_job("job_1a2b3c4d5e6f")
        self.assertIn(NAME, str(ctx.exception))     # the real exception is untouched
        payloads = self.sent()
        self.assert_clean(payloads)
        (event,) = self.errors(payloads)
        exc = event["exception"]["values"][-1]
        self.assertEqual(exc["type"], "TranscriptionError")
        self.assertEqual(exc["value"], "Deepgram returned 400: [redacted]")
        funcs = [f.get("function") for f in exc["stacktrace"]["frames"]]
        self.assertIn("process_consultation_job", funcs)
        self.assertEqual(event["tags"]["job_id"], "job_1a2b3c4d5e6f")
        crumbs = event["breadcrumbs"]["values"]
        self.assertIn("[%s] customer %s", [c.get("message") for c in crumbs])
        self.assertTrue(any(p.get("type") == "transaction" for p in payloads))

    def test_psycopg_detail_redacted_keeps_sqlstate(self):
        import psycopg.errors
        try:
            raise psycopg.errors.NotNullViolation(PG_TEXT)
        except Exception as exc:  # noqa: BLE001
            report(exc, "Could not mark job failed", job_id="job_1a2b3c4d5e6f")
        payloads = self.sent()
        self.assert_clean(payloads)
        (event,) = self.errors(payloads)
        exc = event["exception"]["values"][-1]
        self.assertEqual(exc["type"], "NotNullViolation")
        self.assertEqual(exc["value"], "[redacted] (sqlstate 23502)")
        self.assertEqual(event["contexts"]["voice_intake"]["note"], "Could not mark job failed")

    def test_chained_exceptions_all_redacted(self):
        from extractor import ExtractionError
        try:
            try:
                raise RuntimeError(f"Error code: 400 - {{'message': '{CUSTOMER_TEXT}'}}")
            except RuntimeError as inner:
                try:
                    raise ValueError(f"while handling {NAME}")
                except ValueError as mid:
                    raise ExtractionError(f"Claude API error: {inner} {mid}") from mid
        except ExtractionError as exc:
            report(exc, "test chain")
        payloads = self.sent()
        self.assert_clean(payloads)
        values = {v["type"]: v["value"] for v in self.errors(payloads)[0]["exception"]["values"]}
        self.assertEqual(values["ExtractionError"], "Claude API error: [redacted]")
        self.assertEqual(values["ValueError"], "[redacted]")
        self.assertEqual(values["RuntimeError"], "Error code: 400: [redacted]")

    def test_known_safe_messages_kept(self):
        from customer_source import CustomerSourceError
        from jobs import JobError
        for exc in (JobError("Job not found: job_1a2b3c4d5e6f"),
                    CustomerSourceError("FieldPulse rejected the read API key (HTTP 401).")):
            try:
                raise exc
            except Exception as e:  # noqa: BLE001
                report(e, "safe")
        got = [e["exception"]["values"][-1]["value"] for e in self.errors(self.sent())]
        self.assertEqual(got, ["Job not found: job_1a2b3c4d5e6f",
                               "FieldPulse rejected the read API key (HTTP 401)."])

    def test_safe_template_with_appended_text_is_not_kept(self):
        from jobs import JobError
        try:
            raise JobError(f"Job not found: job_1a2b3c4d5e6f\n{NAME}")
        except JobError as e:
            report(e)
        self.assert_clean(self.sent())

    # --- messages / scope data -------------------------------------------

    def test_report_message_kept_other_messages_redacted(self):
        report(message="FieldPulse comment failed (HTTP 500)", level="warning",
               job_id="job_1a2b3c4d5e6f", step="comment")
        sentry_sdk.capture_message(f"third-party said {CUSTOMER_TEXT}")
        events = self.errors(self.sent())
        self.assert_clean(events)
        self.assertEqual(events[0]["message"], "FieldPulse comment failed (HTTP 500)")
        self.assertEqual(events[0]["level"], "warning")
        self.assertEqual(events[1]["message"], "[redacted message]")

    def test_extra_user_contexts_and_breadcrumb_data_dropped(self):
        sentry_sdk.set_extra("customer", CUSTOMER_TEXT)
        sentry_sdk.set_user({"username": NAME, "email": "zelda@example.invalid"})
        sentry_sdk.set_context("customer", {"name": NAME, "phone": PHONE})
        sentry_sdk.add_breadcrumb(category="custom", message=f"see {SIGNED_URL}",
                                  data={"url": SIGNED_URL, "body": TRANSCRIPT,
                                        "Authorization": f"Bearer {TOKEN}", "status_code": 500})
        sentry_sdk.add_breadcrumb(category="sentry.event", message=f"Error: {NAME}")
        log = logging.getLogger("voice_intake.test")
        log.warning(f"pre-formatted {NAME}")           # no args: text omitted
        log.warning("Customer %s at %s", NAME, ADDRESS)  # template kept
        try:
            raise RuntimeError("x")
        except RuntimeError as e:
            report(e, "scope test")
        (event,) = self.errors(self.sent())
        self.assert_clean([event])
        self.assertNotIn("extra", event)
        self.assertNotIn("user", event)
        self.assertNotIn("customer", event.get("contexts", {}))
        crumbs = {c.get("category"): c for c in event["breadcrumbs"]["values"]}
        self.assertEqual(crumbs["custom"]["data"],
                         {"url": "https://acct.r2.cloudflarestorage.com", "status_code": 500})
        messages = [c.get("message") for c in event["breadcrumbs"]["values"]]
        self.assertIn("Customer %s at %s", messages)
        self.assertIn("[log message omitted]", messages)

    def test_transaction_spans_scrubbed(self):
        with sentry_sdk.start_transaction(op="queue.task.rq", name="worker.test"):
            with sentry_sdk.start_span(op="http.client", name=f"GET {SIGNED_URL}") as span:
                span.set_data("url", SIGNED_URL)
                span.set_data("http.query", f"X-Amz-Signature={SIGNATURE}")
                span.set_data("http.request.body", TRANSCRIPT)
                span.set_data("http.response.status_code", 200)
            with sentry_sdk.start_span(op="db.redis", name="SET 'k' [Filtered]") as span:
                span.set_data("redis.key", NAME)
                span.set_data("redis.command", "SET")
                span.set_data("gen_ai.request.messages", TRANSCRIPT)
        payloads = self.sent()
        self.assert_clean(payloads)
        (txn,) = [p for p in payloads if p.get("type") == "transaction"]
        spans = {s["op"]: s for s in txn["spans"]}
        self.assertEqual(spans["http.client"]["description"],
                         "GET https://acct.r2.cloudflarestorage.com")
        self.assertEqual(spans["http.client"]["data"]["http.response.status_code"], 200)
        self.assertEqual(spans["db.redis"]["data"].get("redis.command"), "SET")

    def test_flask_request_scrubbed(self):
        from flask import Flask
        app = Flask("obs_test")

        @app.post("/consultation/submit")
        def boom():
            raise RuntimeError(f"bad input for {NAME}")

        client = app.test_client()
        client.post(f"/consultation/submit?q={NAME}&sig={SIGNATURE}",
                    json={"transcript": TRANSCRIPT, "customer": {"name": NAME, "phone": PHONE}},
                    headers={"Authorization": f"Bearer {TOKEN}", "Cookie": f"s={COOKIE}",
                             "x-intake-key": INTAKE, "X-Forwarded-For": "203.0.113.9"})
        payloads = self.sent()
        self.assert_clean(payloads)
        (event,) = self.errors(payloads)
        req = event["request"]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["url"], "/consultation/submit")              # route template
        self.assertEqual({k.lower() for k in req["headers"]} - observability._SAFE_HEADERS, set())
        self.assertEqual(event["exception"]["values"][-1]["value"], "[redacted]")


    # --- round 3 (F-33..F-37) --------------------------------------------

    R3 = "R3BackendCustomer"

    def all_bytes(self):
        sentry_sdk.flush()
        out = []
        for env in self.transport.envelopes:
            out.append(json.dumps(env.headers, default=str))
            for item in env.items:
                out.append(json.dumps(item.headers, default=str))
                out.append(item.payload.get_bytes().decode("utf-8", "replace"))
        return "\n".join(out)

    def test_f34_incoming_baggage_never_reaches_envelope_or_trace_context(self):
        from flask import Flask
        app = Flask("obs_baggage")

        @app.get("/probe")
        def probe():
            report(message="baggage probe")
            return "ok"

        p = self.R3
        app.test_client().get("/probe", headers={
            "sentry-trace": "0123456789abcdef0123456789abcdef-0123456789abcdef-1",
            "baggage": ("sentry-trace_id=0123456789abcdef0123456789abcdef,sentry-public_key=" + p
                        + ",sentry-transaction=" + p + ",sentry-user_segment=" + p
                        + ",sentry-environment=" + p + ",sentry-release=" + p
                        + ",sentry-sample_rate=1,sentry-sampled=true,other=" + p)})
        blob = self.all_bytes()
        self.assertNotIn(p, blob)
        traces = [e.headers.get("trace") for e in self.transport.envelopes if e.headers.get("trace")]
        self.assertTrue(traces, "no envelope trace header at all")
        for t in traces:
            self.assertEqual(t["trace_id"], "0123456789abcdef0123456789abcdef")
            self.assertEqual(t["public_key"], "public")             # our own DSN key
            self.assertEqual(t["environment"], "unit-test")
            self.assertLessEqual(set(t), {"trace_id", "public_key", "environment", "release",
                                          "sample_rate", "sample_rand", "sampled"})

    def test_f34_no_trace_headers_on_outgoing_http(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import requests
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({k.lower() for k in self.headers.keys()})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with _loopback_allowed(), \
                    sentry_sdk.start_transaction(op="queue.task.rq", name="worker.test"):
                requests.get(f"http://127.0.0.1:{srv.server_port}/x", timeout=2)
        finally:
            srv.shutdown()
        self.assertTrue(seen)
        self.assertFalse({"sentry-trace", "baggage", "traceparent"} & seen[0])

    def test_f37_upstream_reason_phrase_dropped_from_crumbs_and_spans(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import requests
        p = self.R3

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(400, p)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with _loopback_allowed(), \
                    sentry_sdk.start_transaction(op="queue.task.rq", name="worker.test"):
                requests.get(f"http://127.0.0.1:{srv.server_port}/transcribe", timeout=2)
                report(message="upstream reason probe")
        finally:
            srv.shutdown()
        blob = self.all_bytes()
        self.assertNotIn(p, blob)
        payloads = _payloads(self.transport)
        (txn,) = [x for x in payloads if x.get("type") == "transaction"]
        http = [s for s in txn["spans"] if s["op"] == "http.client"][0]
        self.assertEqual(http["data"]["http.response.status_code"], 400)
        self.assertTrue(http["description"].startswith("GET http://127.0.0.1:"))

    def test_f37_allowed_containers_with_unsafe_contents(self):
        p = self.R3
        with sentry_sdk.new_scope() as scope:
            scope.set_context("os", {"name": p})
            scope.set_context("runtime", {"name": p, "version": p, "build": p})
            scope.set_context("voice_intake", {"note": p})        # only report() may set it
            scope.set_tag("who", p)
            scope.set_tag("job_id", p)
            scope.set_tag("step", "comment")
            scope.set_transaction_name(p)
            sentry_sdk.add_breadcrumb(category="custom", message=p, data={"reason": p, "method": p})
            sentry_sdk.add_breadcrumb(category=p, type=p, level=p, message=p)
            sentry_sdk.add_breadcrumb(type="log", category="x", message=p)
            report(message="metadata probe")
        blob = self.all_bytes()
        self.assertNotIn(p, blob)
        (event,) = self.errors(_payloads(self.transport))
        self.assertEqual(event["tags"].get("step"), "comment")
        self.assertNotIn("job_id", event["tags"])
        self.assertNotIn("voice_intake", event.get("contexts", {}))
        self.assertEqual(event["message"], "metadata probe")

    def test_f36_url_userinfo_removed(self):
        p = self.R3
        cred = f"https://user:{p}@example.invalid/audio?sig=secret"
        with sentry_sdk.start_transaction(op="queue.task.rq", name="worker.test"):
            with sentry_sdk.start_span(op="http.client", name=f"GET {cred}") as span:
                span.set_data("url", cred)
            sentry_sdk.add_breadcrumb(category="httplib", type="http", data={"url": cred})
            logging.getLogger("voice_intake.test").warning("calling " + cred + " %s", "x")
            report(message="cred probe")
        blob = self.all_bytes()
        self.assertNotIn(p, blob)
        self.assertNotIn("secret", blob)
        self.assertIn("https://example.invalid", blob)
        self.assertNotIn("/audio", blob)                         # F-38: origin only

    def test_f33_non_scrubbed_item_types_never_leave(self):
        from sentry_sdk.envelope import Envelope, Item, PayloadRef
        p = self.R3
        env = Envelope(headers={"event_id": "a" * 32, "trace": {"trace_id": "b" * 32, "transaction": p}})
        for ty in ("span", "log", "otel_log", "trace_metric", "profile", "attachment", "check_in"):
            env.add_item(Item(payload=PayloadRef(json={"name": p}), type=ty))
        self.transport.capture_envelope(env)
        self.assertEqual(self.transport.envelopes, [])          # nothing left -> nothing sent
        env2 = Envelope(headers={"trace": {"trace_id": "b" * 32, "user_segment": p}})
        env2.add_item(Item(payload=PayloadRef(json={"name": p}), type="span"))
        env2.add_item(Item(payload=PayloadRef(json={"sid": "a" * 32, "started": "2026-11-05T00:00:00Z",
                                                    "did": p,
                                                    "attrs": {"release": "r", "ip_address": p}}),
                           type="session"))
        self.transport.capture_envelope(env2)
        self.assertNotIn(p, self.all_bytes())
        self.assertEqual([i.type for i in self.transport.envelopes[0].items], ["session"])


    # --- round 4 (F-38..F-41): whole-envelope raw bytes, probes as in Round 3 ---

    R4 = "R4BackendPrivate"
    R4_PHONE = 15555550388

    def raw_bytes(self):
        """Every envelope exactly as it would go over HTTP (headers + items)."""
        sentry_sdk.flush()
        return b"\n".join(env.serialize() for env in self.transport.envelopes).decode(
            "utf-8", "replace")

    def assert_no_sentinel(self, blob):
        self.assertTrue(blob, "nothing was sent")
        self.assertNotIn(self.R4, blob)
        self.assertNotIn(str(self.R4_PHONE), blob)
        self.assertNotIn("5555550388", blob)

    def test_f38_incoming_paths_become_route_templates_outgoing_origin_only(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from flask import Flask, jsonify
        import requests
        p = self.R4
        app = Flask("obs_f38")

        @app.before_request
        def deny():
            return jsonify({"status": "unauthorized"}), 401

        @app.get("/job-status/<job_id>")
        def job_status(job_id):
            return "never"

        client = app.test_client()
        self.assertEqual(client.get(f"/job-status/{p}?token=R4_QUERY_SECRET").status_code, 401)
        client.get(f"/{p}/+15555550388/993%20Synthetic%20Lane")
        client.get(f"/job-status/{p}", headers={"Host": f"{p}.example.invalid"})

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with _loopback_allowed(), \
                    sentry_sdk.start_transaction(op="queue.task.rq", name="worker.test"):
                requests.get(f"http://127.0.0.1:{srv.server_port}/customers/{p}", timeout=2)
                report(message="outgoing path probe")
        finally:
            srv.shutdown()
        blob = self.raw_bytes()
        self.assert_no_sentinel(blob)
        self.assertNotIn("R4_QUERY_SECRET", blob)
        self.assertNotIn("Synthetic", blob)
        names = [x.get("transaction") for x in _payloads(self.transport)
                 if x.get("type") == "transaction"]
        self.assertEqual(names.count("/job-status/<job_id>"), 2)
        self.assertIn("[unmatched route]", names)
        urls = {x["request"]["url"] for x in _payloads(self.transport) if x.get("request")}
        self.assertEqual(urls, {"/job-status/<job_id>", "[unmatched route]"})
        self.assertIn(f"GET http://127.0.0.1:{srv.server_port}", blob)

    def test_f39_only_exact_data_keys_and_fixed_header_values(self):
        from flask import Flask
        p = self.R4
        with sentry_sdk.start_transaction(op="http.server", name="probe") as tx:
            tx.set_data("sentry." + p, 1)
            tx.set_data("sentry.customer_phone", self.R4_PHONE)
            tx.set_data("http.response.status_code", self.R4_PHONE)   # exact key, bad value
            with sentry_sdk.start_span(op="http." + p.lower()) as span:
                span.set_data("gen_ai.usage." + p, 1)
                span.set_data("gen_ai.usage.input_tokens", self.R4_PHONE)
                span.set_data("gen_ai.usage.output_tokens", 42)
            report(message="metadata key probe")
        app = Flask("obs_f39")

        @app.get("/health")
        def health():
            return "ok"

        c = app.test_client()
        c.get("/health", headers={"Content-Type": "application/" + p,
                                  "Origin": "https://example.invalid/" + p})
        c.get("/health", headers={"Content-Type": "audio/webm;codecs=opus",
                                  "Origin": "http://localhost:5173"})
        c.get("/health", headers={"Origin": "http://localhost:5173/" + p})
        blob = self.raw_bytes()
        self.assert_no_sentinel(blob)
        self.assertNotIn(p.lower(), blob)
        self.assertNotIn("example.invalid", blob)
        reqs = [x["request"] for x in _payloads(self.transport) if x.get("request")]
        headers = [r.get("headers", {}) for r in reqs]
        self.assertIn({"Content-Type": "audio/*", "Origin": "http://localhost:5173"}, headers)
        self.assertIn('"gen_ai.usage.output_tokens":42', blob)

    def test_f40_sessions_rebuilt_with_our_release_and_environment(self):
        from sentry_sdk.envelope import Envelope, Item, PayloadRef
        from sentry_sdk.session import Session
        p = self.R4
        sentry_sdk.get_client().capture_session(
            Session(release=p, environment=p, did=p, user_agent=p, ip_address="127.0.0.1"))
        sentry_sdk.flush()
        env = Envelope(headers={})
        env.add_item(Item(payload=PayloadRef(json={
            "attrs": {"release": p, "environment": p, "user_agent": p},
            "aggregates": [{"started": "2026-11-05T17:47:00.000000Z", "exited": 3, "did": p,
                            p: 1, "errored": self.R4_PHONE}], p: p}), type="sessions"))
        env.add_item(Item(payload=PayloadRef(json={
            "sid": "2d44d8b3-9262-4478-9804-4694b15bdc18", "init": True,
            "started": "2026-11-05T17:47:10.396208Z", "status": p, "abnormal_mechanism": p,
            "errors": self.R4_PHONE, "did": p, "attrs": {"release": p}}), type="session"))
        self.transport.capture_envelope(env)
        blob = self.raw_bytes()
        self.assert_no_sentinel(blob)
        items = [i for e in self.transport.envelopes for i in e.items]
        self.assertEqual(sorted(i.type for i in items), ["session", "session", "sessions"])
        for i in items:
            self.assertEqual(i.payload.json["attrs"],
                             {"release": "unit-release", "environment": "unit-test"})
        agg = [i.payload.json for i in items if i.type == "sessions"][0]
        self.assertEqual(agg["aggregates"], [{"started": "2026-11-05T17:47:00.000000Z", "exited": 3}])

    def test_f41_sdk_metadata_rebuilt_from_installed_sdk(self):
        from sentry_sdk.envelope import Envelope, Item, PayloadRef
        p = self.R4
        sentry_sdk.capture_event({"message": "sdk metadata probe",
                                  "sdk": {"name": "sentry.python", "version": "2.73.0",
                                          "customer": p, "integrations": [p],
                                          "packages": [{"name": p, "version": p}]}})
        env = Envelope(headers={"event_id": "a" * 32, "sdk": {"name": p, "customer": p}})
        env.add_item(Item(payload=PayloadRef(json={"event_id": "a" * 32,
                                                   "sdk": {"name": p, "x": p}}), type="event"))
        self.transport.capture_envelope(env)
        blob = self.raw_bytes()
        self.assert_no_sentinel(blob)
        self.assertNotIn("packages", blob)
        for e in self.transport.envelopes:
            if "sdk" in e.headers:
                self.assertEqual(set(e.headers["sdk"]), {"name", "version"})
            for item in e.items:
                sdk = item.payload.json["sdk"]
                self.assertEqual(sdk["version"], sentry_sdk.VERSION)
                self.assertTrue(sdk["name"].startswith("sentry.python"))
                self.assertLessEqual(set(sdk), {"name", "version", "integrations"})
                self.assertLessEqual(set(sdk["integrations"]), observability._PY_INTEGRATIONS)


if __name__ == "__main__":
    unittest.main()
