import type { CacheIndex, Detail, MasterStock, RunInfo } from "./types";

export async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-cache", ...init });
  if (!res.ok) {
    // Worker가 {"error": "..."} 형태로 원인을 돌려주면 그 메시지를 보여준다 (없으면 상태 코드만)
    const body = await res.json().catch(() => null);
    throw new Error(body?.error ? `${path}: ${body.error}` : `${path}: HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

let masterCache: Promise<MasterStock[]> | null = null;

/** 검색용 종목 목록. 정적 파일이며 검색 중에는 서버를 호출하지 않는다.
 * 주 1회 정도만 바뀌므로 no-cache를 강제하지 않아 브라우저 HTTP 캐시를 그대로 쓰고(재방문 시
 * 네트워크 왕복 자체를 건너뜀), 같은 세션 안에서는 메모리에도 담아 재요청하지 않는다. */
export function loadMaster(): Promise<MasterStock[]> {
  if (!masterCache) {
    masterCache = fetch("data/master.json")
      .then((res) => { if (!res.ok) throw new Error(`data/master.json: HTTP ${res.status}`); return res.json(); })
      .then((j: { stocks: Record<string, MasterStock> }) => Object.values(j.stocks))
      .catch((e) => { masterCache = null; throw e; });
  }
  return masterCache;
}

export const loadIndex = () => getJson<CacheIndex>("api/index");

export type StockResponse =
  | { status: "missing"; code: string }
  | { status: "ready"; code: string; stale: boolean; analysis: Detail };

// 같은 세션에서 방금 본 종목은 메모리에 잠깐 담아 둔다(탭 전환·뒤로가기 등에서 네트워크 왕복 자체를
// 건너뜀). Worker의 엣지 캐시(20초)보다 앞단이라 히트하면 요청이 아예 안 나간다.
const STOCK_MEM_TTL = 60_000;
const stockMemCache = new Map<string, { at: number; day: string; data: StockResponse }>();
const kstDayKey = () => new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Seoul",
  year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date());

/** fresh=true: 메모리·엣지 캐시를 모두 건너뛰고 항상 최신을 받는다 (새로고침 진행 중 폴링에 쓴다).
 * 일반 조회는 fresh 없이 호출해 반복 방문·여러 사용자가 같은 종목을 볼 때 캐시 이득을 본다. */
export async function getStock(code: string, fresh = false): Promise<StockResponse> {
  if (!fresh) {
    const hit = stockMemCache.get(code);
    if (hit && hit.day === kstDayKey() && Date.now() - hit.at < STOCK_MEM_TTL) return hit.data;
  }
  const data = await getJson<StockResponse>(`api/stock/${code}${fresh ? `?fresh=${Date.now()}` : ""}`);
  stockMemCache.set(code, { at: Date.now(), day: kstDayKey(), data });
  return data;
}
/** force=true: 최신 여부와 상관없이(캐시 쿨다운 무시) 다시 수집·계산한다. 사용자가 "새로고침"을 눌렀을 때 쓴다. */
export const requestRefresh = (code: string, force = false) =>
  getJson<{ status: "queued" | "running" | "error"; run?: RunInfo; error?: string }>(`api/stock/${code}/refresh`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ force }),
  });
/** 새로고침 폴링용 경량 조회: metadata.json의 last_attempt만 받는다(analysis.json 전체 대신). */
export const getMeta = (code: string) =>
  getJson<{ status: "missing"; code: string } | { status: "ready"; code: string; last_attempt: string | null; updated_at: string | null }>(
    `api/stock/${code}/meta`);
/** KRX 최신 시세로 저장 분석을 잠정 갱신한 결과 (Worker가 KRX를 직접 조회). 실패하면 unavailable — 확정 결과를 기다린다. */
export const getLive = (code: string) =>
  getJson<{ status: "live"; code: string; analysis: Detail } | { status: "unavailable"; reason: string }>(`api/stock/${code}/live`);
export type KrxStatus = {
  state: "ok" | "password_expired" | "login_failed" | "not_configured" | "unreachable";
  repo?: string; checked_at?: string;
};
/** KRX 로그인 상태. fresh=true면 Worker 캐시를 건너뛰고 지금 다시 확인한다(교체 후 '다시 확인'). */
export const getKrxStatus = (fresh = false) => getJson<KrxStatus>(`api/krx/status${fresh ? `?t=${Date.now()}` : ""}`);
export const getRun = (code: string) => getJson<{ run: RunInfo | null }>(`api/stock/${code}/run`);

// ---------------------------------------------------------------- 로컬 검색
const norm = (s: string) => s.normalize("NFKC").toLowerCase().replace(/[\s()\-·.,&]/g, "");

// 한글 초성 검색(예: "ㅅㅅㅈㅈ" → 삼성전자). 완성형 음절만 초성으로 바꾸고 나머지 글자는 그대로 둔다.
const CHOSUNG = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"];
const toChosung = (s: string) => {
  let out = "";
  for (const ch of s) {
    const code = ch.codePointAt(0)! - 0xac00;
    out += code >= 0 && code <= 11171 ? CHOSUNG[Math.floor(code / 588)] : ch;
  }
  return out;
};
const isChosungQuery = (s: string) => s.length > 0 && [...s].every((ch) => CHOSUNG.includes(ch));
// NFKC 정규화(norm)를 거치면 낱자 자음(호환 자모, U+3131대)이 조합용 초성 자모(U+1100대)로 바뀐다.
// 사용자가 입력하는 건 호환 자모이므로 비교 전에 다시 되돌린다.
const LEAD_JAMO_FIX: Record<string, string> = Object.fromEntries(CHOSUNG.map((c, i) => [String.fromCodePoint(0x1100 + i), c]));
const fixLeadJamo = (s: string) => [...s].map((ch) => LEAD_JAMO_FIX[ch] ?? ch).join("");

/** pipeline/master.py search()와 같은 규칙: 정확히 일치 > 앞부분 일치 > 포함. 입력이 초성만이면 초성 검색으로 전환. */
export function searchMaster(master: MasterStock[], q: string, limit = 10): MasterStock[] {
  const nq = fixLeadJamo(norm(q));
  if (!nq) return [];
  const byChosung = isChosungQuery(nq);
  const scored: [number, MasterStock][] = [];
  for (const s of master) {
    const names = [norm(s.name), ...(s.aliases || []).map(norm)];
    const keys = byChosung ? names.map(toChosung) : names;
    let rank = -1;
    if ((!byChosung && s.code === nq) || keys.some((n) => n === nq)) rank = 0;
    else if ((!byChosung && s.code.startsWith(nq)) || keys.some((n) => n.startsWith(nq))) rank = 1;
    else if (keys.some((n) => n.includes(nq))) rank = 2;
    if (rank >= 0) scored.push([rank, s]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1].name.length - b[1].name.length || a[1].name.localeCompare(b[1].name));
  return scored.slice(0, limit).map((x) => x[1]);
}
