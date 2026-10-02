export type Market = "KOSPI" | "KOSDAQ";

export interface ScreenerRow {
  code: string;
  name: string;
  market: Market;
  price: number | null;
  price_date: string;
  dps: number | null;
  yield: number | null;
  multiple: number | null;
  paid10: number | null;
  known10: number | null;
  paid5: number | null;
  pct: number | null;
  p10: number | null;
  p90: number | null;
  mcap: number | null;
  tv: number | null;
  has_div_data: boolean;
  flags: string[];
  /** "crown"(연속 10년 배당·삭감 없음) · "bomb"(최근 확정 결산년도 배당이 그 전년도 대비 50%+ 증가). pipeline/engine.py dividend_tags() */
  tags: ("crown" | "bomb")[];
}

export interface SourceState {
  last_attempt?: string;
  last_success?: string;
  last_error?: string | null;
  data_through?: string;
  note?: string;
  stale: boolean;
}

export interface Meta {
  built_at: string;
  as_of: string;
  us10y: number | null;
  us10y_date: string | null;
  expected_dps_mode: string;
  sources: Record<"prices" | "dividends" | "us10y" | "stocks", SourceState>;
  rules: string[];
  counts: Record<string, number>;
  validation: { flag_counts: Record<string, number>; [k: string]: unknown };
  repo: string | null;
}

export interface Stats {
  n: number; p10: number; p25: number; p50: number; p75: number; p90: number;
  min: number; max: number; current: number; percentile: number;
}

export interface Detail {
  code: string;
  name: string;
  market: Market;
  as_of: string;
  summary: ScreenerRow;
  us10y: number | null;
  us10y_date: string | null;
  components: { label: string; kind: "final" | "interim" | "provisional"; year: number; dps: number; confirmed: string; ref: string }[];
  components_status: "annual_confirmed" | "in_progress" | null;
  /** expected: 잠정(마지막) 연도에 한해 채워지는 '예상 DPS'(pipeline/engine.py expected_dps_asof, 현재 배당수익률과 같은 값). 그 외 연도는 null. */
  annual: { year: number; interim: number; final: number; total: number; quarterly: boolean; confirmed: string | null; flags: string[]; provisional: boolean; expected: number | null }[];
  persistence: {
    latest_fy?: number; paid10?: number; known10?: number; paid5?: number; known5?: number;
    suspensions?: number; cagr10?: number | null; cagr5?: number | null; max_decline?: number | null;
    vs10y?: number | null; dps10y_ago?: number | null;
  };
  multiple_stats: Stats | null;
  yield_stats: Stats | null;
  actions: { date: string; ratio: number }[];
  metadata?: {
    price_through?: string; dividend_through?: string | null; us10y_through?: string | null;
    updated_at?: string; last_attempt?: string; last_error?: Record<string, string> | null;
    steps?: Record<string, unknown>;
  };
  /** Worker가 KRX 최신 시세로 잠정 갱신한 결과일 때만 있다. 확정 계산(Actions)이 끝나면 사라진다. */
  live?: { provisional: boolean; source: string; fetched_at: string; base_as_of: string };
  series: { d: string[]; px: number[]; dps: (number | null)[]; y: (number | null)[]; u: (number | null)[]; m: (number | null)[] };
}

export interface MasterStock { code: string; name: string; market: Market; corp_code: string; aliases: string[] }

export interface IndexRow extends ScreenerRow { updated_at?: string; price_through?: string; has_error?: boolean }

export interface CacheIndex {
  built_at?: string;
  stocks: IndexRow[];
  sources?: Record<string, { data_through?: string; last_success?: string; last_error?: string | null }>;
  rules?: string[];
}

export interface RunStep { name: string; status: string; conclusion: string | null }
export interface RunInfo {
  id: number; status: string; conclusion: string | null; url: string; created_at: string; updated_at: string;
  steps?: RunStep[];   // 실행 중일 때만: stock-refresh.yml의 ①~④ 단계
}
