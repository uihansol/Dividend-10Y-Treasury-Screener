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

const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });

function gh(env, path, init = {}) {
  return fetch(`${GH}/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "user-agent": "dividend-10y-worker",
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

function isStale(meta) {
  if (!meta || !meta.price_through) return true;
  const { day, cutoffIso } = lastCloseKst();
  if (meta.price_through >= day) return false;
  // 공휴일 등으로 새 데이터가 없는 경우: 마감 이후에 이미 확인했다면 다시 돌리지 않는다
  return !(meta.last_attempt && new Date(meta.last_attempt) >= new Date(cutoffIso));
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

async function api(url, req, env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return json({ error: "Worker 설정 필요: GITHUB_TOKEN, GITHUB_REPO" }, 500);
  const p = url.pathname;

  if (p === "/api/index" && req.method === "GET") {
    return json((await readRepoFile(env, "data/cache/index.json")) || { stocks: [] });
  }

  const m = p.match(/^\/api\/stock\/(\d{6})(\/refresh|\/run)?$/);
  if (!m) return json({ error: "not found" }, 404);
  const [, code, sub] = m;

  if (!sub && req.method === "GET") {
    const analysis = await readRepoFile(env, `data/cache/stocks/${code}/analysis.json`);
    if (!analysis) return json({ status: "missing", code });
    return json({ status: "ready", code, stale: isStale(analysis.metadata), analysis });
  }

  if (sub === "/run" && req.method === "GET") {
    return json({ run: runView(await latestRun(env, code)) });
  }

  if (sub === "/refresh" && req.method === "POST") {
    const running = await activeRun(env, code);            // 중복 실행 방지
    if (running) return json({ status: "running", run: runView(running) });
    const r = await gh(env, `/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: env.GITHUB_BRANCH || "main", inputs: { code } }),
    });
    if (r.status !== 204) return json({ status: "error", error: `dispatch 실패 ${r.status}: ${await r.text()}` }, 502);
    return json({ status: "queued" });
  }
  return json({ error: "method not allowed" }, 405);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(req);
    try {
      return await api(url, req, env);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};

export { isStale, lastCloseKst };
