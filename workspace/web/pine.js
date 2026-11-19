// Pine Script (v5 / v6 subset) interpreter. No DOM: runs in the browser and in node (tests).
//   const prog = compile(source)            -> throws PineError {message, line}
//   const out  = run(prog, bars, opts)      -> {meta, inputs, plots, hlines, fills, shapes, bgcolors, barcolors, lines, labels, boxes, logs}
// bars: [{t (sec), o, h, l, c, v}], opts: {inputs: {index: value}, symbol, tf, tfSec, mintick}
// Supported: indicator()/strategy() header, var / varip, typed declarations, tuples, :=, += ..., if / else if / else,
// for (to / by / in), while, switch, break / continue, user functions (single line and block, per-call-site series),
// history [] on anything, ta.*, math.*, str.*, color.*, input.*, plot / plotshape / plotchar / hline / fill / bgcolor /
// barcolor, line.* / label.* / box.* drawings, array.*, method calls (x.push(...)), time / session helpers.

export class PineError extends Error { constructor(msg, line) { super(msg); this.line = line || 0; } }

// ------------------------------------------------------------------ lexer
const KW = new Set(['if', 'else', 'for', 'to', 'by', 'while', 'var', 'varip', 'and', 'or', 'not', 'switch', 'break', 'continue', 'in', 'import', 'export', 'method', 'type']);
const TYPES = new Set(['int', 'float', 'bool', 'string', 'color', 'line', 'label', 'box', 'table', 'array', 'series', 'simple', 'const', 'matrix', 'map', 'linefill', 'polyline', 'chart.point']);
const OPS3 = [':=', '+=', '-=', '*=', '/=', '%=', '==', '!=', '<=', '>=', '=>'];
const ENDS_CONT = /(\+|-|\*|\/|%|,|\?|:|\(|\[|\band|\bor|\bnot|==|!=|<=|>=|<|>|:=|(?<![=!<>:])=)\s*$/;

function stripComment(s) {
  let q = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (q) { if (ch === '\\') i++; else if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") q = ch;
    else if (ch === '/' && s[i + 1] === '/') return s.slice(0, i);
  }
  return s;
}
function depthOf(s) {
  let d = 0, q = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (q) { if (ch === '\\') i++; else if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") q = ch; else if (ch === '(' || ch === '[') d++; else if (ch === ')' || ch === ']') d--;
  }
  return d;
}
const indentOf = s => { let n = 0; for (const ch of s) { if (ch === ' ') n++; else if (ch === '\t') n += 4; else break; } return n; };

function lex(src) {
  const raw = src.replace(/\r/g, '').split('\n').map(stripComment);
  const logical = [];
  for (let k = 0; k < raw.length; k++) {
    const text = raw[k];
    if (!text.trim()) continue;
    if (/^\s*\/\/@/.test(raw[k])) continue;
    let ind = indentOf(text), body = text.trim(), line = k + 1, d = depthOf(body);
    // join continuation lines
    while (k + 1 < raw.length) {
      let j = k + 1; while (j < raw.length && !raw[j].trim()) j++;
      if (j >= raw.length) break;
      const nxt = raw[j], nind = indentOf(nxt);
      const cont = d > 0 || (ENDS_CONT.test(body) && !/=>\s*$/.test(body)) || (nind > ind && nind % 4 !== 0);
      if (!cont) break;
      body += ' ' + nxt.trim(); d += depthOf(nxt.trim()); k = j;
    }
    logical.push({ ind, body, line });
  }
  const toks = [], stack = [0];
  const push = (t, v, line) => toks.push({ t, v, line });
  for (const L of logical) {
    if (L.ind > stack[stack.length - 1]) { stack.push(L.ind); push('indent', null, L.line); }
    else while (L.ind < stack[stack.length - 1]) { stack.pop(); push('dedent', null, L.line); if (L.ind > stack[stack.length - 1]) throw new PineError('Bad indentation', L.line); }
    tokLine(L.body, L.line, push);
    push('nl', null, L.line);
  }
  while (stack.length > 1) { stack.pop(); push('dedent', null, 0); }
  push('eof', null, logical.length ? logical[logical.length - 1].line : 0);
  return toks;
}

function tokLine(s, line, push) {
  let i = 0;
  const idRe = /[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*/y, numRe = /(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/y, colRe = /#[0-9a-fA-F]{8}|#[0-9a-fA-F]{6}/y;
  while (i < s.length) {
    const ch = s[i];
    if (ch === ' ' || ch === '\t') { i++; continue; }
    if (ch === '"' || ch === "'") {
      let j = i + 1, out = '';
      while (j < s.length && s[j] !== ch) { if (s[j] === '\\') { j++; const e = s[j]; out += e === 'n' ? '\n' : e === 't' ? '\t' : e; } else out += s[j]; j++; }
      if (j >= s.length) throw new PineError('Unterminated string', line);
      push('str', out, line); i = j + 1; continue;
    }
    if (ch === '#') { colRe.lastIndex = i; const m = colRe.exec(s); if (!m) throw new PineError('Bad color literal', line); push('color', normColor(m[0]), line); i += m[0].length; continue; }
    if (/[0-9]/.test(ch) || (ch === '.' && /[0-9]/.test(s[i + 1]))) { numRe.lastIndex = i; const m = numRe.exec(s); push('num', parseFloat(m[0]), line); i += m[0].length; continue; }
    if (/[A-Za-z_]/.test(ch)) { idRe.lastIndex = i; const m = idRe.exec(s); push('id', m[0], line); i += m[0].length; continue; }
    const two = s.slice(i, i + 2);
    if (OPS3.includes(two)) { push('op', two, line); i += 2; continue; }
    if ('+-*/%<>=()[],?:.'.includes(ch)) { push('op', ch, line); i++; continue; }
    throw new PineError(`Unexpected character '${ch}'`, line);
  }
}

// ------------------------------------------------------------------ parser
class Parser {
  constructor(toks) { this.t = toks; this.p = 0; }
  peek(o = 0) { return this.t[this.p + o]; }
  next() { return this.t[this.p++]; }
  is(t, v, o = 0) { const k = this.peek(o); return k && k.t === t && (v === undefined || k.v === v); }
  op(v, o = 0) { return this.is('op', v, o); }
  kw(v, o = 0) { return this.is('id', v, o); }
  expect(t, v) { const k = this.next(); if (!k || k.t !== t || (v !== undefined && k.v !== v)) throw new PineError(`Expected ${v || t} but found ${k ? (k.v ?? k.t) : 'end'}`, k ? k.line : 0); return k; }
  skipNl() { while (this.is('nl')) this.p++; }

  program() { const out = []; this.skipNl(); while (!this.is('eof')) { if (this.is('dedent') || this.is('indent')) { this.p++; continue; } out.push(this.statement()); this.skipNl(); } return out; }
  block() { this.expect('indent'); const out = []; this.skipNl(); while (!this.is('dedent') && !this.is('eof')) { out.push(this.statement()); this.skipNl(); } if (this.is('dedent')) this.p++; return out; }
  endStmt(e) { if (e && e.nlDone) return; if (this.is('nl')) this.p++; else if (!this.is('eof') && !this.is('dedent')) { const k = this.peek(); throw new PineError(`Unexpected '${k.v ?? k.t}'`, k.line); } }

  statement() {
    const k = this.peek(), line = k.line;
    if (k.t === 'id') {
      switch (k.v) {
        case 'if': return this.ifStmt();
        case 'for': return this.forStmt();
        case 'while': { this.next(); const cond = this.expr(); this.expect('nl'); return { k: 'while', cond, body: this.block(), line }; }
        case 'switch': return this.switchExpr();
        case 'break': case 'continue': this.next(); this.endStmt(); return { k: k.v, line };
        case 'import': while (!this.is('nl') && !this.is('eof')) this.next(); this.endStmt(); return { k: 'nop', line };
        case 'type': throw new PineError('User-defined types are not supported yet', line);
        case 'export': this.next(); return this.statement();
        case 'method': { this.next(); const s = this.funcDef(); s.method = true; return s; }
        case 'var': case 'varip': { this.next(); this.skipType(); return this.decl(true); }
      }
      if (TYPES.has(k.v) && (this.is('id', undefined, 1) || this.op('<', 1))) { this.skipType(); if (this.op('[')) return this.tupleDecl(); return this.decl(false); }
      if (this.op('(', 1) && this.isFuncDef()) return this.funcDef();
      if (this.op('=', 1)) return this.decl(false);
      if (this.peek(1).t === 'op' && [':=', '+=', '-=', '*=', '/=', '%='].includes(this.peek(1).v)) {
        this.next(); const op = this.next().v; const value = this.exprOrBlock(); this.endStmt(value); return { k: 'assign', name: k.v, op, value, line };
      }
    }
    if (this.op('[') && this.isTupleDecl()) return this.tupleDecl();
    const e = this.exprOrBlock(); this.endStmt(e); return { k: 'expr', e, line };
  }
  skipType() {
    if (this.is('id') && TYPES.has(this.peek().v) && (this.is('id', undefined, 1) || this.op('<', 1))) {
      this.next(); if (this.op('<')) { while (!this.op('>')) this.next(); this.next(); }
    }
  }
  isFuncDef() { let j = this.p + 1, d = 0; for (; j < this.t.length; j++) { const k = this.t[j]; if (k.t === 'nl' || k.t === 'eof') return false; if (k.t === 'op' && k.v === '(') d++; if (k.t === 'op' && k.v === ')') { d--; if (d === 0) break; } } return this.t[j + 1] && this.t[j + 1].t === 'op' && this.t[j + 1].v === '=>'; }
  isTupleDecl() { let j = this.p, d = 0; for (; j < this.t.length; j++) { const k = this.t[j]; if (k.t === 'nl') return false; if (k.t === 'op' && k.v === '[') d++; if (k.t === 'op' && k.v === ']') { d--; if (d === 0) break; } } const n = this.t[j + 1]; return n && n.t === 'op' && (n.v === '=' || n.v === ':='); }
  tupleDecl() {
    const line = this.peek().line; this.expect('op', '['); const names = [];
    while (!this.op(']')) { names.push(this.expect('id').v); if (this.op(',')) this.next(); }
    this.next(); const op = this.next().v; const value = this.exprOrBlock(); this.endStmt(value);
    return { k: 'tuple', names, value, reassign: op === ':=', line };
  }
  decl(isVar) {
    const name = this.expect('id'); this.expect('op', '='); const value = this.exprOrBlock(); this.endStmt(value);
    return { k: 'decl', name: name.v, value, isVar, line: name.line };
  }
  funcDef() {
    const name = this.expect('id'), params = []; this.expect('op', '(');
    while (!this.op(')')) {
      this.skipType(); if (this.is('id') && TYPES.has(this.peek().v) && this.is('id', undefined, 1)) this.next();
      const p = { name: this.expect('id').v };
      if (this.op('=')) { this.next(); p.def = this.expr(); }
      params.push(p); if (this.op(',')) this.next();
    }
    this.next(); this.expect('op', '=>');
    let body;
    if (this.is('nl')) { this.next(); body = this.block(); }
    else { const e = this.exprOrBlock(); this.endStmt(e); body = [{ k: 'expr', e, line: name.line }]; }
    return { k: 'func', name: name.v, params, body, line: name.line };
  }
  ifStmt() {
    const line = this.next().line; const cond = this.expr(); this.expect('nl'); const body = this.block(); let els = null;
    this.skipNl();
    if (this.kw('else')) {
      this.next();
      if (this.kw('if')) els = [this.ifStmt()];
      else { this.expect('nl'); els = this.block(); }
    }
    return { k: 'if', cond, body, els, line };
  }
  forStmt() {
    const line = this.next().line;
    if (this.op('[')) {
      this.next(); const a = this.expect('id').v; this.expect('op', ','); const b = this.expect('id').v; this.expect('op', ']'); this.expect('id', 'in');
      const arr = this.expr(); this.expect('nl'); return { k: 'forin', idx: a, name: b, arr, body: this.block(), line };
    }
    const name = this.expect('id').v;
    if (this.kw('in')) { this.next(); const arr = this.expr(); this.expect('nl'); return { k: 'forin', name, arr, body: this.block(), line }; }
    this.expect('op', '='); const from = this.expr(); this.expect('id', 'to'); const to = this.expr(); let by = null;
    if (this.kw('by')) { this.next(); by = this.expr(); }
    this.expect('nl'); return { k: 'for', name, from, to, by, body: this.block(), line };
  }
  switchExpr() {
    const line = this.next().line; let subject = null;
    if (!this.is('nl')) subject = this.expr();
    this.expect('nl'); this.expect('indent'); const cases = [];
    this.skipNl();
    while (!this.is('dedent') && !this.is('eof')) {
      let cond = null;
      if (!this.op('=>')) cond = this.expr();
      this.expect('op', '=>');
      let body;
      if (this.is('nl')) { this.next(); body = this.block(); }
      else body = [this.statement()];
      cases.push({ cond, body }); this.skipNl();
    }
    if (this.is('dedent')) this.next();
    return { k: 'switch', subject, cases, line };
  }
  exprOrBlock() {
    if (this.kw('if')) { const s = this.ifStmt(); return { k: 'blockexpr', s, line: s.line, nlDone: true }; }
    if (this.kw('switch')) { const s = this.switchExpr(); return { k: 'blockexpr', s, line: s.line, nlDone: true }; }
    return this.expr();
  }

  expr() { return this.ternary(); }
  ternary() {
    const c = this.orE();
    if (this.op('?')) { const line = this.next().line; const a = this.ternary(); this.expect('op', ':'); const b = this.ternary(); return { k: 'tern', c, a, b, line }; }
    return c;
  }
  orE() { let l = this.andE(); while (this.kw('or')) { const line = this.next().line; l = { k: 'or', l, r: this.andE(), line }; } return l; }
  andE() { let l = this.eqE(); while (this.kw('and')) { const line = this.next().line; l = { k: 'and', l, r: this.eqE(), line }; } return l; }
  eqE() { let l = this.relE(); while (this.op('==') || this.op('!=')) { const t = this.next(); l = { k: 'bin', op: t.v, l, r: this.relE(), line: t.line }; } return l; }
  relE() { let l = this.addE(); while (this.op('<') || this.op('>') || this.op('<=') || this.op('>=')) { const t = this.next(); l = { k: 'bin', op: t.v, l, r: this.addE(), line: t.line }; } return l; }
  addE() { let l = this.mulE(); while (this.op('+') || this.op('-')) { const t = this.next(); l = { k: 'bin', op: t.v, l, r: this.mulE(), line: t.line }; } return l; }
  mulE() { let l = this.unary(); while (this.op('*') || this.op('/') || this.op('%')) { const t = this.next(); l = { k: 'bin', op: t.v, l, r: this.unary(), line: t.line }; } return l; }
  unary() {
    if (this.op('-') || this.op('+')) { const t = this.next(); return { k: 'neg', op: t.v, e: this.unary(), line: t.line }; }
    if (this.kw('not')) { const t = this.next(); return { k: 'not', e: this.unary(), line: t.line }; }
    return this.postfix();
  }
  postfix() {
    let e = this.primary();
    for (;;) {
      if (this.op('(') && e.k === 'id') { e = { k: 'call', name: e.name, args: this.args(), line: e.line }; continue; }
      if (this.op('[')) { const line = this.next().line; const idx = this.expr(); this.expect('op', ']'); e = { k: 'hist', e, idx, line }; continue; }
      if (this.op('.') && this.is('id', undefined, 1)) { // method on a call result: f(x).method(...)
        this.next(); const m = this.next(); if (this.op('(')) { e = { k: 'mcall', obj: e, name: m.v, args: this.args(), line: m.line }; continue; }
        e = { k: 'field', obj: e, name: m.v, line: m.line }; continue;
      }
      return e;
    }
  }
  args() {
    this.expect('op', '('); const pos = [], named = {};
    while (!this.op(')')) {
      if (this.is('id') && this.op('=', 1)) { const n = this.next().v; this.next(); named[n] = this.expr(); }
      else pos.push(this.expr());
      if (this.op(',')) this.next(); else if (!this.op(')')) { const k = this.peek(); throw new PineError(`Expected ',' or ')' but found ${k.v ?? k.t}`, k.line); }
    }
    this.next(); return { pos, named };
  }
  primary() {
    const k = this.next();
    if (!k) throw new PineError('Unexpected end of script', 0);
    if (k.t === 'num') return { k: 'lit', v: k.v, line: k.line };
    if (k.t === 'str') return { k: 'lit', v: k.v, line: k.line };
    if (k.t === 'color') return { k: 'lit', v: k.v, line: k.line };
    if (k.t === 'id') {
      if (k.v === 'true') return { k: 'lit', v: true, line: k.line };
      if (k.v === 'false') return { k: 'lit', v: false, line: k.line };
      if (k.v === 'na' && !this.op('(')) return { k: 'lit', v: NaN, line: k.line };
      if (KW.has(k.v) && !['to', 'by', 'in'].includes(k.v)) throw new PineError(`Unexpected keyword '${k.v}'`, k.line);
      if (/\.new$/.test(k.v) && this.op('<')) { while (!this.op('>')) this.next(); this.next(); }
      return { k: 'id', name: k.v, line: k.line };
    }
    if (k.t === 'op' && k.v === '(') { const e = this.expr(); this.expect('op', ')'); return e; }
    if (k.t === 'op' && k.v === '[') { const items = []; while (!this.op(']')) { items.push(this.expr()); if (this.op(',')) this.next(); } this.next(); return { k: 'tuplelit', items, line: k.line }; }
    throw new PineError(`Unexpected '${k.v ?? k.t}'`, k.line);
  }
}

// ------------------------------------------------------------------ values & helpers
const isna = v => v === undefined || v === null || (typeof v === 'number' && Number.isNaN(v));
const truthy = v => v === true || (typeof v === 'number' && v !== 0 && !Number.isNaN(v));
const num = v => typeof v === 'boolean' ? (v ? 1 : 0) : (typeof v === 'number' ? v : NaN);
const at = (a, j) => (j < 0 || a[j] === undefined) ? NaN : a[j];
const nz = (v, r = 0) => isna(v) ? r : v;
function normColor(c) {
  if (typeof c !== 'string') return c;
  if (/^#[0-9a-fA-F]{6}$/.test(c)) return (c + 'ff').toLowerCase();
  if (/^#[0-9a-fA-F]{8}$/.test(c)) return c.toLowerCase();
  return c;
}
const hex2 = n => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
function rgba(c) { c = normColor(c); if (typeof c !== 'string' || c[0] !== '#') return null; return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16), parseInt(c.slice(7, 9), 16)]; }
const mkColor = (r, g, b, a) => '#' + hex2(r) + hex2(g) + hex2(b) + hex2(a);
const COLORS = { aqua: '#00bcd4', black: '#363a45', blue: '#2962ff', fuchsia: '#e040fb', gray: '#787b86', green: '#4caf50', lime: '#00e676', maroon: '#880e4f', navy: '#311b92', olive: '#808000', orange: '#ff9800', purple: '#9c27b0', red: '#f23645', silver: '#b2b5be', teal: '#089981', white: '#ffffff', yellow: '#fdd835' };

let NYF = null;
function nyParts(t) {
  NYF = NYF || new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short' });
  const o = {}; for (const p of NYF.formatToParts(new Date(t * 1000))) o[p.type] = p.value;
  const wd = { Sun: 1, Mon: 2, Tue: 3, Wed: 4, Thu: 5, Fri: 6, Sat: 7 }[o.weekday];
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour, mi: +o.minute, s: +o.second, wd, key: (+o.year) * 10000 + (+o.month) * 100 + (+o.day) };
}

function fmtNum(v, f) {
  if (isna(v)) return 'NaN';
  if (typeof v !== 'number') return String(v);
  if (f === 'mintick' || f === undefined) return f === 'mintick' ? v.toFixed(2) : String(Math.round(v * 1e8) / 1e8);
  if (f === 'percent') return v.toFixed(2) + '%';
  if (f === 'volume') return Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(2) + 'K' : v.toFixed(0);
  if (typeof f === 'string' && /^[#0.,]+$/.test(f)) { const d = (f.split('.')[1] || '').length; return v.toFixed(d); }
  return String(v);
}

// ------------------------------------------------------------------ technical analysis cores (st = per call-site state)
const ser = (st, key, ctx, v) => { const a = st[key] || (st[key] = []); a[ctx.i] = v; return a; };
const sub = (st, k) => st[k] || (st[k] = {});
function rec(st, i, init) { if (st.li !== i) { st.p = st.li === undefined ? init : st.c; st.li = i; } }

function smaC(st, ctx, v, len) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.max(1, Math.round(len));
  if (i < len - 1) return NaN; let s = 0;
  for (let j = i - len + 1; j <= i; j++) { const x = a[j]; if (isna(x)) return NaN; s += x; }
  return s / len;
}
function sumC(st, ctx, v, len) { const r = smaC(st, ctx, v, len); return r * Math.round(len); }
function expC(st, ctx, v, len, alpha) {
  rec(st, ctx.i, NaN); const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.max(1, Math.round(len));
  if (isna(st.p)) {
    if (i < len - 1) { st.c = NaN; return NaN; }
    let s = 0; for (let j = i - len + 1; j <= i; j++) { if (isna(a[j])) { st.c = NaN; return NaN; } s += a[j]; }
    st.c = s / len;
  } else st.c = isna(v) ? st.p : alpha * v + (1 - alpha) * st.p;
  return st.c;
}
const emaC = (st, ctx, v, len) => expC(st, ctx, v, len, 2 / (Math.round(len) + 1));
const rmaC = (st, ctx, v, len) => expC(st, ctx, v, len, 1 / Math.round(len));
function wmaC(st, ctx, v, len) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.max(1, Math.round(len)); if (i < len - 1) return NaN;
  let s = 0, w = 0; for (let k = 0; k < len; k++) { const x = a[i - k]; if (isna(x)) return NaN; s += x * (len - k); w += len - k; } return s / w;
}
function stdevC(st, ctx, v, len) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.round(len); if (i < len - 1) return NaN;
  let m = 0; for (let j = i - len + 1; j <= i; j++) { if (isna(a[j])) return NaN; m += a[j]; } m /= len;
  let s = 0; for (let j = i - len + 1; j <= i; j++) s += (a[j] - m) ** 2; return Math.sqrt(s / len);
}
function devC(st, ctx, v, len) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.round(len); if (i < len - 1) return NaN;
  let m = 0; for (let j = i - len + 1; j <= i; j++) { if (isna(a[j])) return NaN; m += a[j]; } m /= len;
  let s = 0; for (let j = i - len + 1; j <= i; j++) s += Math.abs(a[j] - m); return s / len;
}
function extC(st, ctx, v, len, hi, bars) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.max(1, Math.round(len)); if (i < len - 1) return NaN;
  let best = NaN, bi = 0;
  for (let k = 0; k < len; k++) { const x = a[i - k]; if (isna(x)) continue; if (isna(best) || (hi ? x > best : x < best)) { best = x; bi = -k; } }
  return bars ? bi : best;
}
function trC(ctx, handleNa = true) { const { h, l, c } = ctx.B, i = ctx.i; if (i === 0) return handleNa ? h[0] - l[0] : NaN; return Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])); }
function rsiC(st, ctx, v, len) {
  const a = ser(st, 's', ctx, v), i = ctx.i, p = at(a, i - 1);
  const u = isna(p) || isna(v) ? NaN : Math.max(v - p, 0), d = isna(p) || isna(v) ? NaN : Math.max(p - v, 0);
  const ru = rmaC(sub(st, 'u'), ctx, u, len), rd = rmaC(sub(st, 'd'), ctx, d, len);
  if (isna(ru) || isna(rd)) return NaN; return rd === 0 ? 100 : ru === 0 ? 0 : 100 - 100 / (1 + ru / rd);
}
function crossC(st, ctx, x, y, mode) {
  const A = ser(st, 'x', ctx, num(x)), Bv = ser(st, 'y', ctx, num(y)), i = ctx.i; if (i < 1) return false;
  const a0 = A[i], b0 = Bv[i], a1 = at(A, i - 1), b1 = at(Bv, i - 1);
  if ([a0, b0, a1, b1].some(isna)) return false;
  const up = a0 > b0 && a1 <= b1, dn = a0 < b0 && a1 >= b1;
  return mode === 'over' ? up : mode === 'under' ? dn : (up || dn);
}
function pivotC(st, ctx, v, left, right, hi) {
  const a = ser(st, 'a', ctx, v), i = ctx.i, c = i - right; if (c - left < 0) return NaN;
  const x = a[c]; if (isna(x)) return NaN;
  for (let j = c - left; j <= i; j++) { if (j === c) continue; const y = a[j]; if (isna(y)) return NaN; if (hi ? (j < c ? y >= x : y > x) : (j < c ? y <= x : y < x)) return NaN; }
  return x;
}
function linregC(st, ctx, v, len, off) {
  const a = ser(st, 'a', ctx, v), i = ctx.i; len = Math.round(len); if (i < len - 1) return NaN;
  let sx = 0, sy = 0, sxy = 0, sxx = 0;
  for (let k = 0; k < len; k++) { const y = a[i - len + 1 + k]; if (isna(y)) return NaN; sx += k; sy += y; sxy += k * y; sxx += k * k; }
  const slope = (len * sxy - sx * sy) / (len * sxx - sx * sx), icp = (sy - slope * sx) / len;
  return icp + slope * (len - 1 - (off || 0));
}

