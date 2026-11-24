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
# Share of a deployment's local store that may have no retention age (no ILM delete phase and no
# data-stream retention) and still count as policy-aged. That store carries no weight in the written
# month: it is left out, never given a default age. One unconfigured index does not make a
# configured fleet unmeasured.
RETENTION_UNAGED_MAX_SHARE = 0.10
ECHO_TOLERANCE_SHARE = 0.001
ECHO_TOLERANCE_GB_DAY = 0.02

# size_serverless.py exits with this status when an estimator copy does not have the
# layout its cell addresses were written for. run_consolidation.py stops the run on it.
TEMPLATE_MISMATCH_EXIT = 3
# The quote-window telemetry fetch pages through every index in the window and then reads
# daily counters in batches. Each attempt gets this long, and a timeout is retried.
QUOTE_TELEMETRY_BUDGET_S = 600
QUOTE_TELEMETRY_ATTEMPTS = 2


# How much each evidence check moves the quoted dollar. Confidence is their weighted mean,
# so a check that drives the price counts for more than a flag beside a rate that was still
# measured. The correctness checks — the sheet echo, the line and year totals, plan
# consistency — are not here: they say a number is wrong, which is a pass/fail, not a degree.
#   3  sets the price directly. Retention drives the estimator's retained-data ladder; the
#      coverage and priced share decide how much of the fleet the quote even represents.
#   2  changes the price by a bounded amount: ingest the rate did not see, a substitute
#      region's differential, an EIS line, the window a rate or a comparison was measured over.
#   1  a flag beside a figure that was still measured.
CONFIDENCE_WEIGHTS = {
    "Retention month": 3,
    "Retention basis": 3,
    "Bundle coverage": 3,
    "Priced share": 3,
    "Rated ingest": 2,
    "Region fidelity": 2,
    "Search EIS data": 2,
    "Rate window": 2,
    "Billing window": 2,
    "Counter age": 1,
}
# A band, not a decimal: three decimals imply a precision these inputs do not have.
CONFIDENCE_HIGH = 0.85
CONFIDENCE_MODERATE = 0.60

# Query complexity from the average query time, as the Search estimator sheet's Documentation tab sets it:
# up to 250 ms is simple, 250 to 500 moderate, over 500 complex.
SIMPLE_MAX_MS = 250.0
MODERATE_MAX_MS = 500.0
