# KOSPI/KOSDAQ 배당수익률 ÷ 미국 10년물 스크리너

개인용 배당 스크리닝 사이트. 회원가입·로그인·결제 없음. 실제 KRX 주가·DART 배당·FRED 미국 10년물
데이터를 계산 엔진으로 처리해 정적 사이트(JSON)로 만들고 Cloudflare Pages에 올린다.

- 계산 엔진·규칙 검증: `pipeline/engine.py`, `tests/test_engine.py` (16개 테스트, 요구사항 28의 ①~⑦ 상황 커버)
- 데이터 수집: `pipeline/{fred,krx,dart}.py`
- 계산 결과 생성: `pipeline/build.py` → `web/public/data/*.json`
- 웹: `web/` (React + TypeScript + Recharts)

## 아직 결정이 필요한 것 1가지

중간배당 기업에서 "사업보고서 제출 직후 ~ 다음 중간배당 확정 전" 구간에 요구사항의 규칙 ①과 ③이
서로 충돌한다 (자세한 설명은 `pipeline/engine.py`의 `expected_dps_asof` 주석 참고). 기본값은
`EXPECTED_DPS_MODE=annual`(규칙 ③, 방금 확정된 연도의 연간 DPS 사용)이다. 규칙 ①을 글자 그대로
쓰려면 `final_only`로 바꾼다. GitHub 저장소 Settings → Secrets and variables → Actions →
Variables 탭에서 `EXPECTED_DPS_MODE`를 설정하면 자동 업데이트에 반영된다.

---

## 1. 로컬에서 실행하기

```bash
git clone <이 저장소 URL>
cd kr-div-us10y
cp .env.example .env        # 아래 "4. API 키 설정"을 채운다
pip install -r requirements.txt

# 계산 엔진 테스트 (가상 데이터만 사용, API 키 불필요)
pytest tests/test_engine.py -q

# 최초 데이터 구축 (아래 "5. 데이터 최초 구축" 참고, 시간이 걸림)
python -m pipeline.update all

cd web
npm install
npm run dev                 # http://localhost:5173
```

`npm run dev`는 `web/public/data`의 JSON을 그대로 읽으므로, 파이프라인을 먼저 한 번 돌려야 화면에
데이터가 보인다.

## 2. GitHub에 올리기

```bash
git init                    # 이미 git 저장소면 생략
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<사용자명>/<저장소명>.git
git push -u origin main
```

`.env`는 `.gitignore`에 있어 올라가지 않는다. API 키는 대신 3단계에서 GitHub Secrets로 등록한다.
`data/` 아래 원천 데이터(csv·parquet)는 커밋 대상이다 — 이게 있어야 Actions가 매일 "새 데이터만"
받을 수 있다. `web/public/data`(계산 결과 JSON)는 커밋하지 않는다. 배포할 때마다 새로 만든다.

## 3. GitHub Actions + Cloudflare 배포 설정

1. **Cloudflare 토큰·계정 ID 준비** (2026년부터 Pages 대신 "Workers + 정적 자산"으로 배포하므로
   프로젝트를 미리 만드는 단계 자체가 없다 — 첫 배포 시 Worker가 자동으로 생성된다.)
   - Cloudflare 대시보드 → 오른쪽 위 프로필 → "My Profile" → "API Tokens" → "Create Token" →
     "Edit Cloudflare Workers" 템플릿으로 토큰 발급.
   - 대시보드 오른쪽 사이드바 또는 URL에서 계정 ID(Account ID)를 확인한다.
   - Worker 이름을 직접 정하고 싶으면 3단계의 `CLOUDFLARE_WORKER_NAME` 변수에 적는다(선택,
     비워두면 저장소 이름으로 자동 설정됨).

