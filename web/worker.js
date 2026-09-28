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
 * env: GITHUB_REPO (vars), GITHUB_BRANCH (vars, 기본 main), GITHUB_TOKEN (secret)
 */
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

async function readRepoFile(env, path) {
  const ref = env.GITHUB_BRANCH || "main";
  const prev = etags.get(path);
  const r = await gh(env, `/contents/${path}?ref=${ref}`, {
    headers: { accept: "application/vnd.github.raw+json", ...(prev ? { "if-none-match": prev.etag } : {}) },
  });
  if (r.status === 304 && prev) return prev.body;
  if (r.status === 404) { etags.delete(path); return null; }
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${path}`);
  const body = await r.json();
  const etag = r.headers.get("etag");
  if (etag) {
    etags.delete(path);
    etags.set(path, { etag, body });
    if (etags.size > ETAG_MAX) etags.delete(etags.keys().next().value);   // 가장 오래된 항목부터 버림
  }
  return body;
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

/** 실행 중인 job의 step 중 이름이 ①~④로 시작하는 것 (stock-refresh.yml의 단계) */
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
    return cachedJson(30, async () => (await readRepoFile(env, "data/cache/index.json")) || { stocks: [] });
  }

  const m = p.match(/^\/api\/stock\/(\d{6})(\/refresh|\/run|\/meta)?$/);
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
