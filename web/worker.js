/**
 * Cloudflare Worker: 정적 사이트(dist) + /api/*
 *
 * 종목 데이터 수집(pykrx·DART)은 Python이라 Worker에서 직접 돌릴 수 없다.
 * 그래서 Worker는
 *   - 캐시 읽기: GitHub 저장소 data/cache/... 파일을 GitHub API로 읽어 반환
 *   - 캐시 갱신: repository_dispatch(event_type "stock_refresh")로 GitHub Actions 'stock-refresh.yml'을
 *     해당 종목코드로 실행하고, 실행 상태는 같은 워크플로의 run 목록(run-name "refresh <code>")에서 읽는다
 * 만 한다. DART/KRX 비밀값은 GitHub Secrets에만 있고, Worker에는 GITHUB_TOKEN(이 저장소 전용)만 있다.
 *
 * 또한 새로고침 때 KRX 최신 시세를 직접 받아(web/krx.js) 저장된 분석을 '잠정' 갱신해 먼저 돌려준다(web/live.js).
 * 확정 계산·저장은 여전히 Actions가 한다.
 *
 * env: GITHUB_REPO (vars), GITHUB_BRANCH (vars, 기본 main), GITHUB_TOKEN (secret), KRX_ID·KRX_PW (secret, 선택),
 *      SYNC_KV (KV 네임스페이스, 선택: 기기 간 '최근 조회' 동기화 코드 저장용. 없으면 /api/sync/* 만 꺼짐)
 */
import { recentPrices, marketPrices, withSession, KrxError } from "./krx.js";
import { applyLive, liveIndexRows, parseUs10y } from "./live.js";
import { krxStatus } from "./krxadmin.js";

const GH = "https://api.github.com";
// 실행 상태 조회 대상. POST /refresh의 repository_dispatch(stock_refresh)를 받는 워크플로와 같아야 한다.
const WORKFLOW = "stock-refresh.yml";
const WORKER_VERSION = "2026-09-28-dispatch-fix";

const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });

/** 캐시 가능한 JSON 응답. 실제 캐시는 wrangler.toml의 [cache] enabled(Workers Cache)가 Worker 앞단에서
 * 이 응답의 Cache-Control(public, max-age)을 보고 처리한다 — 히트하면 Worker·GitHub API 호출이 아예 없다.
 * (이전의 Cache API(caches.default)는 *.workers.dev 배포에서는 아무 효과가 없어 제거했다.)
 * 캐시 키에 쿼리스트링이 포함되므로, 새로고침 폴링이 붙이는 ?fresh=... 는 매번 다른 키가 되어 캐시를 건너뛴다.
 * POST(/refresh)는 캐시되지 않고, no-store 응답(/run, 오류)도 캐시되지 않는다. */
async function cachedJson(ttlSeconds, compute) {
  return json(await compute(), 200, { "cache-control": `public, max-age=${ttlSeconds}` });
}

function gh(env, path, init = {}) {
  return fetch(`${GH}/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "user-agent": `dividend-10y-worker/${WORKER_VERSION}`,
      "x-github-api-version": "2022-11-28",
      accept: "application/vnd.github+json",
      ...(init.headers || {}),
    },
  });
}

// 같은 isolate 안에서만 유지되는 best-effort ETag 캐시 (path → {etag, body}).
// 인증된 조건부 요청이 304를 받으면 GitHub primary rate limit에 계산되지 않고 본문 전송도 없다.
const etags = new Map();
const ETAG_MAX = 200;

async function readRepoFile(env, path, { text = false } = {}) {
  const ref = env.GITHUB_BRANCH || "main";
  const prev = etags.get(path);
  const r = await gh(env, `/contents/${path}?ref=${ref}`, {
    headers: { accept: "application/vnd.github.raw+json", ...(prev ? { "if-none-match": prev.etag } : {}) },
  });
  if (r.status === 304 && prev) return prev.body;
  if (r.status === 404) { etags.delete(path); return null; }
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${path}`);
  const body = text ? await r.text() : await r.json();
  const etag = r.headers.get("etag");
  if (etag) {
    etags.delete(path);
    etags.set(path, { etag, body });
    if (etags.size > ETAG_MAX) etags.delete(etags.keys().next().value);   // 가장 오래된 항목부터 버림
  }
  return body;
}

const kstToday = (now = new Date()) => new Date(now.getTime() + 9 * 3600e3).toISOString().slice(0, 10);
const addDays = (day, n) => new Date(Date.parse(`${day}T00:00:00Z`) + n * 86400e3).toISOString().slice(0, 10);

