"""E1: ferguson_cart (pure parser + estimate body). Offline, stdlib only.

Fixtures are HAND-MADE HTML that mirrors the real Ferguson Home "Project cart" email
structure (checked on a real email 2026-11-11): every project name, brand, model, URL and
image here is MADE UP. Product and "View Cart" links use the fergusonhome.com host (the
parser requires it) with made-up paths; every other URL is on example.invalid.

Run: /tmp/vi-f88/bin/python -m unittest test_ferguson_cart
"""

import unittest
from decimal import Decimal
from email.message import EmailMessage
from email.policy import default as default_policy

import ferguson_cart as fc

QP_TRACK = ("?cvo_campaign=project&amp;utm_campaign=project&amp;utm_medium=email"
            "&amp;source=trm_made_up&amp;utm_source=transactional")


def item_html(brand_model, model, slug, pid, finish, price_line):
    finish_p = (f'<p style="margin:0" data-old-class="ma0">Color/Finish: {finish}</p>'
                if finish is not None else "")
    price_p = (f'<p style="margin:0" data-old-class="ma0">{price_line}</p>'
               if price_line is not None else "")
    return f"""
<table style="width:100%"><tbody><tr>
 <td style="vertical-align:top;width:60px"> <img src="https://images.example.invalid/madeup/{slug}.jpg" alt="{model} Photo"> </td>
 <td style="padding-left:16px;text-align:left"> <a target="_blank" href="https://www.fergusonhome.com/{slug}/p{pid}{QP_TRACK}" style="font-weight:700">{brand_model}</a> {finish_p} {price_p} </td>
</tr></tbody></table>"""


def room_html(room, items, thead=True):
    head = (f'<thead><tr><td style="font-weight:700"><span>{room}</span></td></tr></thead>'
            if thead else f'<tr><td style="font-weight:700"><span>{room}</span></td></tr>')
    return (f'<table style="width:100%">{head}<tbody><tr><td style="padding:16px">'
            f'{"".join(items)}</td></tr></tbody></table>')


def cart_html(project="Testproject", rooms=None, subtotal="$1,502.76",
              expires="12/10/2026", thead=True, sharer="Pat Example"):
    if rooms is None:
        rooms = [
            ("bathroom", [item_html("Testbrand TB-100", "TB-100", "testbrand-tb-100", 1000001,
                                    "White", "$1,409.42")]),
            ("Kitchen", [item_html("Otherbrand Co OB-200-NODRAIN", "OB-200-NODRAIN",
                                   "otherbrand-ob-200-nodrain", 1000002, "Polished White",
                                   "2 @ $46.67 each")]),
        ]
    body = "".join(room_html(r, its, thead) for r, its in rooms)
    sub_row = ("" if subtotal is None else
               f'<tr><td style="text-align:left;width:50%">Subtotal:</td>'
               f'<td style="text-align:right;width:50%">{subtotal}</td></tr>')
    oshead = ('<thead><tr><td><span>Order Summary</span></td></tr></thead>' if thead else
              '<tr><td><span>Order Summary</span></td></tr>')
    exp = ("" if expires is None else
           f'<table><tbody><tr><td> <span>Project Pricing Expires: {expires}</span> </td>'
           f'</tr></tbody></table>')
    return f"""<!doctype html>
<html lang="en"><head><title>PROJECT- Build.com</title>
<style>h1 {{ font-size: 24px; }} .x {{ color: $9.99 each; }}</style></head>
<body style="max-width:600px">
<table><tbody><tr><td><a href="https://www.example.invalid/"><img src="https://img.example.invalid/logo.png" alt="Logo"></a></td></tr>
<tr><td><div>
<table><tbody><tr><td><h1>Your Project cart is ready!</h1>
<div><span><a href="https://www.fergusonhome.com/made-up-cart-link">View Cart</a></span></div></td></tr></tbody></table>
<table><tbody><tr><td style="font-weight:700"> {sharer} has shared this Project cart with you for Project: <br> {project} </td></tr></tbody></table>
<div>{body}</div>
<div><table style="width:100%">{oshead}
<tbody><tr><td><table><tbody>{sub_row}
<tr><td>Shipping:</td><td style="font-weight:700">FREE</td></tr></tbody></table></td></tr></tbody>
<tfoot><tr><td><table><tbody><tr><td>Grand Total:</td><td>{subtotal or ""}</td></tr></tbody></table></td></tr></tfoot>
</table></div>
{exp}
<table><tbody><tr><td><div><span><a href="https://www.fergusonhome.com/made-up-cart-link">View Cart</a></span></div></td></tr></tbody></table>
</div></td></tr>
<tr><td><a href="https://www.example.invalid/orders">Orders</a> Copyright &copy; Made Up</td></tr>
</tbody></table>
<img src="https://tracking.example.invalid/open?upn=madeup" alt="" width="1" height="1"/>
</body></html>"""


def direct_email(html, charset="iso-8859-1", cte="quoted-printable",
                 sender="Ferguson Home <support@fergusonhome.com>"):
    m = EmailMessage(policy=default_policy)
    m["From"] = sender
    m["To"] = "someone@example.invalid"
    m["Subject"] = "Project Cart from Pat Example"
    m.set_content(html, subtype="html", charset=charset, cte=cte)
    return m.as_bytes()


