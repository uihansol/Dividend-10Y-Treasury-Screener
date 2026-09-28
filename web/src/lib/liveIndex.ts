import { getJson } from "./data";

export type LiveRow = { price: number; price_date: string; change_pct: number | null; yield: number | null; multiple: number | null };
export type LiveIndex =
  | { status: "live"; trade_date: string; fetched_at: string; us10y: number | null; rows: Record<string, LiveRow> }
  | { status: "unavailable"; reason: string };

// 메인 화면을 떠났다 돌아와도 방금 받은 잠정 시세를 유지한다(같은 KST 날짜, 30분 이내). 새로고침하면 다시 받는다.
const KEEP_MS = 30 * 60_000;
let last: { at: number; day: string; data: Extract<LiveIndex, { status: "live" }> } | null = null;
const kstDay = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul" }).format(new Date());

export function cachedLiveIndex() {
  return last && last.day === kstDay() && Date.now() - last.at < KEEP_MS ? last.data : null;
}

/** 조회한 종목 전체의 잠정 시세 (Worker가 KRX 전종목 시세를 1회 조회). 정확도보다 속도 우선 */
export async function fetchLiveIndex(): Promise<LiveIndex> {
  const res = await getJson<LiveIndex>("api/live/index");
  if (res.status === "live") last = { at: Date.now(), day: kstDay(), data: res };
  return res;
}
