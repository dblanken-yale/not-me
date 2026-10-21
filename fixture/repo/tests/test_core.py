import unittest

from calc import add, div, mean, mul, sub


class CoreTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
        self.assertEqual(sub(0, 4), -4)

    def test_mul(self):
        self.assertEqual(mul(3, 4), 12)
        self.assertEqual(mul(-2, 5), -10)

    def test_div(self):
        self.assertEqual(div(8, 2), 4)

    def test_div_by_zero(self):
        with self.assertRaises(ValueError):
            div(1, 0)

    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_mean_empty(self):
        with self.assertRaises(ValueError):
            mean([])


if __name__ == "__main__":
    unittest.main()