2. **GitHub 저장소 → Settings → Secrets and variables → Actions**
   - **Secrets** 탭에 추가:
     | 이름 | 값 |
     |---|---|
     | `DART_API_KEY` | OpenDART 인증키 |
     | `KRX_ID` | KRX Data Marketplace 계정 ID |
     | `KRX_PW` | KRX Data Marketplace 계정 비밀번호 |
     | `FRED_API_KEY` | (선택) FRED API 키 |
     | `CLOUDFLARE_API_TOKEN` | 위에서 만든 Cloudflare 토큰 |
     | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 계정 ID |
   - **Variables** 탭에 추가:
     | 이름 | 값 |
     |---|---|
     | `CLOUDFLARE_WORKER_NAME` | (선택) Worker 이름. 생략하면 저장소 이름으로 자동 설정 |
     | `EXPECTED_DPS_MODE` | `annual` (또는 `final_only`) — 생략하면 `annual` |

3. **워크플로 켜기**
   - `.github/workflows/update-data.yml`: 평일 KST 오후 8시 자동 실행. 데이터를 받아 `data/`에
     커밋하고, 끝나면 `deploy.yml`을 호출해 사이트를 다시 배포한다.
   - `.github/workflows/deploy.yml`: `web/` 코드가 바뀌어 push되면 단독으로도 실행되고,
     `update-data`가 끝난 뒤에도 실행된다. 저장된 `data/`로 계산 결과를 새로 만들고 Cloudflare
     Pages에 올린다.
   - 처음에는 Actions 탭에서 `Update data`를 수동으로 실행한다(아래 5번).

## 4. API 키 설정

- **OpenDART**: <https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do> 에서 가입 후
  "인증키 신청" (즉시 발급, 40자리). 하루 호출 한도 약 20,000건 — `.env`의 `DART_DAILY_BUDGET`으로
  더 낮게 잡을 수 있다.
- **KRX Data Marketplace**: <https://data.krx.co.kr> 에서 회원가입(2025-12-27부터 필수, 데이터
  조회 자체는 무료). 비밀번호를 90일마다 바꿔야 하며, 바꾼 뒤에는 3단계의 `KRX_PW` Secret도
  반드시 갱신해야 한다 — 갱신을 잊으면 `주가 (KRX)` 상태가 "오래됨"으로 표시된다.
- **FRED**: 선택 사항. 키 없이도 FRED 공개 CSV로 동작한다. 키가 있으면
  <https://fred.stlouisfed.org/docs/api/api_key.html> 에서 발급해 `FRED_API_KEY`에 넣으면
  마지막 저장일 이후 데이터만 정확히 요청한다.

## 5. 데이터 최초 구축

로컬 또는 GitHub Actions("Update data" 워크플로를 "Run workflow"로 수동 실행)에서:

```bash
python -m pipeline.update all
```

10년치 KRX 주가는 거래일마다 KOSPI·KOSDAQ 각 1회씩 요청하므로(약 2,500거래일 × 2 ≈ 5,000회,
요청 사이 0.6초 지연) 처음 한 번은 1시간 가까이 걸린다. DART 배당은 종목 수와 하루 호출 한도
때문에 여러 날에 걸쳐 이어받는다 — 실행할 때마다 어디까지 받았는지 `data/dividends/*.csv`에
남고, 다시 실행하면 이어서 받는다. GitHub Actions로 돌릴 경우 워크플로 제한 시간(현재 350분)
안에서 여러 번의 스케줄 실행에 걸쳐 자연스럽게 완성된다. 급하면 Actions 탭에서 여러 번 수동
실행(Run workflow)해 앞당길 수 있다.

진행 중에 다음 명령으로 단계를 나눠 실행할 수도 있다.

```bash
python -m pipeline.update us10y
python -m pipeline.update prices --start 2016-01-01 --max-days 500   # 이어서 여러 번
python -m pipeline.update stocks
python -m pipeline.update dividends
python -m pipeline.update build     # 계산만 다시 (매번 안전하게 다시 실행 가능)
```

## 6. 매일 데이터 업데이트

- 평일 KST 오후 8시(장 마감 후) `update-data.yml`이 자동 실행되어 그날 주가·배당·미국 10년물
  중 새로 나온 것만 받고, `data/`에 커밋한 뒤 사이트를 다시 배포한다.
