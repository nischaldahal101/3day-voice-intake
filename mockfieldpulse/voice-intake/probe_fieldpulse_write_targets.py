"""Read-only probe of real FieldPulse to plan the write stages (stage 1).

Run by Nischal:

    FIELDPULSE_READ_BASE_URL=... FIELDPULSE_READ_API_KEY=... \\
        python3 probe_fieldpulse_write_targets.py

GET ONLY. Every request goes through customer_source._http_get (verb
hard-coded "GET"), and this script also installs a process-wide guard on
http.client.HTTPConnection.putrequest that raises on any other verb. There is
no helper here that takes a method.

It finds the test customer ("mockdata" in display_name, case-insensitive,
AND phone digits 3143596394), requiring EXACTLY one match, then prints:
  1. GET /customers/{id}: the flattened test-customer record (paths, types,
     values; fake data). Values on staff/user/secret-looking paths are hidden.
  2. GET /pipeline-status: id + name per stage (config).
  3. Candidate routes: status + exists/missing. Definition lists (custom
     fields, job types, tags, forms, lead sources) print item keys plus
     id/name/label/type values (config). Record lists that may belong to
     other customers or staff (jobs, projects, comments, users) print ONLY
     key paths/types and counts, never values.
  4. A summary.
Never prints the API key, base URL, headers or raw bodies.
Each request: 10s max. Whole run: _TOTAL_BUDGET_S max (plus the customer
list's own 20s budget and DNS time).
"""

import http.client
import json
import os
import re
import sys
import time

import customer_source
from customer_source import CustomerSourceError, LIST_KEYS

# --- GET-only guard (process-wide, belt and braces) --------------------------

_orig_putrequest = http.client.HTTPConnection.putrequest


def _get_only_putrequest(self, method, url, *args, **kwargs):
    if method != "GET":
        raise RuntimeError(f"probe is GET-only; refused {method!r}")
    return _orig_putrequest(self, method, url, *args, **kwargs)


http.client.HTTPConnection.putrequest = _get_only_putrequest   # HTTPS inherits

# --- Tunables -----------------------------------------------------------------

_PER_REQUEST_S = 10.0
_TOTAL_BUDGET_S = 120.0
_MIN_START_S = 1.0
_TARGET_PHONE = "3143596394"
_NAME_TOKEN = "mockdata"
_HIGHLIGHT = ("custom", "field", "additional", "meta", "extra", "next_step")
# Values on these paths are hidden even on the test customer (staff / secrets).
_HIDE_TOKENS = ("user", "created_by", "updated_by", "assigned", "owner", "team",
                "technician", "employee", "staff", "password", "token", "secret",
                "api_key", "apikey")
_DEF_VALUE_KEYS = ("id", "name", "label", "title", "key", "slug", "type",
                   "field_type", "input_type", "data_type", "entity", "entity_type",
                   "model", "model_type", "object_type", "applies_to", "required")
_MAX_VALUE_CHARS = 500
_MAX_DEF_ITEMS = 100
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_RUN_DEADLINE = None


def _t(v):
    return type(v).__name__


def _safe_key(k):
    """Key name for output; hides keys that could themselves be personal text."""
    k = str(k)
    if "@" in k or len(k) > 64 or re.fullmatch(r"\+?[\d\s().-]{7,}", k):
        return "<redacted key>"
    return k


# --- Transport ----------------------------------------------------------------

def _get(base, key, path, params=None):
    """One GET. Returns (status|None, payload|None, note). Never raises."""
    left = _RUN_DEADLINE - time.monotonic()
    if left < _MIN_START_S:
        return None, None, "skipped (total time budget used)"
    deadline = time.monotonic() + min(_PER_REQUEST_S, left)
    headers = {"x-api-key": key, "Accept": "application/json",
               "Accept-Encoding": "identity"}
    try:
        status, body = customer_source._http_get(
            f"{base}{path}", params=params or {}, headers=headers, deadline=deadline)
    except CustomerSourceError as exc:
        return None, None, f"request failed: {exc}"
    except Exception as exc:                 # transport; class name only
        return None, None, f"request failed: {type(exc).__name__}"
    try:
        payload = json.loads(body)
    except ValueError:
        return status, None, "body is not JSON"
    return status, payload, ""


