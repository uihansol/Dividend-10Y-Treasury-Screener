# 배당수익률 ÷ 미국 10년물 — 종목 검색형

개인용. 종목을 검색하면 **그 종목만** 2016년부터 가격(KRX)·배당(DART)을 모아 계산하고, 종목별 캐시에 저장한다.
다시 조회하면 캐시를 쓰고 새로 나온 데이터만 받는다. 전체 KOSPI/KOSDAQ을 미리 수집하지 않는다.

## 구조

```
브라우저 (React, Cloudflare Worker 정적 자산)
  │ 검색: data/master.json (정적 파일, 네트워크 요청 없음)
  │ 종목 선택
  ▼
Cloudflare Worker  /api/*   (web/worker.js)
  ├ GET  /api/index               조회한 종목 목록  ← 저장소 data/cache/index.json
  ├ GET  /api/stock/{code}        분석 결과 + stale 여부 ← data/cache/stocks/{code}/analysis.json
  ├ POST /api/stock/{code}/refresh GitHub Actions 'Analyze stock' 실행 (같은 종목 실행 중이면 생략)
  └ GET  /api/stock/{code}/run    최근 실행 상태 + 진행 단계(①~④)
  ▼
GitHub Actions  analyze-stock.yml  (Python: pykrx + OpenDART + FRED → pipeline/engine.py)
  ├ ① 가격 데이터 확인   stock CODE --stage prices      (KRX, 그 종목만)
  ├ ② 배당 데이터 확인   stock CODE --stage dividends   (DART, 그 기업만)
  ├ ③ 미국 10년물 확인   stock CODE --stage us10y       (오늘 성공 기록 있으면 생략)
  ├ ④ 분석 계산          stock CODE --stage compute     (engine.py, 수집 실패 시 기존 캐시)
  └ data/cache/stocks/{code}/ 커밋 → Worker가 다음 요청 때 읽음
```

pykrx(KRX 로그인)와 계산 엔진이 Python이라 Worker 안에서 직접 돌릴 수 없어, 수집·계산은 GitHub Actions가 맡는다.
DART·KRX 비밀값은 GitHub Secrets에만 있고 Worker·브라우저에는 없다. Worker는 이 저장소 전용 토큰만 가진다.

캐시 저장소로 Cloudflare D1/R2 대신 GitHub 저장소를 쓴 이유: 계산이 GitHub Actions에서 일어나므로 결과를 같은 곳에
두면 추가 서비스·토큰이 필요 없고, 조회한 종목만(종목당 파일 4개) 쌓여 파일 수가 작다.

## 데이터

```
data/us10y/dgs10.csv             FRED DGS10 (공통). FRED API(키 있을 때) → FRED CSV → 미 재무부 10Y CMT CSV 순서로 시도
data/stocks/master.json          검색용 종목 목록 (코드·이름·시장·DART 고유번호·별칭)
data/stocks/aliases.json         직접 추가하는 별칭 {"삼전": "005930"}
data/cache/index.json            조회한 종목 요약
data/cache/stocks/{code}/
  metadata.json   price_through, dividend_through, updated_at, last_error, 요약
  prices.csv      일별 원주가·등락률 (2016~)
  dividends.json  DART 보고서별 누적 DPS + 조회 기록
  analysis.json   화면용 계산 결과
data/corporate_actions_override.csv   분할 자동탐지 수동 보정
```

Common data 워크플로가 미국 10Y를 갱신하면 이미 조회한 종목만 네트워크 없이 다시 계산한다(배수·백분위 반영).

## 계산 규칙 (pipeline/engine.py)

- 예상 DPS = 올해 확정된 분기·중간배당 누계 + 전년도의 그 이후 분기분(미확정분 대체) + 전년도 기말배당.
  예) 전년도 Q1·Q2·Q3·기말 300 → 올해 Q1=400 확정: 1,300 / H1 누계 800: 1,400 / Q3까지: 올해 누계+전년 기말.
  사업보고서가 나오면 그 해 실제 연간 DPS.
- 확정일 = DART 정기보고서 접수일. 과거 날짜 계산에는 그날까지 접수된 보고서만 쓴다(look-ahead 방지).
- 미국 10년물은 한국 거래일보다 앞선 가장 최근 값. 0% 이하·데이터 없음은 N/A.

## 설정

GitHub → Settings → Secrets and variables → Actions

| Secrets | 용도 |
|---|---|
| `DART_API_KEY` | OpenDART |
| `KRX_ID`, `KRX_PW` | KRX 정보데이터시스템 로그인 (90일마다 비밀번호 만료) |
| `CLOUDFLARE_API_TOKEN` | "Edit Cloudflare Workers" 템플릿 토큰 |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 계정 ID |
| `WORKER_GITHUB_TOKEN` | Worker가 쓸 fine-grained 토큰: 이 저장소만, **Contents: Read**, **Actions: Read and write** |
| `FRED_API_KEY` | 선택 |

Variables: `CLOUDFLARE_WORKER_NAME`(선택, 없으면 저장소 이름).

## 처음 한 번

1. Actions → **Common data (US10Y + master)** → Run workflow → `init` (미국 10Y + 종목 master만, 수 분)
2. Actions → **Deploy** 가 master 커밋으로 자동 실행 → Cloudflare에 배포 (Cloudflare 시크릿 없으면 빌드만)
3. 사이트에서 종목 검색

## 로컬

```bash
cp .env.example .env && pip install -r requirements.txt
pytest -q                                   # 네트워크 없이 전부 실행
python -m pipeline.update init              # 10Y + master
python -m pipeline.update stock 005930      # 삼성전자만 수집·계산
python -m pipeline.update recompute         # 캐시된 종목만 네트워크 없이 재계산
python -m pipeline.update search 삼성
```

## 문제 확인

- 종목 화면 "실행 기록" 링크 또는 Actions → Analyze stock 로그
- `data/cache/stocks/{code}/metadata.json` 의 `last_error`
- KRX 오류 → `KRX_PW` 만료 확인, DART 020 → 하루 한도 초과(다음 날 자동 해소)
