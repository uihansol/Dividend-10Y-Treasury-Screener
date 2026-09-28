/**
 * KRX 정보데이터시스템(data.krx.co.kr) 직접 조회 — Worker에서 최신 시세를 바로 받기 위한 최소 구현.
 * pykrx 1.2.9의 로그인·조회 방식(pykrx/website/comm/auth.py, krx/market/core.py)을 그대로 따른다.
 *
 *  로그인: GET MDCCOMS001.cmd → GET login.jsp → POST MDCCOMS001D1.cmd (CD001 정상, CD011 중복 → skipDup=Y 재전송)
 *  종목 ISIN: getJsonData.cmd bld=dbms/comm/finder/finder_stkisu (block1[].short_code / full_code)
 *  일별 시세: getJsonData.cmd bld=dbms/MDC/STAT/standard/MDCSTAT01701, adjStkPrc=1(단순종가 = pipeline의 adjusted=False)
 *
 * 자격 증명(KRX_ID/KRX_PW)은 Worker 비밀값으로만 받고, 응답·로그에 절대 내보내지 않는다.
 */
const BASE = "https://data.krx.co.kr";
const LOGIN_PAGE = `${BASE}/contents/MDC/COMS/client/MDCCOMS001.cmd`;
const LOGIN_JSP = `${BASE}/contents/MDC/COMS/client/view/login.jsp?site=mdc`;
const LOGIN_URL = `${BASE}/contents/MDC/COMS/client/MDCCOMS001D1.cmd`;
const JSON_URL = `${BASE}/comm/bldAttendant/getJsonData.cmd`;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const SESSION_TTL_MS = 50 * 60e3;   // KRX 세션은 1시간 만료(pykrx 기준). 여유를 두고 50분

export class KrxError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

