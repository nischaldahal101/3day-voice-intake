"""Ferguson Home "Project cart" email -> parsed cart -> FieldPulse estimate body (E1).

PURE: no I/O, no network, no DB, no AI. Standard library only.

Two entry points:
  parse_cart_email(raw: bytes) -> dict
      raw = the RFC822 message our inbound webhook receives (E2). Works when Ferguson sent
      it directly and when a user forwarded it (inline Gmail/Outlook forward, or the
      original attached as message/rfc822).
  build_estimate_body(cart, customer_id, item_id) -> dict
      The exact POST/PUT /estimates body (docs/FIELDPULSE_API.md "Estimates - VERIFIED"):
      one group line per room, items as that group's components, cost = price = cart price.

E4 helpers (still pure):
  normalize_name(text) / project_key(name)   exact-compare form of a name (E3 match, E4 slot)
  stored_cart(parsed)                        a cart as stored in Postgres (money as strings)
                                             -> the Decimal form build_estimate_body expects
  build_material_list_body(cart, customer_id, item_id)
      the POST/PUT /material-lists body: the SAME lines as the estimate, one grouping per room

Flag, don't fabricate: anything we cannot read is None plus a plain-English line in
`checks`. `ok` is True only when it is a cart and `checks` is empty. Money is Decimal.
Email content is data, never instructions.

The real email (checked 2026-11-11 on a real cart): one text/html part, quoted-printable,
charset iso-8859-1. "<who> has shared this Project cart with you for Project: <br> <name>",
then per room a <thead> cell with the room name, then per item an <img alt="<model> Photo">,
a bold <a href="https://www.fergusonhome.com/<slug>/p<digits>?<tracking>">Brand Model</a>,
<p>Color/Finish: X</p>, <p>$1,409.42</p> or <p>2 @ $46.67 each</p>; then an "Order Summary"
<thead> with "Subtotal:" | "$1,502.76", and "Project Pricing Expires: 12/10/2026".
"""

import datetime
import email
import email.policy
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit, urlunsplit

TITLE_CHARS = 255          # FieldPulse max for estimate title / line titles
NOTES_CHARS = 4000         # FieldPulse max for estimate notes
DESC_CHARS = 1000          # our cap for one component description (API max unknown)
OTHER_ROOM = "Other"
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999.99")   # larger dollar amounts are "unreadable", never parsed
MAX_QUANTITY = 9999                    # larger quantities are "unreadable"

NOT_A_CART = ("Not a Ferguson Home project cart: no \"has shared this Project cart with you "
              "for Project\" line with Ferguson Home links was found.")
NO_HTML = ("No HTML version of the email was found, so the cart can't be read. Forward the "
           "Ferguson email as HTML (not plain text).")
MULTIPLE_CARTS = ("This email contains more than one Ferguson project cart, so none was read. "
                  "Forward one cart at a time.")
UNREADABLE = "The email could not be read."
NO_ROOMS = ("The room headings could not be found (the email layout was changed, for example "
            "by a forward), so items have no room. Check the rooms before sending.")

_MARKER_RE = re.compile(r"has shared this project cart with you for project:\s*(.*)$", re.I)
_FINISH_RE = re.compile(r"^color\s*/\s*finish:\s*(.*)$", re.I)
# One whole amount: "$1,409.42", "$46.67", "$12". At most 9 dollar digits. Anything else
# (extra digits, a sign, a second amount, words) is not an amount.
_AMOUNT_RE = re.compile(r"^\$\s*(\d{1,3}(?:,\d{3}){1,2}|\d{1,9})(\.\d{2})?$")
_QTY_EACH_RE = re.compile(r"^(\d+)\s*@\s*(\S.*?)\s*each$", re.I)
_PRICE_HINT_RE = re.compile(r"\$|@|\beach\b", re.I)
_SUBTOTAL_RE = re.compile(r"^subtotal\s*:?\s*(.*)$", re.I)
_SUMMARY_RE = re.compile(r"^order summary:?$", re.I)
_EXPIRES_RE = re.compile(r"project pricing expires:\s*(\S+)", re.I)
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_PRODUCT_PATH_RE = re.compile(r"^/[^/]+/p\d+/?$")

# <br> is a block boundary too: forwards often turn <p> fields into <br>-separated lines.
_BLOCK_TAGS = {"p", "div", "td", "th", "tr", "table", "thead", "tbody", "tfoot", "li",
               "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "br",
               "section", "body", "html", "center", "hr"}
