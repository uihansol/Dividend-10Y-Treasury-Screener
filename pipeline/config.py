"""경로·상수·데이터 사용 규칙을 한 곳에 모은다.

여기 적힌 규칙 문구(DATA_RULES)는 그대로 웹 화면의 '데이터 설명'에도 노출된다.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRICES_DIR = DATA / "prices"            # 완료된 연도: {YYYY}.parquet
PRICES_DAILY_DIR = PRICES_DIR / "daily" # 진행 중인 연도: {YYYY}/{YYYYMMDD}.csv
DIV_DIR = DATA / "dividends"
STOCKS_CSV = DATA / "stocks.csv"
US10Y_CSV = DATA / "us10y.csv"
CORP_ACTIONS_CSV = DATA / "corporate_actions.csv"
DART_REPORTS_CSV = DIV_DIR / "dart_reports.csv"
DART_FETCH_LOG_CSV = DIV_DIR / "dart_fetch_log.csv"
SOURCES_JSON = DATA / "sources.json"

OUT = ROOT / "web" / "public" / "data"  # 사이트가 읽는 계산 결과(JSON). git에는 올리지 않는다.

PRICE_START = "2016-01-01"   # 주가 수집 시작일
DART_FIRST_YEAR = 2015       # OpenDART 정기보고서 주요정보는 2015 사업연도부터 제공
HISTORY_YEARS = 10           # 역사적 배수/백분위 계산 기간

# 정기보고서 코드 (OpenDART 개발가이드 DS002 '배당에 관한 사항')
REPRT_CODES = {"Q1": "11013", "H1": "11012", "Q3": "11014", "FY": "11011"}
PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}

# 기준가 역산으로 찾은 권리락(분할·병합·무상증자 등) 판정 임계값.
# |전일종가/당일기준가 - 1| 이 이 값보다 크면 주식 수 변동 이벤트로 기록한다.
CORP_ACTION_THRESHOLD = 0.02

# 미국 10년물이 이 값 이하이면 배수를 계산하지 않는다(N/A).
MIN_US10Y_FOR_MULTIPLE = 0.0

# 데이터가 이 달력일수 이상 갱신되지 않으면 화면에 '오래됨'으로 표시
STALE_DAYS = {"prices": 4, "us10y": 5, "dividends": 10, "stocks": 10}

DART_API_KEY = os.environ.get("DART_API_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")  # 선택. 없으면 FRED CSV 다운로드 사용
DART_DAILY_BUDGET = int(os.environ.get("DART_DAILY_BUDGET", "18000"))  # 공식 한도 20,000건/일보다 여유 있게

DATA_RULES = [
    "현재 예상 DPS: 기준일까지 공시가 확인된 배당만 사용한다. 미래 배당을 예측하지 않는다.",
    "예상 DPS: 분기배당 기업은 현재 연도에 확정된 분기분을 사용하고, 미확정 분기는 전년도 같은 분기 배당으로 보완한다. "
    "예: 올해 Q1만 확정되면 올해 Q1 + 전년도 Q2 + 전년도 Q3 + 전년도 기말배당을 사용한다. "
    "중간배당 기업은 확정된 올해 중간배당 + 전년도 기말배당을 사용하며, 사업보고서가 제출되면 확정 연간 DPS로 바뀐다.",
    "배당의 '확정일(confirmed_date)'은 해당 금액이 실린 DART 정기보고서 접수일이다. "
    "이사회 배당결정 공시보다 늦으므로 보수적이며, look-ahead bias가 생기지 않는다.",
    "기말배당 = 사업보고서의 연간 주당배당금 − 같은 연도 3분기(없으면 반기·1분기) 보고서의 누적 주당배당금.",
    "역사적 배당/10Y는 각 거래일 T에 confirmed_date ≤ T 인 배당 정보만 사용한다.",
    "미국 10년물은 FRED DGS10. 한국 거래일 T에는 T보다 앞선 날짜 중 가장 최근 값을 쓴다"
    "(한국 장 마감 시점에 미국 당일 금리는 아직 없다). 결측을 0으로 채우지 않는다.",
    "미국 10년물이 0% 이하인 날은 배수를 계산하지 않는다(N/A).",
    "주가는 KRX 원주가(비수정) 종가. 분할·병합·무상증자는 KRX 기준가와 전일 종가의 차이로 찾아 "
    "과거 DPS를 해당 시점 주식 수 기준으로 환산한다.",
    "연도별 DPS는 사업연도 귀속 기준(중간+분기+기말 합계)이다. 실제 입금 연도와 다를 수 있다.",
    "보통주만 대상으로 한다. 우선주는 DART 보통주 DPS와 달라 제외한다.",
    "데이터가 없는 값은 0이 아니라 N/A로 표시한다.",
]