/** KRX 최신 시세로 저장된 분석을 잠정 갱신한다. 실패는 {status:"unavailable", reason}으로 돌려 화면이 Actions 결과를 기다리게 한다. */
async function liveAnalysis(env, code) {
  if (!env.KRX_ID || !env.KRX_PW) return { status: "unavailable", reason: "no_credentials" };
  const analysis = await readRepoFile(env, `data/cache/stocks/${code}/analysis.json`);
  if (!analysis) return { status: "unavailable", reason: "no_cache" };
  const today = kstToday();
  const [rows, csv] = await Promise.all([
    recentPrices(env, code, addDays(analysis.as_of, -14), today)    // 저장 기준일 앞 거래일을 포함해 비교·권리락 판정
      .catch((e) => { throw e instanceof KrxError ? e : new KrxError("krx_unreachable", String(e)); }),
    readRepoFile(env, "data/us10y/dgs10.csv", { text: true }),
  ]);
  if (!csv) return { status: "unavailable", reason: "no_us10y" };
  const res = applyLive(analysis, rows, parseUs10y(csv), today);
  if (!res.ok) return { status: "unavailable", reason: res.reason };
  res.analysis.live = { provisional: true, source: "KRX", fetched_at: new Date().toISOString(), base_as_of: analysis.as_of };
  return { status: "live", code, analysis: res.analysis };
}

/** 메인 목록 잠정 갱신: 오늘(휴장일이면 가장 가까운 이전 거래일) KRX 전종목 시세 1회 조회로 조회한 종목 전체를 갱신 */
async function liveIndex(env) {
  if (!env.KRX_ID || !env.KRX_PW) return { status: "unavailable", reason: "no_credentials" };
  const [index, csv] = await Promise.all([
    readRepoFile(env, "data/cache/index.json"),
    readRepoFile(env, "data/us10y/dgs10.csv", { text: true }),
  ]);
  if (!index?.stocks?.length) return { status: "unavailable", reason: "no_index" };
  if (!csv) return { status: "unavailable", reason: "no_us10y" };
  const today = kstToday();
  const { day, prices } = await withSession(env, async (s) => {
    for (let back = 0; back < 7; back++) {   // 주말·공휴일은 빈 결과라 하루씩 앞으로
      const d = addDays(today, -back);
      const m = await marketPrices(s, d);
      if (m.size) return { day: d, prices: m };
    }
    return { day: null, prices: new Map() };
  }).catch((e) => { throw e instanceof KrxError ? e : new KrxError("krx_unreachable", String(e)); });
  if (!day) return { status: "unavailable", reason: "no_trading_day" };
  const res = liveIndexRows(index.stocks, prices, day, parseUs10y(csv), today);
  return { status: "live", trade_date: day, fetched_at: new Date().toISOString(), ...res };
}

/** 한국 기준 '가장 최근 장 마감 거래일' (공휴일은 모름 → 주말만 제외) 과 그 마감 시각 */
function lastCloseKst(now = new Date()) {
  const kst = new Date(now.getTime() + 9 * 3600e3);
  const d = new Date(Date.UTC(kst.getUTCFullYear(), kst.getUTCMonth(), kst.getUTCDate()));
  if (kst.getUTCHours() < 16) d.setUTCDate(d.getUTCDate() - 1);   // 16시 전이면 전날 종가가 최신
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6) d.setUTCDate(d.getUTCDate() - 1);
  const day = d.toISOString().slice(0, 10);
  return { day, cutoffIso: `${day}T16:00:00+09:00` };
}

function isStale(meta, now = new Date()) {
  if (!meta || !meta.price_through) return true;
  const attempted = meta.last_attempt ? new Date(meta.last_attempt) : null;
  // 미국10Y 없이 계산된 결과(배수 N/A)는 30분에 한 번까지 다시 시도
  if (!meta.us10y_through && !(attempted && now - attempted < 30 * 60e3)) return true;
  const { day, cutoffIso } = lastCloseKst(now);
  // 마감 전(장중)에 받은 가격이 price_through=오늘로 남아 있어도, 마감 뒤 확인 전이면 종가가 아니므로 stale.
  // price_through가 day보다 뒤면(오늘 16시 전의 장중 데이터) 아직 마감 기준일이 오지 않아 최신이다.
  if (meta.price_through > day) return false;
  // 기준일 데이터가 있거나 공휴일 등으로 새 데이터가 없는 경우: 마감 이후에 이미 확인했다면 다시 돌리지 않는다
  return !(attempted && attempted >= new Date(cutoffIso));
}

async function activeRun(env, code) {
  for (const status of ["in_progress", "queued"]) {
    const r = await gh(env, `/actions/workflows/${WORKFLOW}/runs?status=${status}&per_page=20`);
    if (!r.ok) continue;
    const j = await r.json();
    const hit = (j.workflow_runs || []).find((w) => (w.display_title || "").includes(code));
    if (hit) return hit;
  }
  return null;
}

