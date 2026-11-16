// ETF: correlation matrix of daily returns (sector / industry / commodity / country ETFs; no index, no bond fund).
S.etf = S.etf || { days: 60, sort: 'group' };

async function vEtf() {
  const E = S.etf;
  view(`${head('ETF', 'How the ETFs move together: correlation of daily returns over the window. No broad index, no bond fund.')}<div id="etf-body"><div class="panel"><div class="empty">Loading prices…</div></div></div>`);
  let r;
  try { r = await api('/etf?days=' + E.days); }
  catch (e) { $('#etf-body').innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  E.r = r;
  $('#etf-body').innerHTML = etfHtml(r);
}

function etfColor(c) {                                 // blue = move together, red = opposite, grey = unrelated
  if (c == null) return 'transparent';
  const a = Math.min(1, Math.abs(c)) * 0.85;
  return c >= 0 ? `rgba(59,130,246,${a.toFixed(2)})` : `rgba(239,68,68,${a.toFixed(2)})`;
}

function etfHtml(r) {
  const E = S.etf, n = r.syms.length;
  let idx = [...Array(n).keys()];
  if (E.sort === 'group') idx = r.order;
  else if (E.sort === 'avg') idx.sort((a, b) => r.avg[r.syms[b]] - r.avg[r.syms[a]]);
  const pc = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
  const tabs = r.windows.map(d => `<button class="btn sm${E.days === d ? ' primary' : ''}" data-etf-days="${d}">${d} days</button>`).join('');
  const sorts = [['group', 'Group similar'], ['avg', 'By average correlation'], ['list', 'List order']]
    .map(([k, t]) => `<button class="btn sm${E.sort === k ? ' primary' : ''}" data-etf-sort="${k}">${t}</button>`).join('');
  const headRow = idx.map(i => `<th class="r mono" style="writing-mode:vertical-rl;transform:rotate(180deg);padding:6px 2px">${esc(r.syms[i])}</th>`).join('');
  const body = idx.map(i => `<tr><th class="mono" style="text-align:left;white-space:nowrap">${esc(r.syms[i])}</th>${idx.map(j => {
    const c = r.matrix[i][j];
    return `<td class="r mono" title="${esc(r.syms[i])} vs ${esc(r.syms[j])}: ${c == null ? '—' : c.toFixed(2)}" style="background:${i === j ? 'var(--line)' : etfColor(c)};font-size:12px;padding:6px 4px;min-width:40px">${i === j ? '' : c == null ? '—' : c.toFixed(2)}</td>`;
  }).join('')}<td class="r mono">${r.avg[r.syms[i]].toFixed(2)}</td><td class="r mono ${r.perf[r.syms[i]] >= 0 ? 'gain' : 'loss'}">${pc(r.perf[r.syms[i]])}</td><td class="r mono">${(r.vol[r.syms[i]] * 100).toFixed(0)}%</td></tr>`).join('');
  const pairs = [];
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) if (r.matrix[i][j] != null) pairs.push([r.syms[i], r.syms[j], r.matrix[i][j]]);
  pairs.sort((a, b) => b[2] - a[2]);
  const plist = xs => xs.map(([a, b, c]) => `<span class="mono">${esc(a)}–${esc(b)}</span> <b>${c.toFixed(2)}</b>`).join(' · ');
  return `<section class="panel"><div class="hd"><h2>Correlation matrix</h2><span class="dim" style="font-size:13px">${r.days} daily returns, ${esc(r.from)} → ${esc(r.to)}</span><div class="grow"></div>${tabs}</div>
    <div class="bd" style="padding-top:0;display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="dim" style="font-size:12px">Order:</span>${sorts}
      <span class="dim" style="font-size:12px;margin-left:12px">1 = move together · 0 = unrelated · −1 = opposite</span></div>
    <div class="tbl-wrap"><table class="tbl" style="width:auto"><thead><tr><th></th>${headRow}<th class="r">Avg</th><th class="r">Return</th><th class="r">Vol (yr)</th></tr></thead><tbody>${body}</tbody></table></div>
    <div class="note" style="padding:8px 16px 4px"><b>Most together:</b> ${plist(pairs.slice(0, 5))}</div>
    <div class="note" style="padding:0 16px 14px"><b>Least together:</b> ${plist(pairs.slice(-5).reverse())}${r.missing.length ? `<br><span class="loss">No prices: ${esc(r.missing.join('; '))}</span>` : ''}</div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>ETFs</h2><span class="dim" style="font-size:13px">one ticker per line or comma-separated (2–40)</span></div>
      <div class="bd"><textarea id="etf-syms" rows="3" style="width:100%" class="mono">${esc(r.settings.syms.join(', '))}</textarea>
      <div style="margin-top:8px;display:flex;gap:8px"><button class="btn primary" data-etf="save">Save and recompute</button><button class="btn" data-etf="reset">Default list</button></div></div></section>`;
}

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-etf-days],[data-etf-sort],[data-etf]');
  if (!el || !$('#etf-body')) return;
  const ds = el.dataset;
  try {
    if (ds.etfDays) { S.etf.days = +ds.etfDays; return vEtf(); }
    if (ds.etfSort) { S.etf.sort = ds.etfSort; $('#etf-body').innerHTML = etfHtml(S.etf.r); return; }
    if (ds.etf === 'save' || ds.etf === 'reset') {
      const syms = ds.etf === 'reset' ? ['XLK', 'SMH', 'XLC', 'XLY', 'XLF', 'XLI', 'XLB', 'XLE', 'XLV', 'XBI', 'XLP', 'XLU', 'XLRE', 'GLD', 'SLV', 'EEM', 'FXI']
        : $('#etf-syms').value.split(/[\s,;]+/).filter(Boolean);
      el.disabled = true;
      await api('/etf/settings', { method: 'PUT', body: { syms } });
      return vEtf();
    }
  } catch (e) { toast(e.message); el.disabled = false; }
});
