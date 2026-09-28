// GitHub Actions에서 web/krx.js를 실제 KRX에 대고 검증한다 (workflow: krx-live-check.yml).
// 출력: 공개 시세 요약과 성공/실패만. KRX_ID/KRX_PW 값은 출력하지 않는다.
import { login, findIsin, dailyPrices, marketPrices, KrxError } from "../krx.js";

const env = { KRX_ID: process.env.KRX_ID, KRX_PW: process.env.KRX_PW };
const code = process.env.CODE || "005930";
const kst = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
const start = new Date(Date.now() + 9 * 3600e3 - 14 * 86400e3).toISOString().slice(0, 10);
const t = () => performance.now();
const show = (label, rows) => console.log(`${label}: ${rows.length} rows, last=${JSON.stringify(rows.at(-1))}`);

let a0 = t();
const A = await login(env.KRX_ID, env.KRX_PW);
console.log(`login A: ok (${(t() - a0).toFixed(0)} ms)`);
a0 = t();
const isin = await findIsin(A, code);
console.log(`isin ${code}: ${isin} (${(t() - a0).toFixed(0)} ms)`);
a0 = t();
show(`daily A ${start}..${kst} (${(t() - a0).toFixed(0)} ms)`, await dailyPrices(A, isin, start, kst));

// 전종목 시세(메인 목록 새로고침용): 오늘부터 거슬러 올라가 첫 거래일
for (let back = 0; back < 7; back++) {
  const d = new Date(Date.now() + 9 * 3600e3 - back * 86400e3).toISOString().slice(0, 10);
  a0 = t();
  const m = await marketPrices(A, d);
  console.log(`market ${d}: ${m.size} stocks (${(t() - a0).toFixed(0)} ms)${m.size ? ` ${code}=${JSON.stringify(m.get(code))}` : ""}`);
  if (m.size) break;
}

// 중복 로그인 실험: 같은 계정으로 B가 로그인한 뒤 A 세션으로 다시 조회되는가?
const B = await login(env.KRX_ID, env.KRX_PW);
console.log("login B (duplicate): ok");
for (const [name, s] of [["A after B", A], ["B", B]]) {
  try { show(`daily ${name}`, await dailyPrices(s, isin, start, kst)); }
  catch (e) { console.log(`daily ${name}: FAIL ${e instanceof KrxError ? e.code : ""} ${e.message}`); }
}
