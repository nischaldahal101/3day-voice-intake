"""Read-only structure probe for real FieldPulse GET /customers.

Run by Nischal, once, to check the UNVERIFIED assumptions customer_source.py
makes about the real API (list location, pagination, created-date field).

    FIELDPULSE_READ_BASE_URL=... FIELDPULSE_READ_API_KEY=... \\
        python probe_fieldpulse_customers.py

What it does: ONE GET {FIELDPULSE_READ_BASE_URL}/customers with the READ key
and no query parameters. Never POST/PUT/PATCH/DELETE.

What it prints: STRUCTURE ONLY.
  - The endpoint as a fixed label, never the URL (it could hold credentials).
  - The media type only if it is one of a few known ones (application/json,
    text/html, ...), else "other". Never the raw header.
  - Key names only from customer_source.KNOWN_KEYS (an allowlist of paging,
    wrapper and known customer field names); every other key is only counted
    ("(+N unlisted keys)"). If the real date field turns out to be unlisted,
    the probe says how many date-like keys it hid; add the name to KNOWN_KEYS
    by hand and re-run.
  - Numbers only for known pagination count keys (total, last_page, ...);
    every other value is shown as its type.
  - For the date field, a format mask: ASCII digits -> 9, the separators
    "-/:.+, " and the ISO markers T/Z kept, every other character -> "a".
It never prints customer values (PII), the API key, the URL, or the body.
"""

import os
import sys

from requests import RequestException
from requests import get as _http_get

from customer_source import DATE_FIELDS, KNOWN_KEYS, LIST_KEYS, safe_keys

_ENDPOINT_LABEL = "GET <FIELDPULSE_READ_BASE_URL>/customers"
_PAGING_WORDS = ("page", "total", "count", "limit", "offset", "next", "prev",
                 "more", "cursor", "links", "meta", "pagination", "paging")
# Only these keys may have their (numeric/bool) value printed.
_PAGING_VALUE_KEYS = {
    "total", "total_count", "totalCount", "total_records", "total_pages", "totalPages",
    "last_page", "lastPage", "page_count", "current_page", "currentPage", "page",
    "per_page", "perPage", "limit", "offset", "count", "from", "to",
    "has_more", "hasMore", "has_next", "hasNext",
}
_MASK_KEEP = set("-/:.+, TZ")
_MEDIA_TYPES = frozenset({
    "application/json", "application/problem+json", "application/vnd.api+json",
    "application/xml", "text/xml", "text/html", "text/plain",
})


def _t(v):
    return type(v).__name__


def _mask(v):
    """Shape of a value without its content."""
    if isinstance(v, bool) or v is None:
        return _t(v)
    if isinstance(v, (int, float)):
        return f"{_t(v)} with {len(str(int(v)))} digits"
    if isinstance(v, str):
        out = "".join(
            "9" if c in "0123456789" else c if c in _MASK_KEEP else "a"
            for c in v[:40]
        )
        return f"string '{out}'" + (f" (+{len(v) - 40} chars)" if len(v) > 40 else "")
    if isinstance(v, dict):
        return f"object with keys {safe_keys(v)}"
    return _t(v)


def _media_type(header):
    """One of _MEDIA_TYPES, "(none)", or "other". Never header text."""
    mt = (header or "").split(";", 1)[0].strip().lower()
    if not mt:
        return "(none)"
    return mt if mt in _MEDIA_TYPES else "other"


def _value_for(key, v):
    if key in _PAGING_VALUE_KEYS:
        if isinstance(v, (bool, int, float)) or v is None:
            return repr(v)
        if isinstance(v, str) and v.strip().isdigit() and v.strip().isascii():
            return f"numeric string {v.strip()}"
    if isinstance(v, str):
        return "string (present)" if v else "empty string"
    if isinstance(v, (bool, int, float)) or v is None:
        return _t(v)
    return f"{_t(v)} (len {len(v)})" if hasattr(v, "__len__") else _t(v)