_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"style", "script", "head", "title"}


# ---------------------------------------------------------------------------
# public: parse

def parse_cart_email(raw):
    """RFC822 bytes -> cart dict (see module docstring). Never raises on bad input.

    Every text/html part (incl. attached messages) is read; each "has shared this Project
    cart" line starts a cart. Identical copies (a forward's inline copy and its attachment)
    count once; two different carts -> refused (MULTIPLE_CARTS), never a silent pick.
    """
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        msg = email.message_from_bytes(raw or b"", policy=email.policy.default)
        htmls = list(_html_parts(msg))
        if not htmls:
            return _not_cart(NO_HTML)
        carts = []
        for html in htmls:
            for segment in _cart_segments(_events(html)):
                cart = _parse_segment(segment)
                if cart not in carts:
                    carts.append(cart)
        if not carts:
            return _not_cart(NOT_A_CART)
        if len(carts) > 1:
            return _not_cart(MULTIPLE_CARTS)
        return carts[0]
    except Exception:  # noqa: BLE001 - untrusted input must never crash the caller
        return _not_cart(UNREADABLE)


# ---------------------------------------------------------------------------
# public: estimate body

def build_estimate_body(cart, customer_id, item_id):
    """Parsed cart -> FieldPulse POST/PUT /estimates body (E0 layout).

    Raises ValueError when the cart can't be turned into honest lines (no project name,
    no items, or an item without a quantity or price): we never send a guessed line.
    """
    customer_id = _positive_int(customer_id, "customer_id")
    item_id = _positive_int(item_id, "item_id")
    project = (cart or {}).get("project_name")
    items = (cart or {}).get("items") or []
    if not project:
        raise ValueError("The cart has no project name.")
    if not items:
        raise ValueError("The cart has no items.")
    for n, it in enumerate(items, 1):
        q, p = it.get("quantity"), it.get("unit_price")
        if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= MAX_QUANTITY:
            raise ValueError(f"Item {n} has no readable quantity.")
        if not isinstance(p, Decimal) or not p.is_finite() or not 0 <= p <= MAX_AMOUNT:
            raise ValueError(f"Item {n} has no readable price.")
        if not it.get("model"):
            raise ValueError(f"Item {n} has no readable model.")

    expires = _mmddyyyy(cart.get("pricing_expires"))
    groups = {}
    for it in items:
        room = (it.get("room") or "").strip() or OTHER_ROOM
        groups.setdefault(room, []).append(it)

    line_items = []
    for n, (room, room_items) in enumerate(groups.items(), 1):
        line_items.append({
            "line_number": n,
            "line_type": "group",
            "group_number": 1,
            "line_title": _cap(room, TITLE_CHARS),
            "line_quantity": 1,
            "line_price_visibility": "group",
            "line_visibility": "all",
            "line_components": [_component(it, room, project, expires, item_id)
                                for it in room_items],
        })

    notes = f'Draft from Ferguson Home cart "{project}"'
    if expires:
        notes += f" (pricing expires {expires})"
    notes += (f". {len(items)} item(s), lines total ${_lines_total(items):,.2f}. "
              "Unit price and unit cost are the cart price.")
    return {
        "customer_id": customer_id,
        "tax_rate": "0.00",
        "title": _cap(f"Ferguson cart: {project}", TITLE_CHARS),
        "notes": _cap(notes, NOTES_CHARS),
        "line_items": line_items,
    }


LIST_PRICE_VISIBILITY = "group"        # same as fieldpulse_sync.VENDOR_PRICE_VISIBILITY
LIST_COMPONENT_VISIBILITY = "all"
_STORED_AMOUNT_RE = re.compile(r"^\d{1,9}(\.\d{1,2})?$")
_APOSTROPHES = "'\u2018\u2019\u02bc\u0060\u00b4"


