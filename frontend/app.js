/* Tradejournal Futures: plain JS, no build step. Talks to the HTTP API with the Cognito ID token. */
const C = window.TJ_CONFIG;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (p, d = 2) => p == null || !Number.isFinite(+p) ? '—' : (+p).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (v, signed = true) => v == null ? '—' : (v < 0 ? '−' : signed && v > 0 ? '+' : '') + '$' + Math.abs(Math.round(v)).toLocaleString('en-US');
const sgn = (v, d = 2, suf = '') => v == null ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + Math.abs(v).toFixed(d) + suf;
const cls = v => v > 0 ? 'gain' : v < 0 ? 'loss' : '';
const today = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
const addDays = (d, n) => { const x = new Date(d + 'T12:00:00Z'); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), 2600); }

async function api(path, { method = 'GET', body } = {}) {
  const token = await Auth.token();
  if (!token) { Auth.login(); return new Promise(() => {}); }
  let r;
  const ac = new AbortController(), tm = setTimeout(() => ac.abort(), 45000);
  try { r = await fetch(C.apiUrl + path, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, signal: ac.signal }); }
  catch (e) { throw new Error(ac.signal.aborted ? 'The server didn’t answer in time.' : 'Can’t reach the server.'); }
  finally { clearTimeout(tm); }
  if (r.status === 401) { Auth.login(); return new Promise(() => {}); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || `Request failed (${r.status}).`); e.status = r.status; throw e; }
  return data;
}