def _paging_report(label, d, depth=0):
    """Print pagination-looking keys (selected by name). Values only for the
    allowlisted count keys; everything else as its type."""
    for k in sorted(str(k) for k in d.keys()):
        if not any(w in k.lower() for w in _PAGING_WORDS):
            continue
        name = k if k in KNOWN_KEYS else "<unlisted key>"
        v = d[k]
        if isinstance(v, dict):
            print(f"  {label}.{name}: object with keys {safe_keys(v)}")
            if depth < 2 and name != "<unlisted key>":
                _paging_report(f"{label}.{name}", v, depth + 1)
            continue
        print(f"  {label}.{name}: {_value_for(k, v)}")


def main():
    base = (os.environ.get("FIELDPULSE_READ_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("FIELDPULSE_READ_API_KEY") or "").strip()
    if not base or not key:
        print("Set FIELDPULSE_READ_BASE_URL and FIELDPULSE_READ_API_KEY first. (Nothing was sent.)")
        return 2

    print(f"{_ENDPOINT_LABEL}  (read-only, no query params)")
    try:
        resp = _http_get(
            f"{base}/customers",
            headers={"x-api-key": key, "Accept": "application/json"},
            timeout=(5, 15),
        )
    except RequestException as exc:
        print(f"Request failed: {type(exc).__name__}")
        return 1

    print(f"HTTP status: {int(resp.status_code)}")
    print(f"Media type: {_media_type(resp.headers.get('Content-Type'))}")
    try:
        payload = resp.json()
    except ValueError:
        print("Body is not JSON.")
        return 1

    print(f"Top-level type: {_t(payload)}")
    if resp.status_code != 200:
        if isinstance(payload, dict):
            print(f"Error body keys: {safe_keys(payload)}")
            if payload.get("message") == "Missing Authentication Token":
                print("Body is exactly 'Missing Authentication Token' = wrong path.")
        print("Not 200. 401/403 = bad key or wrong path (docs/FIELDPULSE_API.md §10).")
        return 1

    items, container_label, container = None, None, None
    if isinstance(payload, list):
        items, container_label = payload, "(top level)"
    elif isinstance(payload, dict):
        print(f"Top-level keys: {safe_keys(payload, limit=40)}")
        if "error" in payload:
            e = payload["error"]
            print(f"  'error' is {_t(e)}: {e if isinstance(e, bool) else '(not a bool)'}")
        _paging_report("top", payload)
        if "response" in payload:
            r = payload["response"]
            print(f"'response' type: {_t(r)}")
            if isinstance(r, list):
                items, container_label = r, "response"
            elif isinstance(r, dict):
                print(f"'response' keys: {safe_keys(r, limit=40)}")
                _paging_report("response", r)
                container = r
                for k in LIST_KEYS:
                    if isinstance(r.get(k), list):
                        items, container_label = r[k], f"response.{k}"
                        break
        elif isinstance(payload.get("data"), list):
            items, container_label = payload["data"], "data"

    if items is None:
        print("Could not find a customer list. customer_source.py would return a 502 for this shape.")
        return 1

    print(f"Customer list at: {container_label} (list, length {len(items)})")
    if container is not None:
        for k in LIST_KEYS:
            if k in container and f"response.{k}" != container_label:
                print(f"  note: response.{k} also present ({_t(container[k])})")
    if not items:
        print("List is empty; no customer fields to show.")
        return 0

    first = items[0]
    if not isinstance(first, dict):
        print(f"First item is {_t(first)}, not an object.")
        return 1
    print(f"Fields on one customer ({len(first)}): {safe_keys(first, limit=80)}")
    print(f"  id type: {_t(first.get('id'))}")
    found = [f for f in DATE_FIELDS if f in first]
    print(f"Date fields present (checked in this order {list(DATE_FIELDS)}): {found or 'NONE'}")
    for f in found:
        print(f"  {f}: {_mask(first[f])}")
    other_dates = [k for k in first.keys() if isinstance(k, str)
                   and ("date" in k.lower() or k.lower().endswith("_at")) and k not in found]
    if other_dates:
        print(f"Other date-like field names: {safe_keys(dict.fromkeys(other_dates))}")
        if not found:
            print("  No known date field. If the count above shows unlisted keys, one of them "
                  "may be the created date: add likely names to customer_source.KNOWN_KEYS "
                  "(and DATE_FIELDS) and re-run.")
    with_date = sum(1 for c in items if isinstance(c, dict) and any(c.get(f) for f in DATE_FIELDS))
    print(f"Customers on this page with any known date field set: {with_date}/{len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