/** 쿠키 저장소: 이름 → 값. Set-Cookie의 속성은 무시하고 같은 도메인(data.krx.co.kr)에만 보낸다. */
function storeCookies(jar, res) {
  const list = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie()
    : (res.headers.get("set-cookie") ? [res.headers.get("set-cookie")] : []);
  for (const c of list) {
    const [pair] = c.split(";");
    const i = pair.indexOf("=");
    if (i > 0) jar.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim());
  }
}
const cookieHeader = (jar) => [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

/** requests.Session처럼 리다이렉트를 따라가며 쿠키를 모은다. */
async function send(jar, url, init = {}, fetchImpl = fetch) {
  let target = url;
  for (let hop = 0; hop < 5; hop++) {
    const res = await fetchImpl(target, {
      ...init,
      redirect: "manual",
      headers: { "user-agent": UA, ...(init.headers || {}), ...(jar.size ? { cookie: cookieHeader(jar) } : {}) },
    });
    storeCookies(jar, res);
    const loc = res.headers.get("location");
    if (res.status >= 300 && res.status < 400 && loc) {
      target = new URL(loc, target).toString();
      init = { method: "GET", headers: init.headers };
      continue;
    }
    return res;
  }
  throw new KrxError("redirect", "KRX 리다이렉트가 너무 많습니다");
}

export async function login(id, pw, fetchImpl = fetch) {
  if (!id || !pw) throw new KrxError("no_credentials", "KRX_ID/KRX_PW 미설정");
  const jar = new Map();
  await send(jar, LOGIN_PAGE, {}, fetchImpl);
  await send(jar, LOGIN_JSP, { headers: { referer: LOGIN_PAGE } }, fetchImpl);
  const form = { mbrNm: "", telNo: "", di: "", certType: "", mbrId: id, pw };
  const post = async (extra = {}) => {
    const res = await send(jar, LOGIN_URL, {
      method: "POST",
      headers: { referer: LOGIN_PAGE, "content-type": "application/x-www-form-urlencoded; charset=UTF-8" },
      body: new URLSearchParams({ ...form, ...extra }).toString(),
    }, fetchImpl);
    const j = await res.json().catch(() => ({}));
    return String(j._error_code || "");
  };
  let code = await post();
  if (code === "CD011") code = await post({ skipDup: "Y" });   // 중복 로그인
  if (code === "CD010") throw new KrxError("password_change", "KRX 비밀번호 변경이 필요합니다(90일 만료)");
  if (code !== "CD001") throw new KrxError("login_failed", `KRX 로그인 실패(${code || "응답 없음"})`);
  return { jar, expires: Date.now() + SESSION_TTL_MS };
}

async function getJson(session, params, fetchImpl = fetch) {
  const res = await send(session.jar, JSON_URL, {
    method: "POST",
    headers: {
      referer: `${BASE}/contents/MDC/MDI/outerLoader/index.cmd`,
      "x-requested-with": "XMLHttpRequest",
      "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    },
    body: new URLSearchParams(params).toString(),
  }, fetchImpl);
  const text = await res.text();
  try { return JSON.parse(text); } catch { throw new KrxError("bad_response", `KRX 응답이 JSON이 아닙니다(HTTP ${res.status})`); }
}

export async function findIsin(session, code, fetchImpl = fetch) {
  const j = await getJson(session, { bld: "dbms/comm/finder/finder_stkisu", locale: "ko_KR", mktsel: "ALL", searchText: code, typeNo: "0" }, fetchImpl);
  const hit = (j.block1 || []).find((r) => r.short_code === code);
  if (!hit?.full_code) throw new KrxError("no_isin", `KRX에서 종목 ${code}를 찾지 못했습니다`);
  return hit.full_code;
}

/** "88,000" / "-1.90" / "-" → 숫자. pykrx와 같이 쉼표 등을 지우고 빈 값·'-'는 0 */
const num = (s) => {
  const t = String(s ?? "").replace(/[^-\w.]/g, "").replace(/-$/, "0");
  return t === "" ? 0 : Number(t);
};

/** 단순종가 일별 시세 [{date, close, change_pct, trading_value}] (날짜 오름차순, 종가 0 행 제외 — pipeline/krx.py와 같음) */
export async function dailyPrices(session, isin, start, end, fetchImpl = fetch) {
  const ymd = (d) => d.replaceAll("-", "");
  const j = await getJson(session, {
    bld: "dbms/MDC/STAT/standard/MDCSTAT01701", locale: "ko_KR",
    isuCd: isin, strtDd: ymd(start), endDd: ymd(end), adjStkPrc: "1",
  }, fetchImpl);
  if (!Array.isArray(j.output)) throw new KrxError("bad_response", "KRX 시세 응답에 output이 없습니다");
  return j.output
    .map((r) => ({
      date: String(r.TRD_DD).replaceAll("/", "-"),
      close: num(r.TDD_CLSPRC), change_pct: num(r.FLUC_RT), trading_value: num(r.ACC_TRDVAL),
    }))
    .filter((r) => r.close > 0)
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}

// ---------------------------------------------------------------- isolate 단위 세션·ISIN 캐시
let cached = null;             // {jar, expires}
const isinCache = new Map();   // code → ISIN (상장 중에는 바뀌지 않음)

/** 로그인 세션을 재사용하고, 응답이 이상하면(세션 만료·중복 로그인으로 끊김) 한 번만 다시 로그인한다. */
export async function withSession(env, fn, { fetchImpl = fetch, allowLogin = () => true } = {}) {
  const fresh = async () => {
    if (!(await allowLogin())) throw new KrxError("login_deferred", "다른 KRX 수집이 진행 중이라 로그인을 미룹니다");
    cached = await login(env.KRX_ID, env.KRX_PW, fetchImpl);
    return cached;
  };
  let s = cached && cached.expires > Date.now() ? cached : await fresh();
  try {
    return await fn(s);
  } catch (e) {
    if (!(e instanceof KrxError) || e.code !== "bad_response" || s !== cached) throw e;
    cached = null;
    s = await fresh();
    return await fn(s);
  }
}

export async function recentPrices(env, code, start, end, opts = {}) {
  return withSession(env, async (s) => {
    let isin = isinCache.get(code);
    if (!isin) { isin = await findIsin(s, code, opts.fetchImpl); isinCache.set(code, isin); }
    return dailyPrices(s, isin, start, end, opts.fetchImpl);
  }, opts);
}

export function _resetForTest() { cached = null; isinCache.clear(); }