// ------------------------------------------------------------------ builtin variables
const S = (fn) => ({ s: fn }); // series: fn(ctx, idx)
const BV = {
  open: S((c, j) => at(c.B.o, j)), high: S((c, j) => at(c.B.h, j)), low: S((c, j) => at(c.B.l, j)), close: S((c, j) => at(c.B.c, j)),
  volume: S((c, j) => at(c.B.v, j)), time: S((c, j) => at(c.B.t, j) * 1000), time_close: S((c, j) => (at(c.B.t, j) + c.tfSec) * 1000),
  hl2: S((c, j) => (at(c.B.h, j) + at(c.B.l, j)) / 2), hlc3: S((c, j) => (at(c.B.h, j) + at(c.B.l, j) + at(c.B.c, j)) / 3),
  ohlc4: S((c, j) => (at(c.B.o, j) + at(c.B.h, j) + at(c.B.l, j) + at(c.B.c, j)) / 4), hlcc4: S((c, j) => (at(c.B.h, j) + at(c.B.l, j) + 2 * at(c.B.c, j)) / 4),
  bar_index: S((c, j) => j), last_bar_index: S(c => c.n - 1), timenow: S(() => Date.now()),
  'ta.tr': S((c, j) => { const s = c.i; c.i = j; const r = trC(c, false); c.i = s; return r; }),
  'ta.obv': S((c, j) => c.pre('obv')[j]), 'ta.accdist': S((c, j) => c.pre('ad')[j]), 'ta.vwap': S((c, j) => c.pre('vwap')[j]),
  hour: S((c, j) => c.tp(j).h), minute: S((c, j) => c.tp(j).mi), second: S((c, j) => c.tp(j).s), dayofmonth: S((c, j) => c.tp(j).d),
  dayofweek: S((c, j) => c.tp(j).wd), month: S((c, j) => c.tp(j).mo), year: S((c, j) => c.tp(j).y), weekofyear: S((c, j) => { const p = c.tp(j); return Math.ceil(((Date.UTC(p.y, p.mo - 1, p.d) - Date.UTC(p.y, 0, 1)) / 864e5 + 1) / 7); }),
  'barstate.isfirst': S((c, j) => j === 0), 'barstate.islast': S((c, j) => j === c.n - 1), 'barstate.isconfirmed': S((c, j) => j < c.n - 1 || !c.live),
  'barstate.isrealtime': S((c, j) => c.live && j === c.n - 1), 'barstate.ishistory': S((c, j) => !(c.live && j === c.n - 1)), 'barstate.isnew': S(() => true), 'barstate.islastconfirmedhistory': S((c, j) => j === c.n - (c.live ? 2 : 1)),
  'timeframe.period': S(c => c.tf), 'timeframe.multiplier': S(c => c.tfMult), 'timeframe.isintraday': S(c => c.tfSec < 86400), 'timeframe.isdaily': S(c => c.tfSec === 86400 * (c.tfMult || 1) && /D$/.test(c.tf)),
  'timeframe.isweekly': S(c => /W$/.test(c.tf)), 'timeframe.ismonthly': S(c => /M$/.test(c.tf)), 'timeframe.isminutes': S(c => c.tfSec < 3600 || (c.tfSec < 86400 && !/h$/.test(c.tf))), 'timeframe.isseconds': S(() => false), 'timeframe.isdwm': S(c => c.tfSec >= 86400),
  'syminfo.ticker': S(c => c.symbol), 'syminfo.tickerid': S(c => c.symbol), 'syminfo.mintick': S(c => c.mintick), 'syminfo.pointvalue': S(() => 1), 'syminfo.currency': S(() => 'USD'), 'syminfo.timezone': S(() => 'America/New_York'), 'syminfo.type': S(c => c.symbol.includes('/') ? 'crypto' : 'stock'), 'syminfo.description': S(c => c.symbol),
  'math.pi': S(() => Math.PI), 'math.e': S(() => Math.E), 'math.phi': S(() => 1.618033988749895), 'math.rphi': S(() => 0.618033988749895),
};
const CONST = {
  'plot.style_line': 'line', 'plot.style_linebr': 'linebr', 'plot.style_stepline': 'stepline', 'plot.style_stepline_diamond': 'stepline', 'plot.style_histogram': 'histogram', 'plot.style_columns': 'columns', 'plot.style_circles': 'circles', 'plot.style_cross': 'cross', 'plot.style_area': 'area', 'plot.style_areabr': 'area',
  'shape.xcross': 'xcross', 'shape.cross': 'cross', 'shape.circle': 'circle', 'shape.triangleup': 'triangleup', 'shape.triangledown': 'triangledown', 'shape.flag': 'flag', 'shape.arrowup': 'arrowup', 'shape.arrowdown': 'arrowdown', 'shape.labelup': 'labelup', 'shape.labeldown': 'labeldown', 'shape.square': 'square', 'shape.diamond': 'diamond',
  'location.abovebar': 'abovebar', 'location.belowbar': 'belowbar', 'location.top': 'top', 'location.bottom': 'bottom', 'location.absolute': 'absolute',
  'size.auto': 'auto', 'size.tiny': 'tiny', 'size.small': 'small', 'size.normal': 'normal', 'size.large': 'large', 'size.huge': 'huge',
  'xloc.bar_index': 'bar_index', 'xloc.bar_time': 'bar_time', 'yloc.price': 'price', 'yloc.abovebar': 'abovebar', 'yloc.belowbar': 'belowbar',
  'extend.none': 'none', 'extend.left': 'left', 'extend.right': 'right', 'extend.both': 'both',
  'line.style_solid': 'solid', 'line.style_dashed': 'dashed', 'line.style_dotted': 'dotted', 'line.style_arrow_right': 'solid', 'line.style_arrow_left': 'solid', 'line.style_arrow_both': 'solid',
  'hline.style_solid': 'solid', 'hline.style_dashed': 'dashed', 'hline.style_dotted': 'dotted',
  'label.style_label_down': 'label_down', 'label.style_label_up': 'label_up', 'label.style_label_left': 'label_left', 'label.style_label_right': 'label_right', 'label.style_label_center': 'label_center', 'label.style_none': 'none', 'label.style_text_outline': 'none',
  'label.style_circle': 'circle', 'label.style_triangleup': 'triangleup', 'label.style_triangledown': 'triangledown', 'label.style_xcross': 'xcross', 'label.style_cross': 'cross', 'label.style_diamond': 'diamond', 'label.style_square': 'square', 'label.style_flag': 'flag', 'label.style_arrowup': 'arrowup', 'label.style_arrowdown': 'arrowdown', 'label.style_label_lower_left': 'label_up', 'label.style_label_lower_right': 'label_up', 'label.style_label_upper_left': 'label_down', 'label.style_label_upper_right': 'label_down',
  'text.align_left': 'left', 'text.align_center': 'center', 'text.align_right': 'right', 'text.align_top': 'top', 'text.align_bottom': 'bottom',
  'format.price': 'price', 'format.volume': 'volume', 'format.percent': 'percent', 'format.mintick': 'mintick', 'format.inherit': 'inherit',
  'scale.right': 'right', 'scale.left': 'left', 'scale.none': 'none', 'display.all': 'all', 'display.none': 'none', 'display.pane': 'pane', 'display.data_window': 'all', 'display.status_line': 'all', 'display.price_scale': 'all',
  'barmerge.gaps_off': false, 'barmerge.gaps_on': true, 'barmerge.lookahead_off': false, 'barmerge.lookahead_on': true,
  'order.ascending': 'asc', 'order.descending': 'desc', 'position.top_right': 'top_right', 'position.top_left': 'top_left', 'position.bottom_right': 'bottom_right', 'position.bottom_left': 'bottom_left',
  'dayofweek.sunday': 1, 'dayofweek.monday': 2, 'dayofweek.tuesday': 3, 'dayofweek.wednesday': 4, 'dayofweek.thursday': 5, 'dayofweek.friday': 6, 'dayofweek.saturday': 7,
  'strategy.long': 'long', 'strategy.short': 'short', 'alert.freq_once_per_bar': 1, 'alert.freq_once_per_bar_close': 2, 'alert.freq_all': 3,
  'font.family_default': 'default', 'font.family_monospace': 'mono', 'adjustment.none': 'none', 'adjustment.splits': 'splits', 'session.regular': 'regular', 'session.extended': 'extended',
};
for (const [k, v] of Object.entries(COLORS)) CONST['color.' + k] = normColor(v);

