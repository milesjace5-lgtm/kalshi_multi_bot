import math
import time
import numpy as np


def _arr(bars, key):
    return np.asarray([float(x[key]) for x in bars], dtype=float)


def closes(bars): return _arr(bars, "close")
def highs(bars): return _arr(bars, "high")
def lows(bars): return _arr(bars, "low")
def opens(bars): return _arr(bars, "open")
def volumes(bars): return np.asarray([float(x.get("volume", 0.0)) for x in bars], dtype=float)


def ema(x, n):
    x = np.asarray(x, dtype=float)
    if len(x) < n: return float("nan")
    a = 2.0 / (n + 1.0)
    out = float(x[0])
    for v in x[1:]: out = a * float(v) + (1 - a) * out
    return float(out)


def ema_series(x, n):
    x = np.asarray(x, dtype=float)
    if len(x) == 0: return np.array([], dtype=float)
    a = 2.0 / (n + 1.0)
    out = np.empty(len(x), dtype=float); out[0] = x[0]
    for i in range(1, len(x)): out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(x, n=14):
    x = np.asarray(x, dtype=float)
    if len(x) < n + 1: return float("nan")
    d = np.diff(x); g = np.maximum(d, 0.0); loss = np.maximum(-d, 0.0)
    ag = float(np.mean(g[:n])); al = float(np.mean(loss[:n]))
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + loss[i]) / n
    if al <= 1e-12: return 100.0
    return float(100.0 - 100.0 / (1.0 + ag / al))


def atr(bars, n=14):
    if len(bars) < n + 1: return float("nan")
    h, l, c = highs(bars), lows(bars), closes(bars)
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(np.mean(tr[-n:]))


def macd(x, fast=12, slow=26, signal=9):
    x = np.asarray(x, dtype=float)
    if len(x) < slow + signal: return (float("nan"),) * 3
    line = ema_series(x, fast) - ema_series(x, slow)
    sig = ema_series(line, signal)
    return float(line[-1]), float(sig[-1]), float(line[-1] - sig[-1])


def bollinger(x, n=20, k=2.0):
    x = np.asarray(x, dtype=float)
    if len(x) < n: return (float("nan"),) * 3
    w = x[-n:]; mid = float(np.mean(w)); sd = float(np.std(w))
    return mid, mid + k * sd, mid - k * sd