- 즉시 갱신하고 싶으면 사이트 하단 "데이터 상태" 섹션의 "데이터 업데이트 실행" 버튼(GitHub
  Actions 실행 화면으로 이동) 또는 저장소 Actions 탭 → `Update data` → `Run workflow`.
- 연말에는 지난해 일별 주가 CSV가 자동으로 `data/prices/{연도}.parquet` 하나로 합쳐져 저장소
  용량을 관리한다 (`python -m pipeline.update compact --year 2025`로 수동 실행도 가능).

## 7. 데이터가 업데이트되지 않을 때 확인할 곳

1. 사이트 하단 **"데이터 상태"** 표 — 어떤 데이터(주가/배당/미국10Y/종목정보)가 오래됐는지,
   마지막 시도가 언제 실패했는지 오류 메시지와 함께 보여준다.
2. GitHub 저장소 **Actions 탭** → `Update data` 워크플로의 최근 실행 로그. 각 단계(`us10y`,
   `prices`, `stocks`, `dividends`, `build`)가 성공/실패했는지 한 줄씩 출력된다.
3. 흔한 원인
   - KRX 비밀번호 90일 만료 → `KRX_PW` Secret 갱신 (본문 4번 참고)
   - DART 하루 호출 한도 초과 → 다음날 자동으로 이어받음 (정상, 로그에 "예산 사용" 표시)
   - Cloudflare 배포 실패 → `CLOUDFLARE_API_TOKEN` 권한(Workers 편집 권한 필요) 또는 `CLOUDFLARE_ACCOUNT_ID` 확인
4. 어떤 원인이든 **기존에 저장된 데이터로 사이트 자체는 계속 정상 작동한다** — 새 데이터를
   못 받았을 뿐 사이트가 멈추지는 않는다.

---

## 데이터 사용 규칙 (요약)

사이트 하단 "데이터 상태" → "계산 규칙과 데이터 사용 기준"에도 동일한 내용이 있다.

- 현재 예상 DPS는 예측이 아니라 **기준일까지 확정 공시된 배당만** 사용한다.
- 확정일은 이사회 배당결정 공시가 아니라 **정기보고서(DART) 접수일**이다 — look-ahead bias 방지.
- 역사적 배수·백분위 계산도 각 거래일에 **그날 알 수 있었던 배당 정보만** 사용한다(동일 함수).
- 미국 10년물이 0% 이하이거나, 데이터가 없는 항목은 배수를 계산하지 않고 **N/A**로 표시한다(0 아님).
- 분할·병합·무상증자는 KRX 기준가 역산으로 탐지해 과거 DPS·주가를 현재 주식 수 기준으로 환산한다.
  자동 탐지가 놓치거나 잘못 잡은 이벤트는 `data/corporate_actions_override.csv`
  (`stock_code,date,ratio`, ratio=1이면 무시)로 수동 보정한다.

## 알려진 한계 (요구사항 31)

- KRX 회원제 전환(2025-12-27) 이후 비로그인 접근이 막혀 계정이 필수이며, 90일 비밀번호 만료
  주기를 사람이 챙겨야 한다.
- OpenDART 하루 호출 한도(약 20,000건) 때문에 전 종목 10년 배당을 한 번에 받지 못하고 여러 날에
  걸쳐 받는다.
- 분기보고서의 배당 값이 누적인지 해당 분기분인지는 기업마다 표기가 다를 수 있어, 코드가 자동으로
  감지해 `interim_values_treated_as_per_period` 플래그를 남긴다 — 실제 데이터를 받은 뒤
  `tests/test_real_companies.py`로 삼성전자 등 실제 기업과 대조해 확인해야 한다.
- 권리락 자동 탐지는 분할·병합·무상증자와 유상증자·인적분할을 구분하지 못한다. 탐지 결과는
  `data/corporate_actions.csv`에 남으므로 사람이 검토 후 필요하면 override 파일로 고친다.
- 우선주는 제외한다(DART 고유번호가 없는 종목 코드는 배당 계산에서 빠진다).