const S = { me: null, page: '', timers: [], chart: { sym: 'NQ', tf: 5, days: 3, strat: '', wt: true, cvd: true, prof: true, rth: false }, rp: null, filter: 'All', newSetup: null, trSet: 'study' };
const SIDE = [['long', 'Long'], ['short', 'Short'], ['both', 'Both']];
const SETUPS = {
  orb: { name: 'Opening range break + FVG retest', blurb: 'A close outside the opening range that leaves a fair value gap, a retest of the gap, an engulfing candle: enter, target in R.',
    params: [['rangeMin', 'Opening range (minutes)', 'sel', [[1, '1'], [5, '5'], [10, '10'], [15, '15'], [30, '30'], [60, '60']]], ['tgtR', 'Target (R)', 'num', 0.5], ['fvgWin', 'Gap within (candles)', 'num', 1],
      ['confWin', 'Confirmation within (candles)', 'num', 1], ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  range50: { name: 'Range 50% retrace', blurb: 'A leg, a retrace to its 50%, then back to 25% of the range: enter there, target the 50%, stop past the range end.',
    params: [['tf', 'Candles', 'sel', [[1, '1 min'], [5, '5 min']]], ['legPct', 'Minimum leg (% of price)', 'num', 0.05], ['entryLvl', 'Entry (share of the range)', 'num', 0.05], ['targetLvl', 'Target (share of the range)', 'num', 0.05],
      ['stopBuf', 'Stop past the range end (share)', 'num', 0.05], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  silver: { name: 'ICT Silver Bullet', blurb: '10:00–11:00 (and the other windows): a liquidity sweep sets the bias, enter on the fair value gap, target in R.',
    params: [['tf', 'Candles', 'sel', [[1, '1 min'], [5, '5 min']]], ['pivLen', 'Swing length', 'num', 1], ['expiry', 'Bias expires after (candles)', 'num', 1], ['rr', 'Target (R)', 'num', 0.5],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['flatEod', 'Flat at 15:55', 'bool'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  wt: { name: 'WaveTrend dots', blurb: 'WaveTrend [Krypt]: in at the open after a buy dot, out at the open after a sell dot (and the mirror for shorts).',
    params: [['tf', 'Candles', 'sel', [['1D', 'Daily'], ['60', '1 hour'], ['15', '15 min'], ['5', '5 min']]], ['side', 'Side', 'sel', SIDE], ['stopAtr', 'Stop (× average range, 0 = none)', 'num', 0.25],
      ['flatEod', 'Intraday: flat at 15:55', 'bool'], ['days', 'Candles / sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
};
const STAGES = { idea: 'Idea', study: 'Study', validated: 'Validated', paper: 'Paper', live: 'Live', retired: 'Retired' };

/* ---------- boot & routing ---------- */
async function boot() {
  try { await Auth.handleCallback(); } catch (e) { const el = $('#login-err'); el.textContent = e.message; el.hidden = false; }
  if (!(await Auth.token())) { $('#login').hidden = false; $('#signin').onclick = () => Auth.login(); return; }
  $('#app').hidden = false; $('#who-email').textContent = Auth.email();
  $('#signout').onclick = () => Auth.logout();
  try { S.me = await api('/me'); } catch (e) { toast(e.message); }
  feedPill();
  window.addEventListener('hashchange', route);
  route();
}
function feedPill() {
  const p = $('#feed-pill'), d = S.me && S.me.databento;
  p.className = 'pill ' + (d && d.connected ? 'ok' : 'warn');
  p.textContent = d && d.connected ? `Databento ••${d.last4}` : 'Databento not connected';
}
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; if (S.rpTimer) { clearInterval(S.rpTimer); S.rpTimer = null; } }
function route() {
  clearTimers();
  if (S.ch) { S.ch.destroy(); S.ch = null; }
  const h = (location.hash || '#running').slice(1), [page, arg] = h.split('/');
  S.page = page;
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('on', a.dataset.r === (page === 'strategy' ? 'research' : page)));
  const v = { running: vRunning, chart: vChart, research: vResearch, strategy: () => vStrategy(arg), data: vData }[page] || vRunning;
  v().catch(e => { $('#view').innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; });
}
const view = html => { $('#view').innerHTML = html; };
const head = (title, sub, right = '') => `<div class="page-head"><div><h1>${title}</h1>${sub ? `<div class="dim" style="margin-top:4px">${sub}</div>` : ''}</div><div class="grow"></div>${right}</div>`;
const cellName = c => c ? c.replace('|', ' · ') : '—';

/* ---------- Running ---------- */
async function vRunning() {
  const r = await api('/running');
  S.run = r;
  const any = Object.values(r.coverage).some(c => c.days);
  const tile = m => m.empty ? `<div class="panel tile"><div><b>${m.sym}</b> <span class="dim">${esc(m.name)}</span></div><div class="dim">No candles yet. <a href="#data">Download them</a>.</div></div>` : `
    <div class="panel tile">
      <div style="display:flex;align-items:baseline;gap:10px"><b style="font-size:16px">${m.sym}</b><span class="dim" style="font-size:12px">${esc(m.name)} · continuous</span><div style="flex:1"></div><span class="dim" style="font-size:12px">${esc((m.t || '').replace('T', ' '))} ET</span></div>
      <div class="px"><b>${fmt(m.last)}</b><span class="${cls(m.chg)}">${sgn(m.chg)}  ${m.prevClose ? sgn(m.chg / m.prevClose * 100, 2, '%') : ''}</span></div>
      <div class="spark">${spark(m.spark)}</div>
      <div class="kv"><div><div class="k">VWAP</div><div class="v">${fmt(m.vwap)}</div></div><div><div class="k">Session range</div><div class="v">${fmt(m.low)} – ${fmt(m.high)}</div></div>
        <div><div class="k">Overnight H / L</div><div class="v">${fmt(m.onh, 0)} / ${fmt(m.onl, 0)}</div></div><div><div class="k">Condition</div><div>${esc(cellName(m.cell))}</div></div></div>
    </div>`;
  const rk = r.risk, used = rk.limit ? Math.min(100, Math.max(0, -rk.net / rk.limit * 100)) : 0;
  const rows = r.strategies;
  const fl = S.filterRun || 'All';
  view(`${head('Running', 'Strategies in paper trading. Everything else stays in Research.', `<button class="btn" data-act="refresh">Update data now</button>`)}
    <div class="grid-auto" style="margin-bottom:16px">${r.markets.map(tile).join('')}
      <div class="panel tile"><div style="display:flex;align-items:baseline"><b>Risk today</b><div style="flex:1"></div><span class="dim" style="font-size:12px">${esc(r.today)}</span></div>
        <div><div style="display:flex;font-size:13px;margin-bottom:6px"><span>Daily loss limits</span><div style="flex:1"></div><span class="mono">${money(Math.min(0, rk.net), false).replace('$0', '$0')} / ${money(rk.limit, false)}</span></div><div class="meter"><i class="${used > 75 ? 'warn' : ''}" style="width:${used}%"></i></div></div>
        <div class="kv kv3"><div><div class="k">Net today</div><div class="big ${cls(rk.net)}">${money(rk.net)}</div></div><div><div class="k">Open contracts</div><div class="big">${rk.open}</div></div><div><div class="k">Running</div><div class="big">${rows.filter(x => x.on).length}</div></div></div>
      </div>
    </div>
    <div class="row">
      <section class="panel col-main" style="display:block"><div class="hd"><h2>Strategies</h2><span class="dim" style="font-size:13px">Paper trading runs on the Databento candles, refreshed every 15 minutes.</span></div>
        ${rows.length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:820px"><thead><tr><th>Strategy</th><th>Mode</th><th>Position</th><th class="r">Today</th><th class="r">Trades</th><th class="r">Net since start</th><th>Paper vs test</th><th>Status</th></tr></thead><tbody>
          ${rows.map(s => `<tr><td><a class="name" href="#strategy/${s.id}">${esc(s.name)}</a><div class="sub">${esc(s.meta)}</div></td>
            <td><span class="stage st-paper">${s.mode}</span></td>
            <td class="mono">${s.pos.length ? s.pos.map(p => `${p.side === 'long' ? 'Long' : 'Short'} ${p.s} @ ${fmt(p.e)}`).join('<br>') : 'Flat'}</td>
            <td class="r ${cls(s.today)}">${money(s.today)}</td><td class="r">${s.tradesToday} / ${s.n}</td><td class="r ${cls(s.net)}">${money(s.net)}</td>
            <td>${zbar(s.z)}</td><td style="color:${s.kill ? 'var(--sellT)' : 'var(--muted)'}">${s.kill ? 'Stopped: ' + esc(s.kill) : s.on ? 'Running' : 'Stopped'}</td></tr>`).join('')}
        </tbody></table></div>` : `<div class="empty">Nothing is paper trading yet. Validate a strategy in <a href="#research">Research</a>, then start paper trading from its page.</div>`}
      </section>
      <aside class="panel col-side" style="display:block"><div class="hd"><h2>Today's fills</h2><div class="grow"></div><a href="#chart" style="font-size:13px">Open chart</a></div>
        ${r.fills.length ? r.fills.map(f => `<div class="fill"><span class="t">${esc(f.t)}</span><span class="s ${f.side === 'BUY' ? 'gain' : 'loss'}">${f.side}</span><span class="w">${esc(f.what)}</span><span class="mono">${fmt(f.px)}</span></div>`).join('') : '<div class="empty">No fills today.</div>'}
        <div class="bd note" style="border-top:1px solid var(--line)">${any ? `Candles stored: ${Object.entries(r.coverage).map(([s, c]) => `${s} ${c.from || '—'} → ${(c.to || '').replace('T', ' ')}`).join(' · ')}` : 'No market data yet: <a href="#data">connect Databento and download the candles</a>.'}</div>
      </aside>
    </div>`);
  S.timers.push(setInterval(() => { if (S.page === 'running' && !document.hidden) vRunning().catch(() => {}); }, 60000));
}
function spark(xs) { if (!xs || !xs.length) return ''; const lo = Math.min(...xs), hi = Math.max(...xs); return xs.map((v, i) => `<i style="height:${6 + (v - lo) / (hi - lo || 1) * 38}px;${i === xs.length - 1 ? 'background:#E7EAF0' : ''}"></i>`).join(''); }
function zbar(z) {
  if (z == null) return '<span class="dim">—</span>';
  const left = Math.max(0, Math.min(100, 50 + z * 15));
  return `<div style="display:flex;align-items:center;gap:8px"><div style="position:relative;width:90px;height:6px;background:#1F2632;border-radius:3px"><div style="position:absolute;left:50%;top:-3px;width:1px;height:12px;background:#4B5566"></div><div style="position:absolute;left:calc(${left}% - 5px);top:-2px;width:10px;height:10px;border-radius:5px;background:${z < -2 ? 'var(--sellT)' : 'var(--buyT)'}"></div></div><span class="mono dim" style="font-size:12px">z ${sgn(z, 1)}</span></div>`;
}

/* ---------- Data (Databento) ---------- */
async function vData() {
  const d = await api('/data');
  S.data = d; S.me.databento = d.databento; feedPill();
  const db = d.databento, cv = d.coverage, job = d.job || {};
  const t = today();
  view(`${head('Data', 'CME Globex through Databento: NQ and ES continuous front month (rolled on volume).')}
    <div class="row">
      <div class="col-main">
        <section class="panel"><div class="hd"><h2>Databento</h2><div class="grow"></div>${db.connected ? '<span class="pill ok">Connected</span>' : '<span class="pill warn">Not connected</span>'}</div>
          <div class="bd">${db.connected ? `
            <div class="kv" style="margin-bottom:14px"><div><div class="k">API key</div><div class="v">db-••••${esc(db.last4)}</div></div><div><div class="k">Dataset</div><div class="v">${esc(db.dataset)}</div></div>
              <div><div class="k">Data available to</div><div class="v">${db.range && db.range.end ? esc(db.range.end.replace('T', ' ')) + ' UTC' : '—'}</div></div><div><div class="k">Checked</div><div class="v">${esc((db.testedAt || db.savedAt || '').replace('T', ' ').slice(0, 16))}</div></div></div>
            <div class="form" style="align-items:end">
              <label class="check"><input type="checkbox" id="db-auto" ${db.auto ? 'checked' : ''}> Update candles every 15 min (market hours)</label>
              <label class="check"><input type="checkbox" id="db-autofp" ${db.autoFp ? 'checked' : ''}> Download each day's trades after the close</label>
              <label class="f">Ask me before a download over<input type="number" id="db-cap" min="0" step="0.5" value="${db.maxCost}"></label>
              <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-act="db-save">Save</button><button class="btn" data-act="db-test">Test</button><button class="btn danger" data-act="db-del">Disconnect</button></div>
            </div>
            <div class="sep"></div><div class="note">Replace the key: <input type="password" id="db-key" placeholder="db-…" autocomplete="off" style="width:280px"> <button class="btn sm" data-act="db-connect">Save key</button></div>` : `
            <p class="muted" style="margin-top:0">Paste your Databento API key. It is checked against Databento, then stored encrypted (AWS KMS); the browser never sees it again.</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input type="password" id="db-key" placeholder="db-…" autocomplete="off" style="flex:1 1 320px;max-width:460px">
              <button class="btn primary" data-act="db-connect">Connect</button></div>
            <p class="note">Keys: <a href="https://databento.com/portal/keys" target="_blank" rel="noopener">databento.com/portal/keys</a>. The key needs access to the CME Globex dataset (GLBX.MDP3). Downloads are billed by Databento; every download shows its cost first.</p>`}
          </div></section>

        <section class="panel"><div class="hd"><h2>Download 1-minute candles</h2><span class="dim" style="font-size:13px">Charts, studies, validation and paper trading</span></div>
          <div class="bd"><div class="form" style="align-items:end">
            <div class="f" style="font-size:13px;color:var(--muted)">Contracts<div class="seg">${['NQ', 'ES'].map(s => `<button data-symc="${s}" aria-pressed="${(S.dlSyms || ['NQ', 'ES']).includes(s)}">${s}</button>`).join('')}</div></div>
            <label class="f">From<input type="date" id="dl-from" value="${S.dlFrom || addDays(t, -730)}"></label>
            <label class="f">To<input type="date" id="dl-to" value="${t}"></label>
            <div class="f" style="font-size:13px;color:var(--muted)">Quick<div class="seg">${[['6m', -182], ['1y', -365], ['2y', -730], ['5y', -1826]].map(([l, n]) => `<button data-dlq="${n}">${l}</button>`).join('')}</div></div>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" data-act="est-c" ${db.connected ? '' : 'disabled'}>Estimate cost</button><button class="btn primary" data-act="dl-c" ${db.connected ? '' : 'disabled'}>Download</button><span id="est-c" class="muted"></span></div>
          </div></section>

        <section class="panel"><div class="hd"><h2>Download trades (footprint)</h2><span class="dim" style="font-size:13px">Order flow: footprint, real delta, tape and replay. Regular hours, up to 3 months at a time.</span></div>
          <div class="bd"><div class="form" style="align-items:end">
            <div class="f" style="font-size:13px;color:var(--muted)">Contracts<div class="seg">${['NQ', 'ES'].map(s => `<button data-symf="${s}" aria-pressed="${(S.fpSyms || ['NQ']).includes(s)}">${s}</button>`).join('')}</div></div>
            <label class="f">From<input type="date" id="fp-from" value="${addDays(t, -7)}"></label>
            <label class="f">To<input type="date" id="fp-to" value="${t}"></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" data-act="est-f" ${db.connected ? '' : 'disabled'}>Estimate cost</button><button class="btn primary" data-act="dl-f" ${db.connected ? '' : 'disabled'}>Download</button><span id="est-f" class="muted"></span></div>
          <p class="note">Full order book depth (MBP-10, the heatmap) isn't downloaded: it is many GB per day. The ladder shows the volume traded at each price instead.</p>
          </div></section>
      </div>
      <aside class="col-side">
        <section class="panel"><div class="hd"><h2>Stored</h2><div class="grow"></div><button class="btn sm" data-act="refresh" ${db.connected ? '' : 'disabled'}>Update now</button></div>
          ${Object.entries(cv).map(([s, c]) => `<div class="bd" style="border-top:1px solid var(--row)"><div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px"><b>${s}</b><span class="dim" style="font-size:12px">${esc(c.name)}</span></div>
            <div class="kv kv3"><div><div class="k">Candles</div><div class="v">${c.days ? c.days + ' days' : '—'}</div></div><div><div class="k">From</div><div class="v">${esc(c.from || '—')}</div></div><div><div class="k">To</div><div class="v">${esc((c.to || '—').replace('T', ' '))}</div></div>
            <div><div class="k">Footprint</div><div class="v">${c.fpDays ? c.fpDays + ' days' : '—'}</div></div><div><div class="k">From</div><div class="v">${esc(c.fpFrom || '—')}</div></div><div><div class="k">To</div><div class="v">${esc(c.fpTo || '—')}</div></div></div></div>`).join('')}
        </section>
        <div id="job">${jobHtml(job)}</div>
      </aside>
    </div>`);
  if (job.status === 'running') pollJob('/data', j => j.job, () => vData());
}
function jobHtml(j) {
  if (!j || !j.status) return '';
  return `<div class="job ${j.status === 'error' ? 'error' : ''}">${j.status === 'running' ? '<span class="spin"></span>' : ''}<span>${esc(j.msg || j.status)}</span><span class="dim" style="margin-left:auto;font-size:12px">${esc((j.at || '').slice(11, 16))}</span></div>`;
}
function pollJob(path, pick, done) {
  const h = setInterval(async () => {
    try { const r = await api(path); const j = pick(r); const el = $('#job'); if (el) el.innerHTML = jobHtml(j); if (j.status !== 'running') { clearInterval(h); done(r); } } catch (e) { clearInterval(h); }
  }, 2500);
  S.timers.push(h);
}
async function download(kind, confirm) {
  const isC = kind === 'candles', syms = isC ? (S.dlSyms || ['NQ', 'ES']) : (S.fpSyms || ['NQ']);
  const body = { kind, syms, from: $(isC ? '#dl-from' : '#fp-from').value, to: $(isC ? '#dl-to' : '#fp-to').value, confirm };
  const r = await api('/data/download', { method: 'POST', body });
  if (r.needConfirm) {
    if (window.confirm(`Databento estimates $${r.usd.toFixed(2)} for this download (your limit is $${r.cap}). Download anyway?`)) return download(kind, true);
    return;
  }
  toast(`Download started · estimated $${(r.usd || 0).toFixed(2)}`);
  vData();
}

/* ---------- Research ---------- */
async function vResearch() {
  const r = await api('/strategies');
  S.strats = r.strategies;
  const cov = r.coverage, f = S.filter;
  const rows = r.strategies.filter(s => f === 'All' || STAGES[s.stage] === f).sort((a, b) => (b.created || '').localeCompare(a.created || ''));
  const m = s => (s.valid && s.valid.test) || (s.study && s.study.m) || {};
  view(`${head('Research', 'Every strategy goes through the same protocol: data → study → validation → paper → live.', `<button class="btn primary" data-act="new">New strategy</button>`)}
    <section class="panel" style="margin-bottom:16px"><div class="bd" style="display:flex;flex-wrap:wrap;gap:20px;align-items:center">
      ${Object.entries(cov).map(([s, c]) => `<div><b>${s}</b> <span class="dim">${c.days ? `${c.days} sessions · ${c.from} → ${(c.to || '').slice(0, 10)}${c.fpDays ? ` · footprint ${c.fpDays} days` : ''}` : 'no candles yet'}</span></div>`).join('')}
      <div style="flex:1"></div><a class="btn sm" href="#data">Data</a></div></section>
    <div id="newbox">${S.newSetup !== null ? newForm() : ''}</div>
    <section class="panel"><div class="hd"><h2>Strategies</h2><div class="seg" role="group" aria-label="Stage">${['All', ...Object.values(STAGES)].map(l => `<button data-filter="${l}" aria-pressed="${l === f}">${l}</button>`).join('')}</div></div>
      ${rows.length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:860px"><thead><tr><th>Strategy</th><th>Market</th><th>Stage</th><th class="r">Trades</th><th class="r">Avg R</th><th class="r">Edge vs random</th><th class="r">PF</th><th class="r">$ / trade</th><th>Next step</th></tr></thead><tbody>
        ${rows.map(s => { const x = m(s); return `<tr><td><a class="name" href="#strategy/${s.id}">${esc(s.name)}</a><div class="sub">${esc(s.describe)}</div></td><td>${s.params.tickers.join(', ')}</td>
          <td><span class="stage st-${s.stage}">${STAGES[s.stage] || s.stage}</span></td><td class="r">${x.n ?? '—'}</td><td class="r ${cls(x.avgR)}">${x.avgR != null ? sgn(x.avgR, 2, 'R') : '—'}</td>
          <td class="r ${cls(x.edge)}">${x.edge != null ? sgn(x.edge, 2, ' pts') : '—'}</td><td class="r">${x.pf ?? '—'}</td><td class="r ${cls(x.exp)}">${x.exp != null ? money(x.exp) : '—'}</td><td class="muted">${nextStep(s)}</td></tr>`; }).join('')}
      </tbody></table></div>` : `<div class="empty">${r.strategies.length ? 'None at this stage.' : 'No strategies yet. Start one with <b>New strategy</b>.'}</div>`}
    </section>`);
}
function nextStep(s) {
  if (s.stage === 'idea') return 'Run the study';
  if (s.stage === 'study') return s.valid ? 'Failed a gate: change the rules' : 'Run the validation';
  if (s.stage === 'validated') return (s.paper || {}).kill ? 'Stopped by the kill switch' : 'Start paper trading';
  if (s.stage === 'paper') return `${(s.paper || {}).n || 0} paper trades`;
  return '';
}
function paramForm(setup, p, locked) {
  return `<div class="form">${SETUPS[setup].params.map(([k, lab, type, opt]) => {
    const v = p[k], dis = locked ? 'disabled' : '';
    if (type === 'sel') return `<label class="f">${lab}<select data-p="${k}" ${dis}>${opt.map(([ov, ol]) => `<option value="${ov}" ${String(ov) === String(v) ? 'selected' : ''}>${ol}</option>`).join('')}</select></label>`;
    if (type === 'bool') return `<label class="check" style="align-self:end;min-height:36px"><input type="checkbox" data-p="${k}" ${v ? 'checked' : ''} ${dis}> ${lab}</label>`;
    if (type === 'tick') return `<div class="f" style="font-size:13px;color:var(--muted)">${lab}<div class="seg">${['NQ', 'ES'].map(s => `<button type="button" data-ptick="${s}" aria-pressed="${(v || ['NQ']).includes(s)}" ${dis}>${s}</button>`).join('')}</div></div>`;
    return `<label class="f">${lab}<input type="${type === 'num' ? 'number' : 'text'}" ${type === 'num' ? `step="${opt}"` : ''} data-p="${k}" value="${esc(v ?? '')}" ${dis}></label>`;
  }).join('')}</div>`;
}
function readParams(root) {
  const p = {};
  root.querySelectorAll('[data-p]').forEach(el => { p[el.dataset.p] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? +el.value : el.value; });
  const t = [...root.querySelectorAll('[data-ptick][aria-pressed="true"]')].map(b => b.dataset.ptick);
  if (root.querySelector('[data-ptick]')) p.tickers = t.length ? t : ['NQ'];
  return p;
}
const DEF = { orb: { rangeMin: 5, tgtR: 3, fvgWin: 5, confWin: 10, lastEntry: '11:30', side: 'both', days: 250, tickers: ['NQ'] },
  range50: { tf: 1, legPct: 0.35, entryLvl: 0.25, targetLvl: 0.5, stopBuf: 0, side: 'both', days: 250, tickers: ['NQ'] },
  silver: { tf: 1, pivLen: 3, expiry: 30, rr: 2, bufTicks: 2, flatEod: true, side: 'both', days: 250, tickers: ['NQ'] },
  wt: { tf: '1D', side: 'long', stopAtr: 1.5, flatEod: true, days: 500, tickers: ['NQ'] } };
function newForm() {
  const k = S.newSetup;
  return `<section class="panel" style="margin-bottom:16px"><div class="hd"><h2>New strategy</h2><div class="grow"></div><button class="btn sm" data-act="new-close">Close</button></div><div class="bd">
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:${k ? 16 : 0}px">${Object.entries(SETUPS).map(([key, s]) => `
      <button class="cell" data-setup="${key}" aria-pressed="${k === key}"><span style="font-weight:600;font-size:14px">${s.name}</span><span class="note">${s.blurb}</span></button>`).join('')}</div>
    ${k ? `<div id="newp"><label class="f" style="max-width:420px;margin-bottom:12px">Name<input type="text" id="new-name" value="${esc(SETUPS[k].name)}"></label>${paramForm(k, DEF[k])}
      <div style="margin-top:14px"><button class="btn primary" data-act="create">Create</button></div></div>` : ''}
  </div></section>`;
}

/* ---------- Strategy ---------- */
async function vStrategy(id) {
  const s = await api('/strategies/' + id);
  S.cur = s;
  let ts = [];
  if (s.study) { try { ts = (await api(`/strategies/${id}/trades?set=${S.trSet}`)).trades; } catch (e) { ts = []; } }
  const v = s.valid, st = s.study, pp = s.paper || {}, job = s.job || {};
  const locked = !!pp.on;
  const dataOk = Object.values(s.coverage).every(c => c.days >= 60);
  const steps = [
    ['Data', dataOk ? Object.values(s.coverage).map(c => `${c.days} sessions`).join(' · ') : 'Need 60+ sessions', dataOk ? 'done' : ''],
    ['Study', st ? `${st.n.toLocaleString('en-US')} trades` : 'Not run', st ? 'done' : ''],
    ['Validation', v ? `${v.gates.filter(g => g.ok).length} / ${v.gates.length} gates` : 'Not run', v ? (v.pass ? 'done' : 'fail') : ''],
    ['Paper', pp.on ? `${pp.n || 0} trades` : pp.kill ? 'Stopped' : 'Off', pp.on ? 'run' : ''],
    ['Live', 'Needs a futures broker', ''],
  ];
  const cur = !dataOk ? 0 : !st ? 1 : !(v && v.pass) ? 2 : 3;
  const mrow = (name, k, f) => `<tr><td>${name}</td>${['train', 'test', 'base', 'cost2'].map(w => `<td class="r">${v[w] && v[w][k] != null ? f(v[w][k]) : '—'}</td>`).join('')}</tr>`;
  const grid = v && v.grid;
  view(`<div class="crumb"><a href="#research">Research</a> › ${s.params.tickers.join(', ')} · ${esc(s.setupName)}</div>
    ${head(esc(s.name), esc(s.describe), `<a class="btn" href="#chart" data-act="open-chart">Open on chart</a>${s.stage === 'retired' ? '<button class="btn" data-act="unretire">Bring back</button>' : '<button class="btn" data-act="retire">Retire</button>'}<button class="btn danger" data-act="delete">Delete</button>`)}
    <ol class="steps" aria-label="Validation protocol">${steps.map(([n, sb, stt], i) => `<li class="${i === cur ? 'cur' : ''}"><span class="n"><span class="dot ${stt === 'done' ? 'done' : stt === 'run' ? 'run' : ''}">${stt === 'fail' ? '!' : i + 1}</span>${stt === 'done' ? 'Done' : stt === 'run' ? 'Running' : stt === 'fail' ? 'Failed' : i === cur ? 'Next' : 'Locked'}</span><span class="nm">${n}</span><span class="sb">${esc(sb)}</span></li>`).join('')}</ol>
    <div id="job" style="margin-bottom:12px">${jobHtml(job)}</div>
    <div class="row">
      <div class="col-main">
        <section class="panel"><div class="hd"><h2>${v ? 'Validation' : 'Study'}</h2><span class="dim" style="font-size:13px">${v ? `Train = first 60% of the trades, test = the last 40% (from ${esc(v.split || '')}). Costs: $${s.comm} commission + ${s.slip} tick slippage per side, ${s.contracts} contract(s).` : st ? `${esc(st.from)} → ${esc(st.to)}` : 'Run the study to see the trades this rule would have taken.'}</span>
          <div class="grow"></div><button class="btn sm" data-act="run-study" ${locked || !dataOk ? 'disabled' : ''}>${st ? 'Re-run study' : 'Run study'}</button>${st ? `<button class="btn sm ${v ? '' : 'primary'}" data-act="run-valid" ${locked ? 'disabled' : ''}>${v ? 'Re-validate' : 'Run validation'}</button>` : ''}</div>
          ${v ? `<div class="tbl-wrap"><table class="tbl" style="min-width:600px"><thead><tr><th>Metric</th><th class="r">Train</th><th class="r">Test</th><th class="r">Random entries (test)</th><th class="r">Test at 2× costs</th></tr></thead><tbody>
            ${mrow('Trades', 'n', x => x.toLocaleString('en-US'))}${mrow('Win rate', 'win', x => x + '%')}${mrow('Avg R', 'avgR', x => sgn(x, 2, 'R'))}${mrow('Profit factor', 'pf', x => x)}
            ${mrow('Avg points / trade', 'pts', x => sgn(x, 2))}${mrow('$ / trade (net)', 'exp', x => money(x))}${mrow('Net total', 'total', x => money(x))}${mrow('Max drawdown', 'dd', x => money(-x))}
            ${mrow('Edge vs random (pts)', 'edge', x => sgn(x, 2))}</tbody></table></div>` : st ? `<div class="bd"><div class="kv">${[['Trades', st.m.n], ['Win rate', st.m.win != null ? st.m.win + '%' : '—'], ['Avg R', sgn(st.m.avgR, 2, 'R')], ['Profit factor', st.m.pf ?? '—'],
              ['$ / trade', money(st.m.exp)], ['Net total', money(st.m.total)], ['Max drawdown', money(-st.m.dd)], ['Edge vs random', sgn(st.m.edge, 2, ' pts')]].map(([k, x]) => `<div><div class="k">${k}</div><div class="v big">${x}</div></div>`).join('')}</div></div>` : ''}
          ${ts.length && S.trSet === 'study' ? `<div class="bd" style="border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:24px">
            <div style="flex:2 1 340px;min-width:0"><div class="note" style="margin-bottom:8px">Equity ($, net) · <span class="gain">train</span> then <span class="loss">test</span></div><div class="bars">${equityBars(ts, v && v.split)}</div></div>
            <div style="flex:1 1 220px;min-width:0"><div class="note" style="margin-bottom:8px">R per trade</div><div class="bars" style="gap:3px">${rHist(ts)}</div><div class="note mono" style="display:flex;justify-content:space-between"><span>≤ −1R</span><span>0</span><span>≥ +3R</span></div></div></div>` : ''}
        </section>
        ${grid ? `<section class="panel"><div class="hd"><h2>Where it may trade</h2><span class="dim" style="font-size:13px">Test results by market condition (known before the open). Tick the ones paper trading may use.</span></div>
          <div class="bd tbl-wrap"><div class="cells"><div></div><div class="dim" style="font-size:12px;text-align:center">calm</div><div class="dim" style="font-size:12px;text-align:center">volatile</div>
          ${['Bull', 'Sideways', 'Bear'].map(ph => `<div class="muted" style="display:flex;align-items:center">${ph}</div>${['calm', 'volatile'].map(vo => { const k = ph + '|' + vo, g = grid[k] || {}, on = !!(s.cells || {})[k];
            return `<button class="cell ${S.todayCell === k ? 'today' : ''}" data-cellk="${k}" aria-pressed="${on}" ${locked ? 'disabled' : ''}><span class="ok">${on ? 'Approved' : 'Not approved'}</span><span class="rv ${cls(g.avgR)}">${g.avgR != null ? sgn(g.avgR, 2, 'R') : '—'}</span><span class="note">${g.n || 0} trades · ${money(g.total)}</span></button>`; }).join('')}`).join('')}
          </div></div></section>` : ''}
        ${st && st.factors && Object.keys(st.factors).length ? `<section class="panel"><div class="hd"><h2>What drives it</h2><span class="dim" style="font-size:13px">All study trades, by factor</span></div><div class="bd" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px">
          ${Object.entries(st.factors).map(([lab, rows]) => `<div><div class="note" style="margin-bottom:6px">${esc(lab)}</div><table class="tbl"><tbody>${rows.slice(0, 8).map(r => `<tr><td style="padding:5px 8px">${esc(r.v)}</td><td class="r" style="padding:5px 8px">${r.n}</td><td class="r ${cls(r.avgR)}" style="padding:5px 8px">${r.avgR != null ? sgn(r.avgR, 2, 'R') : '—'}</td><td class="r ${cls(r.exp)}" style="padding:5px 8px">${money(r.exp)}</td></tr>`).join('')}</tbody></table></div>`).join('')}
        </div></section>` : ''}
        ${st ? `<section class="panel"><div class="hd"><h2>Trades</h2><div class="seg"><button data-trset="study" aria-pressed="${S.trSet === 'study'}">Study</button><button data-trset="paper" aria-pressed="${S.trSet === 'paper'}">Paper</button></div><div class="grow"></div><span class="dim" style="font-size:12px">last 200</span></div>
          ${ts.length ? `<div class="tbl-wrap" style="max-height:520px;overflow:auto"><table class="tbl" style="min-width:760px"><thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th class="r">Entry px</th><th class="r">Exit px</th><th class="r">Points</th><th class="r">R</th><th class="r">Net</th><th>Why</th><th>Condition</th></tr></thead><tbody>
            ${ts.slice(-200).reverse().map(t => `<tr><td class="mono">${esc(t.s)} ${esc(t.in.replace('T', ' ').slice(0, 16))}</td><td class="mono">${esc((t.out || '').slice(5, 16).replace('T', ' '))}</td><td>${t.side}</td><td class="r">${fmt(t.e)}</td><td class="r">${fmt(t.x)}</td>
              <td class="r ${cls(t.pts)}">${sgn(t.pts)}</td><td class="r ${cls(t.r)}">${t.r != null ? sgn(t.r, 2) : '—'}</td><td class="r ${cls(t.net)}">${money(t.net)}</td><td class="muted">${esc(t.status === 'open' ? 'open' : t.why || '')}</td><td class="muted">${esc(cellName(t.cell))}</td></tr>`).join('')}
          </tbody></table></div>` : `<div class="empty">${S.trSet === 'paper' ? 'No paper trades yet.' : 'No trades.'}</div>`}</section>` : ''}
      </div>
      <aside class="col-side">
        <section class="panel"><div class="hd"><h2>Rules</h2>${locked ? '<span class="dim" style="font-size:12px">locked while paper trading</span>' : ''}</div><div class="bd" id="rules">
          ${paramForm(s.setup, s.params, locked)}
          ${locked ? '' : `<div style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="btn sm" data-act="save-rules">Save rules</button><span class="note">Saving clears the study and validation.</span></div>`}</div></section>
        ${v ? `<section class="panel"><div class="hd"><h2>Validation gates</h2><div class="grow"></div><span class="${v.pass ? 'gain' : 'loss'}" style="font-weight:600;font-size:13px">${v.gates.filter(g => g.ok).length} / ${v.gates.length} passed</span></div><div class="bd" style="padding-top:4px">
          ${v.gates.map(g => `<div class="gate"><span style="color:${g.ok ? 'var(--buyT)' : 'var(--sellT)'};font-weight:700">${g.ok ? '✓' : '✕'}</span><span class="g">${esc(g.t)}</span><span class="v">${esc(g.v)}</span></div>`).join('')}</div></section>` : ''}
        <section class="panel"><div class="hd"><h2>Paper trading</h2></div><div class="bd" style="display:flex;flex-direction:column;gap:12px">
          ${pp.on || pp.n ? `<div class="kv kv3"><div><div class="k">Trades</div><div class="big">${pp.n || 0}</div></div><div><div class="k">Avg R</div><div class="big ${cls(pp.avgR)}">${pp.avgR != null ? sgn(pp.avgR, 2) : '—'}</div></div><div><div class="k">Net</div><div class="big ${cls(pp.net)}">${money(pp.net)}</div></div></div>${zbar(pp.z)}` : ''}
          ${pp.kill ? `<div class="job error">Kill switch: ${esc(pp.kill)}</div>` : ''}
          <div class="note">Simulated on the stored Databento candles (refreshed every 15 minutes), only in the approved conditions. Kill switch: z below −2 after 10 trades, or a drawdown over 2× the test's.</div>
          <div class="form" style="grid-template-columns:repeat(2,minmax(0,1fr))">
            <label class="f">Contracts<input type="number" id="pp-c" min="1" value="${s.contracts}"></label>
            <label class="f">Daily loss limit ($)<input type="number" id="pp-dl" min="0" step="50" value="${s.dailyLoss}"></label>
            <label class="f">Commission / side ($)<input type="number" id="pp-comm" min="0" step="0.05" value="${s.comm}"></label>
            <label class="f">Slippage / side (ticks)<input type="number" id="pp-slip" min="0" step="0.5" value="${s.slip}"></label>
          </div>
          <button class="btn sm" data-act="save-costs">Save</button>
          <label class="f">Account<select disabled><option>Simulated paper account</option><option>[Futures broker — not connected]</option></select></label>
          ${pp.on ? '<button class="btn" data-act="paper-stop">Stop paper trading</button>' : `<button class="btn primary" data-act="paper" ${v && v.pass ? '' : 'disabled'}>${v && v.pass ? 'Start paper trading' : 'Paper trading needs a passed validation'}</button>`}
          <button class="btn" disabled>Promote to live · needs a futures broker</button>
        </div></section>
      </aside>
    </div>`);
  if (job.status === 'running') pollJob('/strategies/' + id, x => x.job, () => vStrategy(id));
}
function equityBars(ts, split) {
  const xs = ts.filter(t => t.status !== 'open' && t.why !== 'open'); if (!xs.length) return '';
  const step = Math.max(1, Math.ceil(xs.length / 160)); let e = 0; const pts = [];
  xs.forEach((t, i) => { e += t.net; if (i % step === 0 || i === xs.length - 1) pts.push([e, split && t.d >= split]); });
  const lo = Math.min(0, ...pts.map(p => p[0])), hi = Math.max(...pts.map(p => p[0]));
  return pts.map(([v, te]) => `<i style="height:${4 + (v - lo) / (hi - lo || 1) * 104}px;background:${te ? '#B5702A' : '#2F5D93'}"></i>`).join('');
}
function rHist(ts) {
  const bins = new Array(9).fill(0);
  ts.forEach(t => { if (t.r == null) return; const k = Math.max(0, Math.min(8, Math.floor((t.r + 1) * 2))); bins[k]++; });
  const mx = Math.max(...bins) || 1;
  return bins.map((v, i) => `<i title="${v} trades" style="height:${2 + v / mx * 106}px;background:${i < 2 ? 'var(--sell)' : 'var(--buy)'};border-radius:2px 2px 0 0"></i>`).join('');
}

/* ---------- Chart ---------- */
const DAYS = { 1: [1, 3, 5, 10], 5: [1, 3, 5, 10, 30], 15: [5, 10, 30, 60], 60: [30, 60, 120, 180] };
async function vChart() {
  const c = S.chart;
  if (!S.strats) { try { S.strats = (await api('/strategies')).strategies; } catch (e) { S.strats = []; } }
  if (!DAYS[c.tf].includes(c.days)) c.days = DAYS[c.tf][1] || DAYS[c.tf][0];
  view(`<div class="toolbar">
      <div class="seg" role="group" aria-label="Contract">${['NQ', 'ES'].map(s => `<button data-csym="${s}" aria-pressed="${c.sym === s}" style="font-weight:600">${s}</button>`).join('')}</div>
      <div class="quote" id="quote"></div><div class="stats" id="cstats"></div><div style="flex:1"></div>
      <div class="seg" role="group" aria-label="Timeframe">${[1, 5, 15, 60].map(t => `<button data-ctf="${t}" aria-pressed="${c.tf === t}">${t === 60 ? '1h' : t + 'm'}</button>`).join('')}</div>
      <div class="seg" role="group" aria-label="Days">${DAYS[c.tf].map(d => `<button data-cdays="${d}" aria-pressed="${c.days === d}">${d}d</button>`).join('')}</div>
    </div>
    <div class="row">
      <section class="panel col-main" style="display:block;padding-top:12px">
        <div class="legend"><span>Strategy on the chart</span><select id="c-strat" style="min-height:30px;padding:3px 8px"><option value="">None</option>${S.strats.filter(s => s.params.tickers.includes(c.sym)).map(s => `<option value="${s.id}" ${c.strat === s.id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>
          ${[['prof', 'Volume profile'], ['cvd', 'Delta'], ['wt', 'WaveTrend'], ['rth', 'Regular hours only']].map(([k, l]) => `<button class="chip" data-ctog="${k}" aria-pressed="${!!c[k]}">${l}</button>`).join('')}
          <div style="flex:1"></div><span><span class="sw" style="border-top:2px solid var(--vwap)"></span>VWAP</span><span><span class="sw" style="border-top:1px dashed #93A0B2"></span>PDH / PDL / PDC · ONH / ONL</span></div>
        <div class="chartbox"><canvas id="cv" style="height:${S.rp ? 600 : 640}px"></canvas></div>
        ${S.rp ? replayBar() : `<div class="transport"><span class="muted" style="font-size:13px">Replay a session bar by bar with its order flow:</span><input type="date" id="rp-day" value="${S.lastDay || ''}"><button class="btn" data-act="rp-start">Start replay</button><span class="note">Footprint, tape and real delta need that day's trades (Data page).</span></div>`}
        ${S.rp && S.rpView === 'fp' ? `<div class="bd" style="border-top:1px solid var(--line)"><div id="fpview"></div></div>` : ''}
      </section>
      <aside class="col-side">${S.rp ? `
        <section class="panel ladder"><div class="hd"><h2>Volume at price</h2><div class="grow"></div><span class="dim" style="font-size:12px">traded so far · bid × ask</span></div><div id="ladder" style="padding:6px 0"></div></section>
        <section class="panel tape"><div class="hd"><h2>Time &amp; sales</h2><div class="grow"></div><span class="dim" style="font-size:12px">big prints</span></div><div id="tape" style="padding:6px 0;max-height:360px;overflow:auto"></div></section>` : `
        <section class="panel"><div class="hd"><h2>Session</h2></div><div class="bd" id="sessinfo"><span class="dim">Loading…</span></div></section>`}
      </aside>
    </div>`);
  S.ch = new FC.Chart($('#cv'), { span: S.rp ? 120 : (c.tf === 1 ? 240 : 180) });
  await loadChart();
}
function replayBar() {
  const r = S.rp;
  return `<div class="transport">
    <button class="iconbtn" data-act="rp-back" aria-label="One candle back"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg></button>
    <button class="iconbtn play" data-act="rp-play" aria-label="${r.playing ? 'Pause' : 'Play'}">${r.playing ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>' : '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5l12 7-12 7z"/></svg>'}</button>
    <button class="iconbtn" data-act="rp-fwd" aria-label="One candle forward"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>
    <div class="seg" role="group" aria-label="Speed">${[1, 2, 5, 10].map(v => `<button data-rpspeed="${v}" aria-pressed="${r.speed === v}">${v}×</button>`).join('')}</div>
    <label style="flex:1 1 220px;display:flex;align-items:center;gap:10px;font-size:12px" class="dim"><span class="mono" id="rp-time" style="color:var(--text)"></span><input type="range" id="rp-scrub" min="0" max="${(r.n || 1) - 1}" value="${r.k}" style="flex:1" aria-label="Replay position"></label>
    <div class="seg"><button data-rpview="chart" aria-pressed="${S.rpView !== 'fp'}">Candles</button><button data-rpview="fp" aria-pressed="${S.rpView === 'fp'}">+ Footprint</button></div>
    <span class="mono" style="font-size:13px">${esc(r.day)}</span>${r.fp ? '' : '<button class="btn sm" data-act="rp-getfp">Download this day\'s trades</button>'}
    <button class="btn sm" data-act="rp-stop">Exit replay</button></div>`;
}
async function loadChart() {
  const c = S.chart, r = S.rp;
  const q = r ? `sym=${c.sym}&tf=${c.tf <= 5 ? c.tf : 1}&days=1&end=${r.day}${c.strat ? '&strat=' + c.strat : ''}` : `sym=${c.sym}&tf=${c.tf}&days=${c.days}${c.rth ? '&rth=1' : ''}${c.strat ? '&strat=' + c.strat : ''}`;
  let d;
  try { d = await api('/chart?' + q); } catch (e) { toast(e.message); return; }
  S.cd = d;
  if (!d.bars.length) { $('.chartbox').innerHTML = `<div class="empty">No ${c.sym} candles stored${r ? ' for ' + esc(r.day) : ''}. <a href="#data">Download them on the Data page</a>.</div>`; const si = $('#sessinfo'); if (si) si.innerHTML = '<span class="dim">—</span>'; return; }
  S.lastDay = d.days[d.days.length - 1];
  if (r) {
    r.n = d.bars.length; r.tf = d.tf;
    if (r.k == null) { const i = d.bars.findIndex(b => b[0].slice(11, 16) >= '09:30' && b[0].slice(0, 10) === r.day); r.k = Math.max(0, i) + Math.round(15 / d.tf); }
    r.k = Math.min(r.k, r.n - 1);
    const sc = $('#rp-scrub'); if (sc) sc.max = r.n - 1;
    if (r.fp === undefined) { try { r.fp = await api(`/footprint?sym=${c.sym}&day=${r.day}`); } catch (e) { r.fp = null; } if (!r.fp) { const tb = $('.transport'); if (tb) tb.outerHTML = replayBar(); } }
  }
  drawChart(false);
}
function fpDelta() {
  const r = S.rp; if (!r || !r.fp) return null;
  if (!r.delta) { r.delta = {}; r.fp.m.forEach(([m, lv]) => { r.delta[m] = lv.reduce((s, [, b, a]) => s + a - b, 0); }); }
  return { [r.day]: r.delta };
}
function drawChart(keep) {
  const c = S.chart, d = S.cd, r = S.rp;
  if (!S.ch || !d) return;
  S.ch.set({ bars: d.bars, levels: d.levels, marks: d.marks || [], tf: d.tf, showWT: c.wt, showCVD: c.cvd, showProfile: c.prof, replay: r ? r.k : null, fpDelta: fpDelta() }, keep);
  const n = r ? r.k + 1 : d.bars.length, B = d.bars, last = B[n - 1], day = last[6];
  const sess = B.slice(0, n).filter(b => b[6] === day), lv = d.levels[day] || {};
  const pc = lv.pdc, chg = pc ? last[4] - pc : null;
  $('#quote').innerHTML = `<b>${fmt(last[4])}</b><span class="${cls(chg)}">${chg != null ? sgn(chg) + '  ' + sgn(chg / pc * 100, 2, '%') : ''}</span>`;
  const vol = sess.reduce((s, b) => s + b[5], 0), vw = S.ch.vw[n - 1];
  $('#cstats').innerHTML = `<span>VWAP <b>${fmt(vw)}</b></span><span>High <b>${fmt(Math.max(...sess.map(b => b[2])))}</b></span><span>Low <b>${fmt(Math.min(...sess.map(b => b[3])))}</b></span><span>Volume <b>${vol.toLocaleString('en-US')}</b></span><span>Condition <b>${esc(cellName(d.cells[day]))}</b></span>`;
  const si = $('#sessinfo');
  if (si) {
    const mk = d.marks || [];
    si.innerHTML = `<div class="kv kv3" style="margin-bottom:12px">${[['Prior high', lv.pdh], ['Prior low', lv.pdl], ['Prior close', lv.pdc], ['ON high', lv.onh], ['ON low', lv.onl], ['RTH open', lv.open]].map(([k, x]) => `<div><div class="k">${k}</div><div class="v">${fmt(x)}</div></div>`).join('')}</div>
      <div class="note">Session ${esc(day)} · ${esc(cellName(d.cells[day]))}. Footprint stored for ${d.fpDays.length ? esc(d.fpDays.join(', ')) : 'none of these days'}.</div>
      ${c.strat ? `<div class="sep"></div><div class="note" style="margin-bottom:6px">${mk.length} trade(s) of the strategy on these days</div>${mk.slice(-8).reverse().map(t => `<div class="fill" style="padding:6px 0"><span class="t">${esc(t.in.slice(5, 16).replace('T', ' '))}</span><span class="s ${t.side === 'long' ? 'gain' : 'loss'}">${t.side === 'long' ? 'L' : 'S'}</span><span class="w mono">${fmt(t.e)} → ${fmt(t.x)}</span><span class="mono ${cls(t.net)}">${money(t.net)}</span></div>`).join('')}` : ''}
      <div class="note" style="margin-top:10px">Scroll to zoom, drag to pan, double-click to reset.</div>`;
  }
  if (r) { replaySide(); const tm = $('#rp-time'); if (tm) tm.textContent = last[0].slice(11, 16); const sc = $('#rp-scrub'); if (sc) sc.value = r.k; }
}
function replayNowEnd() { const r = S.rp, b = S.cd.bars[r.k]; return FC.minStr(FC.toMin(b[0]) + S.cd.tf); }
function replaySide() {
  const r = S.rp, d = S.cd, lad = $('#ladder'), tape = $('#tape');
  if (!lad) return;
  if (!r.fp || S.cd.bars[r.k][6] !== r.day || S.cd.bars[r.k][0].slice(0, 10) !== r.day) {
    const msg = r.fp ? '<div class="empty">Before the regular session.</div>' : '<div class="empty">No trades stored for this day.</div>';
    lad.innerHTML = msg; tape.innerHTML = msg; const fv = $('#fpview'); if (fv) fv.innerHTML = msg; return;
  }
  const end = replayNowEnd(), last = d.bars[r.k][4], step = S.chart.sym === 'NQ' ? 1 : 0.25;
  const agg = {};
  r.fp.m.forEach(([m, lv]) => { if (m >= end) return; lv.forEach(([p, b, a]) => { const k = Math.floor(p / step) * step; const x = agg[k] || (agg[k] = [0, 0]); x[0] += b; x[1] += a; }); });
  const lk = Math.floor(last / step) * step, rows = [];
  for (let j = 15; j >= -15; j--) { const p = +(lk + j * step).toFixed(2); rows.push([p, ...(agg[p] || [0, 0])]); }
  const mx = Math.max(1, ...rows.map(x => Math.max(x[1], x[2])));
  lad.innerHTML = rows.map(([p, b, a]) => `<div class="lr ${p === lk ? 'last' : ''}"><div class="b"><i style="width:${b / mx * 100}%"></i><span>${b || ''}</span></div><div class="p">${fmt(p)}</div><div class="a"><i style="width:${a / mx * 100}%"></i><span>${a || ''}</span></div></div>`).join('');
  const big = r.fp.big.filter(x => x[0].slice(0, 5) < end).slice(-30).reverse();
  tape.innerHTML = big.length ? big.map(([t, p, s, side]) => `<div class="tr big" style="background:${side === 'B' ? 'rgba(74,158,255,.12)' : side === 'S' ? 'rgba(240,140,46,.12)' : 'transparent'}"><span class="dim">${esc(t)}</span><span>${fmt(p)}</span><span style="text-align:right">${s}</span><span class="${side === 'B' ? 'gain' : 'loss'}">${side}</span></div>`).join('') : '<div class="empty">No big prints yet.</div>';
  const fv = $('#fpview');
  if (fv) fv.innerHTML = footprintHtml(end);
}
function footprintHtml(end) {
  const r = S.rp, d = S.cd, tf = d.tf, k0 = Math.max(0, r.k - 7), bars = d.bars.slice(k0, r.k + 1).filter(b => b[0].slice(0, 10) === r.day && b[0].slice(11, 16) >= '09:30');
  if (!bars.length) return '<div class="empty">Footprint starts at 09:30.</div>';
  const byMin = {}; r.fp.m.forEach(([m, lv]) => { byMin[m] = lv; });
  const hi = Math.max(...bars.map(b => b[2])), lo = Math.min(...bars.map(b => b[3]));
  const step = Math.max(0.25, Math.ceil((hi - lo) / 22 / 0.25) * 0.25), top = Math.ceil(hi / step) * step, nRows = Math.round((top - Math.floor(lo / step) * step) / step) + 1;
  const cols = bars.map(b => {
    const m0 = FC.toMin(b[0]), cells = new Array(nRows).fill(null).map(() => [0, 0]);
    for (let m = m0; m < m0 + tf; m++) { const ms = FC.minStr(m); if (ms >= end) break; (byMin[ms] || []).forEach(([p, bb, aa]) => { const i = Math.round((top - Math.floor(p / step) * step) / step); if (i >= 0 && i < nRows) { cells[i][0] += bb; cells[i][1] += aa; } }); }
    const dl = cells.reduce((s, x) => s + x[1] - x[0], 0);
    return { t: b[0].slice(11, 16), cells, dl, h: b[2], l: b[3] };
  });
  return `<div class="fp">${cols.map(c => `<div class="c"><div class="h">${c.t}</div>${c.cells.map(([b, a], i) => { const p = top - i * step, inBar = p <= c.h + step && p >= c.l - step && (b || a);
      return inBar ? `<div class="x"><span style="${b >= 3 * Math.max(1, a) ? 'color:var(--sellT);font-weight:700' : ''}">${b}</span><span style="${a >= 3 * Math.max(1, b) ? 'color:var(--buyT);font-weight:700' : ''}">${a}</span></div>` : '<div class="x blank"><span>&nbsp;</span><span></span></div>'; }).join('')}
      <div class="h ${cls(c.dl)}" style="margin-top:4px">Δ ${c.dl > 0 ? '+' : ''}${c.dl}</div></div>`).join('')}
    <div class="c px"><div class="h">&nbsp;</div>${new Array(nRows).fill(0).map((_, i) => `<div>${fmt(top - i * step)}</div>`).join('')}</div></div>
    <p class="note">Bid volume × ask volume at each price (${fmt(step)}-point rows). Bold: 3× imbalance (orange sellers, blue buyers).</p>`;
}
function rpMove(k) { const r = S.rp; r.k = Math.max(0, Math.min(r.n - 1, k)); S.ch.d.replay = r.k; drawChart(true); S.ch.follow(); }
function rpPlay(on) {
  const r = S.rp; r.playing = on;
  if (S.rpTimer) { clearInterval(S.rpTimer); S.rpTimer = null; }
  if (on) S.rpTimer = setInterval(() => { if (r.k >= r.n - 1) { rpPlay(false); return; } rpMove(r.k + 1); }, 1000 / r.speed);
  const tb = $('.transport'); if (tb) tb.outerHTML = replayBar();
}

/* ---------- events ---------- */
document.addEventListener('click', async e => {
  const el = e.target.closest('button, a[data-act]');
  if (!el || el.disabled) return;
  const ds = el.dataset, act = ds.act;
  try {
    if (ds.symc) { const s = new Set(S.dlSyms || ['NQ', 'ES']); s.has(ds.symc) ? s.delete(ds.symc) : s.add(ds.symc); S.dlSyms = [...s]; el.setAttribute('aria-pressed', s.has(ds.symc)); return; }
    if (ds.symf) { const s = new Set(S.fpSyms || ['NQ']); s.has(ds.symf) ? s.delete(ds.symf) : s.add(ds.symf); S.fpSyms = [...s]; el.setAttribute('aria-pressed', s.has(ds.symf)); return; }
    if (ds.dlq) { $('#dl-from').value = addDays(today(), +ds.dlq); return; }
    if (ds.ptick) { el.setAttribute('aria-pressed', el.getAttribute('aria-pressed') !== 'true'); return; }
    if (ds.filter) { S.filter = ds.filter; return vResearch(); }
    if (ds.setup) { S.newSetup = ds.setup; $('#newbox').innerHTML = newForm(); return; }
    if (ds.trset) { S.trSet = ds.trset; return vStrategy(S.cur.id); }
    if (ds.cellk) { const on = el.getAttribute('aria-pressed') !== 'true'; await api('/strategies/' + S.cur.id, { method: 'PUT', body: { cells: { [ds.cellk]: on } } }); return vStrategy(S.cur.id); }
    if (ds.csym) { S.chart.sym = ds.csym; S.chart.strat = ''; S.rp = null; return vChart(); }
    if (ds.ctf) { S.chart.tf = +ds.ctf; if (S.rp) { S.rp.k = null; S.rp.delta = null; } return vChart(); }
    if (ds.cdays) { S.chart.days = +ds.cdays; return vChart(); }
    if (ds.ctog) { S.chart[ds.ctog] = !S.chart[ds.ctog]; el.setAttribute('aria-pressed', S.chart[ds.ctog]); if (ds.ctog === 'rth') return loadChart(); return drawChart(true); }
    if (ds.rpspeed) { S.rp.speed = +ds.rpspeed; return rpPlay(S.rp.playing); }
    if (ds.rpview) { S.rpView = ds.rpview; return vChart(); }
    switch (act) {
      case 'refresh': await api('/data/refresh', { method: 'POST', body: {} }); toast('Updating the candles…'); if (S.page === 'data') vData(); break;
      case 'db-connect': { const k = $('#db-key').value.trim(); if (!k) return toast('Paste the key first.'); el.disabled = true; el.textContent = 'Checking…'; await api('/databento', { method: 'PUT', body: { key: k } }); toast('Databento connected.'); vData(); break; }
      case 'db-save': await api('/databento', { method: 'PUT', body: { auto: $('#db-auto').checked, autoFp: $('#db-autofp').checked, maxCost: +$('#db-cap').value } }); toast('Saved.'); vData(); break;
      case 'db-test': el.disabled = true; await api('/databento/test', { method: 'POST' }); toast('Databento answered.'); vData(); break;
      case 'db-del': if (!confirm('Disconnect Databento? The stored candles stay.')) return; await api('/databento', { method: 'DELETE' }); vData(); break;
      case 'est-c': case 'est-f': { const isC = act === 'est-c', out = $(isC ? '#est-c' : '#est-f'); out.textContent = 'Asking Databento…';
        const r = await api('/data/estimate', { method: 'POST', body: { kind: isC ? 'candles' : 'footprint', syms: isC ? (S.dlSyms || ['NQ', 'ES']) : (S.fpSyms || ['NQ']), from: $(isC ? '#dl-from' : '#fp-from').value, to: $(isC ? '#dl-to' : '#fp-to').value } });
        out.textContent = `Estimated cost: $${r.usd.toFixed(2)}${isC ? '' : ' (at most)'}`; break; }
      case 'dl-c': await download('candles'); break;
      case 'dl-f': await download('footprint'); break;
      case 'new': S.newSetup = S.newSetup === null ? '' : null; $('#newbox').innerHTML = S.newSetup !== null ? newForm() : ''; break;
      case 'new-close': S.newSetup = null; $('#newbox').innerHTML = ''; break;
      case 'create': { const p = readParams($('#newp')); const s = await api('/strategies', { method: 'POST', body: { setup: S.newSetup, name: $('#new-name').value, params: p } }); S.newSetup = null; S.strats = null; location.hash = '#strategy/' + s.id; break; }
      case 'run-study': case 'run-valid': await api(`/strategies/${S.cur.id}/run`, { method: 'POST', body: { step: act === 'run-study' ? 'study' : 'validate' } }); vStrategy(S.cur.id); break;
      case 'save-rules': await api('/strategies/' + S.cur.id, { method: 'PUT', body: { params: readParams($('#rules')) } }); toast('Rules saved: run the study again.'); vStrategy(S.cur.id); break;
      case 'save-costs': await api('/strategies/' + S.cur.id, { method: 'PUT', body: { contracts: +$('#pp-c').value, dailyLoss: +$('#pp-dl').value, comm: +$('#pp-comm').value, slip: +$('#pp-slip').value } }); toast('Saved.'); vStrategy(S.cur.id); break;
      case 'paper': case 'paper-stop': case 'retire': case 'unretire': await api(`/strategies/${S.cur.id}/action`, { method: 'POST', body: { action: act === 'paper-stop' ? 'stop' : act } }); S.strats = null; vStrategy(S.cur.id); break;
      case 'delete': if (!confirm('Delete this strategy and its trades?')) return; await api('/strategies/' + S.cur.id, { method: 'DELETE' }); S.strats = null; location.hash = '#research'; break;
      case 'open-chart': { e.preventDefault(); S.chart.strat = S.cur.id; S.chart.sym = S.cur.params.tickers[0]; const tf = S.cur.params.tf; S.chart.tf = tf === '1D' ? 60 : [1, 5, 15, 60].includes(+tf) ? +tf : 5; S.rp = null; location.hash = '#chart'; break; }
      case 'rp-start': { const day = $('#rp-day').value; if (!day) return toast('Pick a day.'); if (S.chart.tf > 5) S.chart.tf = 1; S.rp = { day, k: null, playing: false, speed: 2 }; vChart(); break; }
      case 'rp-stop': rpPlay(false); S.rp = null; vChart(); break;
      case 'rp-play': rpPlay(!S.rp.playing); break;
      case 'rp-back': rpPlay(false); rpMove(S.rp.k - 1); break;
      case 'rp-fwd': rpPlay(false); rpMove(S.rp.k + 1); break;
      case 'rp-getfp': { const r = await api('/data/download', { method: 'POST', body: { kind: 'footprint', syms: [S.chart.sym], from: S.rp.day, to: S.rp.day } });
        if (r.needConfirm && !confirm(`Databento estimates $${r.usd.toFixed(2)} for this day's trades. Download?`)) return;
        if (r.needConfirm) await api('/data/download', { method: 'POST', body: { kind: 'footprint', syms: [S.chart.sym], from: S.rp.day, to: S.rp.day, confirm: true } });
        toast('Downloading the trades… open the replay again in a minute.'); break; }
    }
  } catch (err) { toast(err.message); if (el.textContent === 'Checking…') { el.disabled = false; el.textContent = 'Connect'; } }
});
document.addEventListener('change', e => {
  if (e.target.id === 'c-strat') { S.chart.strat = e.target.value; loadChart(); }
  if (e.target.id === 'rp-scrub') { rpPlay(false); rpMove(+e.target.value); }
});
document.addEventListener('input', e => { if (e.target.id === 'rp-scrub' && S.rp) { S.rp.k = +e.target.value; S.ch.d.replay = S.rp.k; drawChart(true); } });
document.addEventListener('keydown', e => {
  if (!S.rp || S.page !== 'chart' || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (e.key === 'ArrowRight') { rpPlay(false); rpMove(S.rp.k + 1); } else if (e.key === 'ArrowLeft') { rpPlay(false); rpMove(S.rp.k - 1); } else if (e.key === ' ') { e.preventDefault(); rpPlay(!S.rp.playing); }
});

boot();