// ------------------------------------------------------------------ builtin functions
// F[name] = [paramNames, fn(ctx, args, st, nodeId)]
const F = {};
const def = (name, params, fn) => { F[name] = [params.split(',').map(s => s.trim()).filter(Boolean), fn]; };
const tup = (...xs) => xs;
const MAXOBJ = { line: 50, label: 50, box: 50 };

// ta
def('ta.sma', 'source,length', (c, a, st) => smaC(st, c, num(a[0]), a[1]));
def('ta.ema', 'source,length', (c, a, st) => emaC(st, c, num(a[0]), a[1]));
def('ta.rma', 'source,length', (c, a, st) => rmaC(st, c, num(a[0]), a[1]));
def('ta.wma', 'source,length', (c, a, st) => wmaC(st, c, num(a[0]), a[1]));
def('ta.vwma', 'source,length', (c, a, st) => { const vol = c.B.v[c.i]; return smaC(sub(st, 'a'), c, num(a[0]) * vol, a[1]) / smaC(sub(st, 'b'), c, vol, a[1]); });
def('ta.hma', 'source,length', (c, a, st) => { const n = Math.round(a[1]); const x = 2 * wmaC(sub(st, 'a'), c, num(a[0]), Math.floor(n / 2)) - wmaC(sub(st, 'b'), c, num(a[0]), n); return wmaC(sub(st, 'c'), c, x, Math.floor(Math.sqrt(n))); });
def('ta.swma', 'source', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])), i = c.i; return i < 3 ? NaN : (A[i - 3] * 1 + A[i - 2] * 2 + A[i - 1] * 2 + A[i]) / 6; });
def('ta.alma', 'series,length,offset,sigma', (c, a, st) => {
  const A = ser(st, 'a', c, num(a[0])), i = c.i, len = Math.round(a[1]); if (i < len - 1) return NaN;
  const m = a[2] * (len - 1), s = len / a[3]; let w = 0, n = 0;
  for (let k = 0; k < len; k++) { const wt = Math.exp(-((k - m) ** 2) / (2 * s * s)); n += wt * A[i - len + 1 + k]; w += wt; } return n / w;
});
def('ta.stdev', 'source,length,biased', (c, a, st) => stdevC(st, c, num(a[0]), a[1]));
def('ta.variance', 'source,length', (c, a, st) => stdevC(st, c, num(a[0]), a[1]) ** 2);
def('ta.dev', 'source,length', (c, a, st) => devC(st, c, num(a[0]), a[1]));
def('ta.highest', 'source,length', (c, a, st) => a[1] === undefined ? extC(st, c, c.B.h[c.i], a[0], true) : extC(st, c, num(a[0]), a[1], true));
def('ta.lowest', 'source,length', (c, a, st) => a[1] === undefined ? extC(st, c, c.B.l[c.i], a[0], false) : extC(st, c, num(a[0]), a[1], false));
def('ta.highestbars', 'source,length', (c, a, st) => a[1] === undefined ? extC(st, c, c.B.h[c.i], a[0], true, true) : extC(st, c, num(a[0]), a[1], true, true));
def('ta.lowestbars', 'source,length', (c, a, st) => a[1] === undefined ? extC(st, c, c.B.l[c.i], a[0], false, true) : extC(st, c, num(a[0]), a[1], false, true));
def('ta.change', 'source,length', (c, a, st) => { const len = a[1] === undefined ? 1 : Math.round(a[1]); const v = a[0]; if (typeof v === 'boolean') { const A = ser(st, 'a', c, v); return c.i >= len ? v !== A[c.i - len] : false; } const A = ser(st, 'a', c, num(v)); return num(v) - at(A, c.i - len); });
def('ta.mom', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])); return num(a[0]) - at(A, c.i - Math.round(a[1])); });
def('ta.roc', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])), p = at(A, c.i - Math.round(a[1])); return 100 * (num(a[0]) - p) / p; });
def('ta.rsi', 'source,length', (c, a, st) => rsiC(st, c, num(a[0]), a[1]));
def('ta.tr', 'handle_na', (c, a) => trC(c, a[0] === undefined ? false : a[0]));
def('ta.atr', 'length', (c, a, st) => rmaC(st, c, trC(c, true), a[0]));
def('ta.crossover', 'source1,source2', (c, a, st) => crossC(st, c, a[0], a[1], 'over'));
def('ta.crossunder', 'source1,source2', (c, a, st) => crossC(st, c, a[0], a[1], 'under'));
def('ta.cross', 'source1,source2', (c, a, st) => crossC(st, c, a[0], a[1], 'any'));
def('ta.cum', 'source', (c, a, st) => { rec(st, c.i, 0); st.c = st.p + nz(num(a[0])); return st.c; });
def('ta.vwap', 'source,anchor,stdev_mult', (c, a, st) => {
  rec(st, c.i, null); const src = isna(a[0]) ? (c.B.h[c.i] + c.B.l[c.i] + c.B.c[c.i]) / 3 : num(a[0]), v = c.B.v[c.i];
  const reset = isna(a[1]) ? (c.i === 0 || c.tp(c.i).key !== c.tp(c.i - 1).key) : truthy(a[1]);
  const p = reset || !st.p ? { pv: 0, vv: 0, p2: 0 } : st.p; st.c = { pv: p.pv + src * v, vv: p.vv + v, p2: p.p2 + src * src * v };
  const vw = st.c.vv ? st.c.pv / st.c.vv : src;
  if (a[2] !== undefined) { const sd = Math.sqrt(Math.max(0, st.c.p2 / (st.c.vv || 1) - vw * vw)); return tup(vw, vw + sd * a[2], vw - sd * a[2]); }
  return vw;
});
def('ta.macd', 'source,fastlen,slowlen,siglen', (c, a, st) => { const m = emaC(sub(st, 'f'), c, num(a[0]), a[1]) - emaC(sub(st, 's'), c, num(a[0]), a[2]); const s = emaC(sub(st, 'g'), c, m, a[3]); return tup(m, s, m - s); });
def('ta.bb', 'series,length,mult', (c, a, st) => { const m = smaC(sub(st, 'm'), c, num(a[0]), a[1]), d = a[2] * stdevC(sub(st, 'd'), c, num(a[0]), a[1]); return tup(m, m + d, m - d); });
def('ta.bbw', 'series,length,mult', (c, a, st) => { const m = smaC(sub(st, 'm'), c, num(a[0]), a[1]), d = a[2] * stdevC(sub(st, 'd'), c, num(a[0]), a[1]); return 2 * d / m; });
def('ta.kc', 'series,length,mult,useTrueRange', (c, a, st) => { const m = emaC(sub(st, 'm'), c, num(a[0]), a[1]); const r = a[3] === false ? c.B.h[c.i] - c.B.l[c.i] : trC(c, true); const e = emaC(sub(st, 'r'), c, r, a[1]); return tup(m, m + e * a[2], m - e * a[2]); });
def('ta.stoch', 'source,high,low,length', (c, a, st) => { const hh = extC(sub(st, 'h'), c, num(a[1]), a[3], true), ll = extC(sub(st, 'l'), c, num(a[2]), a[3], false); return 100 * (num(a[0]) - ll) / (hh - ll); });
def('ta.cci', 'source,length', (c, a, st) => { const m = smaC(sub(st, 'm'), c, num(a[0]), a[1]), d = devC(sub(st, 'd'), c, num(a[0]), a[1]); return (num(a[0]) - m) / (0.015 * d); });
def('ta.mfi', 'series,length', (c, a, st) => {
  const A = ser(st, 's', c, num(a[0])), ch = num(a[0]) - at(A, c.i - 1), v = c.B.v[c.i];
  const up = sumC(sub(st, 'u'), c, isna(ch) ? NaN : (ch <= 0 ? 0 : v * num(a[0])), a[1]), dn = sumC(sub(st, 'd'), c, isna(ch) ? NaN : (ch >= 0 ? 0 : v * num(a[0])), a[1]);
  return dn === 0 ? 100 : 100 - 100 / (1 + up / dn);
});
def('ta.wpr', 'length', (c, a, st) => { const hh = extC(sub(st, 'h'), c, c.B.h[c.i], a[0], true), ll = extC(sub(st, 'l'), c, c.B.l[c.i], a[0], false); return 100 * (c.B.c[c.i] - hh) / (hh - ll); });
def('ta.supertrend', 'factor,atrPeriod', (c, a, st) => {
  rec(st, c.i, null); const i = c.i, src = (c.B.h[i] + c.B.l[i]) / 2, atr = rmaC(sub(st, 'atr'), c, trC(c, true), a[1]);
  const p = st.p || { lb: NaN, ub: NaN, st: NaN, atr: NaN }, close1 = at(c.B.c, i - 1), close = c.B.c[i];
  let ub = src + a[0] * atr, lb = src - a[0] * atr; const plb = nz(p.lb), pub = nz(p.ub);
  lb = lb > plb || close1 < plb ? lb : plb; ub = ub < pub || close1 > pub ? ub : pub;
  let dir; if (isna(p.atr)) dir = 1; else if (p.st === p.ub) dir = close > ub ? -1 : 1; else dir = close < lb ? 1 : -1;
  const s = dir === -1 ? lb : ub; st.c = { lb, ub, st: s, atr }; return tup(s, dir);
});
def('ta.sar', 'start,inc,max', (c, a, st) => {
  rec(st, c.i, null); const i = c.i, { h, l, c: cl } = c.B; let p = st.p;
  if (i === 0) { st.c = null; return NaN; }
  if (!p) { const up = cl[i] >= cl[i - 1]; st.c = { up, sar: up ? Math.min(l[i - 1], l[i]) : Math.max(h[i - 1], h[i]), ep: up ? h[i] : l[i], af: a[0] }; return st.c.sar; }
  let { up, sar, ep, af } = p; sar = sar + af * (ep - sar);
  if (up) { sar = Math.min(sar, l[i - 1], at(l, i - 2) || l[i - 1]); if (l[i] < sar) { up = false; sar = ep; ep = l[i]; af = a[0]; } else if (h[i] > ep) { ep = h[i]; af = Math.min(af + a[1], a[2]); } }
  else { sar = Math.max(sar, h[i - 1], at(h, i - 2) || h[i - 1]); if (h[i] > sar) { up = true; sar = ep; ep = h[i]; af = a[0]; } else if (l[i] < ep) { ep = l[i]; af = Math.min(af + a[1], a[2]); } }
  st.c = { up, sar, ep, af }; return sar;
});
def('ta.pivothigh', 'source,leftbars,rightbars', (c, a, st) => a[2] === undefined ? pivotC(st, c, c.B.h[c.i], a[0], a[1], true) : pivotC(st, c, num(a[0]), a[1], a[2], true));
def('ta.pivotlow', 'source,leftbars,rightbars', (c, a, st) => a[2] === undefined ? pivotC(st, c, c.B.l[c.i], a[0], a[1], false) : pivotC(st, c, num(a[0]), a[1], a[2], false));
def('ta.valuewhen', 'condition,source,occurrence', (c, a, st) => {
  const L = st.L || (st.L = []); while (L.length && L[L.length - 1][0] === c.i) L.pop();
  if (truthy(a[0])) L.push([c.i, a[1]]); const o = Math.round(a[2] || 0); const e = L[L.length - 1 - o]; return e ? e[1] : NaN;
});
def('ta.barssince', 'condition', (c, a, st) => { rec(st, c.i, NaN); st.c = truthy(a[0]) ? c.i : st.p; return isna(st.c) ? NaN : c.i - st.c; });
def('ta.rising', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])); for (let k = 1; k <= a[1]; k++) if (!(A[c.i] > at(A, c.i - k))) return false; return true; });
def('ta.falling', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])); for (let k = 1; k <= a[1]; k++) if (!(A[c.i] < at(A, c.i - k))) return false; return true; });
def('ta.dmi', 'diLength,adxSmoothing', (c, a, st) => {
  const { h, l } = c.B, i = c.i, up = i ? h[i] - h[i - 1] : NaN, dn = i ? l[i - 1] - l[i] : NaN;
  const pdm = isna(up) ? NaN : (up > dn && up > 0 ? up : 0), mdm = isna(dn) ? NaN : (dn > up && dn > 0 ? dn : 0);
  const tr = rmaC(sub(st, 't'), c, trC(c, true), a[0]), p = 100 * rmaC(sub(st, 'p'), c, pdm, a[0]) / tr, m = 100 * rmaC(sub(st, 'm'), c, mdm, a[0]) / tr, s = p + m;
  return tup(p, m, 100 * rmaC(sub(st, 'x'), c, isna(s) ? NaN : Math.abs(p - m) / (s === 0 ? 1 : s), a[1]));
});
def('ta.linreg', 'source,length,offset', (c, a, st) => linregC(st, c, num(a[0]), a[1], a[2]));
def('ta.correlation', 'source1,source2,length', (c, a, st) => {
  const X = ser(st, 'x', c, num(a[0])), Y = ser(st, 'y', c, num(a[1])), i = c.i, n = Math.round(a[2]); if (i < n - 1) return NaN;
  let mx = 0, my = 0; for (let j = i - n + 1; j <= i; j++) { mx += X[j]; my += Y[j]; } mx /= n; my /= n;
  let sxy = 0, sxx = 0, syy = 0; for (let j = i - n + 1; j <= i; j++) { sxy += (X[j] - mx) * (Y[j] - my); sxx += (X[j] - mx) ** 2; syy += (Y[j] - my) ** 2; }
  return sxy / Math.sqrt(sxx * syy);
});
def('ta.median', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])), i = c.i, n = Math.round(a[1]); if (i < n - 1) return NaN; const w = A.slice(i - n + 1, i + 1).sort((x, y) => x - y); return n % 2 ? w[(n - 1) / 2] : (w[n / 2 - 1] + w[n / 2]) / 2; });
def('ta.percentrank', 'source,length', (c, a, st) => { const A = ser(st, 'a', c, num(a[0])), i = c.i, n = Math.round(a[1]); if (i < n) return NaN; let k = 0; for (let j = i - n; j < i; j++) if (A[j] <= A[i]) k++; return 100 * k / n; });
def('math.sum', 'source,length', (c, a, st) => sumC(st, c, num(a[0]), a[1]));
def('fixnan', 'source', (c, a, st) => { if (!isna(a[0])) st.v = a[0]; return st.v === undefined ? NaN : st.v; });
def('nz', 'source,replacement', (c, a) => nz(a[0], a[1] === undefined ? 0 : a[1]));
def('na', 'x', (c, a) => isna(a[0]));

