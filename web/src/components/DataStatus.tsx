import type { Meta } from "../lib/types";

const LABEL: Record<string, string> = {
  prices: "주가 (KRX)",
  dividends: "배당 (DART)",
  us10y: "미국 10년물 (FRED DGS10)",
  stocks: "종목 정보 (KRX)",
};

export function DataStatus({ meta }: { meta: Meta }) {
  const actionsUrl = meta.repo ? `https://github.com/${meta.repo}/actions/workflows/update-data.yml` : null;
  return (
    <footer className="status">
      <h2>데이터 상태</h2>
      <table className="status-table">
        <thead>
          <tr><th>데이터</th><th>데이터 기준일</th><th>마지막 업데이트 성공</th><th>상태</th></tr>
        </thead>
        <tbody>
          {(["prices", "dividends", "us10y", "stocks"] as const).map((k) => {
            const s = meta.sources[k] ?? { stale: true };
            return (
              <tr key={k} className={s.stale ? "is-stale" : ""}>
                <td>{LABEL[k]}</td>
                <td className="num">{s.data_through ?? "없음"}</td>
                <td className="num">{s.last_success ? s.last_success.replace("T", " ").slice(0, 16) : "없음"}</td>
                <td>
                  {s.stale ? "오래됨" : "정상"}
                  {s.last_error && <div className="err">최근 시도 실패: {s.last_error}</div>}
                  {s.note && <div className="note">{s.note}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="note">
        계산 시각 {meta.built_at.replace("T", " ").slice(0, 16)} KST. 매일 저녁 GitHub Actions가 새 데이터만 받아 다시 계산합니다.
      </p>
      {actionsUrl ? (
        <a className="btn" href={actionsUrl} target="_blank" rel="noreferrer">
          데이터 업데이트 실행 (GitHub Actions)
        </a>
      ) : (
        <p className="note">즉시 업데이트: GitHub 저장소의 Actions 탭에서 “Update data” 워크플로를 실행하세요.</p>
      )}
      <details className="rules">
        <summary>계산 규칙과 데이터 사용 기준</summary>
        <ol>{meta.rules.map((r, i) => <li key={i}>{r}</li>)}</ol>
      </details>
    </footer>
  );
}
