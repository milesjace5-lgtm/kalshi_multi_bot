import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import CFG
from indicators import (
    closes, highs, lows, ema, rsi, atr, macd, bollinger, adx, vwap_from_trades,
    relative_volume, volatility_ratio, slope, efficiency_ratio, structure,
    local_levels, pattern_name, normalized_pattern, trade_flow, candle_features,
    sequence_similarity, candle_pressure, chart_patterns, momentum_divergence, continuation_patterns, flow_acceleration, reversal_reclaim,
)


class Analyzer:
    """Stateful hourly BTC analysis engine.

    The engine intentionally separates:
      1) directional thesis,
      2) projected finish,
      3) $100 target line,
      4) developing/locked state.

    A target can never be emitted on the wrong side of the model's own projection.
    A stale Level-2 book is ignored rather than treated as neutral evidence.
    """

    def __init__(self):
        self.last_report: Optional[Dict[str, Any]] = None
        self.hour_state = self._new_hour_state(None)

    def restore_hour_state(self, report, current_hour_ts=None):
        """Restore a confirmed path after a process restart, never a stale hour.

        Only an explicitly locked outcome with a valid path contract is restored.
        Developing confirmation streaks are intentionally discarded because a
        restart interrupts their timing continuity.
        """
        if not isinstance(report, dict):
            return False
        try:
            report_hour = int(report.get("hour_ts"))
            current_hour = int(current_hour_ts if current_hour_ts is not None else time.time() // 3600 * 3600)
            if report_hour != current_hour:
                return False
            lock = report.get("lock") or {}
            outcome = lock.get("locked_outcome") or (report.get("outcome") if lock.get("locked") else None)
            if not lock.get("locked") or self._outcome_direction(outcome) not in ("UP", "DOWN"):
                return False
            target = self._parse_target(outcome)
            contract = lock.get("path_contract")
            if target is None or not isinstance(contract, dict):
                return False
            direction = self._outcome_direction(outcome)
            if contract.get("direction") != direction or not contract.get("target_is_checkpoint_only", False):
                return False
            age = max(0.0, min(float(lock.get("age", 0.0) or 0.0), 3600.0))
            self.hour_state = self._new_hour_state(current_hour)
            self.hour_state.update({
                "target_level": float(target),
                "locked": str(outcome),
                "locked_dir": direction,
                "locked_target": float(target),
                "locked_at": time.time() - age,
                "invalid_count": 0,
                "path_contract": dict(contract),
            })
            self.last_report = report
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _round_hour_target(hour_open, step=None):
        """Round to nearest target increment with deterministic half-up ties."""
        step = float(CFG.TARGET_STEP if step is None else step)
        if not math.isfinite(step) or step <= 0:
            raise ValueError("TARGET_STEP must be a finite positive number")
        value = float(hour_open)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("hour_open must be a finite positive number")
        return float(math.floor(value / step + 0.5) * step)

    @staticmethod
    def _new_hour_state(hour_ts):
        return {
            "hour_ts": hour_ts,
            "candidate": None,
            "candidate_dir": None,
            "candidate_count": 0,
            "candidate_last_confirmation": 0.0,
            "candidate_started_at": 0.0,
            "locked": None,
            "locked_dir": None,
            "locked_target": None,
            "locked_at": 0.0,
            "target_level": None,
            "target_rebased_for_decision_window": False,
            "invalid_count": 0,
            "path_contract": None,
        }

    def analyze(self, snap, learned=None, learner=None):
        price = snap.get("price")
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            return {"status": "NO_LIVE_PRICE"}
        price = float(price)
        bars = snap.get("bars", {})
        m1, m5, m15 = bars.get("1m", []), bars.get("5m", []), bars.get("15m", [])
        m30, h1 = bars.get("30m", []), bars.get("1h", [])
        if min(len(m1), len(m5), len(m15)) < CFG.MIN_REQUIRED_BARS:
            return {"status": "BUILDING_DATA", "price": price, "bars": {k: len(v) for k, v in bars.items()}}

        now = time.time()
        hour_ts = int(now // 3600) * 3600
        mins_left = max(0.05, 60.0 - (now - hour_ts) / 60.0)
        if self.hour_state["hour_ts"] != hour_ts:
            self.hour_state = self._new_hour_state(hour_ts)

        hour_open, hour_high, hour_low = self._hour_state(h1, m1, hour_ts, price)
        # During the first half-hour, analyze structure without committing to a
        # target based on the hour open. At the decision window (30 minutes left),
        # anchor the target ladder to the LIVE BTC price, e.g. 83,700 -> 83,600,
        # 83,700, 83,800. Keep that anchor stable for the rest of the hour unless
        # the normal alternative-target selector finds a better qualifying line.
        if self.hour_state.get("target_level") is None:
            self.hour_state["target_level"] = self._round_hour_target(hour_open, CFG.TARGET_STEP)
        if (mins_left <= CFG.CALLOUT_START_MINUTES
                and not self.hour_state.get("target_rebased_for_decision_window")
                and not self.hour_state.get("locked")):
            self.hour_state["target_level"] = self._round_hour_target(price, CFG.TARGET_STEP)
            self.hour_state["target_rebased_for_decision_window"] = True
        target_level = float(self.hour_state["target_level"])

        # ---------------- MTF ----------------
        specs = [("1m", m1, .75), ("5m", m5, 1.55), ("15m", m15, 1.55), ("30m", m30, 1.20), ("1h", h1, .90)]
        tf_rows: List[Tuple[str, Dict[str, Any], float]] = []
        for tf, arr, weight in specs:
            if len(arr) >= CFG.MIN_TF_BARS:
                tf_rows.append((tf, self._timeframe(arr, price), weight))
        weighted = sum(r["score"] * w for _, r, w in tf_rows)
        wtotal = sum(w for _, _, w in tf_rows) or 1.0
        mtf_score = weighted / wtotal
        up_votes = sum(1 for _, r, _ in tf_rows if r["direction"] == "UP")
        down_votes = sum(1 for _, r, _ in tf_rows if r["direction"] == "DOWN")
        directional = up_votes + down_votes
        agreement = max(up_votes, down_votes) / max(directional, 1)
        mtf_dir = "UP" if up_votes > down_votes and up_votes >= 2 else "DOWN" if down_votes > up_votes and down_votes >= 2 else "RANGE"

        # ---------------- indicators ----------------
        a1 = self._safe_atr(m1, 14, price * .0007)
        a5 = self._safe_atr(m5, 14, price * .0012)
        a15 = self._safe_atr(m15, 14, a5 * 1.7)
        c1, c5, c15 = closes(m1), closes(m5), closes(m15)
        e9, e21, e50 = ema(c5, 9), ema(c5, 21), ema(c5, 50)
        r5, r15 = rsi(c5, 14), rsi(c15, 14)
        macd_line, macd_signal, macd_hist = macd(c5)
        bb_mid, bb_up, bb_low = bollinger(c5)
        ad = adx(m5, 14)
        rv = relative_volume(m5, 20)
        vr = volatility_ratio(m5, 20)
        vw = vwap_from_trades(snap.get("trades", []), since=hour_ts)
        flow = trade_flow(snap.get("trades", []), seconds=300)
        flow_acc = flow_acceleration(snap.get("trades", []), seconds=300)
        divergence = momentum_divergence(m5, 24)
        # Directional reversal detection must be independent of the hourly target.
        # Target-reclaim is retained as descriptive telemetry only; it cannot vote
        # for UP/DOWN, alter the path thesis, or change the projected finish.
        reversal = reversal_reclaim(m5, target=None, atr_value=a5, lookback=min(20, max(12, len(m5))))
        reversal1 = reversal_reclaim(m1, target=None, atr_value=a1, lookback=min(20, max(12, len(m1))))
        target_reclaim_telemetry = reversal_reclaim(m5, target=target_level, atr_value=a5, lookback=min(20, max(12, len(m5))))
        book = self._book_metrics(snap, price)

        st1 = structure(m1, min(12, max(5, len(m1) // 8)))
        st5 = structure(m5, min(12, max(5, len(m5) // 8)))
        st15 = structure(m15, min(8, max(5, len(m15) // 8)))
        st30 = structure(m30, min(6, max(5, len(m30) // 8))) if len(m30) >= 20 else {}
        p1, p5 = pattern_name(m1), pattern_name(m5)
        candle = candle_features(m1[-1])
        cp1 = candle_pressure(m1, CFG.CANDLE_PRESSURE_1M_BARS)
        cp5 = candle_pressure(m5, CFG.CANDLE_PRESSURE_5M_BARS)
        cp15 = candle_pressure(m15, CFG.CANDLE_PRESSURE_15M_BARS)
        shape5 = chart_patterns(m5, CFG.PATTERN_SHAPE_LOOKBACK)
        shape15 = chart_patterns(m15, CFG.PATTERN_SHAPE_LOOKBACK)
        shape = shape5 if shape5.get("confidence", 0.0) >= shape15.get("confidence", 0.0) else shape15

        # ---------------- momentum / evidence ----------------
        imp1 = float(c1[-1] - c1[-6]) if len(c1) >= 6 else 0.0
        imp5 = float(c5[-1] - c5[-4]) if len(c5) >= 4 else 0.0
        mom1 = self._clip(imp1 / max(a1 * 3.0, 1.0), -1, 1)
        mom5 = self._clip(imp5 / max(a5 * 1.75, 1.0), -1, 1)
        trend_strength = self._clip((ad - 10.0) / 25.0, 0, 1) if math.isfinite(ad) else 0.0

        evidence = {
            "mtf": self._clip(mtf_score / 2.0, -1, 1),
            "momentum_1m": mom1,
            "momentum_5m": mom5,
            "ema": self._ema_signal(price, e9, e21, e50, a5),
            "rsi": self._rsi_signal(r5, r15),
            "macd": self._clip(macd_hist / max(a5 * .10, 1.0), -1, 1) if math.isfinite(macd_hist) else 0.0,
            "structure": self._structure_signal(st5, st15, st30),
            "hour_position": self._clip((price - hour_open) / max(a15 * .75, 1.0), -1, 1),
            "vwap": self._clip((price - vw) / max(a5 * .60, 1.0), -1, 1) if math.isfinite(vw) else 0.0,
            "flow": self._clip(flow["aggressor_delta"] * 1.8, -1, 1) if flow["count"] >= CFG.MIN_FLOW_TRADES else 0.0,
            "book": self._clip(book["imbalance"] * 1.7, -1, 1) if book["usable"] else 0.0,
            "candle": self._candle_signal(candle),
            "candle_pressure": self._clip(cp5["delta"], -1, 1),
            "candle_strength": self._clip((cp5["bull_strength"] - cp5["bear_strength"]) / max(cp5["bull_strength"] + cp5["bear_strength"], 1e-9), -1, 1),
            "volume": self._volume_signal(rv, mom5),
            "flow_acceleration": self._clip(flow_acc.get("acceleration",0.0), -1, 1),
            "divergence": self._clip(divergence.get("score",0.0), -1, 1),
            "reversal_reclaim": self._clip(reversal.get("score",0.0), 0, 1),
            "reversal_reclaim_1m": self._clip(reversal1.get("score",0.0), 0, 1),
        }
        weights = {
            "mtf": 2.25, "momentum_1m": .85, "momentum_5m": 1.20, "ema": 1.10,
            "rsi": .75, "macd": .60, "structure": 1.35, "hour_position": .45,
            "vwap": .55, "flow": .82, "book": .50, "candle": .70, "candle_pressure": 1.05,
            "candle_strength": .85, "volume": .45, "flow_acceleration": .65, "divergence": .55,
            "reversal_reclaim": 1.15, "reversal_reclaim_1m": .45,
        }
        score = sum(evidence[k] * weights[k] for k in weights) / sum(weights.values()) * 4.0
        if trend_strength < .18:
            score *= .70
        elif trend_strength > .65:
            score *= 1.06

        score += .28 * evidence["flow_acceleration"] + .24 * evidence["divergence"]
        # Fresh reversal/reclaim evidence is allowed to overturn stale trend evidence.
        # This is the key fix for a selloff -> recovery -> target reclaim sequence.
        score += 0.72 * evidence["reversal_reclaim"] + 0.24 * evidence["reversal_reclaim_1m"]

        # ---------------- repeating patterns ----------------
        live_pattern = self._current_repeat_vote(m5)
        if live_pattern["direction"] == "UP": score += .42 * live_pattern["strength"]
        elif live_pattern["direction"] == "DOWN": score -= .42 * live_pattern["strength"]
        # Named chart shapes are path information, not blind signals. A confirmed
        # neckline break gets more weight; an unconfirmed W/M/shoulder shape stays lighter.
        if shape["direction"] == "UP": score += CFG.SHAPE_SCORE_WEIGHT * shape["confidence"] * (1.0 if shape["stage"] in ("BREAKOUT","NECKLINE") else .55)
        elif shape["direction"] == "DOWN": score -= CFG.SHAPE_SCORE_WEIGHT * shape["confidence"] * (1.0 if shape["stage"] in ("BREAKDOWN","NECKLINE") else .55)

        feature_vec = self._feature_vector(
            price, hour_open, hour_high, hour_low, a5, r5, r15, ad,
            flow["aggressor_delta"], book["imbalance"] if book["usable"] else 0.0,
            st5, st15, imp1, imp5, mom1, mom5, trend_strength,
            cp5, cp15, cp1, shape,
        )
        legacy_vec = self._legacy_feature_vector(
            price, hour_open, hour_high, hour_low, a5, r5, r15, ad,
            flow["aggressor_delta"], book["imbalance"] if book["usable"] else 0.0,
            st5, st15, imp1, imp5, mom1, mom5, trend_strength,
        )
        feature_payload = {"vector": feature_vec + [evidence["reversal_reclaim"], evidence["reversal_reclaim_1m"]], "legacy_vector": legacy_vec, "schema": 3}
        memory = learner.pattern_vote(feature_payload) if learner else {"direction": "NONE", "strength": 0.0, "matches": 0, "detail": []}
        if memory["direction"] == "UP": score += .48 * memory["strength"]
        elif memory["direction"] == "DOWN": score -= .48 * memory["strength"]

        learned = learned or {}
        score += self._clip(float(learned.get("bias", 0.0)), -.40, .40)
        learned_outcome_bias = self._clip(float(learned.get("outcome_bias", 0.0)), -.25, .25)

        # ---------------- levels / breakouts ----------------
        support, resistance, near_support, near_resistance = self._levels(m5, price, a5)
        breakout_signal, breakout_text = self._breakout_signal(
            price, near_support, near_resistance, m1, m5, hour_high, hour_low, a5
        )
        score += breakout_signal

        direction = "UP" if score >= CFG.MIN_SCORE_GAP else "DOWN" if score <= -CFG.MIN_SCORE_GAP else "RANGE"
        pattern_conflict = live_pattern["direction"] in ("UP", "DOWN") and memory["direction"] in ("UP", "DOWN") and live_pattern["direction"] != memory["direction"]

        # ---------------- projection ----------------
        projected = self._project_finish(
            price, mins_left, a1, a5, a15, imp1, imp5, score, trend_strength,
            vw, support, resistance, hour_open, hour_high, hour_low,
            live_pattern, memory, reversal, reversal1,
        )
        path_thesis = self._path_thesis(
            price, mins_left, a5, score, mom1, mom5, st5, st15, cp1, cp5, flow,
            breakout_signal, shape, reversal, support, resistance, projected,
        )
        # The path engine gets a small vote in the finish projection. This is
        # deliberately bounded so one noisy signal cannot create a giant forecast.
        if path_thesis["direction"] in ("UP", "DOWN") and path_thesis["confidence"] >= CFG.OUTCOME_MIN_PATH_CONFIDENCE:
            tscale = math.sqrt(max(mins_left, 1.0) / 60.0)
            path_push = path_thesis["score"] * a5 * 0.45 * tscale
            projected = float(projected + self._clip(path_push, -a5 * 0.55, a5 * 0.55))
        target_buffer = max(CFG.MIN_TARGET_EDGE_DOLLARS, a5 * CFG.TARGET_ATR_EDGE)
        projection_direction = "UP" if projected >= price + target_buffer * .35 else "DOWN" if projected <= price - target_buffer * .35 else "RANGE"
        projection_conflict = direction in ("UP", "DOWN") and projection_direction not in (direction,)

        # A weak-trend neutral state must not become a fake high-confidence call.
        if math.isfinite(ad) and ad < CFG.LOW_ADX_THRESHOLD and (not math.isfinite(r5) or 44 <= r5 <= 56) and abs(score) < CFG.STRONG_SCORE_THRESHOLD:
            direction = "RANGE"

        evidence_quality = self._evidence_quality(snap, tf_rows, flow, book, ad, rv)
        confidence = self._confidence(
            score, direction, agreement, directional, trend_strength, evidence_quality,
            pattern_conflict, live_pattern, memory, r5, r15, ad, flow, book, rv,
        )
        if projection_conflict:
            confidence = min(confidence, CFG.PROJECTION_CONFLICT_CAP)
        if pattern_conflict:
            confidence = min(confidence, CFG.PATTERN_CONFLICT_CAP)
        if not book["usable"]:
            confidence = min(confidence, CFG.STALE_BOOK_CONFIDENCE_CAP)
        if math.isfinite(rv) and rv < CFG.VERY_LOW_RVOL:
            confidence = min(confidence, .72)
        if agreement < CFG.MIN_MTF_AGREEMENT and directional >= 3:
            confidence = min(confidence, .72)

        # ---------------- independent outcome engine ----------------
        # The hourly target is fixed once per hour from the hour open. It is
        # always a real $100 line and cannot move around just because BTC moves.
        uncertainty = self._finish_uncertainty(price, mins_left, a1, a5, a15, trend_strength, rv)
        target_edge = projected - target_level
        target_z = target_edge / max(uncertainty, 1.0)
        target_signal = math.tanh(target_z)
        model_signal = self._clip(score / 2.5, -1, 1)
        mtf_signal = self._clip(mtf_score / 1.6, -1, 1)
        flow_signal = self._clip(flow["aggressor_delta"], -1, 1) if flow["count"] >= CFG.MIN_FLOW_TRADES else 0.0

        # The bot now builds three separate scenarios instead of forcing every
        # indicator into one yes/no vote. It measures the strength of UP, DOWN
        # and RANGE, then chooses the strongest scenario. Candle pressure and
        # actual aggressor selling/buying are first-class evidence here.
        scenarios = self._scenario_engine(
            target_level=target_level, price=price, target_z=target_z, target_signal=target_signal,
            score=score, model_signal=model_signal, mtf_signal=mtf_signal, flow_signal=flow_signal,
            cp1=cp1, cp5=cp5, cp15=cp15, trend_strength=trend_strength, agreement=agreement,
            structure_signal=evidence["structure"], memory=memory, live_pattern=live_pattern,
            breakout_signal=breakout_signal, ad=ad, rv=rv, pattern_conflict=pattern_conflict,
            projection_conflict=projection_conflict, evidence_quality=evidence_quality,
            mins_left=mins_left, uncertainty=uncertainty, target_edge=target_edge,
            learned_outcome_bias=learned_outcome_bias, shape=shape, flow_acceleration=flow_acc.get("acceleration",0.0), divergence=divergence.get("score",0.0),
            reversal_reclaim=reversal.get("score",0.0), reversal_1m=reversal1.get("score",0.0),
            projected_signal=self._clip((projected - price) / max(uncertainty, 1.0), -1, 1),
        )
        best_scenario = scenarios["best"]
        decision_bias = self._clip((scenarios["UP"]["strength"] - scenarios["DOWN"]["strength"]) / 100.0, -1, 1)
        outcome_confidence = scenarios[best_scenario]["confidence"]
        # Build the expected price path BEFORE comparing the projected finish with
        # the $100 line. The target is only the final checkpoint. It is never used
        # as a trigger to flip a developing or locked thesis.
        path_direction = path_thesis.get("direction") if path_thesis.get("direction") in ("UP", "DOWN") else best_scenario if best_scenario in ("UP", "DOWN") else direction
        if path_direction not in ("UP", "DOWN") and projection_direction in ("UP", "DOWN"):
            path_direction = projection_direction
        path_analysis = self._path_analysis(
            shape, price, a5, cp5, flow, target_level, support, resistance,
            near_support, near_resistance, projected, mins_left, path_direction=path_direction,
        )
        path_analysis.update({
            "thesis_direction": path_thesis.get("direction"),
            "thesis_score": path_thesis.get("score", 0.0),
            "thesis_confidence": path_thesis.get("confidence", 0.0),
            "stage": path_thesis.get("stage", path_analysis.get("stage", "RANGE")),
            "thesis_route": path_thesis.get("route", path_analysis.get("primary", "PATH")),
            "next_point": path_thesis.get("next_point", path_analysis.get("next_point", price)),
            "continuation_point": path_thesis.get("continuation_point", path_analysis.get("continuation_point", projected)),
            "invalidation": path_thesis.get("invalidation", path_analysis.get("invalidation", price)),
        })
        candidate = self._scenario_candidate(
            target_level, scenarios, mins_left, projected=projected, uncertainty=uncertainty,
            path_analysis=path_analysis, path_thesis=path_thesis, price=price, atr5=a5,
        )
        # If the preferred target has no defensible outcome, search a wider,
        # bounded set of valid target lines. Candidate targets are generated from
        # both the current target and projected finish; this helps find a nearby
        # line that matches the modeled path without chasing arbitrary outcomes.
        if not self.hour_state.get("locked") and mins_left <= CFG.CALLOUT_START_MINUTES:
            step = float(CFG.TARGET_STEP)
            alternatives = []
            target_candidates = set()
            # Prioritize the nearest $100 ladder around the live price. For a
            # price of $83,700, evaluate $83,600 / $83,700 / $83,800 first;
            # include one extra rung either way only as a fallback candidate.
            price_anchor = self._round_hour_target(price, step)
            projected_anchor = self._round_hour_target(projected, step)
            # Explore two nearby $100 ladders: one around live spot and one around
            # the projected finish. This finds more plausible checkpoints when the
            # expected finish is displaced from spot, without chasing distant lines.
            for anchor in (price_anchor, projected_anchor):
                for offset in (-2, -1, 0, 1, 2):
                    target_candidates.add(round(anchor + offset * step, 8))
            # Preserve the current line as a candidate for comparison.
            target_candidates.add(round(float(target_level), 8))
            # Bounded search: projected levels can be considered, but only within
            # the greater of $200 or a conservative volatility-based reach.
            # The cap prevents extreme projections from opening arbitrary targets.
            reach = max(step * 2.0, min(step * 3.0, max(float(uncertainty), float(a5)) * 1.25))
            target_candidates = [
                t for t in target_candidates
                if math.isfinite(t) and t > 0
                and abs(t - float(price)) <= reach
            ]
            thesis_dir = path_thesis.get("direction")
            for alt_target in target_candidates:
                alt_candidate = self._scenario_candidate(
                    alt_target, scenarios, mins_left, projected=projected,
                    uncertainty=uncertainty, path_analysis=path_analysis,
                    path_thesis=path_thesis, price=price, atr5=a5,
                )
                alt_dir = self._outcome_direction(alt_candidate)
                if alt_dir not in ("UP", "DOWN"):
                    continue
                if confidence < CFG.MIN_CONFIDENCE_FOR_OUTCOME:
                    continue
                if outcome_confidence < CFG.OUTCOME_MIN_PATH_CONFIDENCE:
                    continue
                if thesis_dir in ("UP", "DOWN") and alt_dir != thesis_dir:
                    continue
                # Rank by actual model path strength/confidence first, then
                # whether the finish projection supports the selected side, then
                # proximity to the projected finish. Never manufacture confidence.
                pscore = float(path_thesis.get("score", 0.0))
                path_strength = pscore if alt_dir == "UP" else -pscore
                edge = float(projected) - alt_target
                projection_support = edge if alt_dir == "UP" else -edge
                dist = abs(float(projected) - alt_target)
                # Prefer genuinely aligned paths first. Confidence is a ranking
                # input, not a guarantee; reject weak edge before this point and
                # favor targets supported by both projection and route direction.
                edge_strength = abs(edge) / max(float(uncertainty), 1.0)
                rank = (
                    -path_strength,
                    -projection_support,
                    -min(edge_strength, 3.0),
                    -float(outcome_confidence),
                    -float(confidence),
                    dist,
                    abs(alt_target - float(price)),
                    alt_target,
                )
                alternatives.append((rank, alt_target, alt_candidate))
            if alternatives:
                _, selected_target, selected_candidate = min(alternatives, key=lambda x: x[0])
                target_level = float(selected_target)
                candidate = selected_candidate
                self.hour_state["target_level"] = target_level
                path_analysis["target_checkpoint"] = target_level
                path_analysis["target_auto_selected"] = True
                path_analysis["target_selection_reason"] = (
                    "nearby targets re-evaluated against live price and current path; "
                    "selected target ranked best among qualifying outcomes"
                )
                path_analysis["target_candidates_checked"] = len(target_candidates)
                path_analysis["target_search_mode"] = "SPOT_AND_PROJECTED_LADDERS"
                path_analysis["target_search_reach_dollars"] = round(float(reach), 2)

        candidate_dir = self._outcome_direction(candidate)
        # The engine analyzes all day, but an early-hour candidate is telemetry only.
        # It cannot become a user-facing outcome or lock before the final 30 minutes.
        # Between 30 and 10 minutes remaining, the normal confirmation rules apply.
        # Inside the final 10 minutes, only a very strong fresh setup may create a
        # new lock; an existing lock can continue to be monitored normally.
        callout_state = "ANALYZING_EARLY"
        if self.hour_state.get("locked"):
            callout_state = "LOCKED_PATH_MONITORING"
            final_outcome, lock_meta = self._apply_lock(
                candidate=candidate, candidate_dir=candidate_dir, confidence=confidence,
                score=score, projected=projected, price=price, atr5=a5, mins_left=mins_left,
                path_analysis=path_analysis, shape=shape, st5=st5, st15=st15,
                cp5=cp5, flow=flow, m5=m5,
            )
        elif mins_left > CFG.CALLOUT_START_MINUTES:
            final_outcome = "WAITING FOR SETUP"
            lock_meta = {
                "locked": False, "candidate_count": 0, "invalidated": False,
                "age": 0, "locked_outcome": None, "reason": "ANALYSIS ONLY UNTIL 30 MINUTES REMAIN",
                "path_state": "EARLY_ANALYSIS", "target_crosses_ignored": True,
            }
            callout_state = "WAITING FOR SETUP"
        elif mins_left < CFG.CALLOUT_LATE_CUTOFF_MINUTES and candidate != "NO CLEAR OUTCOME":
            # In the final 10 minutes, permit a high-confidence directional
            # scenario even when a full structural path has not formed. Prefer
            # the normal path-confirmed setup, but allow the user's 75% fallback.
            path_confirmed_late = (
                confidence >= CFG.MIN_LATE_LOCK_CONFIDENCE
                and outcome_confidence >= CFG.MIN_LATE_LOCK_CONFIDENCE
                and path_thesis.get("confidence", 0.0) >= 0.72
                and abs(path_thesis.get("score", 0.0)) >= CFG.PATH_STRONG_SCORE
            )
            high_confidence_no_path = (
                confidence >= 0.75
                and outcome_confidence >= 0.75
                and candidate_dir in ("UP", "DOWN")
                and path_thesis.get("direction") not in ("UP", "DOWN")
            )
            late_ok = candidate_dir in ("UP", "DOWN") and (path_confirmed_late or high_confidence_no_path)
            if late_ok:
                final_outcome, lock_meta = self._apply_lock(
                    candidate=candidate, candidate_dir=candidate_dir, confidence=confidence,
                    score=score, projected=projected, price=price, atr5=a5, mins_left=mins_left,
                    path_analysis=path_analysis, shape=shape, st5=st5, st15=st15,
                    cp5=cp5, flow=flow, m5=m5,
                )
                callout_state = (
                    "LATE_HIGH_CONFIDENCE_NO_PATH"
                    if high_confidence_no_path and not lock_meta.get("locked")
                    else ("LATE_STRONG_SETUP" if not lock_meta.get("locked") else "LOCKED")
                )
            else:
                final_outcome = "NO CLEAR OUTCOME"
                lock_meta = {
                    "locked": False, "candidate_count": self.hour_state.get("candidate_count", 0),
                    "invalidated": False, "age": 0, "locked_outcome": None,
                    "reason": "LATE WINDOW: NEW CALL REQUIRES STRONGER CONFIRMATION",
                    "path_state": "LATE_WINDOW_CLOSED", "target_crosses_ignored": True,
                }
                callout_state = "LATE_WINDOW_NO_CONFIRMED_SETUP"
        else:
            final_outcome, lock_meta = self._apply_lock(
                candidate=candidate, candidate_dir=candidate_dir, confidence=confidence,
                score=score, projected=projected, price=price, atr5=a5, mins_left=mins_left,
                path_analysis=path_analysis, shape=shape, st5=st5, st15=st15,
                cp5=cp5, flow=flow, m5=m5,
            )
            callout_state = "LOCKED" if lock_meta.get("locked") else "DEVELOPING"

        # If an existing path was invalidated, do NOT instantly flip to the other
        # side. The engine goes back to DEVELOPMENT and must build a new path first.
        if lock_meta.get("invalidated"):
            final_outcome = "NO CLEAR OUTCOME"
            callout_state = "PATH_INVALIDATED_REBUILDING"
        final_direction = self._outcome_direction(final_outcome)

        # ---------------- finish range ----------------
        range_low, range_high = self._finish_range(
            price, projected, a5, mins_left, final_direction, confidence,
            support, resistance,
        )

        reasons = [
            f"multi-TF {up_votes} UP / {down_votes} DOWN",
            f"MTF agreement {agreement:.2f}",
            f"ADX {ad:.1f}" if math.isfinite(ad) else "ADX unavailable",
            f"RSI5/15 {r5:.1f}/{r15:.1f}" if math.isfinite(r5) and math.isfinite(r15) else "RSI partial",
            f"5m relative volume {rv:.2f}x" if math.isfinite(rv) else "5m relative volume unavailable",
            f"aggressor flow {flow['aggressor_delta']:+.2f} (buy {flow.get('buy_ratio',0):.0%} / sell {flow.get('sell_ratio',0):.0%}) | flow acceleration {flow_acc.get('acceleration',0):+.2f}" if flow["count"] >= CFG.MIN_FLOW_TRADES else "flow not confirmed",
            f"candle pressure {cp5['delta']:+.2f} | buy candles {cp5['buy_candles']} | sell candles {cp5['sell_candles']} | exhaustion {cp5['exhaustion']:.2f}",
            f"reversal/reclaim {reversal.get('score',0):.2f} ({reversal.get('text','NONE')}) | 1m {reversal1.get('score',0):.2f}",
            f"L2 {book['imbalance']:+.2f}" if book["usable"] else "L2 unavailable/stale",
            f"live pattern {live_pattern['direction']} ({live_pattern['matches']} matches)",
            f"memory pattern {memory['direction']} ({memory['matches']} matches)",
            f"chart shape {shape['name']} | stage={shape['stage']} | conf={shape['confidence']:.2f} | divergence={divergence.get('text','NONE')}" if shape['name'] != "NONE" else f"chart shape NONE | divergence={divergence.get('text','NONE')}",
            f"path thesis {path_thesis.get('direction')} {path_thesis.get('stage')} score={path_thesis.get('score',0):+.2f} conf={path_thesis.get('confidence',0):.2f}",
            f"path {path_analysis['primary']} | next=${path_analysis.get('next_point',price):,.0f} | continuation=${path_analysis.get('continuation_point',projected):,.0f}",
            breakout_text,
        ]
        if pattern_conflict: reasons.append("live and memory patterns conflict")
        if projection_conflict: reasons.append(f"projection conflicts with score direction {direction}")
        if lock_meta.get("invalidated"): reasons.append(lock_meta.get("reason", "previous path lock invalidated"))

        report = {
            "status": "OK", "ts": now, "hour_ts": hour_ts, "price": price, "minutes_left": mins_left,
            "direction": final_direction, "candidate_direction": direction,
            "confidence": confidence, "score": score,
            "range_low": range_low, "range_high": range_high,
            "projected_finish": projected, "projected_direction": projection_direction,
            "projection_edge": projected - price, "target_buffer": target_buffer,
            "target_level": target_level,
            "target_distance": target_edge,
            "target_z": target_z, "decision_bias": decision_bias,
            "scenarios": scenarios, "best_scenario": best_scenario,
            "candle_pressure_1m": cp1, "candle_pressure_5m": cp5, "candle_pressure_15m": cp15,
            "outcome_confidence": outcome_confidence, "finish_uncertainty": uncertainty,
            "outcome": final_outcome,
            "candidate_outcome": candidate,
            "callout_state": callout_state,
            "callout_window": {
                "start_minutes_remaining": CFG.CALLOUT_START_MINUTES,
                "preferred_end_minutes_remaining": CFG.CALLOUT_LATE_CUTOFF_MINUTES,
                "active": mins_left <= CFG.CALLOUT_START_MINUTES,
            },
            "hour_open": hour_open, "hour_high": hour_high, "hour_low": hour_low,
            "support": support, "resistance": resistance,
            "near_support": near_support, "near_resistance": near_resistance,
            "atr1": a1, "atr5": a5, "atr15": a15,
            "ema9": e9, "ema21": e21, "ema50": e50,
            "rsi": r5, "rsi15": r15, "macd_hist": macd_hist, "adx": ad,
            "bb_mid": bb_mid, "bb_upper": bb_up, "bb_lower": bb_low,
            "relative_volume": rv, "volatility_ratio": vr, "vwap": vw,
            "trade_flow": flow, "flow_acceleration": flow_acc, "divergence": divergence, "reversal_reclaim": reversal, "reversal_reclaim_1m": reversal1, "target_reclaim_telemetry": target_reclaim_telemetry, "book": book,
            "slope1": slope(c1, 20), "slope5": slope(c5, 20), "efficiency5": efficiency_ratio(c5, 20),
            "structure1": st1, "structure": st5, "structure15": st15, "structure30": st30,
            "timeframes": {tf: row for tf, row, _ in tf_rows},
            "pattern": f"{p1} | {p5}", "pattern_live": live_pattern, "pattern_memory": memory, "chart_pattern": shape, "chart_pattern_5m": shape5, "chart_pattern_15m": shape15,
            "path_analysis": path_analysis, "path_thesis": path_thesis,
            "features": feature_payload, "evidence": evidence,
            "trend_strength": trend_strength, "evidence_quality": evidence_quality,
            "mtf_direction": mtf_dir, "mtf_agreement": agreement,
            "projection_conflict": projection_conflict, "pattern_conflict": pattern_conflict,
            "reasons": reasons[-14:], "lock": lock_meta,
        }
        self.last_report = report
        return report

    # ---------------- calculations ----------------
    @staticmethod
    def _safe_atr(bars, n, fallback):
        v = atr(bars, n)
        return float(v) if math.isfinite(v) and v > 0 else float(max(fallback, 1.0))

    @staticmethod
    def _hour_state(h1, m1, hour_ts, price):
        hb = next((b for b in h1 if int(b.get("ts", -1)) == hour_ts), None)
        if hb:
            return float(hb["open"]), max(float(hb["high"]), price), min(float(hb["low"]), price)
        cur = [b for b in m1 if int(b.get("ts", 0)) >= hour_ts]
        if cur:
            return float(cur[0]["open"]), max([float(b["high"]) for b in cur] + [price]), min([float(b["low"]) for b in cur] + [price])
        if h1:
            op = float(h1[-1]["close"]); return op, max(op, price), min(op, price)
        return price, price, price

    @staticmethod
    def _clip(v, lo, hi):
        try: return max(lo, min(hi, float(v)))
        except Exception: return 0.0

    @staticmethod
    def _ema_signal(price, e9, e21, e50, a5):
        if not (math.isfinite(e9) and math.isfinite(e21)): return 0.0
        s = Analyzer._clip((e9 - e21) / max(a5 * .20, 1.0), -1, 1)
        if math.isfinite(e50): s = .70 * s + .30 * Analyzer._clip((price - e50) / max(a5 * .60, 1.0), -1, 1)
        return s

    @staticmethod
    def _rsi_signal(r5, r15):
        vals = []
        if math.isfinite(r5): vals.append(Analyzer._clip((r5 - 50) / 15, -1, 1))
        if math.isfinite(r15): vals.append(Analyzer._clip((r15 - 50) / 15, -1, 1))
        return float(np.mean(vals)) if vals else 0.0

    @staticmethod
    def _structure_signal(*sts):
        vals = []
        for st in sts:
            if not st: continue
            if st.get("HH") and st.get("HL"): vals.append(1.0)
            elif st.get("LH") and st.get("LL"): vals.append(-1.0)
            elif st.get("HH") and st.get("LL"): vals.append(.10)
            elif st.get("LH") and st.get("HL"): vals.append(-.10)
            else: vals.append(0.0)
        return float(np.mean(vals)) if vals else 0.0

    @staticmethod
    def _candle_signal(f):
        if f["bull"] and f["body_pct"] > .55 and f["close_pos"] > .72: return 1.0
        if not f["bull"] and f["body_pct"] > .55 and f["close_pos"] < .28: return -1.0
        if f["lower_wick_pct"] > .55 and f["close_pos"] > .55: return .45
        if f["upper_wick_pct"] > .55 and f["close_pos"] < .45: return -.45
        return 0.0

    @staticmethod
    def _volume_signal(rv, momentum):
        if not math.isfinite(rv) or rv < 1.10: return 0.0
        return Analyzer._clip(momentum * min((rv - 1.0) / 1.0, 1.0), -1, 1)

    @staticmethod
    def _evidence_quality(snap, tf_rows, flow, book, ad, rv):
        q = .25 * min(1.0, len(tf_rows) / 5.0)
        age = snap.get("age")
        if age is not None and age <= CFG.STALE_AFTER_SECONDS: q += .25
        if flow.get("count", 0) >= CFG.MIN_FLOW_TRADES: q += .15
        if book.get("usable"): q += .15
        if math.isfinite(ad): q += .10
        if math.isfinite(rv): q += .10
        return Analyzer._clip(q, 0, 1)

    @staticmethod
    def _confidence(score, direction, agreement, directional, trend_strength, quality,
                    pattern_conflict, live_pattern, memory, r5, r15, ad, flow, book, rv):
        if direction == "RANGE": return CFG.RANGE_CONFIDENCE_CAP
        magnitude = Analyzer._clip(abs(score) / 3.0, 0, 1)
        base = .50 + .18 * magnitude + .08 * agreement + .08 * trend_strength + .10 * quality
        if live_pattern.get("direction") == direction: base += .025 * live_pattern.get("strength", 0)
        if memory.get("direction") == direction: base += .035 * memory.get("strength", 0)
        if pattern_conflict: base -= .10
        if math.isfinite(ad) and ad < CFG.LOW_ADX_THRESHOLD: base -= .08
        if math.isfinite(r5) and math.isfinite(r15) and 44 <= r5 <= 56 and 44 <= r15 <= 56: base -= .06
        if flow.get("count", 0) < CFG.MIN_FLOW_TRADES: base -= .025
        if not book.get("usable"): base -= .01
        if math.isfinite(rv) and rv < CFG.VERY_LOW_RVOL: base -= .04
        if directional < 3: base -= .03
        return Analyzer._clip(base, .50, CFG.MAX_CONFIDENCE)

    @staticmethod
    def _path_thesis(price, mins_left, a5, score, mom1, mom5, st5, st15, cp1, cp5, flow,
                     breakout_signal, shape, reversal, support, resistance, projected):
        """Build the directional path independently of the hourly target.

        This is the core human-like layer: identify what is forming, determine
        whether the latest move is impulse/pullback/reclaim/continuation, then
        project the next levels. The target is deliberately absent from this
        calculation.
        """
        structure_sig = Analyzer._structure_signal(st5, st15)
        pressure = Analyzer._clip(0.30 * cp1.get("delta", 0.0) + 0.70 * cp5.get("delta", 0.0), -1, 1)
        flow_sig = Analyzer._clip(flow.get("aggressor_delta", 0.0), -1, 1) if flow.get("count", 0) else 0.0
        impulse = Analyzer._clip(0.40 * mom1 + 0.60 * mom5, -1, 1)
        reversal_sig = float(reversal.get("score", 0.0)) if reversal else 0.0
        reversal_dir = reversal.get("direction", "NONE") if reversal else "NONE"
        shape_dir = shape.get("direction", "NONE")
        shape_conf = float(shape.get("confidence", 0.0))

        raw = (
            0.24 * Analyzer._clip(score / 3.0, -1, 1) +
            0.21 * structure_sig +
            0.18 * impulse +
            0.15 * pressure +
            0.10 * flow_sig +
            0.06 * Analyzer._clip(breakout_signal / 0.34, -1, 1)
        )
        if shape_dir == "UP": raw += 0.06 * shape_conf
        elif shape_dir == "DOWN": raw -= 0.06 * shape_conf
        if reversal_dir == "UP": raw += 0.10 * Analyzer._clip(reversal_sig, 0, 1)
        elif reversal_dir == "DOWN": raw -= 0.10 * Analyzer._clip(reversal_sig, 0, 1)
        raw = Analyzer._clip(raw, -1, 1)

        # A strong recent move followed by a counter-move is treated as a pullback
        # until structure actually flips. This prevents a normal retrace from
        # destroying the original path.
        counter = 0.0
        if impulse > 0.22 and pressure < -0.18: counter = -1.0
        elif impulse < -0.22 and pressure > 0.18: counter = 1.0

        if raw >= CFG.PATH_STRONG_SCORE:
            direction = "UP"
        elif raw <= -CFG.PATH_STRONG_SCORE:
            direction = "DOWN"
        elif raw >= CFG.PATH_MIN_SCORE:
            direction = "UP"
        elif raw <= -CFG.PATH_MIN_SCORE:
            direction = "DOWN"
        else:
            # Structure wins when momentum is temporarily mixed.
            direction = "UP" if structure_sig >= 0.28 else "DOWN" if structure_sig <= -0.28 else "RANGE"

        stage = "RANGE"
        if direction == "UP":
            if reversal_dir == "UP" and reversal_sig >= 0.55: stage = "REVERSAL DEVELOPING"
            elif counter < 0: stage = "PULLBACK IN UP PATH"
            elif breakout_signal > 0.15 or shape.get("stage") in ("BREAKOUT", "NECKLINE"): stage = "BREAKOUT / CONTINUATION"
            elif impulse > 0.20: stage = "UP IMPULSE"
            else: stage = "UP CONTINUATION"
        elif direction == "DOWN":
            if reversal_dir == "DOWN" and reversal_sig >= 0.55: stage = "REVERSAL DEVELOPING"
            elif counter > 0: stage = "PULLBACK IN DOWN PATH"
            elif breakout_signal < -0.15 or shape.get("stage") in ("BREAKDOWN", "NECKLINE"): stage = "BREAKDOWN / CONTINUATION"
            elif impulse < -0.20: stage = "DOWN IMPULSE"
            else: stage = "DOWN CONTINUATION"

        # Next point is the nearest meaningful level in the path direction.
        if direction == "UP":
            next_point = float(resistance if resistance > price else price + max(a5 * 0.45, 20.0))
            continuation = max(next_point + a5 * 0.35, float(projected))
            invalidation = float(support if support < price else price - a5 * 0.65)
            route = f"{stage} -> TEST ${next_point:,.0f} -> RETEST/CONTINUE -> FINISH ${continuation:,.0f}"
        elif direction == "DOWN":
            next_point = float(support if support < price else price - max(a5 * 0.45, 20.0))
            continuation = min(next_point - a5 * 0.35, float(projected))
            invalidation = float(resistance if resistance > price else price + a5 * 0.65)
            route = f"{stage} -> TEST ${next_point:,.0f} -> BOUNCE/RETEST -> FINISH ${continuation:,.0f}"
        else:
            next_point = price
            continuation = float(projected)
            invalidation = price
            route = "RANGE / WAIT FOR STRUCTURE BREAK"

        # Path confidence comes from agreement among independent components, not
        # from target distance.
        components = [abs(structure_sig), abs(impulse), abs(pressure), abs(flow_sig), abs(raw)]
        same_side = 0
        for x in (structure_sig, impulse, pressure, flow_sig):
            if direction == "UP" and x > 0.12: same_side += 1
            if direction == "DOWN" and x < -0.12: same_side += 1
        confidence = Analyzer._clip(0.48 + 0.10 * same_side + 0.18 * abs(raw) + 0.08 * max(components), 0.0, 0.95)
        return {
            "direction": direction, "score": float(raw), "confidence": float(confidence),
            "stage": stage, "next_point": next_point, "continuation_point": float(continuation),
            "invalidation": invalidation, "route": route,
            "impulse": float(impulse), "structure": float(structure_sig),
            "pressure": float(pressure), "flow": float(flow_sig),
            "counter_move": float(counter),
        }

    @staticmethod
    def _project_finish(price, mins_left, a1, a5, a15, imp1, imp5, score, trend_strength,
                        vw, support, resistance, hour_open, hour_high, hour_low, live_pattern, memory, reversal=None, reversal1=None):
        # Ensemble of short drift, medium drift, score, VWAP and current-hour continuation.
        drift1 = imp1 / 5.0
        drift5 = imp5 / 15.0
        raw_drift = .55 * drift1 + .45 * drift5
        damp = .40 + .60 * trend_strength
        drift_forecast = price + raw_drift * damp * mins_left
        score_forecast = price + (score / 4.0) * a5 * math.sqrt(max(mins_left, 1.0) / 60.0) * .55
        vwap_forecast = price
        if math.isfinite(vw):
            # Strong trend gets less mean reversion; weak trend gets more.
            vwap_forecast = price + (vw - price) * (.10 + .20 * (1.0 - trend_strength))
        hour_cont = price + (price - hour_open) * .25 * math.sqrt(max(mins_left, 1.0) / 60.0)
        pattern_bias = 0.0
        if live_pattern.get("direction") == "UP": pattern_bias += live_pattern.get("strength", 0.0)
        elif live_pattern.get("direction") == "DOWN": pattern_bias -= live_pattern.get("strength", 0.0)
        if memory.get("direction") == "UP": pattern_bias += .60 * memory.get("strength", 0.0)
        elif memory.get("direction") == "DOWN": pattern_bias -= .60 * memory.get("strength", 0.0)
        pattern_forecast = price + pattern_bias * a5 * .35 * math.sqrt(max(mins_left, 1.0) / 60.0)
        reversal = reversal or {}
        reversal1 = reversal1 or {}
        rev_score = float(reversal.get("score", 0.0))
        rev1_score = float(reversal1.get("score", 0.0))
        # A fresh V-reversal/reclaim deserves its own projection component so the
        # forecast does not stay anchored to the earlier selloff.
        reversal_forecast = price + (0.55 * rev_score + 0.20 * rev1_score) * a5 * math.sqrt(max(mins_left, 1.0) / 60.0)

        weights = np.array([.36, .23, .09, .10, .08, .14], dtype=float)
        projected = float(np.dot(weights, [drift_forecast, score_forecast, vwap_forecast, hour_cont, pattern_forecast, reversal_forecast]))

        # Structure soft pull only; no stale hard clamp.
        if math.isfinite(support) and projected < support:
            projected += (support - projected) * .08
        if math.isfinite(resistance) and projected > resistance:
            projected -= (projected - resistance) * .08

        # Volatility envelope. This is an uncertainty guard, not a target.
        max_move = max(a5 * .35, a15 * .16, a1 * .80) * math.sqrt(max(mins_left, 1.0) / 60.0)
        max_move = max(max_move, a5 * .25)
        return float(max(price - max_move, min(price + max_move, projected)))

    @staticmethod
    def _reference_target(projected, direction, projection_direction):
        if projected is None or not math.isfinite(float(projected)):
            return None
        return float(round(float(projected) / CFG.TARGET_STEP) * CFG.TARGET_STEP)

    @staticmethod
    def _finish_uncertainty(price, mins_left, a1, a5, a15, trend_strength, rv):
        t = math.sqrt(max(mins_left, 1.0) / 60.0)
        vol = max(a1 * .90, a5 * .42, a15 * .16) * t
        if math.isfinite(rv) and rv > 1.0:
            vol *= min(1.45, 1.0 + (rv - 1.0) * .18)
        if trend_strength < .20:
            vol *= 1.18
        return float(max(10.0, vol))

    @staticmethod
    def _outcome_confidence(decision_bias, target_z, base_conf, quality, agreement, trend_strength,
                            pattern_conflict, projection_conflict, rv, ad, directional):
        strength = abs(float(decision_bias))
        edge = min(1.0, abs(float(target_z)) / 1.75)
        conf = .49 + .16 * strength + .10 * edge + .08 * quality + .06 * agreement + .05 * trend_strength
        if directional >= 3 and agreement >= .60: conf += .035
        if math.isfinite(ad) and ad >= 20: conf += .025
        if math.isfinite(rv) and rv >= 1.0: conf += .025
        if pattern_conflict: conf -= .055
        if projection_conflict: conf -= .035
        # Never let a strong-looking outcome exceed the underlying evidence quality.
        conf = min(conf, float(base_conf) + .10)
        return Analyzer._clip(conf, .50, CFG.MAX_CONFIDENCE)

    @staticmethod
    def _scenario_engine(target_level, price, target_z, target_signal, score, model_signal, mtf_signal,
                          flow_signal, cp1, cp5, cp15, trend_strength, agreement, structure_signal,
                          memory, live_pattern, breakout_signal, ad, rv, pattern_conflict,
                          projection_conflict, evidence_quality, mins_left, uncertainty, target_edge,
                          learned_outcome_bias=0.0, shape=None, flow_acceleration=0.0, divergence=0.0, reversal_reclaim=0.0, reversal_1m=0.0, projected_signal=0.0):
        def pos(x): return (Analyzer._clip(x, -1, 1) + 1.0) / 2.0
        def neg(x): return (Analyzer._clip(-x, -1, 1) + 1.0) / 2.0
        candle = Analyzer._clip(.25 * cp1.get("delta",0) + .55 * cp5.get("delta",0) + .20 * cp15.get("delta",0), -1, 1)
        # Recent candles get more weight than old candles. Exhaustion trims
        # continuation rather than pretending it automatically means reversal.
        exhaustion = Analyzer._clip(cp5.get("exhaustion",0), 0, 1)
        candle_up = Analyzer._clip(pos(candle) - .16 * exhaustion * pos(candle), 0, 1)
        candle_down = Analyzer._clip(neg(candle) - .16 * exhaustion * neg(candle), 0, 1)
        memory_up = memory.get("strength",0.0) if memory.get("direction") == "UP" else 0.0
        memory_down = memory.get("strength",0.0) if memory.get("direction") == "DOWN" else 0.0
        live_up = live_pattern.get("strength",0.0) if live_pattern.get("direction") == "UP" else 0.0
        live_down = live_pattern.get("strength",0.0) if live_pattern.get("direction") == "DOWN" else 0.0
        breakout_up = max(0.0, breakout_signal) / .34 if breakout_signal > 0 else 0.0
        breakout_down = max(0.0, -breakout_signal) / .34 if breakout_signal < 0 else 0.0
        shape = shape or {}
        shape_up = shape.get("confidence",0.0) if shape.get("direction") == "UP" else 0.0
        shape_down = shape.get("confidence",0.0) if shape.get("direction") == "DOWN" else 0.0
        # A breakout can still pull back/retest. This is a path expectation, not a
        # reversal call: it slightly favors continuation while reducing certainty.
        shape_path = 0.04 if shape.get("pullback_expected") else 0.0
        # The hourly target is deliberately excluded from scenario direction.
        # Scenario direction answers "what path is BTC following?"; the target is
        # checked only afterward against the projected end-of-hour finish.
        path_projection = Analyzer._clip(projected_signal, -1, 1)
        # Weighted evidence. Candle body/close strength + aggressor flow are
        # deliberately heavy because they describe who is actually winning.
        up = (
            .16 * pos(model_signal) + .08 * pos(path_projection) +
            .17 * candle_up + .14 * pos(flow_signal) + .09 * pos(mtf_signal) +
            .07 * pos(structure_signal) + .05 * memory_up + .04 * live_up +
            .04 * breakout_up + .06 * shape_up + .03 * trend_strength + .02 * evidence_quality + .04 * pos(learned_outcome_bias)
            + .10 * Analyzer._clip(reversal_reclaim, 0, 1) + .04 * Analyzer._clip(reversal_1m, 0, 1)
        )
        down = (
            .16 * neg(model_signal) + .08 * neg(path_projection) +
            .17 * candle_down + .14 * neg(flow_signal) + .09 * neg(mtf_signal) +
            .07 * neg(structure_signal) + .05 * memory_down + .04 * live_down +
            .04 * breakout_down + .06 * shape_down + .03 * trend_strength + .02 * evidence_quality + .04 * neg(learned_outcome_bias)
        )
        conflict = (1.0 if pattern_conflict else 0.0) * .10 + (1.0 if projection_conflict else 0.0) * .06
        balance = 1.0 - abs(up - down)
        range_strength = (
            .30 * (1.0 - abs(model_signal)) + .18 * (1.0 - abs(candle)) +
            .16 * (1.0 - abs(flow_signal)) + .12 * (1.0 - trend_strength) +
            .10 * balance + .08 * (1.0 - agreement) + .06 * conflict
        )
        # A real directional path should not disappear just because the most recent
        # candles are mixed. Reduce RANGE when the model/path and either structure
        # or MTF direction agree. This still leaves genuine chop as RANGE.
        directional_path = abs(model_signal) >= .34 and abs(path_projection) >= .10
        aligned_path = (model_signal > 0 and (structure_signal > .10 or mtf_signal > .10)) or (model_signal < 0 and (structure_signal < -.10 or mtf_signal < -.10))
        if directional_path and aligned_path:
            range_strength *= .78
        # Near the close, the developing path still matters. The target is only
        # the final checkpoint, so crossing it cannot amplify or reverse the path.
        if mins_left <= 10:
            up *= 1.06; down *= 1.06
        strengths = {"UP": max(0.0, min(1.0, up)) * 100.0,
                     "DOWN": max(0.0, min(1.0, down)) * 100.0,
                     "RANGE": max(0.0, min(1.0, range_strength)) * 100.0}
        ordered = sorted(strengths.items(), key=lambda x: x[1], reverse=True)
        best = ordered[0][0]
        # Confidence reflects the winning scenario's strength and separation
        # from the runner-up, capped by the actual evidence quality.
        gap = max(0.0, ordered[0][1] - ordered[1][1])
        conf = .48 + .0030 * ordered[0][1] + .0018 * min(gap, 20.0) + .07 * evidence_quality
        if best in ("UP","DOWN") and trend_strength > .55: conf += .02
        if best == "RANGE" and trend_strength < .20: conf += .02
        if pattern_conflict: conf -= .045
        if projection_conflict: conf -= .025
        if math.isfinite(ad) and ad >= 20: conf += .02
        if math.isfinite(rv) and rv >= 1.0: conf += .015
        conf = Analyzer._clip(conf, .50, CFG.MAX_CONFIDENCE)
        details = {
            "UP": {"strength": strengths["UP"], "confidence": conf if best == "UP" else Analyzer._clip(conf-.03,.50,CFG.MAX_CONFIDENCE)},
            "DOWN": {"strength": strengths["DOWN"], "confidence": conf if best == "DOWN" else Analyzer._clip(conf-.03,.50,CFG.MAX_CONFIDENCE)},
            "RANGE": {"strength": strengths["RANGE"], "confidence": conf if best == "RANGE" else Analyzer._clip(conf-.03,.50,CFG.MAX_CONFIDENCE)},
        }
        details["ordered"] = [{"scenario": k, "strength": round(v,1)} for k,v in ordered]
        details["best"] = best
        details["winner_gap"] = gap
        details["candle_delta"] = candle
        details["path_projection_signal"] = path_projection
        details["target_used_for_direction"] = False
        details["exhaustion"] = exhaustion
        details["chart_pattern"] = shape.get("name", "NONE")
        details["chart_pattern_stage"] = shape.get("stage", "NONE")
        details["pullback_expected"] = bool(shape.get("pullback_expected"))
        return details

    @staticmethod
    def _scenario_candidate(target_level, scenarios, mins_left, projected=None, uncertainty=None, path_analysis=None, path_thesis=None, price=None, atr5=None):
        """Choose the hourly outcome from the *path*, then use the target as the finish line.

        The current price is never enough by itself. A target may be crossed repeatedly
        while the same route is developing. Late in the hour, however, the distance to
        the target becomes naturally important because there is less time to travel.
        """
        best = scenarios.get("best")
        up = float(scenarios["UP"]["strength"])
        down = float(scenarios["DOWN"]["strength"])
        rng = float(scenarios["RANGE"]["strength"])
        thesis = path_thesis or {}
        pdir = thesis.get("direction") or (path_analysis or {}).get("thesis_direction")
        pscore = float(thesis.get("score", 0.0))
        pconf = float(thesis.get("confidence", 0.0))
        edge = float(projected) - float(target_level) if projected is not None else 0.0
        u = max(float(uncertainty or 0.0), 10.0)
        atr = max(float(atr5 or 0.0), 1.0)
        target_margin = max(CFG.MIN_TARGET_EDGE_DOLLARS, u * CFG.OUTCOME_MIN_PROJECTED_EDGE_ATR)

        # Path must have a real directional advantage. This prevents a target
        # sitting on the wrong side of a choppy market from creating a call.
        up_gap = up - max(down, rng)
        down_gap = down - max(up, rng)

        # Backward-compatible projection-only fallback for callers/tests that do not
        # provide a path thesis. The live analyzer always supplies one.
        if not thesis:
            threshold = CFG.SCENARIO_MIN_STRENGTH if mins_left > 20 else max(50.0, CFG.SCENARIO_MIN_STRENGTH - 2.0)
            gap = max(up, down, rng) - sorted([up, down, rng])[-2]
            if up >= threshold and up - max(down, rng) >= max(CFG.SCENARIO_MIN_GAP, 1.0) and edge >= target_margin:
                return f"${target_level:,.0f} OVER"
            if down >= threshold and down - max(up, rng) >= max(CFG.SCENARIO_MIN_GAP, 1.0) and edge <= -target_margin:
                return f"${target_level:,.0f} BELOW"
            return "NO CLEAR OUTCOME"

        # Normal hour: projected finish must support the side, but a strong path
        # can call when the target is inside the forecast uncertainty band.
        if pdir == "UP" and pscore >= CFG.PATH_MIN_SCORE and pconf >= CFG.OUTCOME_MIN_PATH_CONFIDENCE:
            if edge >= target_margin or (edge >= -u * CFG.PATH_TARGET_TOLERANCE_ATR and up_gap >= 2.0 and pscore >= CFG.PATH_STRONG_SCORE):
                if up >= CFG.SCENARIO_MIN_STRENGTH - 2 and up_gap >= CFG.OUTCOME_MIN_GAP * 100:
                    return f"${target_level:,.0f} OVER"
        if pdir == "DOWN" and pscore <= -CFG.PATH_MIN_SCORE and pconf >= CFG.OUTCOME_MIN_PATH_CONFIDENCE:
            if edge <= -target_margin or (edge <= u * CFG.PATH_TARGET_TOLERANCE_ATR and down_gap >= 2.0 and pscore <= -CFG.PATH_STRONG_SCORE):
                if down >= CFG.SCENARIO_MIN_STRENGTH - 2 and down_gap >= CFG.OUTCOME_MIN_GAP * 100:
                    return f"${target_level:,.0f} BELOW"

        # Late-hour path rule: when only a few minutes remain, a confirmed path
        # toward the target should not be suppressed just because the projected
        # finish is close to the line. The route, not the current side, decides.
        if mins_left <= CFG.LATE_HOUR_MINUTES and pdir in ("UP", "DOWN") and pconf >= CFG.OUTCOME_MIN_PATH_CONFIDENCE:
            if pdir == "UP" and pscore >= CFG.PATH_MIN_SCORE and up_gap >= 1.5:
                if edge >= -u * 0.35 or (price is not None and float(price) >= float(target_level) - atr * 0.20):
                    return f"${target_level:,.0f} OVER"
            if pdir == "DOWN" and pscore <= -CFG.PATH_MIN_SCORE and down_gap >= 1.5:
                if edge <= u * 0.35 or (price is not None and float(price) <= float(target_level) + atr * 0.20):
                    return f"${target_level:,.0f} BELOW"

        # Very late, very strong directional path: require only that the path
        # itself points through the finish line. This handles the common final
        # 1-5 minute continuation without turning every last-minute move into a call.
        if mins_left <= CFG.VERY_LATE_MINUTES and pconf >= 0.62:
            if pdir == "UP" and pscore >= CFG.PATH_STRONG_SCORE and up > down + 3:
                return f"${target_level:,.0f} OVER"
            if pdir == "DOWN" and pscore <= -CFG.PATH_STRONG_SCORE and down > up + 3:
                return f"${target_level:,.0f} BELOW"

        return "NO CLEAR OUTCOME"

    @staticmethod
    def _target_cross_guard(candidate, target, price, projected, uncertainty, score, candle_delta, flow_delta, mins_left):
        # Kept only for compatibility with older callers/tests. The live engine
        # no longer uses target crossings as a decision or invalidation trigger.
        return candidate, "TARGET CROSS IGNORED: PATH STATE CONTROLS OUTCOME" if candidate else ""

    @staticmethod
    def _candidate_outcome(target_level, decision_bias, confidence, target_edge, uncertainty, mins_left):
        if target_level is None or not math.isfinite(float(target_level)):
            return "NO CLEAR OUTCOME"
        margin = max(8.0, uncertainty * (.16 if mins_left > 20 else .12))
        # As the hour gets close, the price-to-beat matters more than a long-term drift signal.
        bias_gate = .075 if mins_left > 20 else .055
        if target_edge >= margin and decision_bias >= bias_gate and confidence >= CFG.MIN_CONFIDENCE_FOR_OUTCOME:
            return f"${target_level:,.0f} OVER"
        if target_edge <= -margin and decision_bias <= -bias_gate and confidence >= CFG.MIN_CONFIDENCE_FOR_OUTCOME:
            return f"${target_level:,.0f} BELOW"
        return "NO CLEAR OUTCOME"

    @staticmethod
    def _outcome_direction(outcome):
        if not outcome or outcome == "NO CLEAR OUTCOME": return "RANGE"
        if "OVER" in outcome: return "UP"
        if "BELOW" in outcome: return "DOWN"
        return "RANGE"

    def _apply_lock(self, candidate, candidate_dir, confidence, score, projected, price, atr5, mins_left,
                    path_analysis=None, shape=None, st5=None, st15=None, cp5=None, flow=None, m5=None):
        """Lock the developing price PATH, not the current side of the target.

        Once locked, the $100 target can be crossed any number of times without
        changing the outcome. The lock survives normal pullbacks/retests and is
        only invalidated when the structure/path that created it breaks.
        """
        s = self.hour_state; now = time.time()
        path_analysis = path_analysis or {}
        shape = shape or {}; st5 = st5 or {}; st15 = st15 or {}; cp5 = cp5 or {}; flow = flow or {}; m5 = m5 or []
        meta = {
            "locked": bool(s.get("locked")),
            "candidate_count": s.get("candidate_count", 0),
            "invalidated": False,
            "age": max(0, now - s.get("locked_at", now)) if s.get("locked") else 0,
            "locked_outcome": s.get("locked"),
            "reason": "",
            "path_state": "NONE",
            "target_crosses_ignored": True,
        }

        if s.get("locked"):
            contract = s.get("path_contract") or {}
            health = self._path_health(
                contract, price, projected, score, atr5, st5, st15, shape, cp5, flow, m5, mins_left
            )
            if health["broken"]:
                s["invalid_count"] += 1
            else:
                s["invalid_count"] = 0

            meta.update({
                "locked": True, "locked_outcome": s["locked"],
                "locked_target": s.get("locked_target"),
                "path_state": health["state"],
                "path_health": health["health"],
                "path_break": health["broken"],
                "invalid_count": s["invalid_count"],
                "path_contract": contract,
                "reason": health.get("reason", ""),
            })

            # A target cross is explicitly NOT a break. Only persistent path
            # invalidation releases the lock.
            if s["invalid_count"] >= CFG.PATH_BREAK_CONFIRMATIONS:
                old = s["locked"]
                self._clear_lock()
                meta.update({
                    "locked": False, "invalidated": True,
                    "reason": f"invalidated {old}: {health.get('reason','path structure broke')}",
                })
                return "NO CLEAR OUTCOME", meta

            return s["locked"], meta

        if candidate == "NO CLEAR OUTCOME":
            # Confirmation must be consecutive in time: a missing/weak candidate
            # breaks the sequence instead of allowing an old count to carry over.
            s["candidate"] = None
            s["candidate_dir"] = None
            s["candidate_count"] = 0
            s["candidate_last_confirmation"] = 0.0
            s["candidate_started_at"] = 0.0
            meta["candidate_count"] = 0
            return candidate, meta

        if candidate == s.get("candidate") and candidate_dir == s.get("candidate_dir"):
            last_confirm = float(s.get("candidate_last_confirmation", 0.0) or 0.0)
            if now - last_confirm >= CFG.LOCK_CONFIRMATION_SECONDS:
                s["candidate_count"] += 1
                s["candidate_last_confirmation"] = now
        else:
            s["candidate"] = candidate
            s["candidate_dir"] = candidate_dir
            s["candidate_count"] = 1
            s["candidate_started_at"] = now
            s["candidate_last_confirmation"] = now
        meta["candidate_count"] = s["candidate_count"]
        meta["candidate_age_seconds"] = max(0.0, now - float(s.get("candidate_started_at", now) or now))
        meta["next_confirmation_in_seconds"] = max(0.0, CFG.LOCK_CONFIRMATION_SECONDS - (now - float(s.get("candidate_last_confirmation", now) or now)))

        needed = CFG.HARD_LOCK_CONFIRMATIONS if confidence >= CFG.HARD_CALL_CONFIDENCE else CFG.LOCK_CONFIRMATIONS
        if s["candidate_count"] >= needed and confidence >= CFG.MIN_CONFIDENCE_FOR_OUTCOME and candidate_dir in ("UP", "DOWN"):
            contract = self._make_path_contract(
                candidate_dir, candidate, path_analysis, shape, st5, st15, cp5, flow, m5,
                price, projected, atr5, mins_left
            )
            s["locked"] = candidate
            s["locked_dir"] = candidate_dir
            s["locked_target"] = self._parse_target(candidate)
            s["locked_at"] = now
            s["invalid_count"] = 0
            s["path_contract"] = contract
            meta.update({
                "locked": True, "locked_outcome": candidate,
                "locked_target": s["locked_target"], "reason": "PATH CONFIRMED + LOCKED",
                "path_state": contract.get("route", "PATH"), "path_contract": contract,
            })
        return candidate, meta

    def _make_path_contract(self, direction, outcome, path, shape, st5, st15, cp5, flow, m5, price, projected, atr5, mins_left):
        lows5 = [float(x.get("low", x.get("close", price))) for x in m5[-12:]] if m5 else [price]
        highs5 = [float(x.get("high", x.get("close", price))) for x in m5[-12:]] if m5 else [price]
        recent_low = min(lows5); recent_high = max(highs5)
        if direction == "UP":
            base = min(recent_low, float(path.get("invalidation", price - atr5)))
            invalidation = base - atr5 * 0.10
        else:
            base = max(recent_high, float(path.get("invalidation", price + atr5)))
            invalidation = base + atr5 * 0.10
        return {
            "direction": direction,
            "outcome": outcome,
            "created_at": time.time(),
            "anchor_price": float(price),
            "projected_at_lock": float(projected),
            "atr_at_lock": float(atr5),
            "route": path.get("primary", "PATH"),
            "pattern": shape.get("name", "NONE"),
            "pattern_stage": shape.get("stage", "NONE"),
            "pattern_confidence": float(shape.get("confidence", 0.0)),
            "next_point": float(path.get("next_point", price)),
            "continuation_point": float(path.get("continuation_point", price)),
            "invalidation": float(invalidation),
            "structure_at_lock": st5.get("label", "UNKNOWN"),
            "structure15_at_lock": st15.get("label", "UNKNOWN"),
            "candle_pressure_at_lock": float(cp5.get("delta", 0.0)),
            "flow_at_lock": float(flow.get("aggressor_delta", 0.0)),
            "target_is_checkpoint_only": True,
        }

    @staticmethod
    def _path_health(contract, price, projected, score, atr5, st5, st15, shape, cp5, flow, m5, mins_left):
        direction = contract.get("direction")
        inv = float(contract.get("invalidation", price))
        atr_now = max(float(atr5), 1.0)
        structure_bad = False
        if direction == "UP":
            structure_bad = bool(st5.get("LH") and st5.get("LL")) or bool(st15.get("LH") and st15.get("LL"))
            price_break = price < inv
            momentum_break = score <= -CFG.PATH_REVERSAL_SCORE
            flow_break = float(flow.get("aggressor_delta", 0.0)) < -0.45 and float(cp5.get("delta", 0.0)) < -0.30
            broken = price_break and (structure_bad or momentum_break or flow_break)
            if not broken and structure_bad and momentum_break and flow_break:
                broken = True
            state = "PATH INTACT" if not broken else "PATH BROKEN"
            if price > inv + atr_now * .35 and projected >= price - atr_now * .10 and not structure_bad:
                state = "PATH ADVANCING"
            reason = "UP path intact; target crossings ignored" if not broken else "UP path invalidation: structural break + bearish confirmation"
        else:
            structure_bad = bool(st5.get("HH") and st5.get("HL")) or bool(st15.get("HH") and st15.get("HL"))
            price_break = price > inv
            momentum_break = score >= CFG.PATH_REVERSAL_SCORE
            flow_break = float(flow.get("aggressor_delta", 0.0)) > 0.45 and float(cp5.get("delta", 0.0)) > 0.30
            broken = price_break and (structure_bad or momentum_break or flow_break)
            if not broken and structure_bad and momentum_break and flow_break:
                broken = True
            state = "PATH INTACT" if not broken else "PATH BROKEN"
            if price < inv - atr_now * .35 and projected <= price + atr_now * .10 and not structure_bad:
                state = "PATH ADVANCING"
            reason = "DOWN path intact; target crossings ignored" if not broken else "DOWN path invalidation: structural break + bullish confirmation"
        return {"broken": bool(broken), "state": state, "health": 0.0 if broken else 1.0, "reason": reason}

    def _clear_lock(self):
        s = self.hour_state
        s["candidate"] = None; s["candidate_dir"] = None; s["candidate_count"] = 0
        s["candidate_last_confirmation"] = 0.0; s["candidate_started_at"] = 0.0
        s["locked"] = None; s["locked_dir"] = None; s["locked_target"] = None
        s["locked_at"] = 0.0; s["invalid_count"] = 0; s["path_contract"] = None

    @staticmethod
    def _parse_target(outcome):
        if not outcome or outcome == "NO CLEAR OUTCOME": return None
        try: return float(outcome.split()[0].replace("$", "").replace(",", ""))
        except Exception: return None

    # ---------------- levels / patterns ----------------
    @staticmethod
    def _levels(bars, price, atr5):
        if not bars: return price - atr5, price + atr5, price - atr5, price + atr5
        levels = local_levels(bars, min(len(bars), 140))
        below = sorted([x for x in levels if x < price], reverse=True)
        above = sorted([x for x in levels if x > price])
        support = max(below[0] if below else float(np.min(lows(bars[-60:]))), price - atr5 * 2.0)
        resistance = min(above[0] if above else float(np.max(highs(bars[-60:]))), price + atr5 * 2.0)
        near_support = below[0] if below else support
        near_resistance = above[0] if above else resistance
        return float(support), float(resistance), float(near_support), float(near_resistance)

    @staticmethod
    def _breakout_signal(price, support, resistance, m1, m5, hour_high, hour_low, atr5):
        if not m1 or not m5: return 0.0, "breakout state unavailable"
        c1 = closes(m1); h5 = highs(m5); l5 = lows(m5)
        prev_res = max(h5[-12:-1]) if len(h5) >= 12 else resistance
        prev_sup = min(l5[-12:-1]) if len(l5) >= 12 else support
        f = candle_features(m1[-1])
        if price > prev_res + atr5 * .08 and c1[-1] > c1[-2] and f["close_pos"] > .60:
            return .34, "confirmed upside breakout"
        if price < prev_sup - atr5 * .08 and c1[-1] < c1[-2] and f["close_pos"] < .40:
            return -.34, "confirmed downside breakdown"
        if hour_high > resistance and price < resistance and f["upper_wick_pct"] > .45:
            return -.28, "failed upside break / rejection"
        if hour_low < support and price > support and f["lower_wick_pct"] > .45:
            return .28, "failed downside break / reclaim"
        return 0.0, "no confirmed breakout"

    @staticmethod
    def _current_repeat_vote(bars, length=8, search=120):
        c = closes(bars)
        if len(c) < length * 2 + 5:
            return {"direction": "NONE", "strength": 0.0, "matches": 0, "detail": []}
        current = normalized_pattern(c, length); current_start = len(c) - length - 1
        first = max(length + 2, current_start - search); matches = []
        for end in range(first, current_start - 2):
            past = normalized_pattern(c[:end + 1], length); sim = sequence_similarity(current, past)
            if sim < CFG.MIN_PATTERN_SIMILARITY: continue
            before = c[end]; after = c[min(end + CFG.PATTERN_FOLLOW_BARS, len(c) - 1)]
            move = float(after - before)
            d = "UP" if move > 0 else "DOWN" if move < 0 else "FLAT"
            matches.append((sim, d, move))
        matches.sort(reverse=True, key=lambda x: x[0]); top = matches[:CFG.LIVE_PATTERN_NEIGHBORS]
        up = sum(s for s, d, _ in top if d == "UP"); down = sum(s for s, d, _ in top if d == "DOWN"); total = up + down
        if total <= 0: return {"direction": "NONE", "strength": 0.0, "matches": len(top), "detail": []}
        bias = (up - down) / total
        direction = "UP" if bias > CFG.PATTERN_BIAS_THRESHOLD else "DOWN" if bias < -CFG.PATTERN_BIAS_THRESHOLD else "MIXED"
        return {"direction": direction, "strength": min(1.0, abs(bias)), "matches": len(top),
                "detail": [{"similarity": round(s, 3), "direction": d, "move": round(m, 2)} for s, d, m in top[:6]]}

    @staticmethod
    def _timeframe(bars, price):
        c = closes(bars); e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
        rr, aa, ad = rsi(c, 14), atr(bars, 14), adx(bars, 14); st = structure(bars, min(12, max(5, len(bars)//8)))
        vals = []
        if math.isfinite(e9) and math.isfinite(e21): vals.append(Analyzer._clip((e9-e21)/max(aa*.25,1),-1,1))
        if math.isfinite(e50): vals.append(Analyzer._clip((price-e50)/max(aa*.70,1),-1,1))
        if math.isfinite(rr): vals.append(Analyzer._clip((rr-50)/18,-1,1))
        if st.get("HH") and st.get("HL"): vals.append(1.0)
        elif st.get("LH") and st.get("LL"): vals.append(-1.0)
        else: vals.append(0.0)
        if len(c) >= 4 and math.isfinite(aa): vals.append(Analyzer._clip((c[-1]-c[-4])/max(aa*1.2,1),-1,1))
        score = float(np.mean(vals))*2 if vals else 0.0
        if math.isfinite(ad) and ad < CFG.LOW_ADX_THRESHOLD: score *= .55
        d = "UP" if score > .58 else "DOWN" if score < -.58 else "RANGE"
        return {"score": score, "direction": d, "rsi": rr, "adx": ad, "ema9": e9, "ema21": e21, "ema50": e50, "structure": st.get("label","UNKNOWN")}

    @staticmethod
    def _book_metrics(snap, price):
        l2 = snap.get("l2", {}) or {}
        levels = l2.get("levels", {}) if isinstance(l2, dict) else {}
        bids = snap.get("bids", {}) or levels.get("bids", {}) or {}
        asks = snap.get("asks", {}) or levels.get("asks", {}) or {}
        age = float(snap.get("book_age") if snap.get("book_age") is not None else l2.get("age_seconds") or 999.0)
        valid = bool(snap.get("book_valid") if "book_valid" in snap else l2.get("usable")) and age <= CFG.MAX_BOOK_AGE_SECONDS
        if not valid or not bids or not asks:
            return {"usable": False, "imbalance": 0.0, "spread_bps": 0.0, "age": age}
        bp = sorted(((float(p), float(q)) for p,q in bids.items()), key=lambda x:x[0], reverse=True)[:CFG.ORDER_BOOK_LEVELS]
        ap = sorted(((float(p), float(q)) for p,q in asks.items()), key=lambda x:x[0])[:CFG.ORDER_BOOK_LEVELS]
        if not bp or not ap: return {"usable": False, "imbalance": 0.0, "spread_bps": 0.0, "age": age}
        bq, aq = sum(q for _,q in bp), sum(q for _,q in ap); total = bq+aq
        best_bid, best_ask = bp[0][0], ap[0][0]
        return {"usable": True, "imbalance": (bq-aq)/total if total else 0.0,
                "bid_qty": bq, "ask_qty": aq, "spread_bps": (best_ask-best_bid)/price*10000 if price else 0.0,
                "best_bid": best_bid, "best_ask": best_ask, "age": age}

    @staticmethod
    def _legacy_feature_vector(price, open_, high, low, atr5, r5, r15, ad, flow, book,
                               st5, st15, imp1, imp5, mom1, mom5, trend):
        def n(v):
            try: return 0.0 if v is None or not math.isfinite(float(v)) else float(v)
            except Exception: return 0.0
        den = max(atr5, 1.0)
        return [n((price-open_)/den), n((high-price)/den), n((price-low)/den),
                n(imp1/den), n(imp5/den), n(mom1), n(mom5), n((r5-50)/25),
                n((r15-50)/25), n((ad-20)/30), n(flow), n(book), n(trend),
                1.0 if st5.get("HH") else 0.0, 1.0 if st5.get("HL") else 0.0,
                1.0 if st5.get("LH") else 0.0, 1.0 if st5.get("LL") else 0.0,
                1.0 if st15.get("HH") else 0.0, 1.0 if st15.get("HL") else 0.0,
                1.0 if st15.get("LH") else 0.0, 1.0 if st15.get("LL") else 0.0]

    @staticmethod
    def _feature_vector(price, open_, high, low, atr5, r5, r15, ad, flow, book,
                        st5, st15, imp1, imp5, mom1, mom5, trend, cp5, cp15, cp1, shape=None):
        def n(v):
            try: return 0.0 if v is None or not math.isfinite(float(v)) else float(v)
            except Exception: return 0.0
        den = max(atr5, 1.0)
        return [n((price-open_)/den), n((high-price)/den), n((price-low)/den),
                n(imp1/den), n(imp5/den), n(mom1), n(mom5), n((r5-50)/25),
                n((r15-50)/25), n((ad-20)/30), n(flow), n(book), n(trend),
                n(cp1.get("delta")), n(cp5.get("delta")), n(cp15.get("delta")),
                n(cp5.get("last_strength")), n(cp5.get("exhaustion")),
                n(cp5.get("bull_streak"))/8.0, n(cp5.get("bear_streak"))/8.0,
                n(cp5.get("volume_confirmation")),
                n((shape or {}).get("confidence")),
                n(1.0 if (shape or {}).get("direction") == "UP" else -1.0 if (shape or {}).get("direction") == "DOWN" else 0.0),
                n(1.0 if (shape or {}).get("stage") in ("BREAKOUT","BREAKDOWN") else 0.0),
                n(1.0 if (shape or {}).get("pullback_expected") else 0.0),
                1.0 if st5.get("HH") else 0.0, 1.0 if st5.get("HL") else 0.0,
                1.0 if st5.get("LH") else 0.0, 1.0 if st5.get("LL") else 0.0,
                1.0 if st15.get("HH") else 0.0, 1.0 if st15.get("HL") else 0.0,
                1.0 if st15.get("LH") else 0.0, 1.0 if st15.get("LL") else 0.0]

    @staticmethod
    def _path_analysis(shape, price, atr5, cp5, flow, target_level, support, resistance,
                       near_support, near_resistance, projected, mins_left, path_direction=None):
        """Build a route to the projected finish. The target is only a checkpoint.

        A named chart pattern is helpful but NOT required. If no named shape is
        detected, the live directional path still gets a route.
        """
        name = shape.get("name", "NONE"); stage = shape.get("stage", "NONE"); direction = shape.get("direction", "NONE")
        if direction not in ("UP", "DOWN") and path_direction in ("UP", "DOWN"):
            direction = path_direction
        pressure = float(cp5.get("delta", 0.0)); flowd = float(flow.get("aggressor_delta", 0.0))
        if direction == "UP":
            next_point = float(near_resistance if near_resistance > price else price + max(atr5 * .45, 25.0))
            cont = float(max(next_point + atr5 * .45, projected))
            pull = max(atr5 * .18, abs(pressure) * atr5 * .22)
            if flowd > .25: pull *= .75
            invalidation = float(near_support if near_support < price else price - atr5 * .60)
            route = "BOUNCE -> RESISTANCE TEST -> RETEST -> UP CONTINUATION"
            if stage in ("BREAKOUT", "NECKLINE"): route = "BREAKOUT -> RETEST -> UP CONTINUATION"
        elif direction == "DOWN":
            next_point = float(near_support if near_support < price else price - max(atr5 * .45, 25.0))
            cont = float(min(next_point - atr5 * .45, projected))
            pull = max(atr5 * .18, abs(pressure) * atr5 * .22)
            if flowd < -.25: pull *= .75
            invalidation = float(near_resistance if near_resistance > price else price + atr5 * .60)
            route = "DROP -> SUPPORT TEST -> BOUNCE/RETEST -> DOWN CONTINUATION"
            if stage in ("BREAKDOWN", "NECKLINE"): route = "BREAKDOWN -> RETEST -> DOWN CONTINUATION"
        else:
            next_point = float(price + (atr5 * .40 if projected >= price else -atr5 * .40))
            cont = float(projected)
            pull = max(atr5 * .12, 0.0)
            invalidation = float(price - atr5 * .75 if projected >= price else price + atr5 * .75)
            route = "RANGE -> WAIT FOR BREAK -> REPRICE PATH"
        return {
            "primary": route, "direction": direction, "pullback": float(pull), "continuation": float(max(0.0, abs(cont - price))),
            "pattern": name, "stage": stage, "next_point": next_point, "continuation_point": cont,
            "invalidation": invalidation, "projected_finish": float(projected),
            "target_checkpoint": float(target_level), "support": float(support), "resistance": float(resistance),
            "mins_left": float(mins_left),
        }

    @staticmethod
    def _finish_range(price, projected, atr5, mins_left, direction, confidence, support, resistance):
        t = math.sqrt(max(mins_left, 1.0) / 60.0)
        half = max(atr5 * (.25 + .18*t), abs(projected-price)*.45, CFG.MIN_RANGE_DOLLARS)
        half = min(half, atr5 * CFG.MAX_RANGE_ATR_MULT)
        if direction in ("UP","DOWN") and confidence >= .72: half *= .92
        low = min(price-half, projected-atr5*.12); high = max(price+half, projected+atr5*.12)
        low = max(low, price-atr5*CFG.MAX_RANGE_ATR_MULT); high = min(high, price+atr5*CFG.MAX_RANGE_ATR_MULT)
        step = CFG.RANGE_STEP
        low = math.floor(low/step)*step; high = math.ceil(high/step)*step
        if high <= low: high = low + step
        return float(low), float(high)

    @staticmethod
    def format_report(r):
        if r.get("status") != "OK": return f"STATUS: {r.get('status')} | LIVE BTC={r.get('price')}"
        flow, book, lock = r.get("trade_flow",{}), r.get("book",{}), r.get("lock",{})
        why = "; ".join(r.get("reasons",[])[:9])
        l2 = f"{book.get('imbalance',0):+.2f}" if book.get("usable") else "UNAVAILABLE"
        return (
            "\n========== BTC HOURLY CALLOUT =========="
            f"\nWHAT'S GOING ON: BTC ${r['price']:,.2f} | {r['minutes_left']:.1f} min left | score {r['score']:+.2f} | confidence {r['confidence']*100:.1f}%"
            f"\nPATTERN: {r['pattern']}"
            f"\nTREND: {r['direction']} | candidate={r['candidate_direction']} | strength={r['trend_strength']:.2f}"
            f"\nRANGE: ${r['range_low']:,.0f} - ${r['range_high']:,.0f}"
            f"\nTARGET CHECKPOINT: ${r['target_level']:,.0f} | projection distance {r['target_distance']:+.0f}"
            f"\nPROJECTED FINISH: ${r['projected_finish']:,.0f} | projection={r['projected_direction']}"
            f"\nCALLOUT PHASE: {r.get('callout_state', 'UNKNOWN')} | candidate={r.get('candidate_outcome', 'NO CLEAR OUTCOME')} | preferred window=30-10 min remaining"
            f"\nOUTCOME: {r['outcome']} | {'LOCKED' if lock.get('locked') else 'DEVELOPING'} | outcome_conf={r.get('outcome_confidence', r['confidence'])*100:.1f}% | bias={r.get('decision_bias', 0):+.2f}"
            f"\nPATH: {r.get('path_analysis',{}).get('primary','NONE')} | next=${r.get('path_analysis',{}).get('next_point',r['price']):,.0f} | continuation=${r.get('path_analysis',{}).get('continuation_point',r['projected_finish']):,.0f} | lock_path={lock.get('path_state','NONE')} | TARGET CROSSINGS IGNORED"
            f"\nSCENARIOS: {r.get('scenarios',{}).get('UP',{}).get('strength',0):.1f}% UP | {r.get('scenarios',{}).get('DOWN',{}).get('strength',0):.1f}% DOWN | {r.get('scenarios',{}).get('RANGE',{}).get('strength',0):.1f}% RANGE | BEST={r.get('best_scenario','RANGE')}"
            f"\nWHY: {why}"
            f"\nLEVELS: support ${r['support']:,.0f} | resistance ${r['resistance']:,.0f} | hour H/L ${r['hour_high']:,.0f}/${r['hour_low']:,.0f}"
            f"\nINDICATORS: EMA9 ${r['ema9']:,.0f} | EMA21 ${r['ema21']:,.0f} | RSI5 {r['rsi']:.1f} | RSI15 {r['rsi15']:.1f} | ADX {r['adx']:.1f} | ATR5 ${r['atr5']:,.0f} | RVOL {r['relative_volume']:.2f}x"
            f"\nFLOW: {flow.get('aggressor_delta',0):+.2f} | BUY {flow.get('buy_ratio',0):.0%} | SELL {flow.get('sell_ratio',0):.0%} | L2: {l2} | spread {book.get('spread_bps',0):.1f}bps | book age {book.get('age',999):.1f}s"
            f"\nCANDLE PRESSURE: 1m {r.get('candle_pressure_1m',{}).get('delta',0):+.2f} | 5m {r.get('candle_pressure_5m',{}).get('delta',0):+.2f} | 15m {r.get('candle_pressure_15m',{}).get('delta',0):+.2f} | 5m buy/sell candles {r.get('candle_pressure_5m',{}).get('buy_candles',0)}/{r.get('candle_pressure_5m',{}).get('sell_candles',0)}"
            f"\nREVERSAL: {r.get('reversal_reclaim',{}).get('text','NONE')} {r.get('reversal_reclaim',{}).get('score',0):.2f} | target_reclaim_telemetry={r.get('target_reclaim_telemetry',{}).get('reclaimed_target',False)} (not directional evidence)"
            f"\nREPEATING PATTERN: live {r['pattern_live'].get('direction')} ({r['pattern_live'].get('matches',0)}) | memory {r['pattern_memory'].get('direction')} ({r['pattern_memory'].get('matches',0)})"
            "\n========================================\n"
        )