// math
for (const fn of ['abs', 'ceil', 'floor', 'sqrt', 'exp', 'sign', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'log10']) def('math.' + fn, 'number', (c, a) => Math[fn](num(a[0])));
def('math.log', 'number', (c, a) => Math.log(num(a[0])));
def('math.round', 'number,precision', (c, a) => { const v = num(a[0]); if (a[1] === undefined) return Math.round(v); const m = 10 ** a[1]; return Math.round(v * m) / m; });
def('math.round_to_mintick', 'number', (c, a) => Math.round(num(a[0]) / c.mintick) * c.mintick);
def('math.pow', 'base,exponent', (c, a) => num(a[0]) ** num(a[1]));
def('math.max', '', (c, a) => { const v = a.map(num); return v.some(isna) ? NaN : Math.max(...v); });
def('math.min', '', (c, a) => { const v = a.map(num); return v.some(isna) ? NaN : Math.min(...v); });
def('math.avg', '', (c, a) => { const v = a.map(num); return v.reduce((x, y) => x + y, 0) / v.length; });
def('math.random', 'min,max,seed', (c, a) => { const lo = a[0] ?? 0, hi = a[1] ?? 1; return lo + Math.random() * (hi - lo); });
def('math.todegrees', 'radians', (c, a) => a[0] * 180 / Math.PI);
def('math.toradians', 'degrees', (c, a) => a[0] * Math.PI / 180);
def('int', 'x', (c, a) => isna(a[0]) ? NaN : Math.trunc(num(a[0])));
def('float', 'x', (c, a) => num(a[0]));
def('bool', 'x', (c, a) => truthy(a[0]));
def('string', 'x', (c, a) => String(a[0]));

// str
def('str.tostring', 'value,format', (c, a) => typeof a[0] === 'string' ? a[0] : typeof a[0] === 'boolean' ? String(a[0]) : fmtNum(a[0], a[1]));
def('str.format', '', (c, a) => String(a[0]).replace(/\{(\d+)(?:,[^}]*)?\}/g, (_, k) => { const v = a[+k + 1]; return typeof v === 'number' ? fmtNum(v) : String(v); }));
def('str.length', 'string', (c, a) => String(a[0]).length);
def('str.contains', 'source,str', (c, a) => String(a[0]).includes(a[1]));
def('str.startswith', 'source,str', (c, a) => String(a[0]).startsWith(a[1]));
def('str.endswith', 'source,str', (c, a) => String(a[0]).endsWith(a[1]));
def('str.upper', 'source', (c, a) => String(a[0]).toUpperCase());
def('str.lower', 'source', (c, a) => String(a[0]).toLowerCase());
def('str.replace_all', 'source,target,replacement', (c, a) => String(a[0]).split(a[1]).join(a[2]));
def('str.replace', 'source,target,replacement,occurrence', (c, a) => String(a[0]).replace(a[1], a[2]));
def('str.substring', 'source,begin_pos,end_pos', (c, a) => String(a[0]).substring(a[1], a[2]));
def('str.tonumber', 'string', (c, a) => parseFloat(a[0]));
def('str.split', 'string,separator', (c, a) => ({ kind: 'array', a: String(a[0]).split(a[1]) }));
def('str.trim', 'source', (c, a) => String(a[0]).trim());
def('str.repeat', 'source,repeat,separator', (c, a) => Array(Math.max(0, a[1] | 0)).fill(a[0]).join(a[2] || ''));
def('str.format_time', 'time,format,timezone', (c, a) => { const p = nyParts(a[0] / 1000); return `${p.y}-${String(p.mo).padStart(2, '0')}-${String(p.d).padStart(2, '0')} ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`; });

// color
def('color.new', 'color,transp', (c, a) => { const r = rgba(a[0]); if (!r) return a[0]; return mkColor(r[0], r[1], r[2], 255 * (1 - nz(a[1]) / 100)); });
def('color.rgb', 'red,green,blue,transp', (c, a) => mkColor(a[0], a[1], a[2], 255 * (1 - nz(a[3]) / 100)));
def('color.r', 'color', (c, a) => (rgba(a[0]) || [NaN])[0]);
def('color.g', 'color', (c, a) => (rgba(a[0]) || [0, NaN])[1]);
def('color.b', 'color', (c, a) => (rgba(a[0]) || [0, 0, NaN])[2]);
def('color.t', 'color', (c, a) => { const r = rgba(a[0]); return r ? 100 - r[3] / 2.55 : NaN; });
def('color.from_gradient', 'value,bottom_value,top_value,bottom_color,top_color', (c, a) => {
  const v = num(a[0]); if (isna(v)) return NaN; const f = Math.max(0, Math.min(1, (v - a[1]) / (a[2] - a[1]))); const x = rgba(a[3]), y = rgba(a[4]); if (!x || !y) return a[3];
  return mkColor(...x.map((q, k) => q + (y[k] - q) * f));
});

// time
def('time', 'timeframe,session', (c) => c.B.t[c.i] * 1000);
def('time_close', 'timeframe,session', (c) => (c.B.t[c.i] + c.tfSec) * 1000);
for (const [k, f] of Object.entries({ hour: 'h', minute: 'mi', second: 's', dayofmonth: 'd', dayofweek: 'wd', month: 'mo', year: 'y' })) def(k, 'time,timezone', (c, a) => nyParts(num(a[0]) / 1000)[f]);
def('timestamp', '', (c, a) => {
  const v = a.filter(x => typeof x === 'number'); if (v.length >= 3) { const [y, mo, d, h = 0, mi = 0, s = 0] = v; const guess = Date.UTC(y, mo - 1, d, h, mi, s) / 1000; const p = nyParts(guess); const off = (Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) / 1000) - guess; return (guess - off) * 1000; }
  return Date.parse(a[a.length - 1]) || NaN;
});
def('timeframe.in_seconds', 'timeframe', (c, a) => a[0] === undefined ? c.tfSec : tfSeconds(a[0]));
def('timeframe.change', 'timeframe', (c, a) => { const sec = tfSeconds(a[0] || 'D'), i = c.i; if (i === 0) return true; if (sec >= 86400) { const x = c.tp(i), y = c.tp(i - 1); return sec >= 2592000 ? x.mo !== y.mo : sec >= 604800 ? (x.wd < y.wd || x.key - y.key > 6) : x.key !== y.key; } return Math.floor(c.B.t[i] / sec) !== Math.floor(c.B.t[i - 1] / sec); });

// inputs
const inp = (type) => (c, a, st, id, node) => {
  if (st.has) return st.v;
  const named = a.__named || {};
  let idx = c.inputOrder.get(id); if (idx === undefined) { idx = c.inputOrder.size; c.inputOrder.set(id, idx); }
  let defval = a[0];
  if (type === 'source') defval = node.srcName || 'close';
  const spec = { idx, type, title: a[1] ?? named.title ?? (type === 'source' ? 'Source' : `Input ${idx + 1}`), defval, group: named.group, inline: named.inline, tooltip: named.tooltip,
    options: named.options ? named.options : undefined, minval: named.minval, maxval: named.maxval, step: named.step };
  c.out.inputs[idx] = spec;
  let v = c.inputValues && c.inputValues[idx] !== undefined ? c.inputValues[idx] : defval;
  if (type === 'int' && typeof v === 'number') v = Math.round(v);
  if (type === 'color') v = normColor(v);
  spec.value = v; st.has = true; st.v = v; return v;
};
for (const t of ['int', 'float', 'bool', 'string', 'color', 'source', 'timeframe', 'session', 'symbol', 'price', 'text_area', 'time', 'enum']) def('input.' + t, 'defval,title', inp(t));
def('input', 'defval,title', (c, a, st, id, node) => inp(typeof a[0] === 'boolean' ? 'bool' : typeof a[0] === 'string' && a[0][0] === '#' ? 'color' : typeof a[0] === 'string' ? 'string' : Number.isInteger(a[0]) ? 'int' : 'float')(c, a, st, id, node));