def build_material_list_body(cart, customer_id, item_id):
    """Parsed cart -> FieldPulse POST/PUT /material-lists body (E4). Built FROM the estimate
    body, so the list has exactly the estimate's lines: one grouping per room, sku = model,
    unit_cost = unit_price = cart price, prices hidden from the customer like the vendor list.
    Raises ValueError exactly when build_estimate_body does."""
    est = build_estimate_body(cart, customer_id, item_id)
    groupings = []
    for n, group in enumerate(est["line_items"], 1):
        groupings.append({
            "id": None, "order_number": n, "title": group["line_title"], "type": "group",
            "component_visibility": LIST_COMPONENT_VISIBILITY,
            "price_visibility": LIST_PRICE_VISIBILITY,
            "line_items": [{"id": None, "item_id": c["item_id"], "title": c["title"],
                            "description": c["description"], "sku": c["sku"],
                            "quantity": c["quantity"], "unit_price": c["unit_price"],
                            "unit_cost": c["unit_cost"], "type": "product"}
                           for c in group["line_components"]],
        })
    notes = est["notes"] + " This list is replaced when the cart is forwarded again."
    return {"customer_id": est["customer_id"], "is_template": False, "title": est["title"],
            "notes": _cap(notes, NOTES_CHARS), "groupings": groupings}


def stored_cart(parsed):
    """A cart as stored in Postgres (Decimals saved as strings) -> the parser's own shape
    (unit prices as Decimal). Anything that isn't a plain amount string stays as it is, so
    build_estimate_body refuses it ("no readable price") rather than guessing."""
    parsed = parsed if isinstance(parsed, dict) else {}
    items = []
    for it in parsed.get("items") or []:
        it = dict(it) if isinstance(it, dict) else {}
        p = it.get("unit_price")
        if isinstance(p, str) and _STORED_AMOUNT_RE.match(p):
            try:
                it["unit_price"] = Decimal(p).quantize(CENT)
            except InvalidOperation:
                pass
        items.append(it)
    return {**parsed, "items": items}


def normalize_name(text):
    """Name for exact comparison: NFKC, case-folded, apostrophes removed (O'Brien =
    OBrien), every other punctuation/symbol -> a space, spaces collapsed. '' if empty.
    Accented letters stay distinct (no fuzzy matching of any kind)."""
    if not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = "".join("" if ch in _APOSTROPHES else
                " " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in t)
    return " ".join(t.split())


def project_key(name):
    """The project part of an E4 send slot (user + customer + this): '' = no usable name."""
    return normalize_name(name)[:255]


# ---------------------------------------------------------------------------
# MIME

def _html_parts(msg):
    """Every text/html part, in order, including inside attached message/rfc822."""
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            text = part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset / broken encoding
            payload = part.get_payload(decode=True) or b""
            text = payload.decode("utf-8", "replace")
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if text:
            yield text


# ---------------------------------------------------------------------------
# HTML -> flat events:
#   ("block", text, in_thead, cell)  one line of text; cell = id of the innermost <td>/<th>
#   ("section",)                     a <thead> starts (a new room heading)
#   ("link", href, text) | ("img", src, alt)