def _classify(status, payload, note):
    if status is None:
        return note
    if isinstance(payload, dict) and payload.get("message") == "Missing Authentication Token":
        return "missing route"
    if status == 200:
        if isinstance(payload, dict) and payload.get("error") is True:
            return "exists (200 but error:true)"
        return "exists"
    if status in (401, 403):
        return f"error (HTTP {status}: auth/permission)"
    return f"error (HTTP {status})"


def _error_message(payload):
    if isinstance(payload, dict):
        for k in ("message", "error", "errors"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v[:160]
    return None


def _unwrap(payload):
    """(location, list|None, container dict|None)."""
    if isinstance(payload, list):
        return "(top level)", payload, None
    if not isinstance(payload, dict):
        return None, None, None
    r = payload.get("response", payload.get("data"))
    where = "response" if "response" in payload else "data"
    if isinstance(r, list):
        return where, r, payload
    if isinstance(r, dict):
        for k in LIST_KEYS:
            if isinstance(r.get(k), list):
                return f"{where}.{k}", r[k], r
        return where, None, r
    return None, None, payload


def _count_info(container):
    if not isinstance(container, dict):
        return ""
    bits = [f"{k}={container[k]}" for k in ("total_count", "total", "total_pages", "per_page", "page")
            if isinstance(container.get(k), (int, str)) and str(container.get(k)).isdigit()]
    return ("  [" + ", ".join(bits) + "]") if bits else ""


# --- Flattening ---------------------------------------------------------------

def _flatten(v, path=""):
    """Yield (path, value) for every leaf; empty dict/list are leaves."""
    if isinstance(v, dict) and v:
        for k in v:
            yield from _flatten(v[k], f"{path}.{k}" if path else str(k))
    elif isinstance(v, list) and v:
        for i, item in enumerate(v):
            yield from _flatten(item, f"{path}[{i}]")
    else:
        yield path, v


def _schema_paths(v, path="", depth=0):
    """Key paths + types only (no values); lists show their first item as [0]."""
    if isinstance(v, dict):
        if not v or depth > 4:
            return [(path or "(root)", f"object({len(v)} keys)")]
        out = []
        for k in v:
            out += _schema_paths(v[k], f"{path}.{_safe_key(k)}" if path else _safe_key(k), depth + 1)
        return out
    if isinstance(v, list):
        if not v or depth > 4:
            return [(path, f"list(len {len(v)})")]
        return [(path, f"list(len {len(v)})")] + _schema_paths(v[0], f"{path}[0]", depth + 1)
    return [(path, _t(v))]


def _hidden(path):
    p = path.lower()
    return any(tok in p for tok in _HIDE_TOKENS)


def _highlighted(path):
    p = path.lower()
    return any(tok in p for tok in _HIGHLIGHT)


def _show_value(v):
    if isinstance(v, str):
        s = json.dumps(v)
        return s if len(s) <= _MAX_VALUE_CHARS else s[:_MAX_VALUE_CHARS] + f"... (+{len(s) - _MAX_VALUE_CHARS} chars)"
    if isinstance(v, (dict, list)):
        return "{}" if isinstance(v, dict) else "[]"
    return json.dumps(v)


# --- Steps --------------------------------------------------------------------

def _find_test_customer():
    """Exactly one customer matching name token AND phone, else None."""
    customers = customer_source.list_customers()
    name_hits = [c for c in customers if _NAME_TOKEN in (c.get("display_name") or "").lower()]
    phone_hits = [c for c in customers if _phone_matches(c.get("phone"))]
    both = [c for c in name_hits if _phone_matches(c.get("phone"))]
    print(f"Customers read: {len(customers)}; name matches: {len(name_hits)}; "
          f"phone matches: {len(phone_hits)}; both: {len(both)}")
    if len(both) != 1:
        print(f"STOP: need EXACTLY one customer with '{_NAME_TOKEN}' in display_name AND phone "
              f"{_TARGET_PHONE}; found {len(both)}. Nothing else was probed.")
        return None
    return both[0]


def _phone_matches(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits == _TARGET_PHONE


def _probe_customer(base, key, cid):
    print(f"\n=== 1. GET /customers/{cid} (test customer, values shown) ===")
    status, payload, note = _get(base, key, f"/customers/{cid}")
    print(f"status: {status}  ->  {_classify(status, payload, note)}")
    if status != 200 or not isinstance(payload, dict):
        msg = _error_message(payload)
        if msg:
            print(f"message: {msg}")
        return []
    print(f"wrapper top-level keys: {sorted(_safe_key(k) for k in payload)}")
    record = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    if isinstance(record.get("id"), (int, str)) and str(record.get("id")) != str(cid):
        print("STOP: record id does not match the test customer id; not printing it.")
        return []
    leaves = list(_flatten(record))
    print(f"top-level keys: {len(record)}; leaf paths: {len(leaves)}")
    flagged = []
    for path, v in leaves:
        mark = ">>" if _highlighted(path) else "  "
        if mark == ">>":
            flagged.append(path)
        shown = "(value hidden: staff/secret-looking path)" if _hidden(path) else _show_value(v)
        print(f"{mark} {path}  [{_t(v)}]  = {shown}")
    print(f"\nHighlighted paths ({len(flagged)}; contain {'/'.join(_HIGHLIGHT)}):")
    for p in flagged:
        print(f"   {p}")
    nonempty_unknown = [p for p, v in leaves if v not in (None, "", [], {}, 0, False)
                        and p.split(".")[0].split("[")[0] not in customer_source.KNOWN_KEYS]
    print(f"\nNon-empty paths whose top-level key is outside customer_source.KNOWN_KEYS "
          f"({len(nonempty_unknown)}):")
    for p in nonempty_unknown:
        print(f"   {p}")
    return flagged


def _probe_pipeline(base, key):
    print("\n=== 2. GET /pipeline-status (config) ===")
    status, payload, note = _get(base, key, "/pipeline-status")
    print(f"status: {status}  ->  {_classify(status, payload, note)}")
    where, items, _ = _unwrap(payload)
    if items is None:
        print("no stage list found")
        return
    print(f"stages at {where}: {len(items)}")
    for it in items:
        if isinstance(it, dict):
            name = it.get("name", it.get("title", it.get("label")))
            print(f"   id={it.get('id')!r}  name={name!r}")


def _route_list(cid):
    """(path, params, kind). kind: 'def' = config table; 'rec' = records."""
    return [
        ("/custom-fields", None, "def"),
        ("/custom_fields", None, "def"),
        ("/customfields", None, "def"),
        ("/additional-fields", None, "def"),
        ("/customer-custom-fields", None, "def"),
        ("/job-custom-fields", None, "def"),
        ("/job-types", None, "def"),
        ("/jobs", {"customer_id": cid}, "rec"),
        ("/projects", {"customer_id": cid}, "rec"),
        ("/comments", {"customer_id": cid}, "rec"),
        (f"/customers/{cid}/comments", None, "rec"),
        (f"/customers/{cid}/jobs", None, "rec"),
        ("/users", None, "rec"),
        ("/team-members", None, "rec"),
        ("/tags", None, "def"),
        ("/forms", None, "def"),
        ("/lead-source", None, "def"),
    ]


def _probe_routes(base, key, cid):
    print("\n=== 3. Candidate routes ===")
    results = []
    for path, params, kind in _route_list(cid):
        label = path + (f"?customer_id={cid}" if params else "")
        status, payload, note = _get(base, key, path, params)
        verdict = _classify(status, payload, note)
        print(f"\n--- GET {label}  [{'config' if kind == 'def' else 'records: keys/counts only'}]")
        print(f"status: {status}  ->  {verdict}")
        info = {"route": label, "verdict": verdict, "kind": kind, "count": None}
        results.append(info)
        if status != 200:
            msg = _error_message(payload)
            if msg and verdict != "missing route":
                print(f"message: {msg}")
            continue
        if isinstance(payload, dict):
            print(f"top-level keys: {sorted(_safe_key(k) for k in payload)}")
        where, items, container = _unwrap(payload)
        if isinstance(container, dict) and container is not payload:
            print(f"container keys: {sorted(_safe_key(k) for k in container)}")
        if items is None:
            if container is not None and kind == "rec":
                print("not a list; shape (paths/types only):")
                for p, ty in _schema_paths(container)[:80]:
                    print(f"   {p}  [{ty}]")
            elif container is not None:
                print("not a list; shape:")
                for p, ty in _schema_paths(container)[:80]:
                    print(f"   {p}  [{ty}]")
            continue
        info["count"] = len(items)
        print(f"list at {where}: length {len(items)}{_count_info(container)}")
        dict_items = [i for i in items if isinstance(i, dict)]
        if not dict_items:
            continue
        if kind == "rec":
            _print_record_schema(path, dict_items, cid)
        else:
            _print_definitions(dict_items)
    return results


def _print_record_schema(path, items, cid):
    union = sorted({_safe_key(k) for i in items for k in i})
    print(f"key names across items ({len(union)}): {union}")
    print("one item, key paths + types (NO values):")
    for p, ty in _schema_paths(items[0]):
        print(f"   {p}  [{ty}]")
    if "customer_id" in union:
        mine = sum(1 for i in items if str(i.get("customer_id")) == str(cid))
        print(f"items with customer_id == test customer: {mine}/{len(items)}"
              + ("  (filter NOT honoured)" if mine < len(items) else ""))


def _print_definitions(items):
    union = sorted({_safe_key(k) for i in items for k in i})
    print(f"item key names ({len(union)}): {union}")
    for it in items[:_MAX_DEF_ITEMS]:
        parts = [f"{k}={it[k]!r}" for k in _DEF_VALUE_KEYS
                 if k in it and isinstance(it[k], (str, int, float, bool, type(None)))]
        opts = it.get("options", it.get("values", it.get("choices")))
        if isinstance(opts, list):
            parts.append(f"options=list(len {len(opts)})")
        print(f"   {'  '.join(parts) or '(no id/name/label/type fields)'}")
    if len(items) > _MAX_DEF_ITEMS:
        print(f"   ... {len(items) - _MAX_DEF_ITEMS} more not shown")


def _summary(flagged, results):
    print("\n=== 4. Summary ===")
    exists = [r for r in results if r["verdict"].startswith("exists")]
    missing = [r for r in results if r["verdict"] == "missing route"]
    other = [r for r in results if r not in exists and r not in missing]
    print(f"routes that exist ({len(exists)}): "
          + ", ".join(f"{r['route']}" + (f" ({r['count']} items)" if r["count"] is not None else "")
                      for r in exists))
    print(f"missing routes ({len(missing)}): " + ", ".join(r["route"] for r in missing))
    if other:
        print(f"errors/skipped ({len(other)}): " + ", ".join(f"{r['route']} [{r['verdict']}]" for r in other))
    defs = [r for r in exists if r["kind"] == "def" and "field" in r["route"] and r["count"]]
    print("custom fields:")
    if defs:
        print("   definition route(s) with items: " + ", ".join(r["route"] for r in defs))
    else:
        print("   no custom-field definition route returned items")
    if flagged:
        tops = sorted({p.split(".")[0].split("[")[0] for p in flagged})
        print(f"   on the customer record, highlighted top-level keys: {tops}")
    else:
        print("   no highlighted paths on the customer record")


def main():
    global _RUN_DEADLINE
    assert http.client.HTTPConnection.putrequest is _get_only_putrequest
    base = (os.environ.get("FIELDPULSE_READ_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("FIELDPULSE_READ_API_KEY") or "").strip()
    if not base or not key:
        print("Set FIELDPULSE_READ_BASE_URL and FIELDPULSE_READ_API_KEY first. (Nothing was sent.)")
        return 2
    _RUN_DEADLINE = time.monotonic() + _TOTAL_BUDGET_S
    print("Read-only probe of FieldPulse (GET only; key and URL never printed)\n")
    try:
        test = _find_test_customer()
    except CustomerSourceError as exc:
        print(f"STOP: could not list customers: {exc}")
        return 1
    if test is None:
        return 1
    cid = test.get("id")
    if cid is None or not _SAFE_ID.match(str(cid)):
        print("STOP: test customer id is missing or has unexpected characters.")
        return 1
    cid = str(cid)
    print(f"Test customer: id={cid}  display_name={test.get('display_name')!r}")

    flagged = _probe_customer(base, key, cid)
    _probe_pipeline(base, key)
    results = _probe_routes(base, key, cid)
    _summary(flagged, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
