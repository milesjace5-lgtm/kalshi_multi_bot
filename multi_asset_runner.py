"""Multi-asset runner with per-asset worker isolation and portfolio-wide safety gates."""
import json, logging, os, signal, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO").upper(),format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("multi-crypto-runner")
STOP=False
CHILDREN={}
def shutdown(*_):
    global STOP
    STOP=True
    for p in list(CHILDREN.values()):
        if p.poll() is None: p.terminate()
def load_assets():
    data=json.loads((ROOT/"assets.json").read_text())
    out=[]
    for product,item in data.get("assets",{}).items():
        if not item.get("enabled"): continue
        if not product.endswith("-USD"): raise ValueError(f"Invalid product ID: {product}")
        if float(item.get("target_step",0))<=0: raise ValueError(f"{product}: target_step must be positive")
        out.append((product,item))
    if not out: raise ValueError("No assets enabled in assets.json")
    return out
def start_asset(product,item):
    env=os.environ.copy()
    safe=product.replace("-","_")
    series=str(item.get("kalshi_series_ticker","")).strip()
    requested_live=str(item.get("live_enabled",False)).lower() in ("1","true","yes","on")
    global_live=str(os.getenv("LIVE_TRADING","false")).lower() in ("1","true","yes","on")
    # Per-asset series ticker is mandatory for live mode; no inferred mappings.
    live=requested_live and global_live and bool(series)
    env.update({
      "PRODUCT_ID":product,"TARGET_STEP":str(item["target_step"]),"RANGE_STEP":str(item["target_step"]),
      "KALSHI_HOURLY_SERIES_TICKER":series or "UNCONFIGURED",
      "LEARNER_DB":str(ROOT/"state"/f"learner-{safe}.sqlite3"),
      "EXECUTION_DB":str(ROOT/"state"/f"execution-{safe}.sqlite3"),
      "STATE_FILE":str(ROOT/"state"/f"state-{safe}.json"),
      "LIVE_TRADING":"true" if live else "false",
      "EMERGENCY_STOP":"false" if live else "true",
      "PORTFOLIO_RISK_DB":str(ROOT/"state"/"portfolio-risk.sqlite3"),
      "ASSET_ID":product,
    })
    if requested_live and global_live and not series:
        log.error("LIVE MODE BLOCKED | %s has no verified KALSHI series ticker",product)
    log.warning("Starting worker | asset=%s | series=%s | live=%s",product,series or "UNCONFIGURED",live)
    return subprocess.Popen([sys.executable,str(ROOT/"bot.py")],cwd=ROOT,env=env)
def main():
    (ROOT/"state").mkdir(exist_ok=True)
    assets=load_assets()
    log.warning("MULTI-CRYPTO KALSHI BOT | assets=%s | global_live=%s",
        ",".join(a for a,_ in assets),os.getenv("LIVE_TRADING","false"))
    signal.signal(signal.SIGINT,shutdown); signal.signal(signal.SIGTERM,shutdown)
    for product,item in assets:
        CHILDREN[product]=start_asset(product,item); time.sleep(.5)
    while not STOP:
        for product,item in assets:
            if CHILDREN[product].poll() is not None and not STOP:
                log.error("Worker exited | asset=%s | code=%s | restart in 3s",product,CHILDREN[product].returncode)
                time.sleep(3); CHILDREN[product]=start_asset(product,item)
        time.sleep(1)
    for p in list(CHILDREN.values()):
        try:p.wait(timeout=10)
        except subprocess.TimeoutExpired:p.kill()
if __name__=="__main__": main()