def gmail_forward(html):
    """Gmail-style inline forward: multipart/alternative, original inside gmail_quote."""
    m = EmailMessage(policy=default_policy)
    m["From"] = "Pat Example <pat@example.invalid>"
    m["To"] = "mike@cart.example.invalid"
    m["Subject"] = "Fwd: Project Cart from Pat Example"
    m.set_content("---------- Forwarded message ---------\nFrom: Ferguson Home\n(plain text "
                  "version that drops the quantity line)\n")
    inner = html.split("<body", 1)[1].split(">", 1)[1].rsplit("</body>", 1)[0]
    wrapped = ('<div dir="ltr">see cart<br><br><div class="gmail_quote"><div dir="ltr" '
               'class="gmail_attr">---------- Forwarded message ---------<br>From: '
               '<strong>Ferguson Home</strong> &lt;support@fergusonhome.com&gt;<br>Subject: '
               'Project Cart from Pat Example<br></div><br><br>' + inner + '</div></div>')
    m.add_alternative(wrapped, subtype="html", charset="utf-8", cte="base64")
    return m.as_bytes()


def attached_forward(raw_original):
    """Outlook-style 'forward as attachment': original as message/rfc822."""
    m = EmailMessage(policy=default_policy)
    m["From"] = "Pat Example <pat@example.invalid>"
    m["To"] = "mike@cart.example.invalid"
    m["Subject"] = "FW: Project Cart from Pat Example"
    m.set_content("Cart attached.")
    m.add_alternative("<p>Cart attached.</p>", subtype="html")
    from email import message_from_bytes
    m.add_attachment(message_from_bytes(raw_original, policy=default_policy))
    return m.as_bytes()


EXPECTED_ITEMS = [
    {"room": "bathroom", "brand": "Testbrand", "model": "TB-100", "finish": "White",
     "quantity": 1, "unit_price": Decimal("1409.42"),
     "product_url": "https://www.fergusonhome.com/testbrand-tb-100/p1000001",
     "image_url": "https://images.example.invalid/madeup/testbrand-tb-100.jpg"},
    {"room": "Kitchen", "brand": "Otherbrand Co", "model": "OB-200-NODRAIN",
     "finish": "Polished White", "quantity": 2, "unit_price": Decimal("46.67"),
     "product_url": "https://www.fergusonhome.com/otherbrand-ob-200-nodrain/p1000002",
     "image_url": "https://images.example.invalid/madeup/otherbrand-ob-200-nodrain.jpg"},
]


class ParseHappyPath(unittest.TestCase):
    def assert_standard(self, cart):
        self.assertEqual(cart["checks"], [])
        self.assertTrue(cart["ok"])
        self.assertEqual(cart["project_name"], "Testproject")
        self.assertEqual(cart["pricing_expires"], "2026-12-10")
        self.assertEqual(cart["subtotal"], Decimal("1502.76"))
        self.assertIsInstance(cart["subtotal"], Decimal)
        self.assertEqual(cart["items"], EXPECTED_ITEMS)

    def test_direct_send_quoted_printable_latin1(self):
        self.assert_standard(fc.parse_cart_email(direct_email(cart_html())))

    def test_direct_send_base64_utf8(self):
        self.assert_standard(fc.parse_cart_email(
            direct_email(cart_html(), charset="utf-8", cte="base64")))

    def test_gmail_inline_forward(self):
        self.assert_standard(fc.parse_cart_email(gmail_forward(cart_html())))

    def test_forward_as_attached_message(self):
        self.assert_standard(fc.parse_cart_email(attached_forward(direct_email(cart_html()))))

    def test_forward_without_thead_gives_no_rooms_and_flags(self):
        # F-95: without room headings nothing is guessed: rooms null + one check
        c = fc.parse_cart_email(gmail_forward(cart_html(thead=False)))
        self.assertEqual(c["checks"], [fc.NO_ROOMS])
        self.assertFalse(c["ok"])
        self.assertEqual(c["items"], [dict(i, room=None) for i in EXPECTED_ITEMS])
        self.assertEqual(c["subtotal"], Decimal("1502.76"))

    def test_str_input_accepted(self):
        self.assert_standard(fc.parse_cart_email(direct_email(cart_html()).decode("latin-1")))

    def test_tracking_query_stripped_and_outlook_safelinks_unwrapped(self):
        html = cart_html().replace(
            'href="https://www.fergusonhome.com/testbrand-tb-100/p1000001' + QP_TRACK + '"',
            'href="https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.'
            'fergusonhome.com%2Ftestbrand-tb-100%2Fp1000001%3Futm_source%3Dx&amp;data=zz"')
        cart = fc.parse_cart_email(direct_email(html))
        self.assertEqual(cart["items"][0]["product_url"],
                         "https://www.fergusonhome.com/testbrand-tb-100/p1000001")
        self.assertTrue(cart["ok"], cart["checks"])


