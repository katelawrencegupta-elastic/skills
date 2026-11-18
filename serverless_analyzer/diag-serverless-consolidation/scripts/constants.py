"""Shared numbers for the diagnostic consolidation quote.

One copy of each figure. A local copy in another module will drift.
"""

DAYS_PER_MONTH = 30.416
HOURS_PER_MONTH = 730
ESTIMATE_MONTHS = 12
SECURITY_EXPANSION = 1.66
O11Y_METER_FACTOR = 1.595
RATE_WINDOW_MAX_SHARE = 0.25
# Share of local store that may sit in indices that took bulk writes and have no rate.
# Above it the quote carries a warning: a long-lived index written by name has no write
# phase in the bundle, which is a limit of the measurement, not a fault in this run.
UNRATED_STORE_MAX_SHARE = 0.10
COUNTER_AGE_MAX_SHARE = 0.25
PLAN_HEADLINE_SHARE = 0.01
PLAN_HEADLINE_USD = 5.0
BILLING_WINDOW_MIN_SHARE = 0.5
# Share of a deployment's local store that may sit at the 30-day assumed default and still
# count as policy-aged. One unconfigured index does not make a configured fleet unmeasured.
RETENTION_ASSUMED_MAX_SHARE = 0.10
ECHO_TOLERANCE_SHARE = 0.001
ECHO_TOLERANCE_GB_DAY = 0.02

# size_serverless.py exits with this status when an estimator copy does not have the
# layout its cell addresses were written for. run_consolidation.py stops the run on it.
TEMPLATE_MISMATCH_EXIT = 3