// declaration
const header = strat => (c, a) => {
  if (c.i !== 0) return NaN; const n = a.__named || {}, m = c.out.meta;
  m.title = a[0] ?? n.title ?? 'Script'; m.shorttitle = a[1] ?? n.shorttitle ?? m.title; m.overlay = truthy(a[2] ?? n.overlay ?? false);
  m.format = n.format; m.precision = n.precision; m.scale = n.scale; m.strategy = strat;
  for (const k of ['line', 'label', 'box']) if (n[`max_${k}s_count`] !== undefined) c.max[k] = Math.min(500, n[`max_${k}s_count`]);
  if (strat) c.strat = new Strat(c, n);
  return NaN;
};
def('indicator', 'title,shorttitle,overlay', header(false));
def('study', 'title,shorttitle,overlay', header(false));
def('strategy', 'title,shorttitle,overlay', header(true));
def('library', 'title', (c, a) => { c.out.meta.title = a[0]; return NaN; });

// ------------------------------------------------------------------ strategies (backtester)
// Orders placed on a bar fill from the next bar (market at its open; stop / limit when the bar's path reaches them),
// or at this bar's close with process_orders_on_close = true. Intrabar path: open -> nearer extreme -> other extreme -> close.
// Stops and limits that the open gaps through fill at the open. Slippage (ticks) applies to market and stop fills.
const SC = { 'strategy.fixed': 'fixed', 'strategy.cash': 'cash', 'strategy.percent_of_equity': 'percent_of_equity',
  'strategy.commission.percent': 'percent', 'strategy.commission.cash_per_contract': 'cash_per_contract', 'strategy.commission.cash_per_order': 'cash_per_order',
  'strategy.direction.long': 'long', 'strategy.direction.short': 'short', 'strategy.direction.all': 'all', 'strategy.oca.none': 'none', 'strategy.oca.cancel': 'cancel', 'strategy.oca.reduce': 'reduce' };
Object.assign(CONST, SC);
class Strat {
  constructor(c, n) {
    this.c = c; this.B = c.B; this.tick = c.mintick;
    this.cap = n.initial_capital ?? 100000; this.qtyType = n.default_qty_type ?? 'fixed'; this.qtyVal = n.default_qty_value ?? 1;
    this.pyr = n.pyramiding ?? 0; this.comType = n.commission_type ?? 'percent'; this.comVal = n.commission_value ?? 0;
    this.slip = n.slippage ?? 0; this.poc = !!n.process_orders_on_close; this.dirAllowed = 'all';
    if (c.stratOverrides) Object.assign(this, c.stratOverrides);
    this.lots = []; this.orders = []; this.trades = []; this.net = 0; this.eq = new Array(c.n).fill(NaN); this.fills = [];
  }
  size() { return this.lots.reduce((s, l) => s + l.qty * l.dir, 0); }
  avg() { const q = this.lots.reduce((s, l) => s + l.qty, 0); return q ? this.lots.reduce((s, l) => s + l.qty * l.px, 0) / q : NaN; }
  openPnl(px) { return this.lots.reduce((s, l) => s + (px - l.px) * l.qty * l.dir, 0); }
  equity(px) { return this.cap + this.net + this.openPnl(px); }
  com(qty, px) { return this.comType === 'percent' ? Math.abs(qty * px) * this.comVal / 100 : this.comType === 'cash_per_contract' ? qty * this.comVal : this.comVal; }
  qtyFor(q, px) {
    if (!isna(q) && q !== undefined) return q;
    if (this.qtyType === 'cash') return Math.floor(this.qtyVal / px);
    if (this.qtyType === 'percent_of_equity') return Math.floor(this.equity(px) * this.qtyVal / 100 / px);
    return this.qtyVal;
  }
  place(o) {
    o.placed = this.c.i;
    if (o.kind === 'exit') { this.orders = this.orders.filter(x => !(x.kind === 'exit' && x.id === o.id && x.from === o.from)); }
    else if (o.id !== undefined) this.orders = this.orders.filter(x => !(x.kind === o.kind && x.id === o.id));
    this.orders.push(o);
    if (o.immediately) this.process(this.c.i, 'close');
  }
  // levels on the bar's path: when (0..3) the price first reaches `lvl` going `up` (true: price >= lvl)
  when(path, lvl, up, from) {
    for (let k = 0; k < 3; k++) {
      const a = path[k], b = path[k + 1], t0 = k, t1 = k + 1; if (t1 <= from) continue;
      const startT = Math.max(t0, from), sa = a + (b - a) * (startT - t0);
      if (up ? sa >= lvl : sa <= lvl) return { t: startT, px: startT === 0 ? sa : lvl, gap: startT === 0 };
      if (up ? b >= lvl : b <= lvl) return { t: t0 + (lvl - a) / ((b - a) || 1), px: lvl, gap: false };
    }
    return null;
  }
  exitLevels(x) {
    const lots = this.lots.filter(l => x.from === undefined || isna(x.from) || l.id === x.from); if (!lots.length) return null;
    const dir = lots[0].dir, px = lots.reduce((s, l) => s + l.px * l.qty, 0) / lots.reduce((s, l) => s + l.qty, 0);
    let lim = x.limit, st = x.stop;
    if (isna(lim) && !isna(x.profit)) lim = px + dir * x.profit * this.tick;
    if (isna(st) && !isna(x.loss)) st = px - dir * x.loss * this.tick;
    return { lots, dir, lim, st };
  }
  process(i, at) {
    const B = this.B, o = B.o[i], h = B.h[i], l = B.l[i], cl = B.c[i];
    const path = at === 'close' ? [cl, cl, cl, cl] : (h - o < o - l ? [o, h, l, cl] : [o, l, h, cl]);
    let tNow = 0;
    for (let guard = 0; guard < 50; guard++) {
      let best = null;
      for (const x of this.orders) {
        if (at !== 'close' && x.placed >= i) continue;               // placed on this bar: fills from the next one
        if (at === 'close' && !(x.placed === i && (this.poc || x.immediately))) continue;
        let ev = null;
        if (x.kind === 'entry' || x.kind === 'order') {
          const up = x.dir > 0;
          if (isna(x.limit) && isna(x.stop)) ev = tNow === 0 ? { t: 0, px: path[0] + (at === 'close' ? 0 : x.dir * this.slip * this.tick), market: true } : null;
          else {
            const s = !isna(x.stop) ? this.when(path, x.stop, up, tNow) : null, li = !isna(x.limit) ? this.when(path, x.limit, !up, tNow) : null;
            if (s && li) ev = s.t <= li.t ? s : li; else ev = s || li;
            if (ev && s === ev) ev = { ...ev, px: ev.px + x.dir * this.slip * this.tick };
          }
        } else if (x.kind === 'close') ev = tNow === 0 || at === 'close' ? { t: tNow, px: path[0] - (this.size() > 0 ? 1 : -1) * (at === 'close' ? 0 : this.slip * this.tick), market: true } : null;
        else if (x.kind === 'exit') {
          const L = this.exitLevels(x); if (!L) continue;
          const s = !isna(L.st) ? this.when(path, L.st, L.dir < 0, tNow) : null, li = !isna(L.lim) ? this.when(path, L.lim, L.dir > 0, tNow) : null;
          ev = s && li ? (s.t <= li.t ? s : li) : (s || li);
          if (ev && ev === s) ev = { ...ev, px: ev.px - L.dir * this.slip * this.tick, why: x.comment_loss || x.comment || x.id };
          else if (ev) ev = { ...ev, why: x.comment_profit || x.comment || x.id };
        }
        if (ev && (!best || ev.t < best.ev.t)) best = { x, ev };
      }
      if (!best) break;
      tNow = best.ev.t; this.exec(best.x, best.ev.px, i, best.ev.why);
    }
  }
  exec(x, px, i, why) {
    const t = this.B.t[i];
    this.orders = this.orders.filter(y => y !== x);
    if (x.kind === 'entry' || x.kind === 'order') {
      if (this.dirAllowed !== 'all' && this.dirAllowed !== (x.dir > 0 ? 'long' : 'short') && x.kind === 'entry') { if (this.size() * x.dir < 0) this.closeLots(this.lots.slice(), px, i, 'Direction filter'); return; }
      const sz = this.size();
      if (x.kind === 'entry' && sz * x.dir < 0) this.closeLots(this.lots.slice(), px, i, x.comment || x.id);   // reverse
      else if (x.kind === 'entry' && sz * x.dir > 0 && this.lots.length > this.pyr) return;                  // pyramiding limit
      let q = this.qtyFor(x.qty, px); if (!(q > 0)) return;
      if (x.kind === 'order' && sz * x.dir < 0) { const rest = this.reduce(q, px, i, x.comment || x.id); q = rest; if (!(q > 0)) return; }
      this.lots.push({ id: x.id, dir: x.dir, qty: q, px, bar: i, t, com: this.com(q, px), comment: x.comment });
      this.fills.push({ bar: i, t, px, qty: q, dir: x.dir, id: x.id, kind: 'entry' });
    } else if (x.kind === 'close') {
      const lots = this.lots.filter(l => x.id === undefined || l.id === x.id); this.closeLots(lots, px, i, x.comment || 'Close', x.qty);
    } else if (x.kind === 'exit') {
      const L = this.exitLevels(x); if (!L) return; this.closeLots(L.lots, px, i, why || x.id, isna(x.qty) ? (isna(x.qtyPct) ? undefined : L.lots.reduce((s, l) => s + l.qty, 0) * x.qtyPct / 100) : x.qty);
    }
    if (!this.lots.length) this.orders = this.orders.filter(y => y.kind !== 'exit');
  }
  reduce(q, px, i, why) { let rest = q; for (const l of this.lots.slice()) { if (rest <= 0) break; const k = Math.min(rest, l.qty); this.closeLots([l], px, i, why, k); rest -= k; } return rest; }
  closeLots(lots, px, i, why, qty) {
    let left = qty === undefined ? Infinity : qty;
    for (const l of lots) {
      if (left <= 0) break; const k = Math.min(l.qty, left); left -= k;
      const comIn = l.com * k / l.qty, comOut = this.com(k, px), pnl = (px - l.px) * k * l.dir - comIn - comOut;
      this.net += pnl;
      this.trades.push({ id: l.id, dir: l.dir, qty: k, entryBar: l.bar, entryTime: l.t, entryPx: l.px, exitBar: i, exitTime: this.B.t[i], exitPx: px, pnl, pct: (px / l.px - 1) * 100 * l.dir, why, comment: l.comment });
      this.fills.push({ bar: i, t: this.B.t[i], px, qty: k, dir: -l.dir, id: why, kind: 'exit' });
      l.com -= comIn; l.qty -= k;
      if (l.qty <= 1e-12) this.lots.splice(this.lots.indexOf(l), 1);
    }
  }
}
const dirOf = d => d === 'long' || d === 1 || d === true ? 1 : -1;
def('strategy.entry', 'id,direction,qty,limit,stop,oca_name,oca_type,comment,alert_message,disable_alert', (c, a) => { if (c.strat) c.strat.place({ kind: 'entry', id: a[0], dir: dirOf(a[1]), qty: a[2], limit: num(a[3] ?? NaN), stop: num(a[4] ?? NaN), comment: a[7] }); return NaN; });
def('strategy.order', 'id,direction,qty,limit,stop,oca_name,oca_type,comment', (c, a) => { if (c.strat) c.strat.place({ kind: 'order', id: a[0], dir: dirOf(a[1]), qty: a[2], limit: num(a[3] ?? NaN), stop: num(a[4] ?? NaN), comment: a[7] }); return NaN; });
def('strategy.exit', 'id,from_entry,qty,qty_percent,profit,limit,loss,stop,trail_price,trail_points,trail_offset,oca_name,comment,comment_profit,comment_loss', (c, a) => {
  if (c.strat) c.strat.place({ kind: 'exit', id: a[0], from: a[1], qty: a[2] ?? NaN, qtyPct: a[3] ?? NaN, profit: num(a[4] ?? NaN), limit: num(a[5] ?? NaN), loss: num(a[6] ?? NaN), stop: num(a[7] ?? NaN), comment: a[12], comment_profit: a[13], comment_loss: a[14] });
  return NaN;
});
def('strategy.close', 'id,comment,qty,qty_percent,alert_message,immediately', (c, a) => { if (c.strat && c.strat.lots.some(l => l.id === a[0])) c.strat.place({ kind: 'close', id: a[0], comment: a[1], qty: a[2], immediately: !!a[5] }); return NaN; });
def('strategy.close_all', 'comment,alert_message,immediately', (c, a) => { if (c.strat && c.strat.lots.length) c.strat.place({ kind: 'close', id: undefined, comment: a[0] || 'Close all', immediately: !!a[2] }); return NaN; });
def('strategy.cancel', 'id', (c, a) => { if (c.strat) c.strat.orders = c.strat.orders.filter(x => x.id !== a[0]); return NaN; });
def('strategy.cancel_all', '', c => { if (c.strat) c.strat.orders = []; return NaN; });
def('strategy.risk.allow_entry_in', 'value', (c, a) => { if (c.strat) c.strat.dirAllowed = a[0]; return NaN; });
for (const [k, f] of Object.entries({
  'strategy.position_size': s => s.size(), 'strategy.position_avg_price': s => s.avg(), 'strategy.netprofit': s => s.net, 'strategy.openprofit': (s, c) => s.openPnl(c.B.c[c.i]),
  'strategy.equity': (s, c) => s.equity(c.B.c[c.i]), 'strategy.initial_capital': s => s.cap, 'strategy.closedtrades': s => s.trades.length, 'strategy.opentrades': s => s.lots.length,
  'strategy.wintrades': s => s.trades.filter(t => t.pnl > 0).length, 'strategy.losstrades': s => s.trades.filter(t => t.pnl < 0).length, 'strategy.eventrades': s => s.trades.filter(t => t.pnl === 0).length,
  'strategy.grossprofit': s => s.trades.reduce((x, t) => x + Math.max(0, t.pnl), 0), 'strategy.grossloss': s => s.trades.reduce((x, t) => x + Math.max(0, -t.pnl), 0),
  'strategy.position_entry_name': s => s.lots.length ? s.lots[0].id : '',
})) BV[k] = S((c) => c.strat ? f(c.strat, c) : (k === 'strategy.position_entry_name' ? '' : 0));
def('strategy.closedtrades.profit', 'trade_num', (c, a) => c.strat && c.strat.trades[a[0]] ? c.strat.trades[a[0]].pnl : NaN);
def('strategy.closedtrades.entry_price', 'trade_num', (c, a) => c.strat && c.strat.trades[a[0]] ? c.strat.trades[a[0]].entryPx : NaN);
def('strategy.closedtrades.exit_price', 'trade_num', (c, a) => c.strat && c.strat.trades[a[0]] ? c.strat.trades[a[0]].exitPx : NaN);
def('strategy.opentrades.entry_price', 'trade_num', (c, a) => c.strat && c.strat.lots[a[0]] ? c.strat.lots[a[0]].px : NaN);

