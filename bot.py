import logging
import math
import signal
import sys
import time

from config import CFG
from trading_controls import TradingControls
from coinbase_feed import CoinbaseFeed
from learner import HourLearner
from strategy import Analyzer
from live_execution import LiveExecution

logging.basicConfig(
    level=getattr(logging, CFG.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")


class Bot:
    def __init__(self):
        self.controls = TradingControls.from_environment()
        self.feed = CoinbaseFeed()
        self.analyzer = Analyzer()
        self.execution = LiveExecution()
        self.learner = HourLearner(CFG.LEARNER_DB)
        self.stop = False
        self.last_analysis = 0.0
        self.last_log = 0.0
        self.last_learning_check = 0.0
        self.last_snapshot_save = 0.0
        self.last_saved_lock_state = None
        self.hour_forecasts = {}
        self.finalized = set()
        self.last_signature = None
        self.last_execution_result = None

    def run(self):
        current_hour_ts = int(time.time() // 3600) * 3600
        try:
            previous = self.learner.latest_forecast(current_hour_ts)
            if previous and self.analyzer.restore_hour_state(previous, current_hour_ts):
                log.warning("RESTORED CONFIRMED PATH AFTER RESTART | hour=%s | outcome=%s", current_hour_ts, self.analyzer.hour_state.get("locked"))
            else:
                log.info("NO CURRENT-HOUR LOCK TO RESTORE | starting clean confirmation state")
        except Exception:
            log.exception("LOCK RESTORE FAILED SAFELY | starting without a restored lock")
        log.info(
            "CRYPTO HOURLY BOT READY | asset=%s | analysis=continuous | target_step=%s | entry_window=30-5m | late_no_path_min_confidence=75%% | execution=%s",
            CFG.PRODUCT_ID, format(CFG.TARGET_STEP, ".8g"),
            "ENABLED" if self.controls.live_trading and not self.controls.kill_switch else "DISABLED",
        )
        log.warning("TRADING CONTROL | %s | BALANCE_USE=%s | MAX_ACCOUNT_EXPOSURE=%s", self.controls.entry_status(), self.controls.balance_use, self.controls.max_account_exposure)
        try:
            self.execution.startup_execution_check()
        except Exception:
            # A failed self-check must never crash the analysis process or permit an order.
            log.exception("KALSHI LIVE EXECUTION SELF-CHECK | ERROR - execution remains fail-closed")
        self.feed.start()

        while not self.stop:
            now = time.time()
            snap = self.feed.snapshot()
            price, age = snap.get("price"), snap.get("price_age_seconds", snap.get("age", 9999.0))
            if price is None or age is None or age > CFG.STALE_AFTER_SECONDS:
                if now - self.last_log >= CFG.LOG_INTERVAL_SECONDS:
                    log.warning("WAITING FOR FRESH COINBASE PRICE | price=%s | age=%s | status=%s", price, age, snap.get("data_status", snap.get("status")))
                    self.last_log = now
                time.sleep(.25)
                continue

            hour_ts = int(now // 3600) * 3600
            if now - self.last_analysis >= CFG.ANALYSIS_INTERVAL_SECONDS:
                report = self.analyzer.analyze(snap, learned=self.learner.adjustments(), learner=self.learner)
                self.last_analysis = now
                if report.get("status") == "OK":
                    self.hour_forecasts[hour_ts] = report
                    lock = report.get("lock", {})
                    lock_state = (bool(lock.get("locked")), lock.get("locked_outcome"), bool(lock.get("invalidated")))
                    # Save a compact-enough durable checkpoint every 15 seconds, and
                    # immediately when the lock state changes. Analysis itself still
                    # runs every configured poll throughout the full day.
                    if now - self.last_snapshot_save >= 15.0 or lock_state != self.last_saved_lock_state:
                        self.learner.save_snapshot(hour_ts, report)
                        self.last_snapshot_save = now
                        self.last_saved_lock_state = lock_state
                    self._log_report_if_needed(report, now)
                    try:
                        result = self.execution.maybe_trade(report, snap, now=now)
                        # Log a blocked reason when it changes, so Render logs show which
                        # safety gate is preventing entry without spamming every poll.
                        if result != self.last_execution_result:
                            log.warning("KALSHI EXECUTION STATUS | %s", result)
                            self.last_execution_result = result
                    except Exception:
                        # Trading errors must never stop ongoing Coinbase analysis.
                        log.exception("LIVE EXECUTION FAILED CLOSED; analysis continues")

            if now - self.last_learning_check >= 10.0:
                self._finalize_completed_hours(hour_ts - 3600)
                self.last_learning_check = now
            time.sleep(.20)

    def _log_report_if_needed(self, report, now):
        lock = report.get("lock", {})
        sig = (
            report.get("outcome"), bool(lock.get("locked")),
            bool(lock.get("invalidated")), lock.get("path_state"),
            report.get("target_level"), report.get("callout_state"),
            round(float(report.get("projected_finish", 0)), -1),
        )
        if sig == self.last_signature and now - self.last_log < CFG.LOG_INTERVAL_SECONDS:
            return

        # Format prices using the asset's configured strike increment. Whole-dollar
        # formatting hid cents for XRP (for example, $1.38 appeared as $1).
        step = max(float(CFG.TARGET_STEP), 1e-12)
        decimals = max(0, min(8, int(round(-math.log10(step)))))
        price_format = f",.{decimals}f"
        target = report.get("target_level")
        target_text = f"${format(float(target), price_format)}" if target is not None else "N/A"
        projected = report.get("projected_finish")
        projected_text = f"${format(float(projected), price_format)}" if projected is not None else "N/A"
        conf = float(report.get("confidence", 0.0)) * 100.0
        outcome_conf = float(report.get("outcome_confidence", report.get("confidence", 0.0))) * 100.0
        reasons = report.get("reasons") or []
        reason_text = "; ".join(str(x) for x in reasons[:3]) or "insufficient directional evidence"
        lock_label = "LOCKED" if lock.get("locked") else "UNLOCKED"
        log.info(
            "CRYPTO ANALYSIS | price=$%.2f | target=%s | outcome=%s | projected_finish=%s | confidence=%.1f%% | outcome_confidence=%.1f%% | path=%s/%s | state=%s",
            float(report.get("price", 0.0)), target_text, report.get("outcome", "NO CLEAR OUTCOME"),
            projected_text, conf, outcome_conf, lock_label,
            lock.get("path_state", "NONE"), report.get("callout_state", "UNKNOWN"),
        )
        log.info("TRADE REASONING | %s", reason_text)
        self.last_signature = sig
        self.last_log = now

    def _finalize_completed_hours(self, hour_ts):
        if hour_ts in self.finalized:
            return
        snap = self.feed.snapshot()
        bars = snap.get("bars", {}).get("1h", [])
        latest = self.hour_forecasts.get(hour_ts) or self.learner.latest_forecast(hour_ts)
        locked = self.learner.latest_locked_forecast(hour_ts)

        if latest is None:
            bar = next((x for x in bars if int(x.get("ts", -1)) == hour_ts and x.get("complete", True)), None)
            if not bar:
                return
            latest = {
                "ts": time.time(), "price": float(bar["open"]), "range_low": float(bar["low"]),
                "range_high": float(bar["high"]), "outcome": "NO CLEAR OUTCOME", "direction": "RANGE",
                "confidence": CFG.RANGE_CONFIDENCE_CAP, "score": 0.0,
                "features": {"vector": []}, "pattern": "NO FORECAST",
            }
        if self.learner.finalize_hour(hour_ts, latest, bars, locked_forecast=locked):
            self.finalized.add(hour_ts)
            log.info("LEARNER SUMMARY | %s", self.learner.summary())

    def shutdown(self, *_):
        self.stop = True
        self.feed.stop()


if __name__ == "__main__":
    bot = Bot()
    signal.signal(signal.SIGINT, bot.shutdown)
    signal.signal(signal.SIGTERM, bot.shutdown)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.shutdown()
    except Exception:
        log.exception("FATAL BOT ERROR")
        bot.shutdown()
        sys.exit(1)
