import unittest

from ui import label, percent


class FormatTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(label("  hello world "), "Hello World")

    def test_percent(self):
        self.assertEqual(percent(0.25), "25%")


if __name__ == "__main__":
    unittest.main()