// performance of a list of closed trades (also used by the study / optimizer)
export function metrics(trades, opts = {}) {
  const n = trades.length, pnl = trades.map(t => t.pnl), wins = pnl.filter(x => x > 0), losses = pnl.filter(x => x < 0);
  const gp = wins.reduce((a, b) => a + b, 0), gl = -losses.reduce((a, b) => a + b, 0), net = gp - gl;
  let eq = 0, peak = 0, dd = 0; for (const x of pnl) { eq += x; peak = Math.max(peak, eq); dd = Math.max(dd, peak - eq); }
  const days = new Map(); for (const t of trades) { const d = Math.floor((t.exitTime - 5 * 3600) / 86400); days.set(d, (days.get(d) || 0) + t.pnl); }
  const dv = [...days.values()], dm = dv.length ? dv.reduce((a, b) => a + b, 0) / dv.length : 0, dsd = dv.length > 1 ? Math.sqrt(dv.reduce((a, b) => a + (b - dm) ** 2, 0) / (dv.length - 1)) : 0;
  const longs = trades.filter(t => t.dir > 0), shorts = trades.filter(t => t.dir < 0);
  const sum = a => a.reduce((x, t) => x + t.pnl, 0);
  return { n, wins: wins.length, winRate: n ? wins.length / n * 100 : 0, net, gp, gl, pf: gl ? gp / gl : (gp ? Infinity : 0), avg: n ? net / n : 0,
    avgWin: wins.length ? gp / wins.length : 0, avgLoss: losses.length ? -gl / losses.length : 0, maxDD: dd, sharpe: dsd ? dm / dsd * Math.sqrt(252) : 0, days: dv.length,
    long: { n: longs.length, net: sum(longs), winRate: longs.length ? longs.filter(t => t.pnl > 0).length / longs.length * 100 : 0 },
    short: { n: shorts.length, net: sum(shorts), winRate: shorts.length ? shorts.filter(t => t.pnl > 0).length / shorts.length * 100 : 0 },
    avgBars: n ? trades.reduce((a, t) => a + (t.exitBar - t.entryBar), 0) / n : 0, capital: opts.capital };
}
def('alertcondition', 'condition,title,message', () => NaN);
def('alert', 'message,freq', () => NaN);
// request.security: the script runs a second time on the other symbol / timeframe (opts.securities[key] = its bars) and
// each call site records its expression per bar there; here the value of the last of those bars that has closed by the
// end of this bar is returned (no lookahead). Without the bars (or for the chart's own symbol and timeframe) the
// expression on the chart's own series is returned.
export function secKey(sym, tf, ctx) {
  let s = String(sym == null ? '' : sym).trim(); if (s.includes(':')) s = s.slice(s.indexOf(':') + 1);
  s = s.replace(/[0-9]*!$/, '').toUpperCase(); if (!s || s === String(ctx.symbol).toUpperCase()) s = String(ctx.symbol).toUpperCase();
  let t = String(tf == null ? '' : tf).trim(); if (!t) t = ctx.tf;
  if (/^\d+$/.test(t)) t = t + 'm'; if (/^[DWM]$/.test(t)) t = '1' + t;
  const tfs = tfSeconds(t), same = s === String(ctx.symbol).toUpperCase() && tfs === ctx.tfSec;
  return { sym: s, tf: t, tfSec: tfs, key: s + '|' + t, same };
}
def('request.security', 'symbol,timeframe,expression,gaps,lookahead', (c, a, st, id) => {
  const k = secKey(a[0], a[1], c), site = c.f.path + '/' + id;
  if (c.probe) { if (!k.same) c.probe.set(k.key, { sym: k.sym, tf: k.tf }); return a[2]; }
  if (c.secRec) { if (k.key === c.secRec.key) (c.secRec.vals[site] || (c.secRec.vals[site] = []))[c.i] = a[2]; return a[2]; }
  if (k.same) return a[2];
  const S = c.sec && c.sec[k.key];
  if (!S) { const m = `request.security(): no ${k.sym} ${k.tf} data loaded yet; the chart's own series is used.`; c.warn(m); return a[2]; }
  const vals = S.vals[site]; if (!vals) return NaN;
  const end = c.B.t[c.i] + c.tfSec; let j = st.j == null ? -1 : st.j;
  if (j >= 0 && S.t[j] + S.dur > end) j = -1;                    // (replay / a jump back in time)
  while (j + 1 < S.t.length && S.t[j + 1] + S.dur <= end) j++;
  st.j = j; if (j < 0) return NaN;
  const v = vals[j]; return v === undefined ? NaN : v;
});
def('runtime.error', 'message', (c, a) => { throw new PineError(String(a[0]), c.line); });
for (const lv of ['info', 'warning', 'error']) def('log.' + lv, 'message', (c, a) => { if (c.out.logs.length < 200) c.out.logs.push({ level: lv, bar: c.i, msg: F['str.format'][1](c, a) }); return NaN; });

// plots
const colorAt = v => isna(v) ? null : normColor(v);
def('plot', 'series,title,color,linewidth,style,trackprice,histbase,offset,join,editable,show_last,display', (c, a, st, id) => {
  let p = c.out._plots.get(id);
  if (!p) { p = { id, title: a[1] ?? 'Plot', color: colorAt(a[2]) || '#2962ffff', linewidth: a[3] ?? 1, style: a[4] ?? 'line', trackprice: !!a[5], histbase: a[6] ?? 0, offset: a[7] ?? 0, display: a[11] ?? 'all', values: new Array(c.n).fill(NaN), colors: null }; c.out._plots.set(id, p); }
  p.values[c.i] = num(a[0]);
  if (a[2] !== undefined) { const col = colorAt(a[2]); if (col !== p.color || p.colors) { p.colors = p.colors || new Array(c.n).fill(p.color); p.colors[c.i] = col; } }
  return { ref: 'plot', id };
});
def('plotshape', 'series,title,style,location,color,offset,text,textcolor,editable,size,show_last,display', (c, a, st, id) => {
  let p = c.out._shapes.get(id);
  if (!p) { p = { id, kind: 'shape', title: a[1] ?? 'Shape', style: a[2] ?? 'xcross', location: a[3] ?? 'abovebar', color: colorAt(a[4]) || '#2962ffff', offset: a[5] ?? 0, text: a[6] ?? '', textcolor: colorAt(a[7]), size: a[9] ?? 'auto', values: new Array(c.n).fill(NaN), colors: null }; c.out._shapes.set(id, p); }
  const v = a[0]; p.values[c.i] = p.location === 'absolute' ? num(v) : (truthy(v) || (typeof v === 'number' && !isna(v)) ? 1 : NaN);
  if (a[4] !== undefined) { const col = colorAt(a[4]); if (col !== p.color || p.colors) { p.colors = p.colors || new Array(c.n).fill(p.color); p.colors[c.i] = col; } }
  return NaN;
});
def('plotchar', 'series,title,char,location,color,offset,text,textcolor,editable,size,show_last,display', (c, a, st, id) => {
  let p = c.out._shapes.get(id);
  if (!p) { p = { id, kind: 'char', title: a[1] ?? 'Char', char: a[2] ?? '★', location: a[3] ?? 'abovebar', color: colorAt(a[4]) || '#2962ffff', offset: a[5] ?? 0, text: a[6] ?? '', textcolor: colorAt(a[7]), size: a[9] ?? 'auto', values: new Array(c.n).fill(NaN), colors: null }; c.out._shapes.set(id, p); }
  const v = a[0]; p.values[c.i] = p.location === 'absolute' ? num(v) : (truthy(v) || (typeof v === 'number' && !isna(v)) ? 1 : NaN);
  if (a[4] !== undefined) { const col = colorAt(a[4]); if (col !== p.color || p.colors) { p.colors = p.colors || new Array(c.n).fill(p.color); p.colors[c.i] = col; } }
  return NaN;
});
def('plotarrow', 'series,title,colorup,colordown,offset', (c, a, st, id) => NaN);
def('plotcandle', 'open,high,low,close,title,color', () => NaN);
def('plotbar', 'open,high,low,close,title,color', () => NaN);
def('hline', 'price,title,color,linestyle,linewidth,editable,display', (c, a, st, id) => {
  if (!c.out._hlines.has(id)) c.out._hlines.set(id, { id, price: num(a[0]), title: a[1] ?? 'Line', color: colorAt(a[2]) || '#787b86ff', linestyle: a[3] ?? 'dashed', linewidth: a[4] ?? 1 });
  return { ref: 'hline', id };
});
def('fill', 'hline1,hline2,color,title,editable,fillgaps,display', (c, a, st, id) => {
  if (!a[0] || !a[1] || !a[0].ref) return NaN;
  let f = c.out._fills.get(id);
  if (!f) { f = { id, a: a[0], b: a[1], color: colorAt(a[2]) || '#2962ff33', colors: null }; c.out._fills.set(id, f); }
  if (a[2] !== undefined) { const col = colorAt(a[2]); if (col !== f.color || f.colors) { f.colors = f.colors || new Array(c.n).fill(f.color); f.colors[c.i] = col; } }
  return NaN;
});
const perBar = key => (c, a, st, id) => { let p = c.out[key].get(id); if (!p) { p = { id, offset: a[1] ?? 0, colors: new Array(c.n).fill(null) }; c.out[key].set(id, p); } p.colors[c.i] = colorAt(a[0]); return NaN; };
def('bgcolor', 'color,offset,editable,show_last,title', perBar('_bg'));
def('barcolor', 'color,offset,editable,show_last,title', perBar('_bar'));

// drawings
function addObj(c, kind, o) {
  const key = kind === 'box' ? 'boxes' : kind + 's'; const list = c.out[key]; o.kind = kind; o.del = false; list.push(o);
  const live = list.filter(x => !x.del); if (live.length > c.max[kind]) live[0].del = true;
  if (list.length > c.max[kind] * 4) c.out[key] = list.filter(x => !x.del);
  return o;
}
const objOk = o => o && typeof o === 'object' && !o.del;
def('line.new', 'x1,y1,x2,y2,xloc,extend,color,style,width', (c, a) => {
  if (a[0] && typeof a[0] === 'object' && a[0].kind === 'point') { const p1 = a[0], p2 = a[1]; return addObj(c, 'line', { x1: p1.index, y1: p1.price, x2: p2.index, y2: p2.price, xloc: 'bar_index', extend: a[2] ?? 'none', color: colorAt(a[3]) || '#2962ffff', style: a[4] ?? 'solid', width: a[5] ?? 1 }); }
  return addObj(c, 'line', { x1: num(a[0]), y1: num(a[1]), x2: num(a[2]), y2: num(a[3]), xloc: a[4] ?? 'bar_index', extend: a[5] ?? 'none', color: colorAt(a[6]) || '#2962ffff', style: a[7] ?? 'solid', width: a[8] ?? 1 });
});
def('chart.point.from_index', 'index,price', (c, a) => ({ kind: 'point', index: a[0], price: a[1] }));
def('chart.point.now', 'price', (c, a) => ({ kind: 'point', index: c.i, price: a[0] }));
def('chart.point.new', 'time,index,price', (c, a) => ({ kind: 'point', index: a[1], price: a[2] }));
const setter = (kind, fields) => (c, a) => { const o = a[0]; if (!objOk(o)) return NaN; fields.forEach((f, k) => { if (a[k + 1] !== undefined) o[f] = f.includes('color') ? colorAt(a[k + 1]) : a[k + 1]; }); return NaN; };
for (const [n, f] of Object.entries({ x1: ['x1'], y1: ['y1'], x2: ['x2'], y2: ['y2'], xy1: ['x1', 'y1'], xy2: ['x2', 'y2'], color: ['color'], width: ['width'], style: ['style'], extend: ['extend'] })) def('line.set_' + n, 'id', setter('line', f));
for (const n of ['x1', 'y1', 'x2', 'y2']) def('line.get_' + n, 'id', (c, a) => objOk(a[0]) ? a[0][n] : NaN);
def('line.get_price', 'id,x', (c, a) => { const o = a[0]; if (!objOk(o)) return NaN; return o.y1 + (o.y2 - o.y1) * (a[1] - o.x1) / ((o.x2 - o.x1) || 1); });
def('line.delete', 'id', (c, a) => { if (objOk(a[0])) a[0].del = true; return NaN; });
def('line.copy', 'id', (c, a) => objOk(a[0]) ? addObj(c, 'line', { ...a[0] }) : NaN);
def('label.new', 'x,y,text,xloc,yloc,color,style,textcolor,size,textalign,tooltip', (c, a) => {
  if (a[0] && typeof a[0] === 'object' && a[0].kind === 'point') { const p = a[0]; a = [p.index, p.price, ...a.slice(1)]; a.__named = undefined; }
  return addObj(c, 'label', { x: num(a[0]), y: num(a[1]), text: a[2] ?? '', xloc: a[3] ?? 'bar_index', yloc: a[4] ?? 'price', color: colorAt(a[5]) ?? '#2962ffff', style: a[6] ?? 'label_down', textcolor: colorAt(a[7]) || '#ffffffff', size: a[8] ?? 'normal' });
});
for (const [n, f] of Object.entries({ x: ['x'], y: ['y'], xy: ['x', 'y'], text: ['text'], color: ['color'], textcolor: ['textcolor'], style: ['style'], size: ['size'], yloc: ['yloc'], tooltip: [] })) def('label.set_' + n, 'id', setter('label', f));
for (const n of ['x', 'y', 'text']) def('label.get_' + n, 'id', (c, a) => objOk(a[0]) ? a[0][n] : NaN);
def('label.delete', 'id', (c, a) => { if (objOk(a[0])) a[0].del = true; return NaN; });
def('box.new', 'left,top,right,bottom,border_color,border_width,border_style,extend,xloc,bgcolor,text,text_size,text_color,text_halign,text_valign', (c, a) => {
  const n = a.__named || {};
  return addObj(c, 'box', { left: num(a[0]), top: num(a[1]), right: num(a[2]), bottom: num(a[3]), border_color: colorAt(a[4]) || '#2962ffff', border_width: a[5] ?? 1, border_style: a[6] ?? 'solid', extend: a[7] ?? 'none', xloc: a[8] ?? 'bar_index', bgcolor: colorAt(a[9]) || '#2962ff33', text: a[10] ?? '', text_color: colorAt(a[12]) || '#ffffffcc', text_size: a[11] ?? 'auto', text_halign: n.text_halign ?? 'center' });
});
for (const [n, f] of Object.entries({ left: ['left'], top: ['top'], right: ['right'], bottom: ['bottom'], lefttop: ['left', 'top'], rightbottom: ['right', 'bottom'], bgcolor: ['bgcolor'], border_color: ['border_color'], border_width: ['border_width'], border_style: ['border_style'], extend: ['extend'], text: ['text'], text_color: ['text_color'], text_size: ['text_size'], top_left_point: [], bottom_right_point: [] })) def('box.set_' + n, 'id', setter('box', f));
for (const n of ['left', 'top', 'right', 'bottom']) def('box.get_' + n, 'id', (c, a) => objOk(a[0]) ? a[0][n] : NaN);
def('box.delete', 'id', (c, a) => { if (objOk(a[0])) a[0].del = true; return NaN; });
def('table.new', '', () => NaN); def('table.cell', '', () => NaN); def('table.delete', '', () => NaN); def('table.clear', '', () => NaN);
def('linefill.new', '', () => NaN); def('linefill.delete', '', () => NaN);

