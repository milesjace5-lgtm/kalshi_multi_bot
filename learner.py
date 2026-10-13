import json
import logging
import os
import re
import sqlite3
import threading
import time

import numpy as np

from config import CFG
from indicators import sequence_similarity

log = logging.getLogger("learner")


class HourLearner:
    """Persistent completed-hour memory, pattern matching, and calibration.

    The learner never uses future bars while making the current-hour decision.
    Completed hours are only added to pattern memory after the hour is finished.
    """

    def __init__(self, path=None):
        self.path = path or CFG.LEARNER_DB
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
        self.lock = threading.RLock()
        self._init()

    def _db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        # WAL lets readers inspect completed-hour memory while the live bot writes
        # snapshots, reducing avoidable SQLITE_BUSY failures on persistent disks.
        if self.path != ":memory:":
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self):
        with self._db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS hours(
              hour_ts INTEGER PRIMARY KEY,
              forecast_ts REAL,
              forecast_price REAL,
              range_low REAL,
              range_high REAL,
              outcome TEXT,
              direction TEXT,
              confidence REAL,
              score REAL,
              actual_open REAL,
              actual_high REAL,
              actual_low REAL,
              actual_close REAL,
              actual_dir TEXT,
              range_hit INTEGER,
              outcome_hit INTEGER,
              direction_hit INTEGER,
              close_error REAL,
              max_favorable_move REAL,
              max_adverse_move REAL,
              evidence_json TEXT,
              feature_json TEXT,
              pattern_label TEXT,
              completed_at REAL,
              decision_type TEXT,
              locked_outcome TEXT,
              locked_direction TEXT,
              locked_confidence REAL,
              locked_hit INTEGER
            );
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL,
              hour_ts INTEGER,
              price REAL,
              range_low REAL,
              range_high REAL,
              outcome TEXT,
              direction TEXT,
              confidence REAL,
              score REAL,
              evidence_json TEXT
            );
            CREATE TABLE IF NOT EXISTS calibration(
              key TEXT PRIMARY KEY,
              samples INTEGER,
              correct INTEGER,
              avg_error REAL
            );
            CREATE INDEX IF NOT EXISTS idx_hours_completed ON hours(completed_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_hour ON snapshots(hour_ts, ts);
            """)
            cols = {r[1] for r in c.execute("PRAGMA table_info(hours)").fetchall()}
            for name, typ in {
                "actual_dir": "TEXT", "direction_hit": "INTEGER", "feature_json": "TEXT",
                "pattern_label": "TEXT", "decision_type": "TEXT",
                "locked_outcome": "TEXT", "locked_direction": "TEXT", "locked_confidence": "REAL", "locked_hit": "INTEGER",
            }.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE hours ADD COLUMN {name} {typ}")

    def save_snapshot(self, hour_ts, report):
        if report.get("status") != "OK": return
        with self._db() as c:
            c.execute(
                "INSERT INTO snapshots(ts,hour_ts,price,range_low,range_high,outcome,direction,confidence,score,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), hour_ts, report["price"], report["range_low"], report["range_high"],
                 report["outcome"], report["direction"], report["confidence"], report["score"], json.dumps(report, default=str)))

    def finalize_hour(self, hour_ts, forecast, actual_bars, locked_forecast=None):
        bar = next((x for x in sorted(actual_bars, key=lambda z: z["ts"])
                    if int(x["ts"]) == int(hour_ts) and x.get("complete", True)), None)
        if not bar: return False
        o, h, l, close = [float(bar[k]) for k in ("open", "high", "low", "close")]
        flo, fhi = float(forecast.get("range_low", close)), float(forecast.get("range_high", close))
        range_hit = int(flo <= close <= fhi)
        outcome = str(forecast.get("outcome", "NO CLEAR OUTCOME"))
        outcome_hit = None
        target = self._parse_target(outcome)
        if target is not None:
            if "OVER" in outcome: outcome_hit = int(close > target)
            elif "BELOW" in outcome: outcome_hit = int(close < target)

        predicted_dir = forecast.get("direction")
        actual_dir = "UP" if close > o else "DOWN" if close < o else "FLAT"
        direction_hit = int(predicted_dir == actual_dir) if predicted_dir in ("UP", "DOWN") else None
        forecast_price = float(forecast.get("price", o))
        err = close - forecast_price
        features = forecast.get("features", {})
        pattern_label = forecast.get("pattern", "UNKNOWN")
        decision_type = "LOCKED" if forecast.get("lock", {}).get("locked") else "DEVELOPING"

        locked_outcome = locked_forecast.get("outcome") if locked_forecast else None
        locked_direction = locked_forecast.get("direction") if locked_forecast else None
        locked_confidence = float(locked_forecast.get("confidence", 0.0)) if locked_forecast else None
        locked_hit = None
        locked_target = self._parse_target(locked_outcome) if locked_outcome else None
        if locked_target is not None:
            locked_hit = int(close > locked_target) if "OVER" in locked_outcome else int(close < locked_target) if "BELOW" in locked_outcome else None

        # Pattern memory should learn from the state that actually produced the lock
        # when one existed; otherwise use the final developing state.
        memory_source = locked_forecast or forecast
        if locked_forecast and locked_forecast.get("features"):
            features = locked_forecast.get("features", features)
        if locked_forecast and locked_forecast.get("pattern"):
            pattern_label = locked_forecast.get("pattern", pattern_label)

        with self._db() as c:
            c.execute("""
              INSERT OR REPLACE INTO hours
              (hour_ts,forecast_ts,forecast_price,range_low,range_high,outcome,direction,confidence,score,
               actual_open,actual_high,actual_low,actual_close,actual_dir,range_hit,outcome_hit,direction_hit,
               close_error,max_favorable_move,max_adverse_move,evidence_json,feature_json,pattern_label,completed_at,decision_type,locked_outcome,locked_direction,locked_confidence,locked_hit)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                hour_ts, forecast.get("ts"), forecast_price, flo, fhi, outcome, predicted_dir,
                forecast.get("confidence", 0.0), forecast.get("score", 0.0), o, h, l, close,
                actual_dir, range_hit, outcome_hit, direction_hit, err,
                h - forecast_price, l - forecast_price, json.dumps(forecast, default=str),
                json.dumps(features, default=str), pattern_label, time.time(), decision_type,
                locked_outcome, locked_direction, locked_confidence, locked_hit,
            ))
            calibration_dir = locked_direction if locked_direction in ("UP", "DOWN") else predicted_dir
            calibration_hit = locked_hit if locked_direction in ("UP", "DOWN") else direction_hit
            if calibration_dir in ("UP", "DOWN") and calibration_hit is not None:
                self._update_calibration(c, calibration_dir, calibration_hit, abs(err))
            # Separate calibration for the exact hourly question: did the
            # predicted $100 OVER/BELOW line win? This keeps target skill
            # separate from simple open-to-close direction skill.
            outcome_dir = locked_direction if locked_direction in ("UP", "DOWN") else ("UP" if "OVER" in outcome else "DOWN" if "BELOW" in outcome else None)
            outcome_hit_value = locked_hit if locked_direction in ("UP", "DOWN") else outcome_hit
            if outcome_dir in ("UP", "DOWN") and outcome_hit_value is not None:
                self._update_calibration(c, "OUTCOME_" + outcome_dir, outcome_hit_value, abs(err))

        log.info("LEARNED HOUR | hour=%s | close=%.2f | actual=%s | predicted=%s | range=%s | outcome=%s | error=%+.2f",
                 hour_ts, close, actual_dir, predicted_dir, range_hit, outcome_hit, err)
        return True

    @staticmethod
    def _update_calibration(c, key, hit, abs_error):
        row = c.execute("SELECT samples,correct,avg_error FROM calibration WHERE key=?", (key,)).fetchone()
        if row:
            samples = int(row[0]) + 1; correct = int(row[1]) + int(hit)
            avg_error = (float(row[2]) * (samples - 1) + float(abs_error)) / samples
            c.execute("UPDATE calibration SET samples=?,correct=?,avg_error=? WHERE key=?", (samples, correct, avg_error, key))
        else:
            c.execute("INSERT INTO calibration(key,samples,correct,avg_error) VALUES(?,?,?,?)", (key, 1, int(hit), float(abs_error)))

    def latest_forecast(self, hour_ts):
        return self._snapshot_to_report(hour_ts, locked_only=False)

    def latest_locked_forecast(self, hour_ts):
        return self._snapshot_to_report(hour_ts, locked_only=True)

    def _snapshot_to_report(self, hour_ts, locked_only=False):
        with self._db() as c:
            rows = c.execute("SELECT * FROM snapshots WHERE hour_ts=? ORDER BY ts DESC LIMIT 300", (hour_ts,)).fetchall()
        for row in rows:
            try:
                ev = json.loads(row["evidence_json"])
                if locked_only and not ev.get("lock", {}).get("locked"): continue
                return {"ts": row["ts"], "price": row["price"], "range_low": row["range_low"],
                        "range_high": row["range_high"], "outcome": row["outcome"], "direction": row["direction"],
                        "confidence": row["confidence"], "score": row["score"], **ev}
            except Exception:
                continue
        return None

    def pattern_vote(self, features, limit=None):
        limit = int(limit or CFG.PATTERN_NEIGHBORS)
        current = np.asarray(features.get("vector", []), dtype=float)
        legacy_current = np.asarray(features.get("legacy_vector", []), dtype=float)
        if current.size == 0 or not np.all(np.isfinite(current)):
            return {"direction": "NONE", "strength": 0.0, "matches": 0, "detail": []}
        current = np.clip(current, -4, 4)
        legacy_current = np.clip(legacy_current, -4, 4) if legacy_current.size else (np.clip(current, -4, 4) if current.size else legacy_current)
        with self._db() as c:
            rows = c.execute(
                "SELECT hour_ts,feature_json,pattern_label,actual_close,actual_open,evidence_json FROM hours "
                "WHERE feature_json IS NOT NULL AND actual_close IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT ?", (CFG.PATTERN_LOOKBACK_HOURS,)
            ).fetchall()
        matches = []
        for row in rows:
            try:
                f = json.loads(row["feature_json"]); v = np.asarray(f.get("vector", []), dtype=float)
                schema = int(f.get("schema", 1) or 1)
                if schema >= 2 and v.size == current.size and np.all(np.isfinite(v)):
                    v = np.clip(v, -4, 4); sim = sequence_similarity(current, v)
                else:
                    # Keep older learned hours useful after the feature vector grows.
                    oldv = np.asarray(f.get("legacy_vector", f.get("vector", [])), dtype=float)
                    if oldv.size == 0 or legacy_current.size == 0 or oldv.size != legacy_current.size or not np.all(np.isfinite(oldv)): continue
                    oldv = np.clip(oldv, -4, 4); sim = sequence_similarity(legacy_current, oldv)
                if sim < CFG.MIN_PATTERN_SIMILARITY: continue
                actual_close = float(row["actual_close"])
                actual_open = float(row["actual_open"])
                actual = "UP" if actual_close > actual_open else "DOWN" if actual_close < actual_open else "FLAT"
                # Prefer the historical hour's actual $100 target outcome when
                # available. This teaches the pattern learner the same question
                # the live bot is answering, rather than merely open-to-close direction.
                try:
                    ev = json.loads(row["evidence_json"] or "{}")
                    target = float(ev.get("target_level"))
                    actual = "UP" if actual_close > target else "DOWN" if actual_close < target else "FLAT"
                except Exception:
                    pass
                try:
                    ev = json.loads(row["evidence_json"] or "{}")
                    hist_conf = float(ev.get("outcome_confidence", ev.get("confidence", 0.60)))
                except Exception:
                    hist_conf = 0.60
                quality_weight = 0.75 + 0.50 * max(0.0, min(1.0, hist_conf))
                matches.append((sim * quality_weight, actual, int(row["hour_ts"]), row["pattern_label"] or "UNKNOWN", hist_conf))
            except Exception:
                continue
        matches.sort(reverse=True, key=lambda x: x[0]); top = matches[:limit]
        up = sum(s for s,d,_,_,_ in top if d == "UP"); down = sum(s for s,d,_,_,_ in top if d == "DOWN"); total = up + down
        if total <= 0: return {"direction": "NONE", "strength": 0.0, "matches": len(top), "detail": []}
        bias = (up - down) / total
        direction = "UP" if bias > CFG.PATTERN_BIAS_THRESHOLD else "DOWN" if bias < -CFG.PATTERN_BIAS_THRESHOLD else "MIXED"
        return {"direction": direction, "strength": min(1.0, abs(bias)), "matches": len(top),
                "detail": [{"similarity": round(s,3), "direction": d, "hour_ts": ts, "pattern": p, "confidence": round(conf,3)} for s,d,ts,p,conf in top[:6]]}

    def adjustments(self):
        with self._db() as c:
            rows = c.execute("SELECT key,samples,correct FROM calibration").fetchall()
        out = {"bias": 0.0, "up_acc": None, "down_acc": None, "outcome_up_acc": None, "outcome_down_acc": None, "outcome_bias": 0.0}
        for key, samples, correct in rows:
            samples = int(samples); correct = int(correct)
            if samples < CFG.LEARNING_MIN_SAMPLES: continue
            # Light Bayesian shrinkage prevents 12 lucky calls from overpowering live data.
            acc = (correct + 3.0) / (samples + 6.0)
            if key == "UP": out["up_acc"] = acc; out["bias"] += (acc - .5) * .25
            elif key == "DOWN": out["down_acc"] = acc; out["bias"] -= (acc - .5) * .25
            elif key == "OUTCOME_UP": out["outcome_up_acc"] = acc; out["outcome_bias"] += (acc - .5) * .35
            elif key == "OUTCOME_DOWN": out["outcome_down_acc"] = acc; out["outcome_bias"] -= (acc - .5) * .35
        out["bias"] = max(-.35, min(.35, out["bias"]))
        out["outcome_bias"] = max(-.25, min(.25, out["outcome_bias"]))
        return out

    def summary(self):
        with self._db() as c:
            total = c.execute("SELECT COUNT(*) FROM hours").fetchone()[0]
            range_samples = c.execute("SELECT COUNT(*) FROM hours WHERE range_hit IS NOT NULL").fetchone()[0]
            range_hits = c.execute("SELECT COALESCE(SUM(range_hit),0) FROM hours WHERE range_hit IS NOT NULL").fetchone()[0]
            dir_samples = c.execute("SELECT COUNT(*) FROM hours WHERE direction_hit IS NOT NULL").fetchone()[0]
            dir_hits = c.execute("SELECT COALESCE(SUM(direction_hit),0) FROM hours WHERE direction_hit IS NOT NULL").fetchone()[0]
            out_samples = c.execute("SELECT COUNT(*) FROM hours WHERE outcome_hit IS NOT NULL").fetchone()[0]
            out_hits = c.execute("SELECT COALESCE(SUM(outcome_hit),0) FROM hours WHERE outcome_hit IS NOT NULL").fetchone()[0]
            no = c.execute("SELECT COUNT(*) FROM hours WHERE outcome='NO CLEAR OUTCOME'").fetchone()[0]
        return {"hours": total, "range_accuracy": range_hits/range_samples if range_samples else 0.0,
                "direction_samples": dir_samples, "direction_accuracy": dir_hits/dir_samples if dir_samples else 0.0,
                "outcome_samples": out_samples, "outcome_accuracy": out_hits/out_samples if out_samples else 0.0,
                "no_clear_outcomes": no}

    @staticmethod
    def _parse_target(outcome):
        if not outcome or outcome == "NO CLEAR OUTCOME": return None
        m = re.search(r"\$([\d,]+)\s+(OVER|BELOW)", str(outcome))
        return float(m.group(1).replace(",", "")) if m else None
