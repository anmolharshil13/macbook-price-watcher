"""Offline tests for the checker: python -m unittest discover -s checker"""
import unittest

import check_prices as cp

TITLE = "Apple 2026 MacBook Air 13-inch Laptop with M5 chip: 24GB Unified Memory, 1TB SSD - Midnight"


def jsonld(price, avail="https://schema.org/InStock", name=TITLE):
    return f'''<html><head><script type="application/ld+json">
    {{"@context":"https://schema.org","@graph":[{{"@type":"BreadcrumbList"}},
      {{"@type":"Product","name":"{name}","offers":{{"@type":"Offer","price":"{price}",
        "priceCurrency":"INR","availability":"{avail}"}}}}]}}</script></head><body></body></html>'''


class Extraction(unittest.TestCase):
    def test_jsonld(self):
        ex = cp.extract("reliance", "https://www.reliancedigital.in/x/p/1", jsonld("189900.00"))
        self.assertEqual(ex["price"], 189900.0)
        self.assertEqual(ex["source"], "json-ld")

    def test_amazon_fallback(self):
        page = (f'<span id="productTitle"> {TITLE} </span>'
                '<div id="corePriceDisplay_desktop_feature_div"><span class="a-price-whole">1,87,490</span></div>')
        ex = cp.extract("amazon", "https://www.amazon.in/dp/B0X", page)
        self.assertEqual(ex["price"], 187490.0)
        self.assertEqual(ex["title"], TITLE)

    def test_flipkart_class(self):
        page = f'<title>{TITLE}</title><div class="Nx9bqj CxhGGd">₹1,84,990</div>'
        self.assertEqual(cp.extract("flipkart", "https://www.flipkart.com/x/p/itm1", page)["price"], 184990.0)

    def test_croma_jsonld_with_raw_newlines(self):
        page = ('<script type="application/ld+json">{"@type":"Product","name":"Apple MacBook Air (13.6 inch, M5, 24GB, 1TB)",'
                '"description":"<p>line one<br />\nline two</p>","offers":{"price":"195490.00"}}</script>')
        ex = cp.extract("croma", "https://www.croma.com/x/p/324325", page)
        self.assertEqual((ex["price"], ex["source"]), (195490.0, "json-ld"))

    def test_croma_price_reducer(self):
        page = '"pdpPriceReducer":{"pdpPriceData":{"sellingPrice":{"value":"195490.00"},"mrp":{"value":"209900.00"}}}'
        self.assertEqual(cp.extract("croma", "https://www.croma.com/x/p/324325", page)["price"], 195490.0)

    def test_parse_price(self):
        self.assertEqual(cp.parse_price("₹1,89,900.00"), 189900.0)
        self.assertIsNone(cp.parse_price("call for price"))


class Verification(unittest.TestCase):
    def check(self, page, retailer="vijaysales", url="https://www.vijaysales.com/p/1", status=200):
        ex = cp.extract(retailer, url, page)
        return cp.verify(retailer, url, status, page, ex)[0]

    def test_verified(self):
        self.assertEqual(self.check(jsonld("189900")), "verified")

    def test_wrong_variant_rejected(self):
        page = jsonld("149900", name="Apple MacBook Air 13-inch M5 16GB 512GB SSD")
        self.assertEqual(self.check(page), "spec_mismatch")

    def test_out_of_stock(self):
        self.assertEqual(self.check(jsonld("189900", avail="https://schema.org/OutOfStock")), "out_of_stock")

    def test_blocked(self):
        self.assertEqual(self.check("<html>Enter the characters you see below - captcha</html>", status=200), "blocked")
        self.assertEqual(self.check("", status=503), "blocked")

    def test_model_number_confirms_spec(self):
        # Flipkart / Imagine titles omit the screen size or RAM but carry the Apple part number
        self.assertEqual(self.check(jsonld("209900", name="Apple MacBook Air (M5, 2026) M5 - (24 GB/1 TB SSD/Tahoe) MDHK4HN/A")), "verified")
        self.assertEqual(self.check(jsonld("188910", name="13-inch MacBook Air: Apple M5 chip, 1TB SSD - Sky Blue"),
                                    url="https://www.imagineonline.store/products/13-inch-macbook-air-mdhk4hn-a"), "verified")

    def test_15_inch_rejected(self):
        page = jsonld("223100", name="Apple MDVN4HN/A MacBook Air (Apple M5 Chip/24 GB/1 TB SSD), 38.91 cm (15.3 inch), Midnight")
        self.assertEqual(self.check(page), "spec_mismatch")

    def test_absurd_price(self):
        self.assertEqual(self.check(jsonld("18990")), "suspicious")


class Alerts(unittest.TestCase):
    def rec(self, price, status="verified"):
        return {"status": status, "price": price, "retailer": "amazon", "url": "u", "ts": "t"}

    def test_drop(self):
        a = cp.build_alerts(self.rec(189000), {"price": 191000}, 185000, "Amazon India")
        self.assertEqual([x["type"] for x in a], ["drop"])

    def test_no_alert_on_rise_or_same(self):
        self.assertEqual(cp.build_alerts(self.rec(191000), {"price": 189000}, 185000, "A"), [])
        self.assertEqual(cp.build_alerts(self.rec(189000), {"price": 189000}, 185000, "A"), [])

    def test_target_cross(self):
        a = cp.build_alerts(self.rec(184990), {"price": 189000}, 185000, "A")
        self.assertEqual([x["type"] for x in a], ["target"])
        self.assertIn("down", a[0]["message"])

    def test_exactly_target_counts(self):
        self.assertEqual(cp.build_alerts(self.rec(185000), {"price": 186000}, 185000, "A")[0]["type"], "target")

    def test_further_drop_below_target_is_drop(self):
        a = cp.build_alerts(self.rec(183000), {"price": 184000}, 185000, "A")
        self.assertEqual([x["type"] for x in a], ["drop"])

    def test_first_ever_check_below_target(self):
        self.assertEqual(cp.build_alerts(self.rec(184000), None, 185000, "A")[0]["type"], "target")

    def test_unverified_never_alerts(self):
        self.assertEqual(cp.build_alerts(self.rec(150000, "spec_mismatch"), {"price": 189000}, 185000, "A"), [])

    def test_inr_format(self):
        self.assertEqual(cp.fmt_inr(185000), "₹1,85,000")
        self.assertEqual(cp.fmt_inr(1234567), "₹12,34,567")


if __name__ == "__main__":
    unittest.main()
