"""Pure rules for deciding whether a batch outgoing rate can be trusted.

No Frappe import here, so the rules can be unit tested on their own.
"""

# Currency columns are DECIMAL(21,9): the integer part must stay below 10^12.
# Keep a wide safety margin.
MAX_ABS_VALUE = 1e11

# Only check further when a single outgoing row is worth more than this.
LARGE_VALUE = 1e8

QTY_TOLERANCE = 1e-6


def is_suspicious(rate: float, available_qty: float, outgoing_qty: float) -> bool:
	"""Cheap pre-screen. Only suspicious rows pay for an extra database lookup."""
	if available_qty <= 0:
		return True
	if rate < 0:
		return True
	if available_qty + QTY_TOLERANCE < abs(outgoing_qty):
		# The batch holds less than is being issued: the base of the average is unreliable.
		return True
	if abs(rate * outgoing_qty) >= LARGE_VALUE:
		return True
	return False


def is_rate_valid(
	rate: float,
	available_qty: float,
	outgoing_qty: float,
	reference_rate: float,
	cap_ratio: float,
) -> bool:
	"""Return False when the computed rate must be replaced."""
	if available_qty <= 0:
		return False
	if rate < 0:
		return False
	if abs(rate * outgoing_qty) >= MAX_ABS_VALUE:
		return False
	if reference_rate > 0 and cap_ratio > 0 and rate > reference_rate * cap_ratio:
		return False
	return True
