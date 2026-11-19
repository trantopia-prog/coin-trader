/* 공용: API 래퍼, 포맷터, 실시간 WebSocket, 상단바, 토스트, 테마 */
'use strict';

const KST_OFFSET = 9 * 3600; // 차트 시간축을 KST 로 표시하기 위한 오프셋 (lightweight-charts 는 UTC 로 그림)

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 저장 불가 환경 무시 */ } },
};

(function applyTheme() {
  const t = store.get('theme', null);
  if (t) document.documentElement.setAttribute('data-theme', t);
})();

const API = {
  async req(method, url, body) {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    if (r.status === 401 && !location.pathname.endsWith('/login.html')) {
      location.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search);
      throw new Error('로그인이 필요합니다');
    }
    let data = null;
    try { data = await r.json(); } catch { /* 본문 없음 */ }
    if (!r.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg)) msg = msg.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join(', ');
      throw new Error(msg || `요청 실패 (${r.status})`);
    }
    return data;
  },
  get(u) { return this.req('GET', u); },
  post(u, b) { return this.req('POST', u, b ?? {}); },
  put(u, b) { return this.req('PUT', u, b); },
  del(u) { return this.req('DELETE', u); },
};

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') p.set(k, v);
  return p.toString();
}

const fmt = {
  krw(n, d = 0) { return n === null || n === undefined || isNaN(n) ? '-' : Number(n).toLocaleString('ko-KR', { maximumFractionDigits: d, minimumFractionDigits: 0 }); },
  price(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    const a = Math.abs(n);
    const d = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : a >= 0.1 ? 3 : a >= 0.01 ? 4 : 6;
    return Number(n).toLocaleString('ko-KR', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  vol(n) { return n === null || n === undefined || isNaN(n) ? '-' : Number(n).toLocaleString('ko-KR', { maximumFractionDigits: 8 }); },
  pct(n, d = 2) { return n === null || n === undefined || isNaN(n) ? '-' : (n > 0 ? '+' : '') + Number(n).toFixed(d) + '%'; },
  big(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    if (n >= 1e12) return (n / 1e12).toFixed(1) + '조';
    if (n >= 1e8) return Math.round(n / 1e8).toLocaleString('ko-KR') + '억';
    if (n >= 1e4) return Math.round(n / 1e4).toLocaleString('ko-KR') + '만';
    return Math.round(n).toLocaleString('ko-KR');
  },
  time(v) {
    if (!v) return '-';
    const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
    return d.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  },
  ago(v) {
    const s = (Date.now() - new Date(v).getTime()) / 1000;
    if (s < 60) return '방금'; if (s < 3600) return Math.floor(s / 60) + '분 전';
    if (s < 86400) return Math.floor(s / 3600) + '시간 전'; return Math.floor(s / 86400) + '일 전';
  },
  cls(n) { return n > 0 ? 'up' : n < 0 ? 'down' : ''; },
};

const SIDE = { bid: '매수', ask: '매도' };
const STATE = { wait: '대기', done: '체결', cancel: '취소', rejected: '거부' };
const ORD_TYPE = { limit: '지정가', price: '시장가', market: '시장가' };
const IMPORTANCE = { listing: ['신규상장', 'up'], warning: ['유의', 'warn'], delisting: ['거래지원종료', 'warn'], release: ['유의해제', 'ok'], suspend: ['입출금중단', ''] };
const SOURCE = { upbit_notice: '업비트 공지', google_news: 'Google 뉴스', cryptopanic: 'CryptoPanic', disclosure: 'DAXA·쟁글' };
const INTERVAL_LABEL = { minute1: '1분', minute3: '3분', minute5: '5분', minute10: '10분', minute15: '15분', minute30: '30분', minute60: '1시간', minute240: '4시간', day: '일', week: '주', month: '월' };

/* ---------- 실시간 WebSocket (페이지당 1개) ---------- */
const RT = {
  ws: null, handlers: {}, connected: false, market: null, retry: 1000,
  on(topic, fn) { (this.handlers[topic] ||= []).push(fn); },
  emit(topic, data) { (this.handlers[topic] || []).forEach(fn => { try { fn(data); } catch (e) { console.error(e); } }); },
  select(market) { this.market = market; if (this.connected) this.ws.send(JSON.stringify({ market })); },
  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => { this.connected = true; this.retry = 1000; if (this.market) this.select(this.market); this.emit('_conn', true); };
    ws.onmessage = ev => { const m = JSON.parse(ev.data); this.emit(m.topic, m.data); };
    ws.onclose = ev => {
      this.connected = false; this.emit('_conn', false);
      if (ev.code === 4401) { location.href = '/login.html'; return; }
      setTimeout(() => this.connect(), this.retry); this.retry = Math.min(this.retry * 2, 15000);
    };
  },
};

/* ---------- 토스트 ---------- */
function toast(msg, type = 'info', ms = 4000) {
  let box = document.querySelector('.toasts');
  if (!box) { box = document.createElement('div'); box.className = 'toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
  const el = document.createElement('div');
  el.className = 'toast ' + type; el.textContent = msg;
  box.appendChild(el); setTimeout(() => el.remove(), ms);
}
async function guarded(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, 'error', 6000); return null; }
}

