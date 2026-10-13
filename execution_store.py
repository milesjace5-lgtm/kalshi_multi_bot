"""Durable SQLite order intent and lifecycle store."""
import json, sqlite3, threading, time, os
VALID={"CREATED","SUBMITTING","ACCEPTED","PARTIALLY FILLED","FILLED","CANCEL_REQUESTED","CANCELLED","REJECTED","UNKNOWN_PENDING_RECONCILIATION","SETTLED"}
class ExecutionStore:
    def __init__(self,path):
        self.path=path; self.lock=threading.RLock()
        parent=os.path.dirname(os.path.abspath(path))
        os.makedirs(parent,exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS order_intents(
            client_order_id TEXT PRIMARY KEY, created_ts REAL NOT NULL, hour_ts INTEGER NOT NULL,
            ticker TEXT NOT NULL, side TEXT NOT NULL, price_cents INTEGER NOT NULL, quantity INTEGER NOT NULL,
            status TEXT NOT NULL, exchange_order_id TEXT, payload TEXT NOT NULL, updated_ts REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS fills(
            fill_id TEXT PRIMARY KEY, client_order_id TEXT, ticker TEXT, side TEXT, count INTEGER,
            price_cents INTEGER, fee_dollars REAL, ts REAL, raw_json TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_pnl(day_utc TEXT PRIMARY KEY, realized REAL NOT NULL)""")
    def _db(self): return sqlite3.connect(self.path,timeout=10)
    def create_intent(self, client_id, hour_ts, ticker, side, price_cents, quantity, payload):
        with self.lock,self._db() as db:
            db.execute("INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (client_id,time.time(),hour_ts,ticker,side,price_cents,quantity,"CREATED",None,json.dumps(payload),time.time()))
    def transition(self,client_id,status,exchange_order_id=None):
        if status not in VALID: raise ValueError("invalid order lifecycle state")
        with self.lock,self._db() as db:
            cur=db.execute("UPDATE order_intents SET status=?,exchange_order_id=COALESCE(?,exchange_order_id),updated_ts=? WHERE client_order_id=?",
                           (status,exchange_order_id,time.time(),client_id))
            if not cur.rowcount: raise KeyError(client_id)
    def has_hour_order(self,hour_ts,ticker):
        with self._db() as db:
            return db.execute("SELECT 1 FROM order_intents WHERE hour_ts=? AND ticker=? AND status!='REJECTED' LIMIT 1",(hour_ts,ticker)).fetchone() is not None
    def unresolved(self):
        with self._db() as db:
            rows=db.execute("SELECT client_order_id,status,exchange_order_id,payload FROM order_intents WHERE status IN ('SUBMITTING','ACCEPTED','PARTIALLY FILLED','UNKNOWN_PENDING_RECONCILIATION','CANCEL_REQUESTED')").fetchall()
        return [{"client_order_id":r[0],"status":r[1],"exchange_order_id":r[2],"payload":json.loads(r[3])} for r in rows]
    def open_intents(self):
        with self._db() as db: return db.execute("SELECT client_order_id,ticker,side,price_cents,quantity,status FROM order_intents WHERE status NOT IN ('REJECTED','CANCELLED','SETTLED')").fetchall()