def adx(bars, n=14):
    """Wilder-style ADX approximation using smoothed directional movement."""
    if len(bars) < 2 * n + 2: return float("nan")
    h, l, c = highs(bars), lows(bars), closes(bars)
    tr = []; plus = []; minus = []
    for i in range(1, len(bars)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        up = h[i] - h[i - 1]; dn = l[i - 1] - l[i]
        plus.append(up if up > dn and up > 0 else 0.0)
        minus.append(dn if dn > up and dn > 0 else 0.0)
    tr, plus, minus = map(np.asarray, (tr, plus, minus))
    atr_s = float(np.mean(tr[:n])); p_s = float(np.mean(plus[:n])); m_s = float(np.mean(minus[:n]))
    dx = []
    for i in range(n, len(tr)):
        atr_s = (atr_s * (n - 1) + tr[i]) / n
        p_s = (p_s * (n - 1) + plus[i]) / n
        m_s = (m_s * (n - 1) + minus[i]) / n
        if atr_s <= 1e-12:
            dx.append(0.0); continue
        pdi = 100.0 * p_s / atr_s; mdi = 100.0 * m_s / atr_s
        dx.append(100.0 * abs(pdi - mdi) / max(pdi + mdi, 1e-12))
    if len(dx) < n: return float("nan")
    return float(np.mean(dx[-n:]))


def vwap_from_trades(trades, since=None):
    rows = [t for t in trades if since is None or float(t.get("ts", t.get("time", 0))) >= since]
    if not rows: return float("nan")
    vol = sum(max(float(t.get("size", 0)), 0.0) for t in rows)
    if vol <= 0: return float("nan")
    return float(sum(float(t["price"]) * float(t["size"]) for t in rows) / vol)


def trade_flow(trades, seconds=300):
    """Aggressor pressure from Coinbase Advanced market-trades.

    The Advanced Trade market_trades channel's side is the taker/aggressor
    side: BUY is upward pressure and SELL is downward pressure. Timestamp
    aliases are supported because feed snapshots expose both `ts` and `time`.
    """
    empty = {"maker_buy": 0.0, "maker_sell": 0.0, "up_aggressor": 0.0,
             "down_aggressor": 0.0, "aggressor_delta": 0.0, "ratio": 0.0,
             "buy_volume": 0.0, "sell_volume": 0.0, "buy_trades": 0,
             "sell_trades": 0, "buy_ratio": 0.0, "sell_ratio": 0.0,
             "pressure": 0.0, "count": 0}
    if not trades:
        return empty
    now = time.time()
    rows = [t for t in trades if 0 <= now - float(t.get("ts", t.get("time", 0.0))) <= seconds]
    buy = sell = 0.0; buy_n = sell_n = 0
    for t in rows:
        q = max(float(t.get("size", 0.0)), 0.0)
        side = str(t.get("side", "")).lower()
        if side in {"buy", "b", "bid"}:
            buy += q; buy_n += 1
        elif side in {"sell", "s", "ask", "offer"}:
            sell += q; sell_n += 1
    total = buy + sell
    delta = (buy - sell) / total if total else 0.0
    return {"maker_buy": sell, "maker_sell": buy, "up_aggressor": buy,
            "down_aggressor": sell, "aggressor_delta": delta, "ratio": delta,
            "buy_volume": buy, "sell_volume": sell, "buy_trades": buy_n,
            "sell_trades": sell_n, "buy_ratio": buy / total if total else 0.0,
            "sell_ratio": sell / total if total else 0.0,
            "pressure": delta, "count": len(rows)}


def relative_volume(bars, n=20):
    if len(bars) < n + 1: return float("nan")
    v = volumes(bars); base = float(np.mean(v[-n-1:-1]))
    return float(v[-1] / base) if base > 0 else float("nan")


def volatility_ratio(bars, n=20):
    if len(bars) < n + 1: return float("nan")
    ranges = highs(bars) - lows(bars)
    base = float(np.mean(ranges[-n-1:-1]))
    return float(ranges[-1] / base) if base > 0 else float("nan")


def slope(x, n=20):
    x = np.asarray(x, dtype=float)
    if len(x) < n: return float("nan")
    return float(np.polyfit(np.arange(n, dtype=float), x[-n:], 1)[0])


def efficiency_ratio(x, n=20):
    x = np.asarray(x, dtype=float)
    if len(x) < n + 1: return float("nan")
    path = float(np.sum(np.abs(np.diff(x[-n-1:])))); net = abs(float(x[-1] - x[-n-1]))
    return net / path if path > 0 else 0.0


def candle_features(b):
    o, h, l, c = [float(b[k]) for k in ("open", "high", "low", "close")]
    rng = max(h - l, 1e-9); body = abs(c - o)
    upper = h - max(o, c); lower = min(o, c) - l
    return {"open": o, "high": h, "low": l, "close": c,
            "body_pct": body / rng, "upper_wick_pct": upper / rng,
            "lower_wick_pct": lower / rng, "bull": c > o,
            "close_pos": (c - l) / rng, "range": rng,
            "doji": body / rng < 0.12,
            "inside": False}


def structure(bars, lookback=12):
    if len(bars) < lookback * 2 + 2:
        return {"HH": False, "HL": False, "LH": False, "LL": False, "label": "UNKNOWN"}
    h, l = highs(bars), lows(bars)
    old_h = float(np.max(h[-lookback*2:-lookback])); new_h = float(np.max(h[-lookback:]))
    old_l = float(np.min(l[-lookback*2:-lookback])); new_l = float(np.min(l[-lookback:]))
    hh, lh = new_h > old_h, new_h < old_h
    hl, ll = new_l > old_l, new_l < old_l
    if hh and hl: label = "HIGHER HIGHS / HIGHER LOWS"
    elif lh and ll: label = "LOWER HIGHS / LOWER LOWS"
    elif hh and ll: label = "EXPANSION / MIXED"
    elif lh and hl: label = "COMPRESSION"
    else: label = "RANGE"
    return {"HH": hh, "HL": hl, "LH": lh, "LL": ll, "label": label,
            "old_high": old_h, "new_high": new_h, "old_low": old_l, "new_low": new_l}


def local_levels(bars, n=120):
    if len(bars) < 7: return []
    b = bars[-n:]; h, l = highs(b), lows(b); levels = []
    for i in range(2, len(b) - 2):
        if h[i] >= h[i-2] and h[i] >= h[i-1] and h[i] >= h[i+1] and h[i] >= h[i+2]: levels.append(float(h[i]))
        if l[i] <= l[i-2] and l[i] <= l[i-1] and l[i] <= l[i+1] and l[i] <= l[i+2]: levels.append(float(l[i]))
    return levels




def momentum_divergence(bars, n=20):
    """Price-vs-RSI divergence and momentum exhaustion score."""
    if len(bars) < max(10, n):
        return {"bullish": False, "bearish": False, "score": 0.0, "text": "INSUFFICIENT"}
    c = closes(bars[-n:])
    rr = np.array([rsi(closes(bars[:i+1]), 14) for i in range(len(bars)-n, len(bars))], dtype=float)
    rr = rr[np.isfinite(rr)]
    if len(rr) < 6: return {"bullish": False, "bearish": False, "score": 0.0, "text": "NONE"}
    half=max(3,len(c)//2)
    p1,p2=float(np.min(c[:half])),float(np.min(c[half:]))
    h1,h2=float(np.max(c[:half])),float(np.max(c[half:]))
    r1,r2=float(np.nanmin(rr[:len(rr)//2])),float(np.nanmin(rr[len(rr)//2:]))
    rh1,rh2=float(np.nanmax(rr[:len(rr)//2])),float(np.nanmax(rr[len(rr)//2:]))
    bull = p2 < p1 and r2 > r1 + 2.0
    bear = h2 > h1 and rh2 < rh1 - 2.0
    score = 0.0
    if bull: score += .75
    if bear: score -= .75
    return {"bullish": bull, "bearish": bear, "score": score,
            "text": "BULLISH DIVERGENCE" if bull else "BEARISH DIVERGENCE" if bear else "NONE"}



def reversal_reclaim(bars, target=None, atr_value=None, lookback=16):
    """Detect a fresh reversal/reclaim so an old trend does not dominate new price action.

    This is intentionally event-based: it looks for a selloff/flush, a low, a
    recovery, and a reclaim of a nearby level.  It returns strength rather than
    declaring a guaranteed reversal.
    """
    empty = {
        "direction": "NONE", "score": 0.0, "stage": "NONE", "reclaimed_target": False,
        "flush": False, "higher_low": False, "breakout": False, "rejection": False,
        "text": "NONE", "recent_low": float("nan"), "recovery": 0.0,
    }
    if len(bars) < max(10, lookback):
        return empty
    b = bars[-lookback:]
    c = closes(b); h = highs(b); l = lows(b)
    # Use a robust local volatility proxy when ATR is not supplied.
    if atr_value is None or not np.isfinite(float(atr_value)) or float(atr_value) <= 0:
        diffs = np.abs(np.diff(c[-min(12, len(c)):]))
        atr_value = float(np.mean(diffs)) if len(diffs) else 1.0
    atr_value = max(float(atr_value), 1.0)

    split = max(4, len(c) // 2)
    old = c[:split]
    recent = c[split:]
    old_slope = float(np.polyfit(np.arange(len(old)), old, 1)[0]) if len(old) >= 3 else 0.0
    new_slope = float(np.polyfit(np.arange(len(recent)), recent, 1)[0]) if len(recent) >= 3 else 0.0
    recent_low = float(np.min(l[-max(5, len(l)//2):]))
    low_idx = int(np.argmin(l[-max(8, len(l)//2):]))
    last = float(c[-1])
    prev = float(c[-2])
    window_high = float(np.max(h[:-3]))
    window_low = float(np.min(l[:-3]))
    recovery = (last - recent_low) / atr_value
    flush = old_slope < -0.10 * atr_value and (window_low - recent_low) >= -0.25 * atr_value
    rising = new_slope > 0.10 * atr_value
    last_three_up = c[-1] > c[-2] and c[-2] >= c[-3]
    reclaim_level = window_high
    breakout = last > reclaim_level - 0.10 * atr_value and recovery > 0.80
    higher_low = False
    if len(l) >= 10:
        left_low = float(np.min(l[:len(l)//2]))
        right_low = float(np.min(l[len(l)//2:]))
        higher_low = right_low > left_low - 0.15 * atr_value
    reclaimed_target = bool(target is not None and last > float(target) and np.min(c[-12:]) < float(target) + 0.15 * atr_value)
    # A failed breakdown is especially useful: price dipped through the level,
    # then recovered and held it instead of continuing lower.
    failed_break = bool(target is not None and np.min(l[-6:]) < float(target) - 0.05 * atr_value and last > float(target) + 0.12 * atr_value)
    rejection = bool(float(l[-1]) < recent_low + 0.25 * atr_value and last > float(l[-1]) + 0.55 * atr_value)

    score = 0.0
    if flush: score += 0.22
    if rising: score += 0.20
    if last_three_up: score += 0.14
    if recovery >= 0.75: score += min(0.18, recovery * 0.09)
    if higher_low: score += 0.10
    if breakout: score += 0.12
    if rejection: score += 0.08
    if reclaimed_target: score += 0.12
    if failed_break: score += 0.16
    if old_slope < -0.20 * atr_value and new_slope > 0.15 * atr_value: score += 0.08
    score = float(max(0.0, min(1.0, score)))

    if score < 0.34:
        return empty
    stage = "RECLAIM" if reclaimed_target or failed_break else "REVERSAL" if rising and recovery >= 0.75 else "FORMING"
    return {
        "direction": "UP", "score": score, "stage": stage,
        "reclaimed_target": reclaimed_target, "flush": flush,
        "higher_low": higher_low, "breakout": breakout, "rejection": rejection,
        "text": f"BULLISH {stage}", "recent_low": recent_low,
        "recovery": float(recovery), "old_slope": float(old_slope), "new_slope": float(new_slope),
    }

def continuation_patterns(bars, lookback=60):
    """Lightweight geometry detector for flags, pennants, triangles and wedges."""
    empty={"name":"NONE","direction":"NONE","confidence":0.0,"stage":"NONE","breakout":False,"failure_level":float("nan")}
    if len(bars)<24: return empty
    b=bars[-min(lookback,len(bars)):]; c=closes(b); h=highs(b); l=lows(b)
    n=len(c); cut=max(10,n//2)
    old=np.arange(cut,dtype=float); new=np.arange(n-cut,dtype=float)
    def sl(x): return float(np.polyfit(np.arange(len(x),dtype=float),x,1)[0]) if len(x)>2 else 0.0
    s_old=sl(c[:cut]); s_new=sl(c[cut:]);
    rh=float(np.mean(h[-10:])-np.mean(l[-10:]))
    early_range=max(float(np.max(h[:cut])-np.min(l[:cut])),1.0)
    late_range=max(float(np.max(h[cut:])-np.min(l[cut:])),1.0)
    compression=max(0.0,min(1.0,1.0-late_range/early_range))
    # Flag: strong directional impulse followed by countertrend shallow channel.
    impulse=(c[cut-1]-c[0])
    pull=(c[-1]-c[cut])
    if abs(impulse)>early_range*.28 and abs(pull)<abs(impulse)*.55 and compression<.55:
        d="UP" if impulse>0 else "DOWN"
        counter_ok=(pull<=0 if d=="UP" else pull>=0)
        if counter_ok:
            conf=min(.90,.58+.22*min(1,abs(impulse)/max(early_range,1))+.12*max(0,1-compression))
            return {"name":"BULL FLAG" if d=="UP" else "BEAR FLAG","direction":d,"confidence":conf,
                    "stage":"FORMING","breakout":(c[-1]>max(h[-8:-1]) if d=="UP" else c[-1]<min(l[-8:-1])),
                    "failure_level":float(np.min(l[-10:]) if d=="UP" else np.max(h[-10:]))}
    # Triangle/pennant: shrinking range with converging slopes.
    hs=sl(h[-12:]); ls=sl(l[-12:])
    if compression>.18 and hs<0 and ls>0:
        return {"name":"SYMMETRICAL TRIANGLE / PENNANT","direction":"RANGE","confidence":min(.90,.55+.35*compression),"stage":"FORMING","breakout":False,"failure_level":float(c[-1])}
    if compression>.18 and hs<0 and ls>=-abs(hs)*.25:
        return {"name":"DESCENDING TRIANGLE","direction":"DOWN","confidence":min(.88,.56+.28*compression),"stage":"FORMING","breakout":False,"failure_level":float(np.max(h[-8:]))}
    if compression>.18 and ls>0 and hs<=abs(ls)*.25:
        return {"name":"ASCENDING TRIANGLE","direction":"UP","confidence":min(.88,.56+.28*compression),"stage":"FORMING","breakout":False,"failure_level":float(np.min(l[-8:]))}
    # Wedges: both boundaries slope together.
    if hs>0 and ls>0 and abs(hs)>abs(ls)*1.15:
        return {"name":"RISING WEDGE","direction":"DOWN","confidence":min(.86,.54+.20*compression),"stage":"FORMING","breakout":False,"failure_level":float(np.min(l[-8:]))}
    if hs<0 and ls<0 and abs(ls)>abs(hs)*1.15:
        return {"name":"FALLING WEDGE","direction":"UP","confidence":min(.86,.54+.20*compression),"stage":"FORMING","breakout":False,"failure_level":float(np.max(h[-8:]))}
    return empty


def flow_acceleration(trades, seconds=300):
    """Compare recent aggressor pressure with the earlier half of the window."""
    empty = {"delta": 0.0, "acceleration": 0.0, "recent_delta": 0.0, "older_delta": 0.0}
    if not trades:
        return empty
    now = time.time()
    stamp = lambda item: float(item.get("ts", item.get("time", 0.0)))
    rows = [item for item in trades if 0 <= now - stamp(item) <= seconds]
    if len(rows) < 8:
        return empty
    rows = sorted(rows, key=stamp)
    mid = len(rows) // 2

    def delta_for(group):
        up = down = 0.0
        for item in group:
            qty = max(float(item.get("size", 0.0)), 0.0)
            side = str(item.get("side", "")).lower()
            if side in {"buy", "b", "bid"}:
                up += qty
            elif side in {"sell", "s", "ask", "offer"}:
                down += qty
        total = up + down
        return (up - down) / total if total else 0.0

    older = delta_for(rows[:mid])
    recent = delta_for(rows[mid:])
    return {
        "delta": recent,
        "acceleration": max(-1.0, min(1.0, recent - older)),
        "recent_delta": recent,
        "older_delta": older,
    }

def chart_patterns(bars, lookback=80):
    """Detect larger chart shapes and their current stage.

    This is deliberately geometry based: pivots, neckline, symmetry, and
    breakout/retest state. It is not a claim that a named pattern guarantees
    the next move.
    """
    empty={"name":"NONE","direction":"NONE","confidence":0.0,"stage":"NONE",
           "neckline":float("nan"),"target":float("nan"),"pullback_expected":False,
           "continuation_after_pullback":False,"failure_level":float("nan"),"age_bars":0}
    if len(bars)<20: return empty
    b=bars[-lookback:]; h=highs(b); l=lows(b); c=closes(b)
    n=len(b); w=max(2, min(4,n//18)); piv=[]
    for i in range(w,n-w):
        if h[i]>=max(h[i-w:i+w+1]): piv.append((i,float(h[i]),"H"))
        if l[i]<=min(l[i-w:i+w+1]): piv.append((i,float(l[i]),"L"))
    piv=sorted(piv,key=lambda x:x[0])
    # Keep alternating pivots; nearby duplicate extrema are reduced to the
    # more important one so shapes are not triggered by noise.
    clean=[]
    for x in piv:
        if clean and clean[-1][2]==x[2]:
            if x[2]=="H" and x[1]>clean[-1][1]: clean[-1]=x
            elif x[2]=="L" and x[1]<clean[-1][1]: clean[-1]=x
        else: clean.append(x)
    def near(a,b,tol): return abs(a-b)<=tol*max(abs(a),abs(b),1.0)
    atrv=float(np.mean(np.abs(np.diff(c[-min(20,n):])))) if n>2 else 1.0
    tol=max(atrv*1.8, (max(h)-min(l))*0.025, 1.0)
    best=None
    # Double bottom/top (W/M).
    for seq in [clean[-7:]]:
        if len(seq)>=3:
            for i in range(len(seq)-2):
                a,m,z=seq[i:i+3]
                if a[2]=="L" and m[2]=="H" and z[2]=="L":
                    sym=1-abs(a[1]-z[1])/max(tol,1.0)
                    if sym>=0.35 and m[1]-max(a[1],z[1])>tol*0.55:
                        neck=m[1]; conf=min(0.94,0.55+0.22*max(0,sym)+0.12*min(1,(m[1]-max(a[1],z[1]))/(tol*2)))
                        stage="BREAKOUT" if c[-1]>neck+tol*.10 else "NECKLINE" if c[-1]>neck-tol*.35 else "SECOND_LOW"
                        if stage=="BREAKOUT": conf+=.06
                        cand={"name":"W / DOUBLE BOTTOM","direction":"UP","confidence":conf,"stage":stage,
                              "neckline":neck,"target":neck+(neck-min(a[1],z[1])),"pullback_expected":stage=="BREAKOUT",
                              "continuation_after_pullback":stage=="BREAKOUT","failure_level":min(a[1],z[1])-tol*.25,"age_bars":n-z[0]}
                        if best is None or cand["confidence"]>best["confidence"]: best=cand
                if a[2]=="H" and m[2]=="L" and z[2]=="H":
                    sym=1-abs(a[1]-z[1])/max(tol,1.0)
                    if sym>=0.35 and min(a[1],z[1])-m[1]>tol*0.55:
                        neck=m[1]; conf=min(0.94,0.55+0.22*max(0,sym)+0.12*min(1,(min(a[1],z[1])-m[1])/(tol*2)))
                        stage="BREAKDOWN" if c[-1]<neck-tol*.10 else "NECKLINE" if c[-1]<neck+tol*.35 else "SECOND_HIGH"
                        if stage=="BREAKDOWN": conf+=.06
                        cand={"name":"M / DOUBLE TOP","direction":"DOWN","confidence":conf,"stage":stage,
                              "neckline":neck,"target":neck-(max(a[1],z[1])-neck),"pullback_expected":stage=="BREAKDOWN",
                              "continuation_after_pullback":stage=="BREAKDOWN","failure_level":max(a[1],z[1])+tol*.25,"age_bars":n-z[0]}
                        if best is None or cand["confidence"]>best["confidence"]: best=cand
    # Head and shoulders / inverse head and shoulders.
    if len(clean)>=5:
        for i in range(len(clean)-4):
            a,bp,head,dp,e=clean[i:i+5]
            if [x[2] for x in (a,bp,head,dp,e)]==["H","L","H","L","H"]:
                shoulders=near(a[1],e[1],tol/max(abs(a[1]),abs(e[1]),1)) and head[1]>max(a[1],e[1])+tol*.5
                if shoulders:
                    neck=(bp[1]+dp[1])/2; conf=min(.96,.63+.12*max(0,1-abs(a[1]-e[1])/max(tol,1)))
                    stage="BREAKDOWN" if c[-1]<neck-tol*.10 else "NECKLINE" if c[-1]<neck+tol*.30 else "RIGHT_SHOULDER"
                    cand={"name":"HEAD & SHOULDERS","direction":"DOWN","confidence":conf,"stage":stage,"neckline":neck,
                          "target":neck-(head[1]-neck),"pullback_expected":stage=="BREAKDOWN","continuation_after_pullback":stage=="BREAKDOWN",
                          "failure_level":head[1]+tol*.15,"age_bars":n-e[0]}
                    if best is None or cand["confidence"]>best["confidence"]: best=cand
            if [x[2] for x in (a,bp,head,dp,e)]==["L","H","L","H","L"]:
                shoulders=near(a[1],e[1],tol/max(abs(a[1]),abs(e[1]),1)) and head[1]<min(a[1],e[1])-tol*.5
                if shoulders:
                    neck=(bp[1]+dp[1])/2; conf=min(.96,.63+.12*max(0,1-abs(a[1]-e[1])/max(tol,1)))
                    stage="BREAKOUT" if c[-1]>neck+tol*.10 else "NECKLINE" if c[-1]>neck-tol*.30 else "RIGHT_SHOULDER"
                    cand={"name":"INVERSE HEAD & SHOULDERS","direction":"UP","confidence":conf,"stage":stage,"neckline":neck,
                          "target":neck+(neck-head[1]),"pullback_expected":stage=="BREAKOUT","continuation_after_pullback":stage=="BREAKOUT",
                          "failure_level":head[1]-tol*.15,"age_bars":n-e[0]}
                    if best is None or cand["confidence"]>best["confidence"]: best=cand
    if best is not None:
        return best
    cont=continuation_patterns(bars, lookback)
    return cont if cont.get("name") != "NONE" else empty

def pattern_name(bars):
    if len(bars) < 8: return "INSUFFICIENT DATA"
    fs = [candle_features(x) for x in bars[-8:]]; c = closes(bars[-8:]); h = highs(bars[-8:]); l = lows(bars[-8:])
    names = []; f = fs[-1]; p = fs[-2]
    if f["bull"] and f["body_pct"] > .65 and f["close_pos"] > .80: names.append("BULLISH IMPULSE")
    if not f["bull"] and f["body_pct"] > .65 and f["close_pos"] < .20: names.append("BEARISH IMPULSE")
    if f["lower_wick_pct"] > .55 and f["close_pos"] > .55: names.append("LOWER-WICK REJECTION")
    if f["upper_wick_pct"] > .55 and f["close_pos"] < .45: names.append("UPPER-WICK REJECTION")
    if p["bull"] and not f["bull"] and f["open"] >= p["close"] and f["close"] <= p["open"]: names.append("BEARISH ENGULFING")
    if not p["bull"] and f["bull"] and f["open"] <= p["close"] and f["close"] >= p["open"]: names.append("BULLISH ENGULFING")
    if f["high"] < p["high"] and f["low"] > p["low"]: names.append("INSIDE BAR")
    if f["high"] > p["high"] and f["low"] < p["low"]: names.append("OUTSIDE BAR")
    if c[-3] < c[-4] and c[-2] > c[-3] and c[-1] > c[-2]: names.append("TWO-BAR RECOVERY")
    if c[-3] > c[-4] and c[-2] < c[-3] and c[-1] < c[-2]: names.append("TWO-BAR SELLING")
    if max(h[-4:]) - min(l[-4:]) < max(max(h[-12:]) - min(l[-12:]), 1e-9) * .45: names.append("COMPRESSION")
    return " + ".join(names) if names else "PRICE ACTION / CONSOLIDATION"


def normalized_pattern(x, length=8):
    x = np.asarray(x, dtype=float)
    if len(x) < length + 1: return None
    r = np.diff(x[-length-1:]); scale = float(np.std(r))
    if scale < 1e-9: return np.zeros(length, dtype=float)
    return r / scale


def sequence_similarity(a, b):
    if a is None or b is None or len(a) != len(b): return 0.0
    d = float(np.linalg.norm(np.asarray(a) - np.asarray(b)) / max(len(a), 1))
    return float(1.0 / (1.0 + d))


def candle_pressure(bars, n=12):
    """Measure how strongly recent candles are buying or selling.

    Uses body strength, close location, wick rejection and volume. This is not
    a prediction by itself; it measures the pressure that is actually visible
    in the candles.
    """
    if not bars:
        return {
            "bull_strength": 0.0, "bear_strength": 0.0, "delta": 0.0,
            "buy_volume": 0.0, "sell_volume": 0.0, "buy_candles": 0,
            "sell_candles": 0, "bull_streak": 0, "bear_streak": 0,
            "last_strength": 0.0, "last_direction": "NONE", "exhaustion": 0.0,
            "volume_confirmation": 0.0, "count": 0,
        }
    rows = bars[-max(1, n):]
    vols = volumes(rows)
    base_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    bull = bear = buy_vol = sell_vol = 0.0
    buy_candles = sell_candles = 0
    last_strength = 0.0
    last_dir = "NONE"
    bull_streak = bear_streak = 0
    max_bull = max_bear = 0
    weighted_parts = []
    for i, b in enumerate(rows):
        f = candle_features(b)
        rng = max(f["range"], 1e-9)
        body = min(1.0, f["body_pct"])
        close_force = (f["close_pos"] - 0.5) * 2.0
        wick_rejection = (f["lower_wick_pct"] - f["upper_wick_pct"])
        vol = max(float(b.get("volume", 0.0)), 0.0)
        vr = min(2.5, vol / base_vol) if base_vol > 0 else 1.0
        # Body is the main signal. Close location and wick rejection tell us
        # whether the candle actually finished with control.
        signed = 0.62 * ((1.0 if f["bull"] else -1.0) * body) + 0.28 * close_force + 0.10 * wick_rejection
        signed = max(-1.0, min(1.0, signed))
        weight = 0.65 + 0.35 * min(vr, 2.0)
        part = signed * weight
        weighted_parts.append(part)
        if part > 0:
            bull += part; buy_candles += 1; buy_vol += vol * max(part, 0.0)
            bull_streak += 1; bear_streak = 0
        elif part < 0:
            bear += -part; sell_candles += 1; sell_vol += vol * max(-part, 0.0)
            bear_streak += 1; bull_streak = 0
        else:
            bull_streak = bear_streak = 0
        max_bull = max(max_bull, bull_streak); max_bear = max(max_bear, bear_streak)
        if i == len(rows) - 1:
            last_strength = abs(part)
            last_dir = "UP" if part > 0.12 else "DOWN" if part < -0.12 else "NEUTRAL"
    total = bull + bear
    delta = (bull - bear) / total if total > 0 else 0.0
    # A huge wick against the recent direction after a large body is a useful
    # exhaustion warning. It reduces continuation strength without inventing a
    # reversal.
    last = candle_features(rows[-1])
    exhaustion = 0.0
    if delta > 0.25 and last["upper_wick_pct"] > 0.45:
        exhaustion = min(1.0, last["upper_wick_pct"])
    elif delta < -0.25 and last["lower_wick_pct"] > 0.45:
        exhaustion = min(1.0, last["lower_wick_pct"])
    volume_confirmation = 0.0
    if base_vol > 0:
        volume_confirmation = max(-1.0, min(1.0, (float(rows[-1].get("volume", 0.0)) / base_vol - 1.0) / 1.5))
    return {
        "bull_strength": float(bull), "bear_strength": float(bear), "delta": float(delta),
        "buy_volume": float(buy_vol), "sell_volume": float(sell_vol),
        "buy_candles": buy_candles, "sell_candles": sell_candles,
        "bull_streak": max_bull, "bear_streak": max_bear,
        "last_strength": float(last_strength), "last_direction": last_dir,
        "exhaustion": float(exhaustion), "volume_confirmation": float(volume_confirmation),
        "count": len(rows),
    }
