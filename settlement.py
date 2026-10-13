"""Conservative interpretation of BTC hourly settlement propositions."""
import re
from decimal import Decimal

def interpret_settlement(title, subtitle="", rules="", ticker=""):
    """Return {'predicate': 'above'|'below'|'at_or_above'|'at_or_below', 'strike': Decimal} or None.

    Intentionally rejects ambiguous/unsupported language. Caller must separately verify
    the market is a BTC hourly contract and that close time matches the current UTC hour.
    """
    text=" ".join(str(x or "") for x in (title,subtitle,rules))
    if not re.search(r'\b(bitcoin|btc)\b',text,re.I): return None
    # Strictly support explicit dollar strike and common proposition forms.
    patterns=[
      (r'\b(?:above|greater than|higher than|over)\s*\$?\s*([\d,]+(?:\.\d+)?)\b','above'),
      (r'\b(?:below|less than|lower than|under)\s*\$?\s*([\d,]+(?:\.\d+)?)\b','below'),
      (r'\b(?:at least|at or above)\s*\$?\s*([\d,]+(?:\.\d+)?)\b','at_or_above'),
      (r'\b(?:at most|at or below)\s*\$?\s*([\d,]+(?:\.\d+)?)\b','at_or_below'),
    ]
    found=[]
    for pattern,op in patterns:
        for m in re.finditer(pattern,text,re.I):
            found.append((op,Decimal(m.group(1).replace(",",""))))
    unique=set(found)
    if len(unique)!=1: return None
    op,strike=next(iter(unique))
    return {"predicate":op,"strike":strike}

def choose_side(forecast_price, proposition):
    if not proposition: return None
    p=Decimal(str(forecast_price)); k=proposition["strike"]; op=proposition["predicate"]
    yes={"above":p>k,"below":p<k,"at_or_above":p>=k,"at_or_below":p<=k}[op]
    # Strict above/below ties are not forecastable confidently: don't trade exact strike.
    if p==k and op in ("above","below"): return None
    return "yes" if yes else "no"
