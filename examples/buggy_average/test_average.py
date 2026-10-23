import unittest

from average import average


class AverageTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(average([]), 0)

    def test_nonempty(self):
        self.assertEqual(average([2, 4]), 3)

    def test_negative(self):
        self.assertEqual(average([-2, 0]), -1)


if __name__ == '__main__':
    unittest.main()