async function latestRun(env, code) {
  const r = await gh(env, `/actions/workflows/${WORKFLOW}/runs?per_page=30`);
  if (!r.ok) return null;
  const j = await r.json();
  return (j.workflow_runs || []).find((w) => (w.display_title || "").includes(code)) || null;
}

const runView = (w) => w && {
  id: w.id, status: w.status, conclusion: w.conclusion, url: w.html_url,
  created_at: w.created_at, updated_at: w.updated_at,
};

/** 실행 중인 job의 step 중 이름이 ①~④로 시작하는 것 (stock-refresh.yml의 단계) */
async function runSteps(env, runId) {
  const r = await gh(env, `/actions/runs/${runId}/jobs`);
  if (!r.ok) return [];
  const j = await r.json();
  return ((j.jobs || [])[0]?.steps || [])
    .filter((s) => /^[①②③④]/.test(s.name))
    .map((s) => ({ name: s.name, status: s.status, conclusion: s.conclusion }));
}

// ---------------------------------------------------------------- 기기 간 '최근 조회' 동기화
// 로그인이 없는 개인용 사이트라 사용자 식별 없이, 사용자가 기기 사이에 직접 옮기는 코드(8자, 길이로
// 짐작하기 어려운 랜덤값) 를 키로 쓴다. 저장 내용은 종목코드·이름·시장·조회시각뿐이라 민감하지 않다.
// 서버는 그 코드를 아는 사람 누구나 읽고 쓸 수 있다고 전제한다(로그인 대신 코드 자체가 비밀).
const SYNC_MAX_ENTRIES = 300;
const SYNC_TTL_SEC = 400 * 24 * 3600;   // 한동안 안 쓰는 코드만 자연히 정리된다. 기기의 로컬 목록 자체는
                                         // 이 코드와 무관하게 영구 보존되므로(recent.ts), 사용자 데이터 손실은 없다.

function isValidRecentList(list) {
  return Array.isArray(list) && list.length <= SYNC_MAX_ENTRIES && list.every((r) =>
    r && typeof r === "object"
    && typeof r.code === "string" && /^\d{6}$/.test(r.code)
    && typeof r.name === "string" && r.name.length <= 100
    && typeof r.market === "string" && r.market.length <= 20
    && typeof r.at === "number" && Number.isFinite(r.at));
}

// 삭제 기록(tombstone). web/src/lib/sync.ts 참고 — 한쪽이 지운 종목을 다른 쪽이 아직 모른 채 들고
// 있을 때, 단순히 두 목록을 합치면 삭제가 매번 취소되고 되살아난다. 서버는 그대로 저장·반환만 한다.
function isValidDeletedList(list) {
  return Array.isArray(list) && list.length <= SYNC_MAX_ENTRIES && list.every((t) =>
    t && typeof t === "object" && typeof t.code === "string" && /^\d{6}$/.test(t.code)
    && typeof t.at === "number" && Number.isFinite(t.at));
}

async function syncHandler(code, req, env) {
  if (!env.SYNC_KV) return json({ error: "동기화 기능이 설정되지 않았습니다 (wrangler.toml kv_namespaces, "
    + "Cloudflare API 토큰에 Workers KV Storage 편집 권한이 있는지 확인하세요)." }, 500);
  const key = `sync:${code}`;
  if (req.method === "GET") {
    const raw = await env.SYNC_KV.get(key);
    if (!raw) return json({ status: "missing" });
    try {
      const data = JSON.parse(raw);
      return json({ status: "ok", recent: data.recent, deleted: data.deleted ?? [], updated_at: data.updated_at });
    } catch {
      return json({ status: "missing" });   // 손상된 값은 없는 것과 같게 취급 — 다음 PUT이 덮어쓴다
    }
  }
  if (req.method === "PUT") {
    let body;
    try { body = await req.json(); } catch { return json({ error: "잘못된 요청 본문" }, 400); }
    if (!isValidRecentList(body?.recent) || !isValidDeletedList(body?.deleted ?? []))
      return json({ error: "잘못된 목록 형식" }, 400);
    const updated_at = new Date().toISOString();
    await env.SYNC_KV.put(key, JSON.stringify({ recent: body.recent, deleted: body.deleted ?? [], updated_at }),
      { expirationTtl: SYNC_TTL_SEC });
    return json({ status: "ok", updated_at });
  }
  return json({ error: "method not allowed" }, 405);
}

