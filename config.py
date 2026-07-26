"""Auspex configuration. Every tunable number lives here — nowhere else."""

import os

# --- YouTube API ---
YT_API_KEY_ENV = "YT_API_KEY"
UPSTREAM_TIMEOUT_S = 10

# --- Quota (units per Google quota day; the quota day resets at midnight Pacific) ---
DAILY_UNIT_BUDGET = int(os.environ.get("DAILY_UNIT_BUDGET", "9000"))
COST_SEARCH = 100
COST_LIST = 1

# --- Shorts filter & channel baselines ---
SHORTS_MAX_SECONDS = 62
BASELINE_RECENT_UPLOADS = 30
BASELINE_DEEP_CHANNELS = 10
BASELINE_MIN_VIDEOS = 5
BASELINE_FLOOR = 100
BASELINE_MIN_AGE_DAYS = 7
BASELINE_CONCURRENCY = 5

# --- Outliers ---
SCAN_OUTLIER_MULTIPLE = 3.0
SCAN_OUTLIERS_MAX = 15
SMALL_CHANNEL_SUBS = 100_000

# --- Saturation score & verdict ---
FRESH_WINDOW_DAYS = 90
OPENNESS_W_DIVERSITY = 40
OPENNESS_W_CONCENTRATION = 25
OPENNESS_W_FRESHNESS = 20
OPENNESS_W_SMALL_OUTLIERS = 15
SMALL_OUTLIERS_CAP = 5
VERDICT_ENTER_MAX_SATURATION = 45
VERDICT_ENTER_MIN_SMALL_OUTLIERS = 2
VERDICT_AVOID_MIN_SATURATION = 70   # AVOID if saturation > this
VERDICT_AVOID_MAX_FRESH_SHARE = 0.10  # AVOID if fresh_share_90d < this

# --- scan_niche input bounds & defaults ---
QUERY_MIN_LEN = 2
QUERY_MAX_LEN = 80
DEFAULT_REGION_CODE = "US"
RECENCY_DAYS_DEFAULT = 365
RECENCY_DAYS_MIN = 30
RECENCY_DAYS_MAX = 1825
MAX_RESULTS_DEFAULT = 50
MAX_RESULTS_MIN = 10
MAX_RESULTS_MAX = 50

# --- channel_outliers input bounds & defaults ---
LOOKBACK_VIDEOS_DEFAULT = 30
LOOKBACK_VIDEOS_MIN = 10
LOOKBACK_VIDEOS_MAX = 100
MIN_MULTIPLE_DEFAULT = 2.5
MIN_MULTIPLE_MIN = 1.5
MIN_MULTIPLE_MAX = 10.0

# --- Paid endpoints (OKX Agent Payments Protocol, x402) ---
PAYMENTS_ENABLED_ENV = "PAYMENTS_ENABLED"  # "false" serves the /paid routes free
PAID_PRICE_USDT = "0.05"         # per-call price for the paid REST endpoints
X402_NETWORK = "eip155:196"      # X Layer (USDT, 6 decimals)
PAID_SCAN_PATH = "/paid/scan_niche"
PAID_CHANNEL_PATH = "/paid/channel_outliers"
OKX_API_KEY_ENV = "OKX_API_KEY"
OKX_SECRET_KEY_ENV = "OKX_SECRET_KEY"
OKX_PASSPHRASE_ENV = "OKX_PASSPHRASE"
PAY_TO_ADDRESS_ENV = "PAY_TO_ADDRESS"
OKX_BASE_URL_ENV = "OKX_BASE_URL"

# --- Cache ---
SCAN_CACHE_TTL_S = 6 * 3600
CHANNEL_CACHE_TTL_S = 3 * 3600
CACHE_MAX_ENTRIES = 500

# --- Worst-case unit cost of one uncached scan_niche (for the pre-pipeline quota check):
# search (100) + videos.list (1) + channels.list (1) + up to 10 deep channels x (playlistItems + videos.list)
SCAN_WORST_CASE_UNITS = (
    COST_SEARCH + COST_LIST + COST_LIST + BASELINE_DEEP_CHANNELS * 2 * COST_LIST
)