/* ---------- 상단바 ---------- */
const NAV = [['index.html', '트레이딩'], ['screener.html', '조건검색'], ['strategies.html', '자동매매'], ['backtest.html', '백테스트'], ['news.html', '뉴스·공시'], ['settings.html', '설정']];

function renderHeader() {
  const host = document.getElementById('app-header');
  if (!host) return;
  const page = location.pathname.split('/').pop() || 'index.html';
  host.innerHTML = `
  <div x-data="shell" x-init="init()">
    <template x-if="st.risk && st.risk.halted"><div class="banner halt">거래 정지 상태: <span x-text="st.risk.halt_reason"></span> — 신규 매수가 차단됩니다. <a href="/settings.html" style="color:#fff;text-decoration:underline">설정에서 재개</a></div></template>
    <template x-if="st.mode === 'live'"><div class="banner">실거래 모드: 실제 자산으로 주문이 실행됩니다</div></template>
    <header class="topbar">
      <div class="brand">Coin<b>Trader</b></div>
      <nav class="nav">${NAV.map(([h, l]) => `<a href="/${h}" class="${h === page ? 'active' : ''}">${l}</a>`).join('')}</nav>
      <div class="status">
        <span class="badge" :class="st.mode === 'live' ? 'live' : 'paper'" x-text="st.mode === 'live' ? '실거래' : '모의투자'"></span>
        <span class="muted" title="총 평가자산">자산 <b class="num" style="color:var(--text)" x-text="fmt.krw(st.equity)"></b>
          <span class="num" :class="fmt.cls(st.daily_pnl_pct)" x-show="st.daily_pnl_pct !== null && st.daily_pnl_pct !== undefined" x-text="'(' + fmt.pct(st.daily_pnl_pct) + ')'"></span></span>
        <span class="row muted" title="실시간 연결 상태"><span class="dot" :class="conn && st.ws_connected ? 'on' : 'off'"></span><span x-text="conn && st.ws_connected ? '실시간' : '연결 끊김'"></span></span>
        <button class="sm danger solid" @click="estop()" title="모든 신규 매수 차단 + 미체결 취소">긴급정지</button>
        <button class="sm ghost" @click="toggleTheme()" title="테마 전환" aria-label="테마 전환">◐</button>
        <button class="sm ghost" @click="logout()">로그아웃</button>
      </div>
    </header>
  </div>`;
}

document.addEventListener('alpine:init', () => {
  Alpine.data('shell', () => ({
    st: { mode: 'paper', equity: null, risk: null, ws_connected: false, daily_pnl_pct: null },
    conn: false,
    async init() {
      RT.on('engine', d => { this.st = d; window.dispatchEvent(new CustomEvent('engine-status', { detail: d })); });
      RT.on('_conn', c => { this.conn = c; });
      RT.on('log', l => { if (l.level === 'trade' || l.level === 'error') toast(l.message, l.level, 6000); });
      RT.on('news', n => { if (['warning', 'delisting'].includes(n.importance)) toast('[공지] ' + n.title, 'warning', 10000); });
      const s = await API.get('/api/status').catch(() => null);
      if (s) this.st = s;
    },
    async estop() {
      const liquidate = confirm('긴급정지: 신규 매수 차단 + 미체결 주문 취소\n\n[확인] 자동매매 포지션도 시장가로 전량 청산\n[취소] 청산 없이 정지만');
      const r = await guarded(() => API.post('/api/emergency-stop', { liquidate }));
      if (r) toast(`긴급정지 완료: 미체결 ${r.cancelled}건 취소, ${r.liquidated}건 청산`, 'warning', 8000);
    },
    toggleTheme() {
      const cur = document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next); store.set('theme', next);
      window.dispatchEvent(new Event('theme-change'));
    },
    async logout() { await API.post('/api/logout'); location.href = '/login.html'; },
  }));
});

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

/* lightweight-charts 공통 옵션 (테마 색상 반영) */
function chartOptions(extra = {}) {
  return Object.assign({
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: cssVar('--muted'), fontSize: 11 },
    grid: { vertLines: { color: cssVar('--border') }, horzLines: { color: cssVar('--border') } },
    rightPriceScale: { borderColor: cssVar('--border') },
    timeScale: { borderColor: cssVar('--border'), timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    autoSize: true,
  }, extra);
}

if (!location.pathname.endsWith('/login.html')) {
  renderHeader();
  RT.connect();
}
