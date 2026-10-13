import os
from dataclasses import dataclass


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except Exception:
        return int(default)


def _bool(name, default=False):
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    # Analysis only. No trading, Kalshi, or order execution.
    PRODUCT_ID: str = os.getenv("PRODUCT_ID", "BTC-USD")
    WS_URL: str = os.getenv("COINBASE_WS_URL", "wss://advanced-trade-ws.coinbase.com")
    REST_URL: str = os.getenv("COINBASE_REST_URL", "https://api.coinbase.com/api/v3/brokerage")

    ANALYSIS_INTERVAL_SECONDS: float = _float("ANALYSIS_INTERVAL_SECONDS", "5")
    # Analyze continuously, but suppress early-hour final outcomes.
    CALLOUT_START_MINUTES: float = _float("CALLOUT_START_MINUTES", "30")
    CALLOUT_LATE_CUTOFF_MINUTES: float = _float("CALLOUT_LATE_CUTOFF_MINUTES", "10")
    LOCK_CONFIRMATION_SECONDS: float = _float("LOCK_CONFIRMATION_SECONDS", "20")
    MIN_LATE_LOCK_CONFIDENCE: float = _float("MIN_LATE_LOCK_CONFIDENCE", "0.65")
    LOG_INTERVAL_SECONDS: float = _float("LOG_INTERVAL_SECONDS", "10")
    STALE_AFTER_SECONDS: float = _float("STALE_AFTER_SECONDS", "10")
    RECONNECT_SECONDS: float = _float("RECONNECT_SECONDS", "2")
    HEARTBEAT_STALE_SECONDS: float = _float("HEARTBEAT_STALE_SECONDS", "5")

    HIST_BARS: int = _int("HIST_BARS", "300")
    TRADE_BUFFER: int = _int("TRADE_BUFFER", "60000")
    ORDER_BOOK_LEVELS: int = _int("ORDER_BOOK_LEVELS", "50")
    MAX_BOOK_AGE_SECONDS: float = _float("MAX_BOOK_AGE_SECONDS", "3")
    MIN_FLOW_TRADES: int = _int("MIN_FLOW_TRADES", "20")
    MIN_REQUIRED_BARS: int = _int("MIN_REQUIRED_BARS", "40")
    MIN_TF_BARS: int = _int("MIN_TF_BARS", "30")

    MIN_SCORE_GAP: float = _float("MIN_SCORE_GAP", "0.90")
    STRONG_SCORE_THRESHOLD: float = _float("STRONG_SCORE_THRESHOLD", "1.55")
    MIN_CONFIDENCE_FOR_DEVELOPING: float = _float("MIN_CONFIDENCE_FOR_DEVELOPING", "0.64")
    MIN_CONFIDENCE_FOR_OUTCOME: float = _float("MIN_CONFIDENCE_FOR_OUTCOME", "0.56")
    HARD_CALL_CONFIDENCE: float = _float("HARD_CALL_CONFIDENCE", "0.79")
    MAX_CONFIDENCE: float = _float("MAX_CONFIDENCE", "0.92")

    LOCK_CONFIRMATIONS: int = _int("LOCK_CONFIRMATIONS", "3")
    HARD_LOCK_CONFIRMATIONS: int = _int("HARD_LOCK_CONFIRMATIONS", "2")
    INVALIDATION_CONFIRMATIONS: int = _int("INVALIDATION_CONFIRMATIONS", "3")
    REVERSAL_SCORE_GAP: float = _float("REVERSAL_SCORE_GAP", "2.10")
    LOCK_MAX_AGE_SECONDS: float = _float("LOCK_MAX_AGE_SECONDS", "3600")

    # Path-lock rules. Crossing the $100 target is NOT an invalidation.
    PATH_INVALIDATION_ATR: float = _float("PATH_INVALIDATION_ATR", "0.55")
    PATH_REVERSAL_SCORE: float = _float("PATH_REVERSAL_SCORE", "2.25")
    PATH_BREAK_CONFIRMATIONS: int = _int("PATH_BREAK_CONFIRMATIONS", "3")
    PATH_STALE_AFTER_SECONDS: float = _float("PATH_STALE_AFTER_SECONDS", "180")
    PATH_RELOCK_CONFIRMATIONS: int = _int("PATH_RELOCK_CONFIRMATIONS", "3")

    LOW_ADX_THRESHOLD: float = _float("LOW_ADX_THRESHOLD", "12")
    VERY_LOW_RVOL: float = _float("VERY_LOW_RVOL", "0.35")
    MIN_MTF_AGREEMENT: float = _float("MIN_MTF_AGREEMENT", "0.60")
    RANGE_CONFIDENCE_CAP: float = _float("RANGE_CONFIDENCE_CAP", "0.62")
    PATTERN_CONFLICT_CAP: float = _float("PATTERN_CONFLICT_CAP", "0.66")
    PROJECTION_CONFLICT_CAP: float = _float("PROJECTION_CONFLICT_CAP", "0.60")
    STALE_BOOK_CONFIDENCE_CAP: float = _float("STALE_BOOK_CONFIDENCE_CAP", "0.75")
    SCENARIO_MIN_STRENGTH: float = _float("SCENARIO_MIN_STRENGTH", "52")
    SCENARIO_MIN_GAP: float = _float("SCENARIO_MIN_GAP", "3")
    PATH_MIN_SCORE: float = _float("PATH_MIN_SCORE", "0.22")
    PATH_STRONG_SCORE: float = _float("PATH_STRONG_SCORE", "0.42")
    PATH_TARGET_TOLERANCE_ATR: float = _float("PATH_TARGET_TOLERANCE_ATR", "0.55")
    LATE_HOUR_MINUTES: float = _float("LATE_HOUR_MINUTES", "12")
    VERY_LATE_MINUTES: float = _float("VERY_LATE_MINUTES", "6")
    OUTCOME_MIN_GAP: float = _float("OUTCOME_MIN_GAP", "0.018")
    OUTCOME_MIN_PATH_CONFIDENCE: float = _float("OUTCOME_MIN_PATH_CONFIDENCE", "0.56")
    OUTCOME_MIN_PROJECTED_EDGE_ATR: float = _float("OUTCOME_MIN_PROJECTED_EDGE_ATR", "0.10")
    CANDLE_PRESSURE_1M_BARS: int = _int("CANDLE_PRESSURE_1M_BARS", "12")
    CANDLE_PRESSURE_5M_BARS: int = _int("CANDLE_PRESSURE_5M_BARS", "12")
    CANDLE_PRESSURE_15M_BARS: int = _int("CANDLE_PRESSURE_15M_BARS", "8")
    PATTERN_SHAPE_LOOKBACK: int = _int("PATTERN_SHAPE_LOOKBACK", "100")
    SHAPE_SCORE_WEIGHT: float = _float("SHAPE_SCORE_WEIGHT", "0.70")
    REVERSAL_SCORE_WEIGHT: float = _float("REVERSAL_SCORE_WEIGHT", "0.72")
    RECLAIM_RESCUE_MIN: float = _float("RECLAIM_RESCUE_MIN", "0.58")

    TARGET_STEP: float = _float("TARGET_STEP", "100")
    RANGE_STEP: float = _float("RANGE_STEP", "100")
    MIN_TARGET_EDGE_DOLLARS: float = _float("MIN_TARGET_EDGE_DOLLARS", "25")
    TARGET_ATR_EDGE: float = _float("TARGET_ATR_EDGE", "0.15")
    MIN_RANGE_DOLLARS: float = _float("MIN_RANGE_DOLLARS", "100")
    MIN_RANGE_ATR_MULT: float = _float("MIN_RANGE_ATR_MULT", "0.30")
    MAX_RANGE_ATR_MULT: float = _float("MAX_RANGE_ATR_MULT", "1.35")

    PATTERN_LOOKBACK_HOURS: int = _int("PATTERN_LOOKBACK_HOURS", "1000")
    PATTERN_NEIGHBORS: int = _int("PATTERN_NEIGHBORS", "30")
    LIVE_PATTERN_NEIGHBORS: int = _int("LIVE_PATTERN_NEIGHBORS", "8")
    MIN_PATTERN_SIMILARITY: float = _float("MIN_PATTERN_SIMILARITY", "0.58")
    PATTERN_FOLLOW_BARS: int = _int("PATTERN_FOLLOW_BARS", "3")
    PATTERN_BIAS_THRESHOLD: float = _float("PATTERN_BIAS_THRESHOLD", "0.20")

    LEARNER_DB: str = os.getenv("LEARNER_DB", "learner.sqlite3")
    STATE_FILE: str = os.getenv("STATE_FILE", "state.json")
    LEARNING_MIN_SAMPLES: int = _int("LEARNING_MIN_SAMPLES", "12")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    DEBUG_RAW_WS: bool = _bool("DEBUG_RAW_WS", False)

    # Compatibility aliases used by the public Coinbase feed.
    @property
    def product_id(self): return self.PRODUCT_ID
    @property
    def poll_seconds(self): return self.ANALYSIS_INTERVAL_SECONDS
    @property
    def max_rest_retries(self): return 3
    @property
    def trade_window_seconds(self): return 300
    @property
    def l2_max_age_seconds(self): return self.MAX_BOOK_AGE_SECONDS


CFG = Config()