// arrays
const arr = a => (a && a.kind === 'array') ? a.a : null;
const newArr = (c, a) => ({ kind: 'array', a: new Array(Math.max(0, Math.round(nz(a[0], 0)))).fill(a[1] === undefined ? NaN : a[1]) });
for (const t of ['float', 'int', 'bool', 'string', 'color', 'line', 'label', 'box', 'table', 'linefill']) def('array.new_' + t, 'size,initial_value', newArr);
def('array.new', 'size,initial_value', newArr);
def('array.from', '', (c, a) => ({ kind: 'array', a: [...a] }));
def('array.copy', 'id', (c, a) => ({ kind: 'array', a: [...(arr(a[0]) || [])] }));
const A1 = (n, p, fn) => def('array.' + n, 'id,' + p, (c, a) => { const x = arr(a[0]); if (!x) { if (isna(a[0])) throw new PineError(`array.${n}: the array is na`, c.line); return NaN; } return fn(x, a, c); });
A1('push', 'value', (x, a) => { x.push(a[1]); return NaN; });
A1('pop', '', x => x.length ? x.pop() : NaN);
A1('shift', '', x => x.length ? x.shift() : NaN);
A1('unshift', 'value', (x, a) => { x.unshift(a[1]); return NaN; });
A1('size', '', x => x.length);
A1('clear', '', x => { x.length = 0; return NaN; });
A1('get', 'index', (x, a, c) => { const k = Math.round(a[1]); const j = k < 0 ? x.length + k : k; if (j < 0 || j >= x.length) throw new PineError(`array.get: index ${k} out of bounds (size ${x.length})`, c.line); return x[j]; });
A1('set', 'index,value', (x, a, c) => { const k = Math.round(a[1]); const j = k < 0 ? x.length + k : k; if (j < 0 || j >= x.length) throw new PineError(`array.set: index ${k} out of bounds (size ${x.length})`, c.line); x[j] = a[2]; return NaN; });
A1('insert', 'index,value', (x, a) => { x.splice(Math.round(a[1]), 0, a[2]); return NaN; });
A1('remove', 'index', (x, a) => { const r = x.splice(Math.round(a[1]), 1); return r.length ? r[0] : NaN; });
A1('first', '', x => x.length ? x[0] : NaN);
A1('last', '', x => x.length ? x[x.length - 1] : NaN);
A1('max', '', x => { const v = x.filter(q => !isna(q)); return v.length ? Math.max(...v) : NaN; });
A1('min', '', x => { const v = x.filter(q => !isna(q)); return v.length ? Math.min(...v) : NaN; });
A1('sum', '', x => x.reduce((s, q) => s + nz(q), 0));
A1('avg', '', x => x.length ? x.reduce((s, q) => s + nz(q), 0) / x.length : NaN);
A1('stdev', '', x => { if (!x.length) return NaN; const m = x.reduce((s, q) => s + q, 0) / x.length; return Math.sqrt(x.reduce((s, q) => s + (q - m) ** 2, 0) / x.length); });
A1('includes', 'value', (x, a) => x.includes(a[1]));
A1('indexof', 'value', (x, a) => x.indexOf(a[1]));
A1('lastindexof', 'value', (x, a) => x.lastIndexOf(a[1]));
A1('reverse', '', x => { x.reverse(); return NaN; });
A1('sort', 'order', (x, a) => { x.sort((p, q) => a[1] === 'desc' ? q - p : p - q); return NaN; });
A1('slice', 'index_from,index_to', (x, a) => ({ kind: 'array', a: x.slice(a[1], a[2]) }));
A1('concat', 'id2', (x, a) => { x.push(...(arr(a[1]) || [])); return a[0]; });
A1('join', 'separator', (x, a) => x.map(v => typeof v === 'number' ? fmtNum(v) : String(v)).join(a[1] ?? ','));
A1('fill', 'value,index_from,index_to', (x, a) => { x.fill(a[1], a[2] ?? 0, a[3] ?? x.length); return NaN; });
A1('every', '', x => x.every(truthy));
A1('some', '', x => x.some(truthy));

// ------------------------------------------------------------------ compiler
let NODE = 0;
class Scope { constructor(parent, global) { this.parent = parent; this.vars = new Map(); this.global = global; } find(n) { for (let s = this; s; s = s.parent) if (s.vars.has(n)) return s.vars.get(n); return null; } }

function compileProgram(ast) {
  const funcs = new Map();
  let declN = 0;
  const gscope = new Scope(null, true);
  const slot = (ctx, d) => { const fr = d.g ? ctx.gf : ctx.f; return fr.s[d.id] || (fr.s[d.id] = { cur: NaN, hist: [], init: false }); };
  const declare = (scope, name) => { const d = { id: declN++, g: scope.global, name }; scope.vars.set(name, d); return d; };

  function block(stmts, scope) {
    const fns = stmts.map(s => stmt(s, scope));
    return ctx => { let v = NaN; for (let k = 0; k < fns.length; k++) { v = fns[k](ctx); if (ctx.brk) break; } return v; };
  }
  function stmt(s, scope) {
    const line = s.line;
    const wrap = fn => ctx => { ctx.line = line; return fn(ctx); };
    switch (s.k) {
      case 'nop': return () => NaN;
      case 'expr': return wrap(expr(s.e, scope));
      case 'decl': {
        const val = expr(s.value, scope); const d = declare(scope, s.name);
        if (s.isVar) return wrap(ctx => { const sl = slot(ctx, d); if (!sl.init) { sl.init = true; sl.cur = val(ctx); } sl.hist[ctx.i] = sl.cur; return sl.cur; });
        return wrap(ctx => { const v = val(ctx); const sl = slot(ctx, d); sl.cur = v; sl.hist[ctx.i] = v; return v; });
      }
      case 'tuple': {
        const val = expr(s.value, scope);
        const ds = s.names.map(n => s.reassign ? (scope.find(n) || err(`Undeclared identifier '${n}'`, line)) : (n === '_' ? null : declare(scope, n)));
        return wrap(ctx => { const v = val(ctx); const arrv = Array.isArray(v) ? v : [v]; ds.forEach((d, k) => { if (!d) return; const sl = slot(ctx, d); sl.cur = arrv[k] === undefined ? NaN : arrv[k]; sl.hist[ctx.i] = sl.cur; }); return v; });
      }
      case 'assign': {
        const d = scope.find(s.name); if (!d) err(`Undeclared identifier '${s.name}'. Use = to declare it first.`, line);
        const val = expr(s.value, scope), op = s.op;
        return wrap(ctx => {
          const sl = slot(ctx, d); let v = val(ctx);
          if (op !== ':=') { const o = sl.cur; v = op === '+=' ? (typeof o === 'string' ? o + v : num(o) + num(v)) : op === '-=' ? num(o) - num(v) : op === '*=' ? num(o) * num(v) : op === '/=' ? num(o) / num(v) : num(o) % num(v); }
          sl.cur = v; sl.hist[ctx.i] = v; return v;
        });
      }
      case 'if': return wrap(ifc(s, scope));
      case 'switch': return wrap(switchc(s, scope));
      case 'for': {
        const from = expr(s.from, scope), to = expr(s.to, scope), by = s.by ? expr(s.by, scope) : null;
        const inner = new Scope(scope, scope.global && false); const d = declare(inner, s.name); d.g = scope.global; const body = block(s.body, inner);
        return wrap(ctx => {
          const a = num(from(ctx)), b = num(to(ctx)); if (isna(a) || isna(b)) return NaN;
          let step = by ? Math.abs(num(by(ctx))) : 1; if (!(step > 0)) step = 1; if (b < a) step = -step;
          let v = NaN, guard = 0; const sl = slot(ctx, d);
          for (let x = a; step > 0 ? x <= b : x >= b; x += step) {
            sl.cur = x; sl.hist[ctx.i] = x; v = body(ctx);
            if (ctx.brk === 'break') { ctx.brk = null; break; } if (ctx.brk === 'continue') ctx.brk = null;
            if (++guard > 100000) throw new PineError('Loop takes too long (100000 iterations)', line);
          }
          return v;
        });
      }
      case 'forin': {
        const ae = expr(s.arr, scope), inner = new Scope(scope, false); const d = declare(inner, s.name); d.g = scope.global; const di = s.idx ? declare(inner, s.idx) : null; if (di) di.g = scope.global; const body = block(s.body, inner);
        return wrap(ctx => {
          const x = arr(ae(ctx)) || []; let v = NaN; const sl = slot(ctx, d), si = di ? slot(ctx, di) : null;
          for (let k = 0; k < x.length; k++) { sl.cur = x[k]; if (si) si.cur = k; v = body(ctx); if (ctx.brk === 'break') { ctx.brk = null; break; } if (ctx.brk === 'continue') ctx.brk = null; }
          return v;
        });
      }
      case 'while': {
        const cond = expr(s.cond, scope), body = block(s.body, new Scope(scope, false));
        return wrap(ctx => { let v = NaN, guard = 0; while (truthy(cond(ctx))) { v = body(ctx); if (ctx.brk === 'break') { ctx.brk = null; break; } if (ctx.brk === 'continue') ctx.brk = null; if (++guard > 100000) throw new PineError('Loop takes too long (100000 iterations)', line); } return v; });
      }
      case 'break': return ctx => { ctx.brk = 'break'; return NaN; };
      case 'continue': return ctx => { ctx.brk = 'continue'; return NaN; };
      case 'func': {
        const f = { name: s.name, params: s.params, line, method: s.method };
        funcs.set(s.name, f);
        const fscope = new Scope(gscope, false);
        f.pdecl = s.params.map(p => declare(fscope, p.name));
        f.pdefs = s.params.map(p => p.def ? expr(p.def, gscope) : null);
        f.body = block(s.body, fscope);
        return () => NaN;
      }
    }
    err(`Unsupported statement ${s.k}`, line);
  }
  function ifc(s, scope) {
    const cond = expr(s.cond, scope), body = block(s.body, new Scope(scope, false)), els = s.els ? block(s.els, new Scope(scope, false)) : null;
    return ctx => truthy(cond(ctx)) ? body(ctx) : (els ? els(ctx) : NaN);
  }
  function switchc(s, scope) {
    const subj = s.subject ? expr(s.subject, scope) : null;
    const cases = s.cases.map(c => ({ cond: c.cond ? expr(c.cond, scope) : null, body: block(c.body, new Scope(scope, false)) }));
    return ctx => {
      const v = subj ? subj(ctx) : undefined;
      for (const c of cases) { if (!c.cond) return c.body(ctx); const x = c.cond(ctx); if (subj ? x === v : truthy(x)) return c.body(ctx); }
      return NaN;
    };
  }
  function err(msg, line) { throw new PineError(msg, line); }

  function expr(e, scope) {
    const id = NODE++, line = e.line;
    switch (e.k) {
      case 'lit': { const v = e.v; return () => v; }
      case 'tuplelit': { const it = e.items.map(x => expr(x, scope)); return ctx => it.map(f => f(ctx)); }
      case 'blockexpr': return e.s.k === 'if' ? ifc(e.s, scope) : switchc(e.s, scope);
      case 'id': {
        const d = scope.find(e.name);
        if (d) return ctx => slot(ctx, d).cur;
        if (BV[e.name]) { const s = BV[e.name].s; return ctx => s(ctx, ctx.i); }
        if (e.name in CONST) { const v = CONST[e.name]; return () => v; }
        if (funcs.has(e.name)) err(`'${e.name}' is a function: call it with ()`, line);
        // method-style field access, e.g. myLine.x1 is not supported; namespaces like color.red handled above
        const dot = e.name.indexOf('.');
        if (dot > 0) { const base = scope.find(e.name.slice(0, dot)); if (base) { const field = e.name.slice(dot + 1); return ctx => { const o = slot(ctx, base).cur; return o && typeof o === 'object' ? (o[field] ?? NaN) : NaN; }; } }
        err(`Undeclared identifier '${e.name}'`, line);
      }
      case 'hist': {
        const kf = expr(e.idx, scope);
        if (e.e.k === 'id') {
          const d = scope.find(e.e.name);
          if (d) return ctx => { const k = Math.round(num(kf(ctx))); const sl = slot(ctx, d); if (k === 0) return sl.cur; const v = sl.hist[ctx.i - k]; return v === undefined ? NaN : v; };
          if (BV[e.e.name]) { const s = BV[e.e.name].s; return ctx => { const j = ctx.i - Math.round(num(kf(ctx))); return j < 0 || j >= ctx.n ? NaN : s(ctx, j); }; }
        }
        const inner = expr(e.e, scope);
        return ctx => { const v = inner(ctx); const b = ctx.f.b[id] || (ctx.f.b[id] = []); b[ctx.i] = v; const k = Math.round(num(kf(ctx))); const r = b[ctx.i - k]; return r === undefined ? NaN : r; };
      }
      case 'neg': { const f = expr(e.e, scope); return e.op === '-' ? ctx => -num(f(ctx)) : ctx => num(f(ctx)); }
      case 'not': { const f = expr(e.e, scope); return ctx => !truthy(f(ctx)); }
      case 'and': { const l = expr(e.l, scope), r = expr(e.r, scope); return ctx => truthy(l(ctx)) && truthy(r(ctx)); }
      case 'or': { const l = expr(e.l, scope), r = expr(e.r, scope); return ctx => truthy(l(ctx)) || truthy(r(ctx)); }
      case 'tern': { const c = expr(e.c, scope), a = expr(e.a, scope), b = expr(e.b, scope); return ctx => truthy(c(ctx)) ? a(ctx) : b(ctx); }
      case 'bin': {
        const l = expr(e.l, scope), r = expr(e.r, scope);
        switch (e.op) {
          case '+': return ctx => { const x = l(ctx), y = r(ctx); return (typeof x === 'string' || typeof y === 'string') ? (isna(x) ? '' : String(x)) + (isna(y) ? '' : String(y)) : num(x) + num(y); };
          case '-': return ctx => num(l(ctx)) - num(r(ctx));
          case '*': return ctx => num(l(ctx)) * num(r(ctx));
          case '/': return ctx => { const y = num(r(ctx)); return y === 0 ? NaN : num(l(ctx)) / y; };
          case '%': return ctx => num(l(ctx)) % num(r(ctx));
          case '<': return ctx => num(l(ctx)) < num(r(ctx));
          case '>': return ctx => num(l(ctx)) > num(r(ctx));
          case '<=': return ctx => num(l(ctx)) <= num(r(ctx));
          case '>=': return ctx => num(l(ctx)) >= num(r(ctx));
          case '==': return ctx => { const x = l(ctx), y = r(ctx); return (typeof x === 'number' || typeof y === 'number') ? num(x) === num(y) : x === y; };
          case '!=': return ctx => { const x = l(ctx), y = r(ctx); return (typeof x === 'number' || typeof y === 'number') ? !(num(x) === num(y)) && !(isna(x) && isna(y)) : x !== y; };
        }
        break;
      }
      case 'field': { const o = expr(e.obj, scope); return ctx => { const v = o(ctx); return v && typeof v === 'object' ? (v[e.name] ?? NaN) : NaN; }; }
      case 'mcall': {
        const o = expr(e.obj, scope), pos = e.args.pos.map(x => expr(x, scope));
        return ctx => { const v = o(ctx); const kind = v && v.kind; const b = F[kind + '.' + e.name]; if (!b) throw new PineError(`Unknown method '${e.name}'`, line); const a = [v, ...pos.map(f => f(ctx))]; const fr = ctx.f; return b[1](ctx, a, fr.st[id] || (fr.st[id] = {}), id, e); };
      }
      case 'call': return call(e, scope, id);
    }
    err(`Unsupported expression ${e.k}`, line);
  }

  function call(e, scope, id) {
    const line = e.line, name = e.name;
    const posF = e.args.pos.map(x => expr(x, scope)), namedF = Object.fromEntries(Object.entries(e.args.named).map(([k, v]) => [k, expr(v, scope)]));
    const uf = funcs.get(name);
    if (uf) return userCall(uf, posF, namedF, id, line, null);
    const b = F[name];
    if (b) {
      const params = b[0], fn = b[1], node = e;
      if (name === 'input.source' || name === 'input') { const a0 = e.args.pos[0] || e.args.named.defval; if (a0 && a0.k === 'id' && BV[a0.name]) node.srcName = a0.name; }
      const map = posF.slice(); const extra = {};
      for (const [k, f] of Object.entries(namedF)) { const j = params.indexOf(k); if (j >= 0) map[j] = f; else extra[k] = f; }
      const isSource = name === 'input.source';
      const hasExtra = Object.keys(namedF).length > 0;
      return ctx => {
        const a = new Array(map.length); for (let k = 0; k < map.length; k++) a[k] = map[k] ? map[k](ctx) : undefined;
        if (hasExtra) { const n = {}; for (const [k, f] of Object.entries(namedF)) n[k] = f(ctx); a.__named = n; }
        const fr = ctx.f; const st = fr.st[id] || (fr.st[id] = {});
        let v = fn(ctx, a, st, id, node);
        if (isSource) { const nm = typeof v === 'string' ? v : 'close'; const s = BV[nm] || BV.close; v = s.s(ctx, ctx.i); }
        return v;
      };
    }
    const dot = name.indexOf('.');
    if (dot > 0) {
      const base = scope.find(name.slice(0, dot)), mname = name.slice(dot + 1);
      if (base) {
        const um = funcs.get(mname);
        if (um) return userCall(um, posF, namedF, id, line, base);
        return ctx => {
          const obj = slot(ctx, base).cur, kind = obj && obj.kind; if (isna(obj)) return NaN; const bb = F[(kind || 'array') + '.' + mname];
          if (!bb) throw new PineError(isna(obj) ? `Cannot call '${mname}' on an na value` : `Unknown method '${mname}'`, line);
          const a = [obj, ...posF.map(f => f(ctx))];
          for (const [k, f] of Object.entries(namedF)) { const j = bb[0].indexOf(k); if (j >= 0) a[j] = f(ctx); }
          const fr = ctx.f; return bb[1](ctx, a, fr.st[id] || (fr.st[id] = {}), id, e);
        };
      }
    }
    err(`Could not find function or function reference '${name}'`, line);
  }

  function userCall(uf, posF, namedF, id, line, selfDecl) {
    return ctx => {
      const args = posF.map(f => f(ctx)); if (selfDecl) args.unshift(slot(ctx, selfDecl).cur);
      for (const [k, f] of Object.entries(namedF)) { const j = uf.params.findIndex(p => p.name === k); if (j >= 0) args[j] = f(ctx); }
      const key = ctx.f.path + '/' + id; let fr = ctx.frames.get(key);
      if (!fr) { fr = { path: key, s: [], st: [], b: [] }; ctx.frames.set(key, fr); }
      const prev = ctx.f; ctx.f = fr;
      if (++ctx.depth > 200) throw new PineError('Recursion is not allowed', line);
      try {
        uf.pdecl.forEach((d, k) => { let v = args[k]; if (v === undefined) v = uf.pdefs[k] ? uf.pdefs[k](ctx) : NaN; const sl = fr.s[d.id] || (fr.s[d.id] = { cur: NaN, hist: [], init: false }); sl.cur = v; sl.hist[ctx.i] = v; });
        return uf.body(ctx);
      } finally { ctx.f = prev; ctx.depth--; ctx.brk = null; }
    };
  }

  return block(ast, gscope);
}

