# 배당수익률 ÷ 미국 10년물 — 종목 검색형

개인용. 종목을 검색하면 **그 종목만** 가격(KRX)·배당(DART)을 모아 계산하고, 종목별 캐시에 저장한다.
최근 90일 가격을 먼저 받아 바로 보여 주고, 2015년까지의 과거 가격은 뒤에서 1년씩 보완한다(배당은 2015 사업연도부터).
다시 조회하면 캐시를 쓰고 새로 나온 데이터만 받는다. 전체 KOSPI/KOSDAQ을 미리 수집하지 않는다.

## 구조

```
브라우저 (React, Cloudflare Worker 정적 자산)
  │ 검색: data/master.json (정적 파일, 네트워크 요청 없음)
  │ 종목 선택
  ▼
Cloudflare Worker  /api/*   (web/worker.js)
  ├ GET  /api/index               조회한 종목 목록  ← 저장소 data/cache/index.json   (Workers Cache 30초)
  ├ GET  /api/stock/{code}        분석 결과 + stale 여부 ← data/cache/stocks/{code}/analysis.json   (Workers Cache 20초, ?fresh= 는 우회)
  ├ GET  /api/stock/{code}/meta   last_attempt·updated_at만 ← metadata.json (새로고침 폴링용, 캐시 안 함)
  ├ GET  /api/stock/{code}/live   KRX를 Worker가 직접 조회해 저장 분석을 '잠정' 갱신 (Workers Cache 15초, 아래 참고)
  ├ GET  /api/krx/status          KRX 로그인 상태 (비밀번호 만료 알림용)
  ├ POST /api/stock/{code}/refresh repository_dispatch(stock_refresh) → 'Stock refresh' 실행 (같은 종목 실행 중이면 생략)
  │   body {"force": true} 이면 캐시가 최신이어도 다시 수집·계산 (화면의 "새로고침" 버튼이 씀)
  └ GET  /api/stock/{code}/run    최근 실행 상태 + 진행 단계(①~④)
  ▼
GitHub Actions  stock-refresh.yml  run-name "refresh {code}"  (Python: pykrx + OpenDART + FRED → pipeline/engine.py)
  ├ ① 가격 데이터 확인   stock CODE --stage prices --quick  (KRX, 그 종목만. 최초 조회는 최근 90일만)
  ├ ② 배당 데이터 확인   stock CODE --stage dividends   (DART, 그 기업만)
  ├ ③ 미국 10년물 확인   stock CODE --stage us10y       (오늘 성공 기록 있으면 생략)
  ├ ④ 분석 계산          stock CODE --stage compute     (engine.py, 수집 실패 시 기존 캐시)
  ├ Fast commit          최신 결과를 먼저 커밋 → Worker가 다음 요청 때 읽음
  └ Progressive backfill backfill CODE + compute 를 1년씩 반복(최대 12회), 새 행이 있는 회차만 커밋.
                         2015년 첫 거래일에 닿거나(done) 새 행이 없으면 멈춘다
```

### 새로고침: KRX 잠정 반영 → Actions 확정 계산

새로고침(버튼·stale·장중 10분 주기)을 누르면 화면은 두 가지를 동시에 요청한다.

1. `GET /live` — Worker가 KRX에 로그인해(`web/krx.js`, pykrx와 같은 방식) 저장 기준일 이후 일별 시세를 받고,
   저장된 analysis.json을 `web/live.js`로 **잠정** 갱신한다(보통 1초 안팎, Worker 인스턴스가 새로 뜨면 로그인·ISIN 조회로 약 5초).
   가격에만 의존하는 값(주가·배당수익률·배당/10Y·역사적 백분위·차트 마지막 구간)을 engine.py와 같은 공식으로 계산한다.
   화면에는 "잠정" 배지를 붙인다. 장중 45초 확인도 Actions 없이 이 경로로 가격만 갱신한다.
2. `POST /refresh` — 지금처럼 Actions가 가격·배당·미국10Y를 수집하고 engine.py로 **확정** 계산해 커밋한다.
   확정 결과가 도착하면 잠정 값을 대체한다(잠정 값은 저장하지 않는다).

잠정 계산은 가정이 깨지면 하지 않는다(`{"status":"unavailable","reason":...}`): 저장 이후 권리락(분할·병합·무상증자) 의심
`corp_action`, 저장 종가와 KRX 불일치 `mismatch`, 저장 이후 배당 확정일 경과 `dps_changed`, 연도 변경 `year_changed`,
KRX 로그인·응답 문제 `login_failed`·`password_change`·`krx_unreachable` 등. 이때는 확정 결과만 기다린다.
검증: `python web/scripts/live_parity.py /tmp/p.json && node web/scripts/live-parity.mjs /tmp/p.json` —
캐시된 109종목 654경우에서 주가·수익률·배수·DPS·미국10Y·차트 마지막 점은 엔진과 같고, 저장 시계열이 소수 3자리로
반올림돼 있어 분위수는 ±0.0005, 백분위는 최대 0.4%p 차이가 날 수 있다(확정 결과로 바로 대체됨).
KRX 연결 점검: Actions → **KRX live check** → Run workflow (실제 KRX 로그인·시세, 배포된 /api 응답·캐시 헤더 확인).