class _Flattener(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self._buf = []
        self._skip = 0
        self._thead = 0
        self._cells = [0]
        self._next_cell = 1
        self._link = None      # [href, [text]] while inside <a>

    def _flush(self):
        text = _norm("".join(self._buf))
        self._buf = []
        if text:
            self.events.append(("block", text, self._thead > 0, self._cells[-1]))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        a = dict(attrs)
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _CELL_TAGS:
            self._cells.append(self._next_cell)
            self._next_cell += 1
        elif tag == "br" and self._link is not None:
            self._link[1].append(" ")
        elif tag == "thead":
            self._thead += 1
            self.events.append(("section",))
        elif tag == "a":
            self._flush()
            self._link = [a.get("href") or "", []]
        elif tag == "img":
            self._flush()
            self.events.append(("img", a.get("src") or "", _norm(a.get("alt") or "")))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _SKIP_TAGS:
            self._skip -= 1
        elif tag in _CELL_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._link is not None:
            href, parts = self._link
            self._link = None
            self.events.append(("link", href, _norm("".join(parts))))
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _CELL_TAGS and len(self._cells) > 1:
            self._cells.pop()
        elif tag == "thead":
            self._thead = max(0, self._thead - 1)

    def handle_data(self, data):
        if self._skip:
            return
        if self._link is not None:
            self._link[1].append(data)   # link text is not block text
        else:
            self._buf.append(data)

    def close(self):
        super().close()
        if self._link is not None:
            self.events.append(("link", self._link[0], _norm("".join(self._link[1]))))
            self._link = None
        self._flush()


def _events(html):
    p = _Flattener()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - keep whatever was read
        pass
    return p.events


def _cart_segments(events):
    """Split at each cart line; keep segments that link to fergusonhome.com."""
    starts = [i for i, ev in enumerate(events)
              if ev[0] == "block" and _MARKER_RE.search(ev[1])]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(events)
        segment = events[start:end]
        if any(ev[0] == "link" and _is_ferguson_host(_unwrap(ev[1].strip()))
               for ev in segment):
            yield segment


def _is_ferguson_host(href):
    try:
        u = urlsplit(href)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and (
        host == "fergusonhome.com" or host.endswith(".fergusonhome.com"))


# ---------------------------------------------------------------------------
# one cart segment -> cart

def _parse_segment(seg):
    checks = []
    project = _project_name(seg)
    if not project:
        checks.append("The project name could not be read.")
    has_sections = any(ev[0] == "section" for ev in seg)
    if not has_sections:
        checks.append(NO_ROOMS)

    items, cur = [], None
    room, room_heading_count = None, 0
    imgs = []              # images since the previous item (the next item's photo)
    in_summary = False
    sub_texts = []         # every Subtotal value line (label row + its whole value cell)
    sub_awaiting, sub_label_cell, sub_value_cell = False, None, None
    expiry_texts = []

    def finish():
        nonlocal cur
        if cur is not None:
            items.append(cur)
            cur = None

    for ev in seg[1:]:
        kind = ev[0]
        if kind == "section":
            if not in_summary:
                finish()
                room, room_heading_count = None, 0   # a new heading: never inherit a room
            continue
        if kind == "block":
            text, in_thead = ev[1], ev[2]
            cell = ev[3]
            found = [x.group(1) for x in _EXPIRES_RE.finditer(text)]
            expiry_texts.extend(found)
            if in_summary:
                sm = _SUBTOTAL_RE.match(text)
                if sm:
                    if sm.group(1):
                        sub_texts.append(sm.group(1))
                        sub_awaiting, sub_value_cell = False, cell
                    else:                             # amount is in the next cell
                        sub_awaiting, sub_label_cell, sub_value_cell = True, cell, None
                elif sub_awaiting:
                    sub_texts.append(text)
                    sub_awaiting, sub_value_cell = False, cell
                elif sub_value_cell is not None and cell == sub_value_cell:
                    sub_texts.append(text)            # more lines in the same value cell
                else:
                    sub_value_cell = None
                continue
            if _SUMMARY_RE.match(text):
                finish()
                in_summary = True
                continue
            if found:
                continue
            if in_thead:
                finish()
                room_heading_count += 1
                # two text lines in one heading: we can't tell which is the room
                room = text if room_heading_count == 1 else None
            elif cur is not None:
                _detail(cur, text)
            continue
        if kind == "img":
            if not in_summary and ev[1]:
                imgs.append((ev[1], ev[2]))
            continue
        if kind == "link" and not in_summary:
            url = _product_url(ev[1])
            if not url or not ev[2]:
                continue
            finish()
            photos = list(dict.fromkeys(imgs))
            cur = {"room": room, "link_text": ev[2], "product_url": url,
                   "image_url": photos[0][0] if len(photos) == 1 else None,
                   "_img_alt": photos[0][1] if len(photos) == 1 else "",
                   "_photo_conflict": len(photos) > 1,
                   "_finishes": [], "_prices": []}
            imgs = []
    finish()

    out_items = [_finish_item(n, it, checks, has_sections) for n, it in enumerate(items, 1)]
    if not out_items:
        checks.append("No items were found in the cart.")

    subtotal = _one_value(
        sub_texts, _amount, checks,
        missing="The Subtotal could not be found in the Order Summary.",
        unreadable="The Subtotal in the Order Summary could not be read.",
        conflict="More than one Subtotal was found ({}), so none was used.",
        show=lambda v: f"${v:,.2f}")
    if out_items and subtotal is not None:
        if all(it["quantity"] is not None and it["unit_price"] is not None for it in out_items):
            total = _lines_total(out_items)
            if total != subtotal:
                checks.append(f"The lines add up to ${total:,.2f} but the email's Subtotal is "
                              f"${subtotal:,.2f}.")
        else:
            checks.append("The lines could not be checked against the Subtotal because some "
                          "items have no readable quantity or price.")

    pricing_expires = _one_value(
        expiry_texts, _iso_date, checks,
        missing="The project pricing expiry date could not be found.",
        unreadable="The project pricing expiry date could not be read.",
        conflict="More than one project pricing expiry date was found ({}), so none was used.",
        show=str)

    return {"project_name": project, "pricing_expires": pricing_expires,
            "subtotal": subtotal, "items": out_items, "checks": checks,
            "ok": not checks}


def _one_value(texts, parse, checks, missing, unreadable, conflict, show):
    """Several places may state the same value. Exactly one readable distinct value -> it;
    none -> `missing`; any unreadable -> `unreadable`; two different ones -> `conflict`.
    Never picks the first of several."""
    if not texts:
        checks.append(missing)
        return None
    values = [parse(t) for t in texts]
    if any(v is None for v in values):
        bad = next(t for t, v in zip(texts, values) if v is None)
        checks.append(f"{unreadable[:-1]} (\"{_norm(bad)[:40]}\").")
        return None
    distinct = list(dict.fromkeys(values))
    if len(distinct) > 1:
        checks.append(conflict.format(", ".join(show(v) for v in distinct[:3])))
        return None
    return distinct[0]


def _project_name(seg):
    """Name after "for Project:" on the same line, else the next line IN THE SAME CELL
    (the real email puts a <br> before the name; a forward may wrap it in a <div>)."""
    first = seg[0]
    m = _MARKER_RE.search(first[1])
    name = m.group(1).strip() if m else ""
    if name:
        return name
    for ev in seg[1:]:
        if ev[0] != "block":
            return None                # an image/link/heading came first: no name here
        if ev[3] != first[3] or ev[2]:
            return None
        return ev[1].strip() or None
    return None


def _detail(cur, text):
    fm = _FINISH_RE.match(text)
    if fm:
        cur["_finishes"].append(fm.group(1).strip())
    elif _PRICE_HINT_RE.search(text):
        cur["_prices"].append(text)


def _finish_item(n, it, checks, has_sections):
    label = f"Item {n} ({it['link_text'][:80]})"
    model, brand, alt_model = _split_brand_model(it["link_text"], it["_img_alt"])
    if alt_model is not None:
        checks.append(f"{label}: the photo is labelled model \"{alt_model[:40]}\" but the link "
                      "says otherwise, so brand and model were not used.")
    elif brand is None:
        checks.append(f"{label}: the brand could not be separated from the model number.")
    if it["_photo_conflict"]:
        checks.append(f"{label}: more than one photo was found, so none was used.")

    finishes = list(dict.fromkeys(f for f in it["_finishes"] if f))
    finish = finishes[0] if len(finishes) == 1 else None
    if not finishes:
        checks.append(f"{label}: no Color/Finish was found.")
    elif len(finishes) > 1:
        checks.append(f"{label}: more than one Color/Finish was found "
                      f"({'; '.join(x[:40] for x in finishes[:3])}), so none was used.")

    room = it["room"]
    if not room and has_sections:
        checks.append(f"{label}: no room heading could be read; it will go under "
                      f"\"{OTHER_ROOM}\".")

    quantity = unit_price = None
    prices = list(dict.fromkeys(it["_prices"]))
    if not prices:
        checks.append(f"{label}: no price was found.")
    elif len(prices) > 1:
        checks.append(f"{label}: more than one price was found "
                      f"({'; '.join(x[:40] for x in prices[:3])}), so none was used.")
    else:
        quantity, unit_price = _read_price_line(prices[0], label, checks)
    return {"room": room, "brand": brand, "model": model, "finish": finish,
            "quantity": quantity, "unit_price": unit_price,
            "product_url": it["product_url"], "image_url": it["image_url"]}


def _read_price_line(line, label, checks):
    """'$X' -> (1, X); 'N @ $X each' -> (N, X); anything else -> nulls + a check."""
    shown = line[:80]
    whole = _amount(line)
    if whole is not None:
        return 1, whole
    qm = _QTY_EACH_RE.match(line)
    if qm:
        unit_price = _amount(qm.group(2))
        if unit_price is None:
            checks.append(f"{label}: the price in \"{shown}\" could not be read.")
        qty_text = qm.group(1)
        quantity = int(qty_text) if len(qty_text) <= 4 else None
        if quantity is None or not 1 <= quantity <= MAX_QUANTITY:
            checks.append(f"{label}: the quantity in \"{shown}\" is not a usable quantity.")
            quantity = None
        return quantity, unit_price
    if re.search(r"@|\beach\b", line, re.I):
        # quantity unreadable ("two @ $46.67 each"): the WHOLE price part must be one amount
        price_part = re.sub(r"\s*\beach\s*$", "", line.rsplit("@", 1)[-1], flags=re.I)
        unit_price = _amount(price_part)
        checks.append(f"{label}: the quantity could not be read from \"{shown}\".")
        if unit_price is None:
            checks.append(f"{label}: the price in \"{shown}\" could not be read.")
        return None, unit_price
    checks.append(f"{label}: the price line \"{shown}\" could not be read.")
    return None, None


def _split_brand_model(text, img_alt):
    """'Signature Hardware 483534' -> ('483534', 'Signature Hardware', None).

    The image alt is '<model> Photo'; when the link text ends with that model we trust it
    (handles a model with spaces); no alt -> the last word is the model. When the alt names
    a model the link text doesn't end with, the two disagree: (None, None, alt_model).
    """
    alt_model = None
    if img_alt.lower().endswith(" photo"):
        alt_model = img_alt[:-len(" photo")].strip() or None
    if alt_model and alt_model != text:
        if not text.endswith(alt_model):
            return None, None, alt_model
        brand = text[:-len(alt_model)].strip()
        if brand:
            return alt_model, brand, None
    parts = text.rsplit(None, 1)
    if len(parts) == 2:
        return parts[1], parts[0], None
    return text, None, None


# ---------------------------------------------------------------------------
# small pure helpers

def _product_url(href):
    """Ferguson product page URL without the tracking query, else None."""
    href = _unwrap(href.strip())
    try:
        u = urlsplit(href)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if not _is_ferguson_host(href) or not _PRODUCT_PATH_RE.match(u.path):
        return None
    return urlunsplit(("https", host, u.path, "", ""))


def _unwrap(href):
    """Undo Outlook Safe Links / Google redirect wrappers added by a forward."""
    try:
        u = urlsplit(href)
    except ValueError:
        return href
    host = (u.hostname or "").lower()
    q = parse_qs(u.query)
    if host.endswith("safelinks.protection.outlook.com") and len(q.get("url", [])) == 1:
        return q["url"][0]
    if host in ("www.google.com", "google.com") and u.path == "/url" and len(q.get("q", [])) == 1:
        return q["q"][0]
    return href


def _amount(text):
    """A whole dollar amount ("$1,409.42") -> Decimal; anything else -> None."""
    text = _norm(text)
    if len(text) > 20:
        return None
    m = _AMOUNT_RE.match(text)
    if not m:
        return None
    return Decimal(m.group(1).replace(",", "") + (m.group(2) or "")).quantize(CENT)


def _lines_total(items):
    return sum((it["unit_price"] * it["quantity"] for it in items), Decimal("0.00"))


def _iso_date(text):
    m = _DATE_RE.match(text.strip().rstrip("."))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def _mmddyyyy(iso):
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(iso).strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return None


def _component(it, room, project, expires, item_id):
    brand, model = it.get("brand"), it.get("model") or ""
    name = f"{brand} {model}" if brand else model
    desc = (f"MODEL - {name} COLOR - {it.get('finish') or 'Not listed'} "
            f'NOTES - Ferguson cart "{project}"')
    if expires:
        desc += f", pricing expires {expires}"
    price = float(it["unit_price"].quantize(CENT))
    return {
        "id": item_id,
        "item_id": item_id,
        "type": "product",
        "title": _cap(f"{name} - {room}", TITLE_CHARS),
        "description": _cap(desc, DESC_CHARS),
        "sku": _cap(model, TITLE_CHARS),
        "quantity": it["quantity"],
        "unit_price": price,
        "unit_cost": price,
        "taxable": False,
        "price_update_method": "set_price_manually",
    }


def _positive_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive whole number.")
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive whole number.") from None
    if n < 1:
        raise ValueError(f"{name} must be a positive whole number.")
    return n


def _cap(text, limit):
    text = text or ""
    return text[:limit]


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _not_cart(reason):
    return {"project_name": None, "pricing_expires": None, "subtotal": None,
            "items": [], "checks": [reason], "ok": False}