// ------------------------------------------------------------------ public api
export function tfSeconds(tf) {
  const m = String(tf).trim().match(/^(\d*)\s*([smhDWM]?)$/); if (!m) return 60;
  const n = +(m[1] || 1), u = m[2] || 'm';
  if (!m[2] && m[1]) return n * 60; // Pine: "15" = 15 minutes
  return n * { s: 1, m: 60, h: 3600, D: 86400, W: 604800, M: 2592000 }[u];
}

export function compile(src) {
  NODE = 0;
  const ast = new Parser(lex(src)).program();
  const hasHeader = ast.some(s => s.k === 'expr' && s.e.k === 'call' && ['indicator', 'strategy', 'study', 'library'].includes(s.e.name));
  if (!hasHeader) throw new PineError('The script needs an indicator() declaration, e.g. indicator("My script", overlay = true)', 1);
  const prog = compileProgram(ast);
  return { prog, src };
}

// The other symbols / timeframes a script asks for with request.security: [{ sym, tf, key }] (run on a few bars)
export function securityRequests(compiled, bars, opts = {}) {
  if (!/request\.security\s*\(/.test(compiled.src || '')) return [];
  const probe = new Map();
  try { run(compiled, bars.slice(0, Math.min(bars.length, 3)), { ...opts, securities: null, _probe: probe }); } catch (e) { /* the requests seen so far */ }
  return [...probe.entries()].map(([key, v]) => ({ ...v, key }));
}

export function run(compiled, bars, opts = {}) {
  let sec = null;
  if (opts.securities && !opts._probe && !opts._secRec) {
    sec = {};
    for (const [key, sb] of Object.entries(opts.securities)) {
      if (!sb || !sb.length) continue;
      const [sym, tf] = key.split('|'), rec = { key, vals: {} };
      run(compiled, sb, { ...opts, symbol: sym, tf, tfSec: tfSeconds(tf), securities: null, _secRec: rec, live: false });
      sec[key] = { t: sb.map(b => b.t), dur: tfSeconds(tf), vals: rec.vals };
    }
  }
  const n = bars.length;
  const B = { t: new Array(n), o: new Array(n), h: new Array(n), l: new Array(n), c: new Array(n), v: new Array(n) };
  for (let i = 0; i < n; i++) { const b = bars[i]; B.t[i] = b.t; B.o[i] = b.o; B.h[i] = b.h; B.l[i] = b.l; B.c[i] = b.c; B.v[i] = b.v || 0; }
  const out = { meta: { title: 'Script', overlay: false }, inputs: [], logs: [], lines: [], labels: [], boxes: [], _plots: new Map(), _shapes: new Map(), _hlines: new Map(), _fills: new Map(), _bg: new Map(), _bar: new Map() };
  const tpc = new Array(n), prec = {};
  const gf = { path: '', s: [], st: [], b: [] };
  const tf = opts.tf || '1D';
  const ctx = {
    B, n, i: 0, out, gf, f: gf, frames: new Map(), depth: 0, brk: null, line: 0, live: !!opts.live,
    symbol: opts.symbol || 'SYMBOL', tf, tfSec: opts.tfSec || tfSeconds(tf), tfMult: parseInt(tf, 10) || 1, mintick: opts.mintick || 0.01,
    inputValues: opts.inputs || null, inputOrder: new Map(), max: { ...MAXOBJ }, strat: null, stratOverrides: opts.strategy || null,
    tp: j => tpc[j] || (tpc[j] = nyParts(B.t[j])),
    warn: msg => { if (!out.logs.some(l => l.msg === msg)) out.logs.push({ level: 'warning', bar: ctx.i, msg }); },
    pre: k => prec[k] || (prec[k] = precompute(k, B, ctx)),
    probe: opts._probe || null, secRec: opts._secRec || null, sec,
  };
  const t0 = Date.now(), budget = opts.budgetMs || 4000;
  try {
    for (let i = 0; i < n; i++) {
      ctx.i = i; ctx.brk = null;
      if (ctx.strat && i > 0) ctx.strat.process(i, 'open');
      compiled.prog(ctx);
      if (ctx.strat) { ctx.strat.process(i, 'close'); ctx.strat.eq[i] = ctx.strat.equity(B.c[i]); }
      if ((i & 255) === 0 && Date.now() - t0 > budget) throw new PineError(`Script took longer than ${budget / 1000}s`, ctx.line);
    }
  } catch (e) {
    if (e instanceof PineError) out.error = { message: e.message, line: e.line || ctx.line, bar: ctx.i };
    else out.error = { message: 'Runtime error: ' + e.message, line: ctx.line, bar: ctx.i };
  }
  const res = {
    meta: out.meta, inputs: out.inputs, logs: out.logs, error: out.error,
    plots: [...out._plots.values()], shapes: [...out._shapes.values()], hlines: [...out._hlines.values()], fills: [...out._fills.values()],
    bgcolors: [...out._bg.values()], barcolors: [...out._bar.values()],
    lines: out.lines.filter(x => !x.del), labels: out.labels.filter(x => !x.del), boxes: out.boxes.filter(x => !x.del), ms: Date.now() - t0,
  };
  if (ctx.strat) {
    const st = ctx.strat, last = B.c[n - 1];
    res.strategy = { trades: st.trades, open: st.lots.map(l => ({ ...l, pnl: (last - l.px) * l.qty * l.dir })), equity: st.eq, fills: st.fills, net: st.net, capital: st.cap,
      settings: { qtyType: st.qtyType, qtyVal: st.qtyVal, comType: st.comType, comVal: st.comVal, slip: st.slip, pyr: st.pyr, poc: st.poc }, metrics: metrics(st.trades, { capital: st.cap }) };
  }
  return res;
}

function precompute(k, B, ctx) {
  const n = B.c.length, r = new Array(n).fill(NaN);
  if (k === 'obv') { let s = 0; for (let i = 0; i < n; i++) { if (i) s += Math.sign(B.c[i] - B.c[i - 1]) * B.v[i]; r[i] = s; } }
  if (k === 'ad') { let s = 0; for (let i = 0; i < n; i++) { const hl = B.h[i] - B.l[i]; s += hl ? ((2 * B.c[i] - B.l[i] - B.h[i]) / hl) * B.v[i] : 0; r[i] = s; } }
  if (k === 'vwap') { let pv = 0, vv = 0; for (let i = 0; i < n; i++) { if (i === 0 || ctx.tp(i).key !== ctx.tp(i - 1).key) { pv = 0; vv = 0; } const s = (B.h[i] + B.l[i] + B.c[i]) / 3; pv += s * B.v[i]; vv += B.v[i]; r[i] = vv ? pv / vv : s; } }
  return r;
}

export const BUILTIN_NAMES = [...Object.keys(F), ...Object.keys(BV), ...Object.keys(CONST)];
export { normColor, rgba };