class Quantities(unittest.TestCase):
    def one(self, price_line, finish="White"):
        rooms = [("Bath", [item_html("Testbrand TB-1", "TB-1", "tb-1", 11, finish, price_line)])]
        return fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$46.67")))

    def test_qty_one_price_only(self):
        c = self.one("$46.67")
        self.assertEqual((c["items"][0]["quantity"], c["items"][0]["unit_price"]),
                         (1, Decimal("46.67")))
        self.assertTrue(c["ok"], c["checks"])

    def test_qty_two_each(self):
        rooms = [("Bath", [item_html("Testbrand TB-1", "TB-1", "tb-1", 11, "White",
                                     "2 @ $46.67 each")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$93.34")))
        self.assertEqual((c["items"][0]["quantity"], c["items"][0]["unit_price"]),
                         (2, Decimal("46.67")))
        self.assertTrue(c["ok"], c["checks"])

    def test_unparseable_quantity_is_null_and_flagged(self):
        c = self.one("two @ $46.67 each")
        it = c["items"][0]
        self.assertIsNone(it["quantity"])
        self.assertEqual(it["unit_price"], Decimal("46.67"))
        self.assertFalse(c["ok"])
        self.assertTrue(any("quantity could not be read" in x for x in c["checks"]), c["checks"])
        self.assertTrue(any("could not be checked against the Subtotal" in x
                            for x in c["checks"]))

    def test_zero_quantity_flagged(self):
        c = self.one("0 @ $46.67 each")
        self.assertIsNone(c["items"][0]["quantity"])
        self.assertFalse(c["ok"])

    def test_two_prices_on_one_line_not_guessed(self):
        c = self.one("$60.00 $46.67")
        self.assertIsNone(c["items"][0]["unit_price"])
        self.assertIsNone(c["items"][0]["quantity"])
        self.assertTrue(any("could not be read" in x for x in c["checks"]))

    def test_missing_price_is_null_and_flagged(self):
        c = self.one(None)
        it = c["items"][0]
        self.assertIsNone(it["unit_price"])
        self.assertIsNone(it["quantity"])
        self.assertFalse(c["ok"])
        self.assertTrue(any("no price was found" in x for x in c["checks"]), c["checks"])

    def test_missing_finish_is_null_and_flagged(self):
        c = self.one("$46.67", finish=None)
        self.assertIsNone(c["items"][0]["finish"])
        self.assertTrue(any("Color/Finish" in x for x in c["checks"]))


class Totals(unittest.TestCase):
    def test_subtotal_mismatch_warns(self):
        c = fc.parse_cart_email(direct_email(cart_html(subtotal="$1,456.09")))
        self.assertFalse(c["ok"])
        self.assertIn("The lines add up to $1,502.76 but the email's Subtotal is $1,456.09.",
                      c["checks"])

    def test_missing_subtotal_warns(self):
        c = fc.parse_cart_email(direct_email(cart_html(subtotal=None)))
        self.assertIsNone(c["subtotal"])
        self.assertTrue(any("Subtotal could not be found" in x for x in c["checks"]))

    def test_unreadable_subtotal_warns(self):
        c = fc.parse_cart_email(direct_email(cart_html(subtotal="TBD")))
        self.assertIsNone(c["subtotal"])
        self.assertTrue(any("Subtotal in the Order Summary could not be read" in x
                            for x in c["checks"]))

    def test_entities_and_commas_in_prices(self):
        rooms = [("Kitchen", [
            item_html("Testbrand TB-9", "TB-9", "tb-9", 9, "Matte&nbsp;Black", "&#36;12,345.60"),
            item_html("Testbrand TB-8", "TB-8", "tb-8", 8, "Brushed &amp; Satin",
                      "3&nbsp;@&nbsp;&#x24;1,000.05&nbsp;each"),
        ])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="&#36;15,345.75")))
        self.assertTrue(c["ok"], c["checks"])
        self.assertEqual([(i["finish"], i["quantity"], i["unit_price"]) for i in c["items"]],
                         [("Matte Black", 1, Decimal("12345.60")),
                          ("Brushed & Satin", 3, Decimal("1000.05"))])
        self.assertEqual(c["subtotal"], Decimal("15345.75"))

    def test_missing_or_bad_expiry_flagged(self):
        c = fc.parse_cart_email(direct_email(cart_html(expires=None)))
        self.assertIsNone(c["pricing_expires"])
        self.assertTrue(any("expiry date could not be found" in x for x in c["checks"]))
        c = fc.parse_cart_email(direct_email(cart_html(expires="13/45/2026")))
        self.assertIsNone(c["pricing_expires"])
        self.assertTrue(any("could not be read" in x for x in c["checks"]))


class RoomsAndItems(unittest.TestCase):
    def test_multiple_rooms_and_items_keep_order(self):
        rooms = [
            ("Primary Bath", [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White", "$10.00"),
                              item_html("Testbrand TB-2", "TB-2", "tb-2", 2, "Chrome",
                                        "2 @ $5.00 each")]),
            ("Kitchen", [item_html("Testbrand TB-3", "TB-3", "tb-3", 3, "Black", "$1.00")]),
            ("Laundry", [item_html("Testbrand TB-4", "TB-4", "tb-4", 4, "Gray", "$2.50")]),
        ]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$23.50")))
        self.assertTrue(c["ok"], c["checks"])
        self.assertEqual([(i["room"], i["model"]) for i in c["items"]],
                         [("Primary Bath", "TB-1"), ("Primary Bath", "TB-2"),
                          ("Kitchen", "TB-3"), ("Laundry", "TB-4")])

    def test_single_word_link_text_brand_null(self):
        rooms = [("Bath", [item_html("TB-77", "TB-77", "tb-77", 77, "White", "$1.00")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$1.00")))
        self.assertIsNone(c["items"][0]["brand"])
        self.assertEqual(c["items"][0]["model"], "TB-77")
        self.assertTrue(any("brand could not be separated" in x for x in c["checks"]))

    def test_model_with_space_uses_image_alt(self):
        rooms = [("Bath", [item_html("Testbrand TB 500 X", "TB 500 X", "tb-500", 5, "White",
                                     "$1.00")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$1.00")))
        self.assertEqual((c["items"][0]["brand"], c["items"][0]["model"]),
                         ("Testbrand", "TB 500 X"))

    def test_missing_project_name_flagged(self):
        c = fc.parse_cart_email(direct_email(cart_html(project="")))
        self.assertIsNone(c["project_name"])
        self.assertFalse(c["ok"])
        self.assertTrue(any("project name could not be read" in x for x in c["checks"]))
        self.assertEqual(len(c["items"]), 2)

    def test_project_name_same_line(self):
        html = cart_html().replace("for Project: <br> Testproject", "for Project: Same Line")
        self.assertEqual(fc.parse_cart_email(direct_email(html))["project_name"], "Same Line")

    def test_cart_with_no_items(self):
        c = fc.parse_cart_email(direct_email(cart_html(rooms=[("Bath", [])], subtotal="$0.00")))
        self.assertEqual(c["items"], [])
        self.assertIn("No items were found in the cart.", c["checks"])

    def test_two_carts_in_one_html_refused(self):
        c = fc.parse_cart_email(gmail_forward(cart_html() + cart_html(project="Second")))
        self.assertFalse(c["ok"])
        self.assertEqual(c["checks"], [fc.MULTIPLE_CARTS])
        self.assertEqual(c["items"], [])

    def test_same_cart_twice_in_one_html_reads_once(self):
        c = fc.parse_cart_email(gmail_forward(cart_html() + cart_html()))
        self.assertTrue(c["ok"], c["checks"])
        self.assertEqual(len(c["items"]), 2)


class NotACart(unittest.TestCase):
    def assert_not_cart(self, c, reason=fc.NOT_A_CART):
        self.assertFalse(c["ok"])
        self.assertEqual(c["checks"], [reason])
        self.assertEqual(c["items"], [])
        self.assertIsNone(c["project_name"])

    def test_other_email(self):
        self.assert_not_cart(fc.parse_cart_email(direct_email(
            "<html><body><p>Your order has shipped. Total $12.00</p></body></html>",
            sender="Shop <orders@example.invalid>")))

    def test_ferguson_sender_but_not_a_cart(self):
        self.assert_not_cart(fc.parse_cart_email(direct_email(
            "<html><body><p>Thanks for your order from Ferguson Home.</p></body></html>")))

    def test_marker_without_ferguson_product_links(self):
        html = cart_html().replace("fergusonhome.com", "evil.example.invalid")
        self.assert_not_cart(fc.parse_cart_email(direct_email(html)))

    def test_lookalike_host_rejected(self):
        html = cart_html().replace("https://www.fergusonhome.com/", "https://fergusonhome.com.evil.invalid/")
        self.assert_not_cart(fc.parse_cart_email(direct_email(html)))

    def test_lookalike_product_links_are_never_items(self):
        html = cart_html().replace("https://www.fergusonhome.com/testbrand-tb-100/",
                                   "https://fergusonhome.com.evil.invalid/testbrand-tb-100/")
        c = fc.parse_cart_email(direct_email(html))
        self.assertEqual([i["model"] for i in c["items"]], ["OB-200-NODRAIN"])
        self.assertFalse(c["ok"])

    def test_plain_text_only(self):
        m = EmailMessage()
        m["From"] = "pat@example.invalid"
        m.set_content("Pat has shared this Project cart with you for Project: X\n$1.00")
        self.assert_not_cart(fc.parse_cart_email(m.as_bytes()), fc.NO_HTML)

    def test_garbage_and_empty(self):
        for raw in (b"", b"\x00\xff garbage", None):
            c = fc.parse_cart_email(raw)
            self.assertFalse(c["ok"])
            self.assertEqual(len(c["checks"]), 1)

    def test_unknown_charset_does_not_crash(self):
        raw = direct_email(cart_html(), charset="utf-8", cte="base64").replace(
            b'charset="utf-8"', b'charset="x-made-up"')
        c = fc.parse_cart_email(raw)
        self.assertEqual(c["project_name"], "Testproject")


class EstimateBody(unittest.TestCase):
    def setUp(self):
        self.cart = fc.parse_cart_email(direct_email(cart_html()))

    def test_exact_shape(self):
        body = fc.build_estimate_body(self.cart, "19000001", 900001)
        desc = ('NOTES - Ferguson cart "Testproject", pricing expires 12/10/2026')
        self.assertEqual(body, {
            "customer_id": 19000001,
            "tax_rate": "0.00",
            "title": "Ferguson cart: Testproject",
            "notes": ('Draft from Ferguson Home cart "Testproject" (pricing expires 12/10/2026). '
                      "2 item(s), lines total $1,502.76. Unit price and unit cost are the "
                      "cart price."),
            "line_items": [
                {"line_number": 1, "line_type": "group", "group_number": 1,
                 "line_title": "bathroom", "line_quantity": 1,
                 "line_price_visibility": "group", "line_visibility": "all",
                 "line_components": [{
                     "id": 900001, "item_id": 900001, "type": "product",
                     "title": "Testbrand TB-100 - bathroom",
                     "description": f"MODEL - Testbrand TB-100 COLOR - White {desc}",
                     "sku": "TB-100", "quantity": 1, "unit_price": 1409.42,
                     "unit_cost": 1409.42, "taxable": False,
                     "price_update_method": "set_price_manually"}]},
                {"line_number": 2, "line_type": "group", "group_number": 1,
                 "line_title": "Kitchen", "line_quantity": 1,
                 "line_price_visibility": "group", "line_visibility": "all",
                 "line_components": [{
                     "id": 900001, "item_id": 900001, "type": "product",
                     "title": "Otherbrand Co OB-200-NODRAIN - Kitchen",
                     "description": ("MODEL - Otherbrand Co OB-200-NODRAIN COLOR - "
                                     f"Polished White {desc}"),
                     "sku": "OB-200-NODRAIN", "quantity": 2, "unit_price": 46.67,
                     "unit_cost": 46.67, "taxable": False,
                     "price_update_method": "set_price_manually"}]},
            ],
        })

    def test_totals_match_subtotal_and_json_serializable(self):
        import json
        body = fc.build_estimate_body(self.cart, 19000001, 900001)
        json.dumps(body)
        total = sum(Decimal(str(c["unit_price"])) * c["quantity"]
                    for li in body["line_items"] for c in li["line_components"])
        self.assertEqual(total, self.cart["subtotal"])

    def test_rooms_first_seen_order_and_empty_room_other(self):
        cart = {"project_name": "P", "pricing_expires": None, "items": [
            {"room": "Kitchen", "brand": "B", "model": "M1", "finish": None,
             "quantity": 1, "unit_price": Decimal("1.00")},
            {"room": "", "brand": None, "model": "M2", "finish": "F",
             "quantity": 1, "unit_price": Decimal("2.00")},
            {"room": "Bath", "brand": "B", "model": "M3", "finish": "F",
             "quantity": 1, "unit_price": Decimal("3.00")},
            {"room": "Kitchen", "brand": "B", "model": "M4", "finish": "F",
             "quantity": 2, "unit_price": Decimal("4.00")},
            {"room": None, "brand": "B", "model": "M5", "finish": "F",
             "quantity": 1, "unit_price": Decimal("5.00")},
        ]}
        body = fc.build_estimate_body(cart, 1, 2)
        self.assertEqual([(li["line_title"], [c["sku"] for c in li["line_components"]])
                          for li in body["line_items"]],
                         [("Kitchen", ["M1", "M4"]), ("Other", ["M2", "M5"]), ("Bath", ["M3"])])
        self.assertEqual([li["line_number"] for li in body["line_items"]], [1, 2, 3])
        c0 = body["line_items"][0]["line_components"][0]
        self.assertEqual(c0["description"], 'MODEL - B M1 COLOR - Not listed NOTES - Ferguson cart "P"')
        other = body["line_items"][1]["line_components"][0]
        self.assertEqual(other["title"], "M2 - Other")
        self.assertNotIn("pricing expires", body["notes"])

    def test_caps(self):
        long = "X" * 600
        cart = {"project_name": long, "pricing_expires": "2026-12-10", "items": [
            {"room": long, "brand": long, "model": long, "finish": long * 3,
             "quantity": 1, "unit_price": Decimal("1.00")}]}
        body = fc.build_estimate_body(cart, 1, 2)
        self.assertEqual(len(body["title"]), fc.TITLE_CHARS)
        self.assertLessEqual(len(body["notes"]), fc.NOTES_CHARS)
        li = body["line_items"][0]
        comp = li["line_components"][0]
        self.assertEqual(len(li["line_title"]), fc.TITLE_CHARS)
        self.assertEqual(len(comp["title"]), fc.TITLE_CHARS)
        self.assertEqual(len(comp["sku"]), fc.TITLE_CHARS)
        self.assertEqual(len(comp["description"]), fc.DESC_CHARS)

    def test_refuses_to_guess(self):
        base = {"project_name": "P", "items": [
            {"room": "R", "brand": "B", "model": "M", "finish": "F",
             "quantity": 1, "unit_price": Decimal("1.00")}]}
        bad = [
            dict(base, project_name=None),
            dict(base, items=[]),
            dict(base, items=[dict(base["items"][0], quantity=None)]),
            dict(base, items=[dict(base["items"][0], quantity=True)]),
            dict(base, items=[dict(base["items"][0], unit_price=None)]),
            dict(base, items=[dict(base["items"][0], unit_price=1.0)]),
        ]
        for cart in bad:
            with self.assertRaises(ValueError):
                fc.build_estimate_body(cart, 1, 2)
        for cid, iid in ((0, 2), ("abc", 2), (True, 2), (1, None), (1, -5)):
            with self.assertRaises(ValueError):
                fc.build_estimate_body(base, cid, iid)

    def test_does_not_mutate_cart(self):
        import copy
        before = copy.deepcopy(self.cart)
        fc.build_estimate_body(self.cart, 1, 2)
        self.assertEqual(self.cart, before)


def one_item_cart(price_html, subtotal="$46.67", finish_html="<p>Color/Finish: White</p>"):
    """A cart whose single item's detail cell is given raw (to test forwarded layouts)."""
    item = f"""
<table style="width:100%"><tbody><tr>
 <td><img src="https://images.example.invalid/madeup/tb-1.jpg" alt="TB-1 Photo"></td>
 <td><a href="https://www.fergusonhome.com/tb-1/p11{QP_TRACK}">Testbrand TB-1</a>
 {finish_html}{price_html}</td>
</tr></tbody></table>"""
    return direct_email(cart_html(rooms=[("Bath", [item])], subtotal=subtotal))


class R2Regressions(unittest.TestCase):
    """Backtest R1 (E1) findings F-93..F-98."""

    # F-93: huge numbers never crash; null + check
    def test_f93_huge_item_price(self):
        c = fc.parse_cart_email(one_item_cart("<p>$" + "9" * 30 + ".00</p>"))
        self.assertIsNone(c["items"][0]["unit_price"])
        self.assertFalse(c["ok"])

    def test_f93_huge_quantity(self):
        c = fc.parse_cart_email(one_item_cart("<p>" + "9" * 5000 + " @ $1.00 each</p>"))
        it = c["items"][0]
        self.assertIsNone(it["quantity"])
        self.assertEqual(it["unit_price"], Decimal("1.00"))
        self.assertTrue(any("not a usable quantity" in x for x in c["checks"]), c["checks"])

    def test_f93_quantity_over_cap(self):
        c = fc.parse_cart_email(one_item_cart("<p>10000 @ $1.00 each</p>"))
        self.assertIsNone(c["items"][0]["quantity"])
        c = fc.parse_cart_email(one_item_cart("<p>9999 @ $1.00 each</p>", subtotal="$9,999.00"))
        self.assertEqual(c["items"][0]["quantity"], 9999)
        self.assertTrue(c["ok"], c["checks"])

    def test_f93_huge_subtotal_and_huge_in_unparseable_line(self):
        c = fc.parse_cart_email(one_item_cart("<p>$46.67</p>", subtotal="$" + "1" * 40))
        self.assertIsNone(c["subtotal"])
        self.assertTrue(any("Subtotal in the Order Summary could not be read" in x
                            for x in c["checks"]))
        c = fc.parse_cart_email(one_item_cart("<p>two @ $" + "9" * 50 + " each</p>"))
        self.assertIsNone(c["items"][0]["unit_price"])

    def test_f93_never_raises_on_internal_error(self):
        from unittest import mock
        with mock.patch.object(fc, "_parse_segment", side_effect=ArithmeticError("boom")):
            c = fc.parse_cart_email(direct_email(cart_html()))
        self.assertEqual(c["checks"], [fc.UNREADABLE])
        self.assertFalse(c["ok"])

    def test_f93_builder_rejects_out_of_range(self):
        base = {"project_name": "P", "items": [
            {"room": "R", "brand": "B", "model": "M", "finish": "F",
             "quantity": 1, "unit_price": Decimal("1.00")}]}
        for bad in ({"quantity": 10000}, {"unit_price": Decimal("1E+30")},
                    {"unit_price": Decimal("NaN")}, {"unit_price": Decimal("-1")}):
            cart = dict(base, items=[dict(base["items"][0], **bad)])
            with self.assertRaises(ValueError):
                fc.build_estimate_body(cart, 1, 2)

    # F-94: <br>-separated fields keep their boundaries
    def test_f94_br_between_finish_and_price(self):
        c = fc.parse_cart_email(one_item_cart(
            "", subtotal="$1,234.50",
            finish_html="<p>Color/Finish: Brushed &amp; Satin<br>$1,234.50</p>"))
        it = c["items"][0]
        self.assertEqual((it["finish"], it["quantity"], it["unit_price"]),
                         ("Brushed & Satin", 1, Decimal("1234.50")))
        self.assertTrue(c["ok"], c["checks"])

    def test_f94_br_between_finish_and_qty_line(self):
        c = fc.parse_cart_email(one_item_cart(
            "", subtotal="$93.34",
            finish_html="<div>Color/Finish: White<br/>2 @ $46.67 each</div>"))
        self.assertEqual((c["items"][0]["quantity"], c["items"][0]["unit_price"]),
                         (2, Decimal("46.67")))
        self.assertTrue(c["ok"], c["checks"])

    def test_f94_project_name_in_child_div(self):
        html = cart_html().replace("for Project: <br> Testproject",
                                   "for Project: <div><b>Testproject</b></div>")
        c = fc.parse_cart_email(direct_email(html))
        self.assertEqual(c["project_name"], "Testproject")
        self.assertTrue(c["ok"], c["checks"])

    def test_f94_project_name_never_taken_from_another_cell(self):
        html = cart_html().replace("for Project: <br> Testproject </td>",
                                   "for Project: </td><td>Not the name</td>")
        c = fc.parse_cart_email(direct_email(html))
        self.assertIsNone(c["project_name"])
        self.assertFalse(c["ok"])

    # F-95: no silent room inheritance or invention
    def test_f95_empty_heading_does_not_inherit_previous_room(self):
        rooms = [("Guest Bath", [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White", "$1.00")]),
                 ("", [item_html("Testbrand TB-2", "TB-2", "tb-2", 2, "White", "$2.00")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$3.00")))
        self.assertEqual([i["room"] for i in c["items"]], ["Guest Bath", None])
        self.assertFalse(c["ok"])
        self.assertTrue(any("Item 2" in x and "no room heading" in x for x in c["checks"]))
        body = fc.build_estimate_body(c, 1, 2)
        self.assertEqual([li["line_title"] for li in body["line_items"]], ["Guest Bath", "Other"])

    def test_f95_loose_line_without_headings_never_becomes_room(self):
        items = [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White", "$1.00"),
                 "<p>Ships in 3 days</p>",
                 item_html("Testbrand TB-2", "TB-2", "tb-2", 2, "White", "$2.00")]
        c = fc.parse_cart_email(direct_email(
            cart_html(rooms=[("Bath", items)], subtotal="$3.00", thead=False)))
        self.assertEqual([i["room"] for i in c["items"]], [None, None])
        self.assertIn(fc.NO_ROOMS, c["checks"])
        self.assertFalse(c["ok"])

    def test_f95_loose_line_with_headings_is_ignored(self):
        items = [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White", "$1.00"),
                 "<p>Ships in 3 days</p>",
                 item_html("Testbrand TB-2", "TB-2", "tb-2", 2, "White", "$2.00")]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=[("Bath", items)],
                                                        subtotal="$3.00")))
        self.assertEqual([i["room"] for i in c["items"]], ["Bath", "Bath"])
        self.assertTrue(c["ok"], c["checks"])

    def test_f95_two_lines_in_one_heading_is_ambiguous(self):
        rooms = [("Bath<br>Kitchen", [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White",
                                                "$1.00")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$1.00")))
        self.assertIsNone(c["items"][0]["room"])
        self.assertFalse(c["ok"])

    # F-96: malformed / contradictory subtotal -> null + check
    def test_f96_malformed_subtotals(self):
        for bad in ("$46.679", "$46.67 $2,000.00", "-$46.67", "Not available; originally $46.67",
                    "$46,67", "$4,6.67", "46.67 USD"):
            with self.subTest(bad=bad):
                c = fc.parse_cart_email(one_item_cart("<p>$46.67</p>", subtotal=bad))
                self.assertIsNone(c["subtotal"])
                self.assertFalse(c["ok"])
                self.assertTrue(any("Subtotal in the Order Summary could not be read" in x
                                    for x in c["checks"]), c["checks"])

    def test_f96_malformed_item_prices(self):
        for bad in ("$46.679", "-$46.67", "$46,67", "2 @ $46.679 each", "2 @ -$46.67 each"):
            with self.subTest(bad=bad):
                c = fc.parse_cart_email(one_item_cart(f"<p>{bad}</p>"))
                self.assertIsNone(c["items"][0]["unit_price"])
                self.assertFalse(c["ok"])

    def test_f96_subtotal_same_cell(self):
        html = cart_html().replace(
            '<td style="text-align:left;width:50%">Subtotal:</td>'
            '<td style="text-align:right;width:50%">$1,502.76</td>',
            '<td>Subtotal: $1,502.76</td>')
        c = fc.parse_cart_email(direct_email(html))
        self.assertEqual(c["subtotal"], Decimal("1502.76"))
        self.assertTrue(c["ok"], c["checks"])

    # F-97: several carts across MIME parts -> refused, duplicates collapse
    def test_f97_two_different_attached_carts_refused(self):
        a = direct_email(cart_html(project="Fictional Alpha"))
        b = direct_email(cart_html(project="Fictional Beta"))
        m = EmailMessage(policy=default_policy)
        m["From"] = "pat@example.invalid"
        m.set_content("two carts")
        from email import message_from_bytes
        m.add_attachment(message_from_bytes(a, policy=default_policy))
        m.add_attachment(message_from_bytes(b, policy=default_policy))
        c = fc.parse_cart_email(m.as_bytes())
        self.assertFalse(c["ok"])
        self.assertEqual(c["checks"], [fc.MULTIPLE_CARTS])
        self.assertEqual(c["items"], [])

    def test_f97_inline_copy_plus_same_attachment_reads_once(self):
        raw = direct_email(cart_html())
        m = EmailMessage(policy=default_policy)
        m["From"] = "pat@example.invalid"
        m.set_content("fwd")
        m.add_alternative(cart_html(), subtype="html")
        from email import message_from_bytes
        m.add_attachment(message_from_bytes(raw, policy=default_policy))
        c = fc.parse_cart_email(m.as_bytes())
        self.assertTrue(c["ok"], c["checks"])
        self.assertEqual(c["items"], EXPECTED_ITEMS)

    # F-98: conflicting price blocks / finishes -> null + check
    def test_f98_two_price_blocks(self):
        c = fc.parse_cart_email(one_item_cart("<p><s>$1,234.50</s></p><p>$1,000.00</p>",
                                              subtotal="$1,234.50"))
        it = c["items"][0]
        self.assertIsNone(it["unit_price"])
        self.assertIsNone(it["quantity"])
        self.assertFalse(c["ok"])
        self.assertTrue(any("more than one price" in x for x in c["checks"]), c["checks"])

    def test_f98_repeated_identical_price_block_is_fine(self):
        c = fc.parse_cart_email(one_item_cart("<p>$46.67</p><p>$46.67</p>"))
        self.assertTrue(c["ok"], c["checks"])

    def test_f98_two_finishes(self):
        c = fc.parse_cart_email(one_item_cart(
            "<p>$46.67</p>", finish_html="<p>Color/Finish: White</p><p>Color/Finish: Black</p>"))
        self.assertIsNone(c["items"][0]["finish"])
        self.assertTrue(any("more than one Color/Finish" in x for x in c["checks"]))


def summary_cart(sub_rows_html, lines_price="$46.67"):
    """one_item_cart with the Subtotal row(s) replaced by raw HTML."""
    raw_html = cart_html(rooms=[("Bath", [item_html("Testbrand TB-1", "TB-1", "tb-1", 1, "White",
                                                    lines_price)])], subtotal="$46.67")
    old = ('<tr><td style="text-align:left;width:50%">Subtotal:</td>'
           '<td style="text-align:right;width:50%">$46.67</td></tr>')
    assert old in raw_html
    return direct_email(raw_html.replace(old, sub_rows_html))


class R3Regressions(unittest.TestCase):
    """Backtest R2 (E1) findings F-99, F-100 and the same rule applied elsewhere."""

    def assert_sub_conflict(self, c):
        self.assertIsNone(c["subtotal"])
        self.assertFalse(c["ok"])
        self.assertTrue(any("More than one Subtotal" in x for x in c["checks"]), c["checks"])

    # F-99
    def test_f99_two_amounts_br_in_value_cell(self):
        self.assert_sub_conflict(fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal:</td><td>$46.67<br>$2,000.00</td></tr>")))

    def test_f99_two_amounts_p_in_value_cell(self):
        self.assert_sub_conflict(fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal:</td><td><p>$46.67</p><p>$2,000.00</p></td></tr>")))

    def test_f99_two_subtotal_rows(self):
        self.assert_sub_conflict(fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal:</td><td>$46.67</td></tr>"
            "<tr><td>Subtotal:</td><td>$2,000.00</td></tr>")))

    def test_f99_same_line_then_more_lines_in_cell(self):
        self.assert_sub_conflict(fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal: $46.67<br>$2,000.00</td></tr>")))

    def test_f99_value_cell_with_words_is_unreadable(self):
        c = fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal:</td><td>$46.67<br>was $50.00</td></tr>"))
        self.assertIsNone(c["subtotal"])
        self.assertTrue(any("Subtotal in the Order Summary could not be read" in x
                            for x in c["checks"]), c["checks"])

    def test_f99_repeated_identical_subtotal_ok_and_other_rows_separate(self):
        c = fc.parse_cart_email(summary_cart(
            "<tr><td>Subtotal:</td><td>$46.67</td></tr>"
            "<tr><td>Subtotal:</td><td><p>$46.67</p></td></tr>"
            "<tr><td>Tax:</td><td>$3.00</td></tr>"))
        self.assertEqual(c["subtotal"], Decimal("46.67"))
        self.assertTrue(c["ok"], c["checks"])   # Shipping FREE / Grand Total / Tax ignored

    # F-100
    def test_f100_malformed_amount_after_unreadable_quantity(self):
        for bad in ("two @ -$46.67 each", "two @ $46.67oops each", "two @ $46.67.89 each",
                    "two @ $46.67 $1.00 each", "$46.67oops each"):
            with self.subTest(bad=bad):
                c = fc.parse_cart_email(one_item_cart(f"<p>{bad}</p>"))
                it = c["items"][0]
                self.assertIsNone(it["unit_price"])
                self.assertIsNone(it["quantity"])
                self.assertTrue(any("price in" in x and "could not be read" in x
                                    for x in c["checks"]), c["checks"])

    def test_f100_readable_price_with_unreadable_quantity_kept(self):
        c = fc.parse_cart_email(one_item_cart("<p>two @ $46.67 each</p>"))
        self.assertEqual((c["items"][0]["quantity"], c["items"][0]["unit_price"]),
                         (None, Decimal("46.67")))
        self.assertFalse(c["ok"])

    # same rule elsewhere: expiry dates, photos, photo label vs link text
    def test_two_different_expiry_dates(self):
        html = cart_html().replace(
            "<span>Project Pricing Expires: 12/10/2026</span>",
            "<span>Project Pricing Expires: 12/10/2026</span><p>Project Pricing Expires: "
            "01/05/2027</p>")
        c = fc.parse_cart_email(direct_email(html))
        self.assertIsNone(c["pricing_expires"])
        self.assertTrue(any("More than one project pricing expiry date" in x
                            for x in c["checks"]), c["checks"])

    def test_same_expiry_date_twice_ok(self):
        html = cart_html().replace(
            "<span>Project Pricing Expires: 12/10/2026</span>",
            "<span>Project Pricing Expires: 12/10/2026</span><p>Project Pricing Expires: "
            "12/10/2026</p>")
        c = fc.parse_cart_email(direct_email(html))
        self.assertEqual(c["pricing_expires"], "2026-12-10")
        self.assertTrue(c["ok"], c["checks"])

    def test_two_photos_before_one_item(self):
        extra = '<img src="https://images.example.invalid/madeup/other.jpg" alt="ZZ-1 Photo">'
        c = fc.parse_cart_email(one_item_cart("<p>$46.67</p>",
                                              finish_html=extra + "<p>Color/Finish: White</p>"))
        # the extra photo comes AFTER the link, so it belongs to "the next item": no item
        # follows, so the first item keeps its single photo
        self.assertTrue(c["ok"], c["checks"])
        html = cart_html().replace(
            '<td style="vertical-align:top;width:60px"> <img src="https://images.example.invalid/'
            'madeup/testbrand-tb-100.jpg"',
            '<td style="vertical-align:top;width:60px"> ' + extra +
            ' <img src="https://images.example.invalid/madeup/testbrand-tb-100.jpg"')
        c = fc.parse_cart_email(direct_email(html))
        self.assertIsNone(c["items"][0]["image_url"])
        self.assertTrue(any("more than one photo" in x for x in c["checks"]), c["checks"])

    def test_photo_label_contradicts_link_text(self):
        rooms = [("Bath", [item_html("Testbrand TB-1", "TB-2", "tb-1", 1, "White", "$1.00")])]
        c = fc.parse_cart_email(direct_email(cart_html(rooms=rooms, subtotal="$1.00")))
        it = c["items"][0]
        self.assertEqual((it["brand"], it["model"]), (None, None))
        self.assertFalse(c["ok"])
        self.assertTrue(any("photo is labelled model" in x for x in c["checks"]), c["checks"])
        with self.assertRaises(ValueError):
            fc.build_estimate_body(c, 1, 2)

    def test_safelinks_with_two_targets_not_unwrapped(self):
        html = cart_html().replace(
            'href="https://www.fergusonhome.com/testbrand-tb-100/p1000001' + QP_TRACK + '"',
            'href="https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.'
            'fergusonhome.com%2Ftestbrand-tb-100%2Fp1000001&amp;url=https%3A%2F%2Fevil.invalid'
            '%2Fx%2Fp1"')
        c = fc.parse_cart_email(direct_email(html))
        self.assertEqual([i["model"] for i in c["items"]], ["OB-200-NODRAIN"])
        self.assertFalse(c["ok"])     # the dropped line shows up as a Subtotal mismatch

    def test_same_photo_two_labels_flagged(self):
        html = cart_html().replace(
            '<td style="vertical-align:top;width:60px"> <img src="https://images.example.invalid/'
            'madeup/testbrand-tb-100.jpg"',
            '<td style="vertical-align:top;width:60px"> <img src="https://images.example.invalid/'
            'madeup/testbrand-tb-100.jpg" alt="QQ Photo"> <img src="https://images.example.invalid/'
            'madeup/testbrand-tb-100.jpg"')
        c = fc.parse_cart_email(direct_email(html))
        self.assertIsNone(c["items"][0]["image_url"])
        self.assertTrue(any("more than one photo" in x for x in c["checks"]), c["checks"])


if __name__ == "__main__":
    unittest.main()
