"""SQLite-backed atomic portfolio entry gate.

Call reserve_entry(asset, intended_dollars, available_cash, max_per_hour=3) immediately
before submitting an order. This module is included for portfolio integration. The
inherited execution path does not yet invoke it, so it is not an enforced trade cap.
"""
import sqlite3, time, os
from contextlib import contextmanager
DB=os.getenv("PORTFOLIO_RISK_DB","state/portfolio-risk.sqlite3")
@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB) or ".",exist_ok=True)
    c=sqlite3.connect(DB,timeout=10,isolation_level=None)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, hour INTEGER NOT NULL, asset TEXT NOT NULL, dollars REAL NOT NULL, status TEXT NOT NULL, created REAL NOT NULL)")
        yield c
    finally:c.close()
def reserve_entry(asset, intended_dollars, available_cash, max_per_hour=3, fraction=0.25):
    now=time.time(); hour=int(now//3600); available=max(0.0,float(available_cash))
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows=c.execute("SELECT asset,dollars,status FROM reservations WHERE hour=? AND status IN ('reserved','submitted','filled')",(hour,)).fetchall()
        if len(rows)>=max_per_hour:
            c.execute("ROLLBACK"); return None,"hourly trade limit reached"
        already=sum(float(r[1]) for r in rows)
        remaining=max(0.0,available-already)
        budget=min(float(intended_dollars),remaining*fraction)
        if budget<=0:
            c.execute("ROLLBACK"); return None,"no available budget"
        cur=c.execute("INSERT INTO reservations(hour,asset,dollars,status,created) VALUES(?,?,?,?,?)",(hour,asset,budget,"reserved",now))
        c.execute("COMMIT")
        return {"id":cur.lastrowid,"budget":budget},"reserved"
def mark_status(reservation_id,status):
    if status not in {"submitted","filled","released","failed"}: raise ValueError("invalid status")
    with connect() as c:c.execute("UPDATE reservations SET status=? WHERE id=?",(status,int(reservation_id)))
