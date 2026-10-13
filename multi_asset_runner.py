"""Isolated multi-Coinbase-asset analysis runner. Live execution is forcibly disabled."""
import json, logging, os, signal, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO').upper(),format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('multi-crypto-runner'); STOP=False; CHILDREN={}
def shutdown(*_):
    global STOP
    STOP=True
    for p in list(CHILDREN.values()):
        if p.poll() is None: p.terminate()
def load_assets():
    with open(ROOT/'assets.json',encoding='utf-8') as f: cfg=json.load(f)
    enabled=[]
    for product,item in cfg.get('assets',{}).items():
        if item.get('enabled',False):
            if not product.endswith('-USD'): raise ValueError(f'Invalid Coinbase product ID: {product}')
            step=float(item.get('target_step',0))
            if step<=0: raise ValueError(f'{product}: target_step must be positive')
            enabled.append((product,item))
    if not enabled: raise ValueError('No assets enabled in assets.json')
    return enabled
def start_asset(product,item):
    env=os.environ.copy(); safe=product.replace('-','_')
    env.update({'PRODUCT_ID':product,'TARGET_STEP':str(item['target_step']),'RANGE_STEP':str(item['target_step']),
      'LEARNER_DB':str(ROOT/'state'/f'learner-{safe}.sqlite3'),'EXECUTION_DB':str(ROOT/'state'/f'execution-{safe}.sqlite3'),
      'STATE_FILE':str(ROOT/'state'/f'state-{safe}.json'),'LIVE_TRADING':'false','EMERGENCY_STOP':'true'})
    log.warning('Starting asset worker | asset=%s | target_step=%s | live trading=FORCED OFF',product,item['target_step'])
    return subprocess.Popen([sys.executable,str(ROOT/'bot.py')],cwd=ROOT,env=env)
def main():
    (ROOT/'state').mkdir(exist_ok=True); assets=load_assets()
    log.warning('MULTI-CRYPTO ANALYSIS RUNNER | assets=%s | mode=ANALYSIS ONLY',','.join(a for a,_ in assets))
    log.warning('No orders can be submitted: inherited Kalshi execution is BTC-series-specific; multi-asset market mapping and shared portfolio controls are not verified.')
    signal.signal(signal.SIGINT,shutdown); signal.signal(signal.SIGTERM,shutdown)
    for product,item in assets: CHILDREN[product]=start_asset(product,item); time.sleep(.5)
    while not STOP:
        for product,item in assets:
            if CHILDREN[product].poll() is not None and not STOP:
                log.error('Worker exited | asset=%s | exit_code=%s | restarting in 3s',product,CHILDREN[product].returncode)
                time.sleep(3); CHILDREN[product]=start_asset(product,item)
        time.sleep(1)
    for p in list(CHILDREN.values()):
        try: p.wait(timeout=10)
        except subprocess.TimeoutExpired: p.kill()
if __name__=='__main__': main()
