"""Atomic cross-worker portfolio gate: max entries/hour and 25% of remaining cash.

The first balance observed in each UTC hour becomes that hour's cash baseline. The
baseline is reduced if exchange-reported free cash plus this hour's active reservations
shows an external cash decrease. Reservations are persisted across workers/redeploys.
"""
import os, sqlite3, time, math
from contextlib import contextmanager
DB=os.getenv("PORTFOLIO_RISK_DB","state/portfolio-risk.sqlite3")
ACTIVE=("reserved","submitted","filled","unknown")
@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB) or ".",exist_ok=True)
    c=sqlite3.connect(DB,timeout=20,isolation_level=None)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=20000")
        c.execute("CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, hour INTEGER NOT NULL, asset TEXT NOT NULL, dollars REAL NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, client_order_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS hourly_cash(hour INTEGER PRIMARY KEY, baseline REAL NOT NULL, updated REAL NOT NULL)")
        cols={r[1] for r in c.execute("PRAGMA table_info(reservations)").fetchall()}
        if "client_order_id" not in cols: c.execute("ALTER TABLE reservations ADD COLUMN client_order_id TEXT")
        yield c
    finally:c.close()
def reserve_entry(asset, intended_dollars, available_cash, max_per_hour=3, fraction=0.25, client_order_id=None, now=None):
    now=time.time() if now is None else float(now)
    hour=int(now//3600); available=float(available_cash); intended=float(intended_dollars); fraction=float(fraction)
    if not all(math.isfinite(x) and x>=0 for x in (available,intended)) or not math.isfinite(fraction):
        return None,"invalid risk values"
    if not 0 < fraction <= 1 or int(max_per_hour)<1: return None,"invalid risk limits"
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows=c.execute("SELECT dollars,status FROM reservations WHERE hour=? AND status IN ('reserved','submitted','filled','unknown')",(hour,)).fetchall()
        if len(rows)>=int(max_per_hour):
            c.execute("ROLLBACK"); return None,"global hourly entry limit reached"
        reserved=sum(max(0.0,float(r[0])) for r in rows)
        row=c.execute("SELECT baseline FROM hourly_cash WHERE hour=?",(hour,)).fetchone()
        # Restore this-hour committed reservations to free cash to estimate the original
        # baseline, then keep the lower baseline if cash fell due to an external event.
        observed_baseline=available+reserved
        if row is None:
            baseline=observed_baseline
            c.execute("INSERT INTO hourly_cash(hour,baseline,updated) VALUES(?,?,?)",(hour,baseline,now))
        else:
            baseline=min(float(row[0]),observed_baseline)
            c.execute("UPDATE hourly_cash SET baseline=?,updated=? WHERE hour=?",(baseline,now,hour))
        remaining=max(0.0,baseline-reserved)
        per_trade_fraction=float(os.getenv("PORTFOLIO_MAX_TRADE_FRACTION","0.25"))
        if not math.isfinite(per_trade_fraction) or not 0 < per_trade_fraction <= 1:
            c.execute("ROLLBACK"); return None,"invalid per-trade risk limit"
        # Hard cap each individual order at 25% (configurable) of the hour cash baseline.
        budget=min(intended,remaining*fraction,baseline*per_trade_fraction)
        if budget<=0:
            c.execute("ROLLBACK"); return None,"no uncommitted cash budget"
        cur=c.execute("INSERT INTO reservations(hour,asset,dollars,status,created,client_order_id) VALUES(?,?,?,?,?,?)",
                      (hour,str(asset),budget,"reserved",now,client_order_id))
        c.execute("COMMIT")
        return {"id":cur.lastrowid,"budget":budget,"hour":hour,"remaining_before":remaining},"reserved"
def mark_status(reservation_id,status,client_order_id=None):
    allowed={"submitted","filled","unknown","released","failed"}
    if status not in allowed: raise ValueError("invalid reservation status")
    with connect() as c:
        if client_order_id:
            c.execute("UPDATE reservations SET status=?,client_order_id=? WHERE id=?",(status,str(client_order_id),int(reservation_id)))
        else:
            c.execute("UPDATE reservations SET status=? WHERE id=?",(status,int(reservation_id)))
def release_expired_reservations(now=None, older_than_seconds=86400):
    """Only release stale reservations never submitted; never auto-release live orders."""
    now=time.time() if now is None else float(now)
    with connect() as c:
        cur=c.execute("UPDATE reservations SET status='released' WHERE status='reserved' AND created<?",(now-float(older_than_seconds),))
        return cur.rowcount
