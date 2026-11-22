import unittest

from bot import build_embeds, extract_links, is_useful, parse_listing

SAMPLE = """<html><head>
<meta property="og:title" content="Standing Desk &amp; Chair">
<meta property="og:description" content="Barely used, pickup only.">
<meta property="og:url" content="https://www.facebook.com/marketplace/item/123456789/">
<meta property="og:image" content="https://scontent.xx.fbcdn.net/a.jpg?x=1&amp;y=2">
</head><body><script>{"listing_price":{"formatted_amount":"$75","amount":"75"},
"location":{"text":"Austin, TX"},
"listing_photos":[{"image":{"uri":"https:\\/\\/scontent.xx.fbcdn.net\\/a.jpg?x=1&y=2"}},
{"image":{"uri":"https:\\/\\/scontent.xx.fbcdn.net\\/b.jpg"}},
{"image":{"uri":"https:\\/\\/scontent.xx.fbcdn.net\\/c.jpg"}}]}</script></body></html>"""


class Tests(unittest.TestCase):
    def test_links(self):
        cases = {
            "look https://www.facebook.com/marketplace/item/123/ wow": ["https://www.facebook.com/marketplace/item/123/"],
            "(https://m.facebook.com/marketplace/item/123?ref=x)": ["https://m.facebook.com/marketplace/item/123?ref=x"],
            "https://web.facebook.com/marketplace/austin/item/55": ["https://web.facebook.com/marketplace/austin/item/55"],
            "https://www.facebook.com/share/v/AbC123/": ["https://www.facebook.com/share/v/AbC123/"],
            "https://facebook.com/share/1Ab_c/.": ["https://facebook.com/share/1Ab_c/"],
            "https://www.facebook.com/marketplace/item/9/ https://m.facebook.com/marketplace/item/9/": ["https://www.facebook.com/marketplace/item/9/"],
        }
        for text, want in cases.items():
            self.assertEqual(extract_links(text), want, text)

    def test_non_links(self):
        for t in ["https://www.facebook.com/marketplace/", "https://example.com/marketplace/item/1",
                  "https://notfacebook.com/marketplace/item/1", "hello", ""]:
            self.assertEqual(extract_links(t), [], t)

    def test_parse_and_embeds(self):
        info = parse_listing(SAMPLE)
        self.assertEqual(info["title"], "Standing Desk & Chair")
        self.assertEqual(info["price"], "$75")
        self.assertEqual(info["location"], "Austin, TX")
        self.assertEqual(len(info["images"]), 3)
        self.assertTrue(info["images"][0].endswith("x=1&y=2"))
        self.assertTrue(is_useful(info))
        embeds = build_embeds("https://www.facebook.com/marketplace/item/123456789/", info)
        self.assertEqual(len(embeds), 3)
        self.assertEqual({e.url for e in embeds}, {"https://www.facebook.com/marketplace/item/123456789/"})
        self.assertEqual(len(embeds[0].fields), 2)

    def test_login_wall(self):
        self.assertFalse(is_useful(parse_listing("<title>Log in to Facebook</title>")))
        self.assertFalse(is_useful(parse_listing('<meta property="og:title" content="Facebook">')))


if __name__ == "__main__":
    unittest.main()
