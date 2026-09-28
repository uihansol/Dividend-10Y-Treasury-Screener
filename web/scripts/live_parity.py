# web/live.js(Worker 잠정 계산)와 pipeline 엔진의 일치 검증용 픽스처 생성. 사용: python web/scripts/live_parity.py /tmp/parity.json && node web/scripts/live-parity.mjs /tmp/parity.json
import json, sys, random
from datetime import date, timedelta
import pandas as pd
sys.path.insert(0, ".")
from pipeline import cache
from pipeline.master import load_master
master = load_master()
us_dates, us_vals = cache.load_us10y()
cases = []
codes = sorted(p.name for p in cache.C.CACHE_DIR.iterdir() if (p / "prices.csv").exists() and (p / "dividends.json").exists())
random.seed(1)
for code in codes:
    if code not in master: continue
    prices = cache.load_prices(code)
    div = cache.load_dividends(code)
    if len(prices) < 300 or not div["reports"]: continue
    for k in (1, 3, 0):          # k=0: 같은 기준일의 장중 값(종가 -1.3%)을 KRX 종가로 교체하는 경우
        full = prices.copy()
        if k == 0:
            trunc = prices.copy(); trunc.loc[trunc.index[-1], "close"] = round(trunc["close"].iloc[-1] * 0.987)
        else:
            trunc = prices.iloc[:-k]
        T = date.fromisoformat(full["date"].iloc[-1])
        for val_a in ("as_of", "T"):
            asof_a = date.fromisoformat(trunc["date"].iloc[-1])
            try:
                A = cache.compute_analysis(master[code], trunc, div, us_dates, us_vals, valuation_date=asof_a if val_a == "as_of" else T)
                B = cache.compute_analysis(master[code], full, div, us_dates, us_vals, valuation_date=T)
            except Exception as e:
                continue
            if A["summary"]["dps"] is None: continue
            win = full[full["date"] >= (asof_a - timedelta(days=14)).isoformat()]
            rows = [{"date": r.date, "close": float(r.close), "change_pct": float(r.change_pct), "trading_value": float(r.trading_value) if pd.notna(r.trading_value) else 0.0} for r in win.itertuples()]
            A["valuation_date"] = (asof_a if val_a == "as_of" else T).isoformat()
            cases.append({"code": code, "k": k, "val_a": val_a, "today": T.isoformat(), "A": A, "B": B, "rows": rows})
json.dump(cases, open(sys.argv[1], "w"))
print(len(cases), "cases from", len({c['code'] for c in cases}), "stocks")
