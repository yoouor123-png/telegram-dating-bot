import unittest

from message_pages import MAX_MESSAGE_LINES, MOBILE_LINE_WIDTH, short_pages


class ShortPagesTests(unittest.TestCase):
    def test_pages_are_short_and_lossless(self):
        original = " ".join(["טקסט ארוך לבדיקה"] * 30)
        pages = short_pages(original)
        self.assertGreater(len(pages), 1)
        for page in pages:
            lines = page.splitlines()
            self.assertLessEqual(len(lines), MAX_MESSAGE_LINES)
            self.assertTrue(all(len(line) <= MOBILE_LINE_WIDTH for line in lines))
        self.assertEqual(
            " ".join(" ".join(pages).split()),
            " ".join(original.split()),
        )

    def test_long_unbroken_user_text_remains_accessible(self):
        original = "א" * 250
        pages = short_pages(original, heading="תיאור מלא:")
        self.assertEqual(
            "".join("".join(pages).split()).removeprefix("תיאורמלא:"),
            original,
        )
        self.assertTrue(all(
            len(line) <= MOBILE_LINE_WIDTH
            for page in pages for line in page.splitlines()
        ))


if __name__ == "__main__":
    unittest.main()