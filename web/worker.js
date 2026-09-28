/**
 * Cloudflare Worker: 정적 사이트(dist) + /api/*
 *
 * 종목 데이터 수집(pykrx·DART)은 Python이라 Worker에서 직접 돌릴 수 없다.
 * 그래서 Worker는
 *   - 캐시 읽기: GitHub 저장소 data/cache/... 파일을 GitHub API로 읽어 반환
 *   - 캐시 갱신: GitHub Actions 'analyze-stock.yml'을 해당 종목코드로 실행(workflow_dispatch)
 * 만 한다. DART/KRX 비밀값은 GitHub Secrets에만 있고, Worker에는 GITHUB_TOKEN(이 저장소 전용)만 있다.
 *
 * env: GITHUB_REPO (vars), GITHUB_BRANCH (vars, 기본 main), GITHUB_TOKEN (secret)
 */
const GH = "https://api.github.com";
const WORKFLOW = "analyze-stock.yml";
const WORKER_VERSION = "2026-09-28-dispatch-fix";

const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });

/** Cloudflare 엣지 캐시(Cache API). GitHub Contents API 호출을 줄인다.
 * 캐시 키는 요청 URL 그대로라, 화면이 새로고침 폴링 중 붙이는 ?fresh=... 는 매번 다른 키가 되어
 * 자동으로 캐시를 건너뛴다(폴링은 항상 최신 데이터를 봐야 하므로). ctx.waitUntil로 응답 후 기록해
 * 캐시 쓰기가 응답 시간에 영향을 주지 않는다. */
async function cachedJson(req, ctx, ttlSeconds, compute) {
  const cache = caches.default;
  const cacheKey = new Request(req.url, { method: "GET" });
  const hit = await cache.match(cacheKey);
  if (hit) return hit;
  const data = await compute();
  const res = json(data, 200, { "cache-control": `public, max-age=${ttlSeconds}` });
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
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

async function readRepoFile(env, path) {
  const ref = env.GITHUB_BRANCH || "main";
  const r = await gh(env, `/contents/${path}?ref=${ref}`, { headers: { accept: "application/vnd.github.raw+json" } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${path}`);
  return r.json();
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
  if (meta.price_through >= day) return false;
  // 공휴일 등으로 새 데이터가 없는 경우: 마감 이후에 이미 확인했다면 다시 돌리지 않는다
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

/** 실행 중인 job의 step 중 이름이 ①~④로 시작하는 것 (analyze-stock.yml의 단계) */
async function runSteps(env, runId) {
  const r = await gh(env, `/actions/runs/${runId}/jobs`);
  if (!r.ok) return [];
  const j = await r.json();
  return ((j.jobs || [])[0]?.steps || [])
    .filter((s) => /^[①②③④]/.test(s.name))
    .map((s) => ({ name: s.name, status: s.status, conclusion: s.conclusion }));
}

async function api(url, req, env, ctx) {
  if (!env.GITHUB_TOKEN) return json({ error: "GitHub 저장소 접근용 토큰이 없습니다. "
    + "저장소 Settings → Secrets and variables → Actions에 WORKER_GITHUB_TOKEN을 등록한 뒤 Deploy를 다시 실행하세요." }, 500);
  if (!env.GITHUB_REPO) return json({ error: "Worker 설정 필요: GITHUB_REPO (wrangler.toml vars)" }, 500);
  const p = url.pathname;

  // 조회한 종목 목록: 새 종목이 생기거나 값이 바뀌어도 30초 정도는 늦게 보여도 무방하다.
  if (p === "/api/index" && req.method === "GET") {
    return cachedJson(req, ctx, 30, async () => (await readRepoFile(env, "data/cache/index.json")) || { stocks: [] });
  }

  const m = p.match(/^\/api\/stock\/(\d{6})(\/refresh|\/run)?$/);
  if (!m) return json({ error: "not found" }, 404);
  const [, code, sub] = m;

  if (!sub && req.method === "GET") {
    return cachedJson(req, ctx, 20, async () => {
      const analysis = await readRepoFile(env, `data/cache/stocks/${code}/analysis.json`);
      if (!analysis) return { status: "missing", code };
      return { status: "ready", code, stale: isStale(analysis.metadata), analysis };
    });
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