Worker 앞단 캐시는 `web/wrangler.toml`의 `[cache] enabled = true`(Workers Cache, Wrangler 4.69.0 이상)가 맡고,
응답의 `Cache-Control`(public, max-age)을 따른다. `*.workers.dev`에서는 Cache API(`caches.default`)가 동작하지 않는다.
GitHub Contents API 조회에는 ETag(If-None-Match)를 붙여, 바뀌지 않았으면 304(rate limit 미차감)로 끝난다.

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
  prices.csv      일별 원주가·등락률 (2015~)
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
- 정기보고서는 배당 결정 후 45~90일 뒤에 나온다. 그 사이엔 '현금·현물배당결정' 수시공시를 먼저 읽어
  아직 정기보고서로 확정 안 된 올해분을 잠정치(PROV)로 반영한다(화면에 "미확정" 표시).
  확정일은 이 공시의 접수일이라 여전히 look-ahead가 아니며, 정기보고서가 나오면 그 값이 우선한다.
- 미국 10년물은 한국 거래일보다 앞선 가장 최근 값. 0% 이하·데이터 없음은 N/A.

## 설정

GitHub → Settings → Secrets and variables → Actions

| Secrets | 쓰는 워크플로 | 용도 |
|---|---|---|
| `DART_API_KEY` | common-data, stock-refresh | OpenDART |
| `KRX_ID`, `KRX_PW` | common-data, stock-refresh, krx-live-check, deploy (Worker 비밀값으로도 올림) | KRX 정보데이터시스템 로그인 (아래 'KRX 로그인' 참고). Worker는 `/live` 잠정 반영에만 쓰고, 없으면 `/live`만 꺼진다 |
| `FRED_API_KEY` | common-data, stock-refresh | 선택. 없으면 FRED CSV → 미 재무부 CSV 순서로 시도 |
| `CLOUDFLARE_API_TOKEN` | deploy | "Edit Cloudflare Workers" 템플릿 토큰. 없으면 빌드만 하고 배포는 건너뜀 |
| `CLOUDFLARE_ACCOUNT_ID` | deploy | Cloudflare 계정 ID |
| `WORKER_GITHUB_TOKEN` | deploy (Worker 비밀값 `GITHUB_TOKEN`으로 올림) | Worker가 쓸 fine-grained 토큰: 이 저장소만, **Contents: Read**, **Actions: Read and write**. 새로고침이 `repository dispatch 실패 403`이면 응답의 `X-Accepted-GitHub-Permissions` 헤더가 요구하는 권한을 추가 |

| Variables | 용도 |
|---|---|
| `CLOUDFLARE_WORKER_NAME` | 선택. 없으면 저장소 이름 |

선택 환경변수 `REFRESH_COOLDOWN_SEC`(기본 600초): 같은 종목 재요청 시 네트워크 조회를 건너뛰는 간격(`updated_at` 기준).
워크플로에서는 설정하지 않아 기본값을 쓴다. 화면의 장중 자동 갱신 요청 간격(10분)은 이 값에 맞춰 두었다.

### KRX 로그인

- KRX 정보데이터시스템은 2025-12-27부터 회원제이고 2026-09부터 비로그인 요청을 거절한다. pykrx는 import 시
  `KRX_ID`/`KRX_PW` 환경변수로 로그인하므로 두 시크릿이 없으면 가격 수집(①, 백필, master)이 실패한다.
- **비밀번호는 90일마다 만료된다.** 만료되면 KRX 사이트에서 비밀번호를 바꾼 뒤 `KRX_PW` 시크릿도 새 값으로 갱신하고,
  Worker에도 반영되도록 **Deploy를 한 번 다시 실행**한다(Actions → Deploy → Run workflow).
- 같은 계정으로 Worker와 Actions가 동시에 로그인해도 기존 세션이 끊기지 않는 것을 확인했다(KRX live check의 중복 로그인 실험).
- **만료 알림**: Worker가 `GET /api/krx/status`로 KRX 로그인 상태를 확인해(결과는 Workers Cache 5~10분),
  만료(`password_expired`, KRX CD010)나 로그인 실패(`login_failed`)면 모든 화면 위에 알림을 띄운다.
  알림에서 ① KRX 비밀번호 변경 ② GitHub 저장소 시크릿 목록(여기서 `KRX_PW` 수정) ③ Deploy 실행 페이지를 바로 열 수 있고, "다시 확인"으로 즉시 재확인한다.
  틀린 비밀번호로 반복 로그인해 계정이 잠기지 않도록, Worker는 실패한 같은 비밀번호로 6시간 동안 다시 로그인하지 않는다
  (시크릿을 새 값으로 바꾸면 바로 다시 시도). 앱이 비밀번호를 직접 저장하지는 않는다.
  증상: 종목 화면 "일부 데이터 업데이트 실패(prices…)", `metadata.json`의 `last_error.prices`에
  "pykrx import/KRX 로그인 실패", Stock refresh 로그의 ① 단계 오류.

### 배포 버전 표시

메인 제목 오른쪽의 `Build #{번호} · {커밋 7자리}`는 Deploy 워크플로가 빌드 때 주입한다
(`VITE_APP_BUILD` = `github.run_number`, `VITE_APP_SHA` = `github.sha`; 화면에는 SHA 앞 7자리, 마우스를 올리면 전체 SHA).
로컬 개발 빌드는 `Build #dev · local`. 날짜 등으로 하드코딩하지 않는다.

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

- 종목 화면 "실행 기록" 링크 또는 Actions → Stock refresh 로그 (run 이름 `refresh {code}`)
- `data/cache/stocks/{code}/metadata.json` 의 `last_error`
- KRX 오류 → `KRX_PW` 만료 확인, DART 020 → 하루 한도 초과(다음 날 자동 해소)
