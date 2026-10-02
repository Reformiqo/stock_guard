import unittest

from stock_guard.overrides.rate_rules import is_rate_valid, is_suspicious


class TestRateRules(unittest.TestCase):
	def test_normal_row_is_not_suspicious(self):
		# 120,672 on hand at 1.2319, issuing all of it.
		self.assertFalse(is_suspicious(1.2319, 120672, -120672))

	def test_near_zero_base_is_suspicious_and_invalid(self):
		# Case seen on SLZ-0318 F: base qty collapses, rate explodes.
		rate = 148655.84 / 0.00124
		self.assertTrue(is_suspicious(rate, 0.00124, -120672))
		self.assertFalse(is_rate_valid(rate, 0.00124, -120672, 1.2319, 20))

	def test_overflow_value_is_invalid_even_without_reference(self):
		self.assertFalse(is_rate_valid(119776571.2, 5, -120672, 0, 20))

	def test_negative_rate_is_invalid(self):
		self.assertTrue(is_suspicious(-3.0, 100, -10))
		self.assertFalse(is_rate_valid(-3.0, 100, -10, 2.0, 20))

	def test_large_but_plausible_value_is_kept(self):
		# 2,00,000 kg of billet at 260 = 5.2 crore: large, but the rate is in line.
		self.assertFalse(is_suspicious(260.0, 200000, -200000))
		self.assertTrue(is_rate_valid(260.0, 200000, -200000, 255.0, 20))

	def test_short_batch_with_plausible_rate_is_kept(self):
		# Batch holds less than issued (allowed negative), but rate matches last inward.
		self.assertTrue(is_suspicious(2.1, 50, -80))
		self.assertTrue(is_rate_valid(2.1, 50, -80, 2.0, 20))


if __name__ == "__main__":
	unittest.main()