async function api(url, req, env, ctx) {
  const p = url.pathname;

  // 기기 간 '최근 조회' 동기화 코드. GitHub를 안 쓰므로 아래 GITHUB_TOKEN 확인보다 먼저 처리한다.
  const sm = p.match(/^\/api\/sync\/([A-Z0-9]{8})$/);
  if (sm) return syncHandler(sm[1], req, env);

  if (!env.GITHUB_TOKEN) return json({ error: "GitHub 저장소 접근용 토큰이 없습니다. "
    + "저장소 Settings → Secrets and variables → Actions에 WORKER_GITHUB_TOKEN을 등록한 뒤 Deploy를 다시 실행하세요." }, 500);
  if (!env.GITHUB_REPO) return json({ error: "Worker 설정 필요: GITHUB_REPO (wrangler.toml vars)" }, 500);

  // 조회한 종목 목록: 새 종목이 생기거나 값이 바뀌어도 30초 정도는 늦게 보여도 무방하다.
  if (p === "/api/index" && req.method === "GET") {
    return cachedJson(30, async () => (await readRepoFile(env, "data/cache/index.json")) || { stocks: [] });
  }

  // 메인 화면 '조회한 종목' 새로고침: 전 종목 잠정 시세(KRX 1회 조회). 반복 요청은 Workers Cache(15초)가 받는다.
  if (p === "/api/live/index" && req.method === "GET") {
    try {
      const out = await liveIndex(env);
      return out.status === "live" ? json(out, 200, { "cache-control": "public, max-age=15" }) : json(out);
    } catch (e) {
      if (e instanceof KrxError) return json({ status: "unavailable", reason: e.code });
      throw e;
    }
  }

  // KRX 로그인 상태(비밀번호 만료 알림용). 로그인 시도를 줄이도록 Workers Cache에 맡긴다(쿼리를 붙이면 우회).
  if (p === "/api/krx/status" && req.method === "GET") {
    const st = await krxStatus(env);
    return json({ ...st, repo: env.GITHUB_REPO, checked_at: new Date().toISOString() }, 200,
      { "cache-control": `public, max-age=${st.state === "ok" ? 300 : 600}` });
  }

  const m = p.match(/^\/api\/stock\/(\d{6})(\/refresh|\/run|\/meta|\/live)?$/);
  if (!m) return json({ error: "not found" }, 404);
  const [, code, sub] = m;

  if (!sub && req.method === "GET") {
    return cachedJson(20, async () => {
      const analysis = await readRepoFile(env, `data/cache/stocks/${code}/analysis.json`);
      if (!analysis) return { status: "missing", code };
      return { status: "ready", code, stale: isStale(analysis.metadata), analysis };
    });
  }

  // 새로고침 폴링용 경량 응답: metadata.json(1KB 미만)만 읽는다. analysis.json(100KB+)은
  // last_attempt가 바뀌었을 때만 화면이 따로 받는다. 폴링은 항상 최신을 봐야 하므로 캐시하지 않는다.
  if (sub === "/meta" && req.method === "GET") {
    const meta = await readRepoFile(env, `data/cache/stocks/${code}/metadata.json`);
    if (!meta) return json({ status: "missing", code });
    return json({ status: "ready", code, last_attempt: meta.last_attempt ?? null, updated_at: meta.updated_at ?? null });
  }

  // KRX 최신 시세로 잠정 갱신한 분석. 같은 종목 반복 요청은 Workers Cache(15초)가 받아 KRX 호출을 줄인다.
  // KRX 쪽 실패(로그인·응답 이상)는 오류가 아니라 unavailable로 돌려준다 — 화면은 Actions 확정 결과를 기다린다.
  if (sub === "/live" && req.method === "GET") {
    try {
      const out = await liveAnalysis(env, code);
      return out.status === "live" ? json(out, 200, { "cache-control": "public, max-age=15" }) : json(out);
    } catch (e) {
      if (e instanceof KrxError) return json({ status: "unavailable", reason: e.code });
      throw e;
    }
  }

  if (sub === "/run" && req.method === "GET") {
    const run = runView(await latestRun(env, code));
    if (run && run.status !== "completed") run.steps = await runSteps(env, run.id);
    return json({ run });
  }

  if (sub === "/refresh" && req.method === "POST") {
    const running = await activeRun(env, code);            // 중복 실행 방지
    if (running) return json({ status: "running", run: runView(running) });
    let force = false;
    try { force = !!(await req.json())?.force; } catch { /* 본문 없어도 됨 */ }
    const r = await gh(env, "/dispatches", {
      method: "POST",
      body: JSON.stringify({
        event_type: "stock_refresh",
        client_payload: { code, force: force ? "true" : "false" },
      }),
    });
    if (r.status !== 204) return json({ status: "error", error: `repository dispatch 실패 ${r.status}: ${await r.text()}` }, 502);
    return json({ status: "queued" });
  }
  return json({ error: "method not allowed" }, 405);
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(req);
    try {
      return await api(url, req, env, ctx);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};

export { isStale, lastCloseKst };
