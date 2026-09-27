import { useMemo, useState } from "react";
import type { CacheIndex, MasterStock } from "../lib/types";
import { mult, pct, won, NA } from "../lib/format";
import { BandGauge } from "../components/BandGauge";
import { SearchBox } from "../components/SearchBox";

type SortKey = "mult_desc" | "yield_desc" | "paid_desc" | "recent";

export function Home({ master, index, indexError, onPick }: {
  master: MasterStock[] | null; index: CacheIndex | null; indexError: string | null; onPick: (s: MasterStock) => void;
}) {
  const [sort, setSort] = useState<SortKey>("mult_desc");
  const rows = useMemo(() => {
    const r = [...(index?.stocks ?? [])];
    const k = (v: number | null | undefined) => (v == null ? -Infinity : v);
    r.sort((a, b) =>
      sort === "recent" ? (b.updated_at ?? "").localeCompare(a.updated_at ?? "")
      : sort === "yield_desc" ? k(b.yield) - k(a.yield)
      : sort === "paid_desc" ? k(b.paid10) - k(a.paid10) || k(b.multiple) - k(a.multiple)
      : k(b.multiple) - k(a.multiple));
    return r;
  }, [index, sort]);
  const us = index?.sources?.us10y;

  return (
    <main className="page">
      <header className="masthead">
        <h1>배당수익률 ÷ 미국 10년물</h1>
        <dl className="headline-figures">
          <div><dt>미국 10년물 데이터</dt><dd>{us?.data_through ?? NA}</dd></div>
          <div><dt>조회한 종목</dt><dd>{index ? `${index.stocks.length}개` : NA}</dd></div>
        </dl>
      </header>

      <section className="search-wrap">
        <SearchBox master={master} onPick={onPick} autoFocus />
        <p className="note">
          처음 조회하는 종목은 2016년부터 가격·배당을 모으느라 1~3분 걸립니다. 한 번 조회한 종목은 저장해 두고 다음부터는 새 데이터만 확인합니다.
        </p>
      </section>

      <section>
        <div className="list-head">
          <h2>조회한 종목</h2>
          <label className="inline">정렬
            <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
              <option value="mult_desc">배당/10Y 높은 순</option>
              <option value="yield_desc">배당수익률 높은 순</option>
              <option value="paid_desc">배당 지속성 높은 순</option>
              <option value="recent">최근 조회 순</option>
            </select>
          </label>
        </div>
        {indexError && <p className="err">목록을 불러오지 못했습니다: {indexError}</p>}
        {index && rows.length === 0 && <p className="empty-msg">아직 조회한 종목이 없습니다. 위에서 종목을 검색해 보세요.</p>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="grid">
              <thead><tr>
                <th className="stick">종목</th><th className="hide-sm">시장</th>
                <th className="num hide-sm">주가</th><th className="num hide-sm">예상 DPS</th>
                <th className="num">배당률</th><th className="num key">배당/10Y</th>
                <th className="num">10년 배당</th><th>역사적 위치</th><th className="hide-sm">기준일</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.code} onClick={() => { window.location.hash = `#/stock/${r.code}`; }}>
                    <td className="stick">
                      <a href={`#/stock/${r.code}`} className="name">{r.name}</a>
                      <span className="code">{r.code}<span className="show-sm"> {r.market}</span></span>
                    </td>
                    <td className="hide-sm">{r.market}</td>
                    <td className="num hide-sm">{won(r.price)}</td>
                    <td className="num hide-sm">{r.has_div_data ? won(r.dps) : NA}</td>
                    <td className="num">{pct(r.yield)}</td>
                    <td className="num key">{mult(r.multiple)}</td>
                    <td className="num">{r.paid10 == null ? NA : `${r.paid10}/10`}</td>
                    <td><BandGauge pct={r.pct} /></td>
                    <td className="hide-sm num">{r.price_date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {index?.rules && (
        <details className="rules">
          <summary>계산 규칙과 데이터 사용 기준</summary>
          <ol>{index.rules.map((r, i) => <li key={i}>{r}</li>)}</ol>
        </details>
      )}
    </main>
  );
}
