/* 트레이딩 화면: 종목 목록, 실시간 차트, 호가, 주문, 계좌 */
'use strict';

// 차트 객체는 Alpine 반응성 프록시 밖에 둔다 (성능/호환성)
const C = { main: null, sub: null, candle: null, vol: null, lines: {}, subSeries: [], data: null, lastBar: null };

function bucketTime(tsSec, interval) {
  if (interval.startsWith('minute')) { const u = +interval.slice(6) * 60; return Math.floor(tsSec / u) * u; }
  if (interval === 'day') return Math.floor(tsSec / 86400) * 86400; // 업비트 일봉은 00:00 UTC(09:00 KST) 시작
  return null; // 주/월봉은 마지막 봉만 갱신
}

function priceFormat(p) {
  const a = Math.abs(p || 0);
  const precision = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : a >= 0.1 ? 3 : a >= 0.01 ? 4 : 6;
  return { type: 'price', precision, minMove: Math.pow(10, -precision) };
}

document.addEventListener('alpine:init', () => {
  Alpine.data('trade', () => ({
    intervals: ['minute1', 'minute5', 'minute15', 'minute60', 'minute240', 'day', 'week'],
    tickers: [], q: '', listTab: 'all', sortKey: 'value_24h',
    favs: store.get('favs', ['KRW-BTC', 'KRW-ETH']),
    market: new URLSearchParams(location.search).get('market') || store.get('market', 'KRW-BTC'),
    interval: store.get('interval', 'minute60'),
    ind: Object.assign({ ma: true, bb: false, vol: true, marks: true, sub: 'rsi' }, store.get('ind', {})),
    head: {}, asks: [], bids: [],
    of: { side: 'bid', type: 'limit', price: null, volume: null, amount: 10000 },
    balances: [], orders: [], openOrders: [], news: [], logs: [],
    tab: 'hold', busy: false, st: { mode: 'paper' },

    get symbol() { return this.market.split('-')[1]; },
    get info() { return this.tickers.find(t => t.market === this.market) || {}; },
    get avail() {
      const krw = this.balances.find(b => b.currency === 'KRW');
      const coin = this.balances.find(b => b.currency === this.symbol);
      return { krw: krw ? krw.balance : 0, coin: coin ? coin.balance : 0 };
    },
    get visibleTickers() {
      const q = this.q.trim().toLowerCase();
      let rows = this.tickers;
      if (this.listTab === 'fav') rows = rows.filter(t => this.favs.includes(t.market));
      if (this.listTab === 'hold') { const held = new Set(this.balances.map(b => 'KRW-' + b.currency)); rows = rows.filter(t => held.has(t.market)); }
      if (q) rows = rows.filter(t => t.symbol.toLowerCase().includes(q) || t.korean_name.toLowerCase().includes(q) || t.english_name.toLowerCase().includes(q));
      return [...rows].sort((a, b) => (this.sortKey === 'change_rate' ? b.change_rate - a.change_rate : b.value_24h - a.value_24h));
    },

    async init() {
      window.addEventListener('engine-status', e => { this.st = e.detail; });
      API.get('/api/status').then(s => { this.st = s; }).catch(() => {});
      this.$watch('ind', v => store.set('ind', v), { deep: true });
      this.buildChart();
      window.addEventListener('theme-change', () => { this.buildChart(); this.loadCandles(); });

      RT.on('ticker', t => this.onTicker(t));
      RT.on('trade', t => this.onTrade(t));
      RT.on('orderbook', ob => this.onOrderbook(ob));
      RT.on('order', () => { this.loadOrders(); this.loadBalances(); });
      RT.on('log', l => { this.logs.unshift(l); this.logs.length = Math.min(this.logs.length, 300); });
      RT.on('news', n => { if ((n.coins || []).includes(this.symbol)) this.news.unshift(n); });

      await Promise.all([this.loadTickers(), this.loadBalances(), this.loadLogs()]);
      this.selectMarket(this.market, true);
      setInterval(() => this.loadTickers(), 5000);
      setInterval(() => this.loadBalances(), 10000);
      setInterval(() => this.loadCandles(true), 60000); // 지표 재계산
    },

    buildChart() {
      const el = document.getElementById('chart'), sel = document.getElementById('subchart');
      if (C.main) { C.main.remove(); C.sub.remove(); }
      const up = cssVar('--up'), down = cssVar('--down');
      C.main = LightweightCharts.createChart(el, chartOptions({ localization: { locale: 'ko-KR', priceFormatter: p => fmt.price(p) } }));
      C.candle = C.main.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false });
      C.candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
      C.vol = C.main.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
      C.main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      const line = (color, style = 0) => C.main.addLineSeries({ color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      C.lines = {
        ma5: line('#f59e0b'), ma20: line('#a855f7'), ma60: line('#10b981'),
        bb_upper: line(cssVar('--muted'), 2), bb_mid: line(cssVar('--muted'), 1), bb_lower: line(cssVar('--muted'), 2),
      };
      C.sub = LightweightCharts.createChart(sel, chartOptions({ timeScale: { visible: false } }));
      C.main.timeScale().subscribeVisibleLogicalRangeChange(r => { if (r) C.sub.timeScale().setVisibleLogicalRange(r); });
    },

    async loadTickers() {
      try { this.tickers = await API.get('/api/tickers'); } catch (e) { /* 일시 오류는 다음 주기에 재시도 */ }
    },
    async loadBalances() {
      const r = await API.get('/api/balances').catch(() => null);
      if (r) this.balances = r.rows;
    },
    async loadOrders() {
      const [all, open] = await Promise.all([API.get('/api/orders?limit=100'), API.get('/api/orders?state=wait')]).catch(() => [[], []]);
      this.orders = all; this.openOrders = open;
      this.applyMarkers();
    },
    async loadNews() { this.news = await API.get('/api/news?' + qs({ coin: this.symbol, limit: 40 })).catch(() => []); },
    async loadLogs() { this.logs = await API.get('/api/logs?limit=200').catch(() => []); },

    selectMarket(m, first = false) {
      if (!first && m === this.market) return;
      this.market = m; store.set('market', m);
      history.replaceState(null, '', '?market=' + m);
      this.head = Object.assign({}, this.info);
      this.head.price = this.info.price;
      this.asks = []; this.bids = [];
      this.of.price = this.info.price || null; this.of.volume = null;
      RT.select(m);
      this.loadCandles();
      this.loadOrders();
      if (this.tab === 'news') this.loadNews(); else this.news = [];
      API.get('/api/orderbook?market=' + m).then(ob => this.onOrderbook(ob)).catch(() => {});
    },
    setInterval(iv) { this.interval = iv; store.set('interval', iv); this.loadCandles(); },
    toggleFav(m) { this.favs = this.favs.includes(m) ? this.favs.filter(x => x !== m) : [...this.favs, m]; store.set('favs', this.favs); },

    async loadCandles(keepRange = false) {
      const market = this.market, interval = this.interval;
      let d;
      try { d = await API.get('/api/candles?' + qs({ market, interval, count: 400 })); } catch (e) { toast(e.message, 'error'); return; }
      if (market !== this.market || interval !== this.interval) return; // 그 사이 종목/주기 변경
      const range = keepRange ? C.main.timeScale().getVisibleLogicalRange() : null;
      C.data = d;
      const up = cssVar('--up'), down = cssVar('--down');
      const bars = d.candles.map(c => ({ time: c.time + KST_OFFSET, open: c.open, high: c.high, low: c.low, close: c.close }));
      C.candle.applyOptions({ priceFormat: priceFormat(bars.length ? bars[bars.length - 1].close : 1) });
      C.candle.setData(bars);
      C.vol.setData(d.candles.map(c => ({ time: c.time + KST_OFFSET, value: c.volume, color: c.close >= c.open ? up + '66' : down + '66' })));
      C.lastBar = bars.length ? { ...bars[bars.length - 1], volume: d.candles[d.candles.length - 1].volume } : null;
      this.applyIndicators();
      this.applyMarkers();
      if (range) C.main.timeScale().setVisibleLogicalRange(range); else C.main.timeScale().scrollToRealTime();
    },

    applyIndicators() {
      const d = C.data; if (!d) return;
      const times = d.candles.map(c => c.time + KST_OFFSET);
      const ser = arr => arr.map((v, i) => (v === null ? { time: times[i] } : { time: times[i], value: v }));
      for (const k of ['ma5', 'ma20', 'ma60']) C.lines[k].setData(this.ind.ma ? ser(d.indicators[k]) : []);
      for (const k of ['bb_upper', 'bb_mid', 'bb_lower']) C.lines[k].setData(this.ind.bb ? ser(d.indicators[k]) : []);
      C.vol.applyOptions({ visible: this.ind.vol });
      C.subSeries.forEach(s => C.sub.removeSeries(s)); C.subSeries = [];
      if (this.ind.sub === 'rsi') {
        const s = C.sub.addLineSeries({ color: '#a855f7', lineWidth: 1, priceLineVisible: false });
        s.setData(ser(d.indicators.rsi14));
        s.createPriceLine({ price: 70, color: cssVar('--up'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true });
        s.createPriceLine({ price: 30, color: cssVar('--down'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true });
        C.subSeries.push(s);
      } else if (this.ind.sub === 'macd') {
        const h = C.sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
        h.setData(d.indicators.macd_hist.map((v, i) => (v === null ? { time: times[i] } : { time: times[i], value: v, color: v >= 0 ? cssVar('--up') + '88' : cssVar('--down') + '88' })));
        const m = C.sub.addLineSeries({ color: '#f59e0b', lineWidth: 1, priceLineVisible: false });
        const sg = C.sub.addLineSeries({ color: '#3b82f6', lineWidth: 1, priceLineVisible: false });
        m.setData(ser(d.indicators.macd)); sg.setData(ser(d.indicators.macd_signal));
        C.subSeries.push(h, m, sg);
      }
      const r = C.main.timeScale().getVisibleLogicalRange(); if (r) C.sub.timeScale().setVisibleLogicalRange(r);
    },

    applyMarkers() {
      if (!C.candle || !C.data) return;
      if (!this.ind.marks) { C.candle.setMarkers([]); return; }
      const first = C.data.candles.length ? C.data.candles[0].time : 0;
      const marks = this.orders
        .filter(o => o.market === this.market && o.executed_volume > 0)
        .map(o => {
          const t = Math.floor(new Date(o.created_at).getTime() / 1000);
          const b = bucketTime(t, this.interval);
          return { t: b === null ? t : b, o };
        })
        .filter(x => x.t >= first)
        .sort((a, b) => a.t - b.t)
        .map(({ t, o }) => ({
          time: t + KST_OFFSET, position: o.side === 'bid' ? 'belowBar' : 'aboveBar', shape: o.side === 'bid' ? 'arrowUp' : 'arrowDown',
          color: o.side === 'bid' ? cssVar('--up') : cssVar('--down'), text: o.side === 'bid' ? 'B' : 'S',
        }));
      C.candle.setMarkers(marks);
    },

    onTicker(t) {
      const row = this.tickers.find(x => x.market === t.code);
      if (row) { row.price = t.trade_price; row.change_rate = t.signed_change_rate; row.value_24h = t.acc_trade_price_24h; }
      if (t.code === this.market) {
        this.head = { ...this.head, price: t.trade_price, change_rate: t.signed_change_rate, high: t.high_price, low: t.low_price, value_24h: t.acc_trade_price_24h };
        if (!this.of.price) this.of.price = t.trade_price;
      }
    },

    onTrade(t) {
      if (t.code !== this.market || !C.lastBar) return;
      const ts = Math.floor(t.trade_timestamp / 1000);
      const b = bucketTime(ts, this.interval);
      const price = t.trade_price;
      let bar = C.lastBar;
      if (b !== null && b + KST_OFFSET > bar.time) {
        bar = { time: b + KST_OFFSET, open: price, high: price, low: price, close: price, volume: 0 };
      } else if (b !== null && b + KST_OFFSET < bar.time) {
        return; // 지연 도착한 과거 체결
      }
      bar.high = Math.max(bar.high, price); bar.low = Math.min(bar.low, price); bar.close = price;
      bar.volume += t.trade_volume;
      C.lastBar = bar;
      C.candle.update({ time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close });
      C.vol.update({ time: bar.time, value: bar.volume, color: (bar.close >= bar.open ? cssVar('--up') : cssVar('--down')) + '66' });
    },

    onOrderbook(ob) {
      if (!ob || ob.code !== this.market && ob.market !== this.market) return;
      const units = (ob.orderbook_units || []).slice(0, 12);
      const max = Math.max(1e-12, ...units.map(u => Math.max(u.ask_size, u.bid_size)));
      this.asks = units.map(u => ({ ...u, w: (u.ask_size / max) * 100 })).reverse();
      this.bids = units.map(u => ({ ...u, w: (u.bid_size / max) * 100 }));
    },

    pickPrice(p) { this.of.type = 'limit'; this.of.price = p; },
    fillPct(p) {
      const r = p / 100;
      if (this.of.side === 'bid') {
        const krw = Math.floor(this.avail.krw * r / 1.0005);
        if (this.of.type === 'market') this.of.amount = krw;
        else if (this.of.price) this.of.volume = +(krw / this.of.price).toFixed(8);
      } else {
        this.of.volume = +(this.avail.coin * r).toFixed(8);
      }
    },
    async submitOrder() {
      const f = this.of;
      let body;
      if (f.type === 'limit') body = { market: this.market, side: f.side, ord_type: 'limit', price: f.price, volume: f.volume };
      else if (f.side === 'bid') body = { market: this.market, side: 'bid', ord_type: 'price', price: f.amount };
      else body = { market: this.market, side: 'ask', ord_type: 'market', volume: f.volume };
      const what = `${this.market} ${SIDE[f.side]} ${f.type === 'limit' ? '지정가' : '시장가'}`;
      if (this.st.mode === 'live' && !confirm(`[실거래] ${what} 주문을 실행할까요?`)) return;
      this.busy = true;
      const r = await guarded(() => API.post('/api/orders', body));
      this.busy = false;
      if (r) {
        if (r.ok) toast(`${what} 주문 ${STATE[r.state] || r.state}`, 'trade');
        else toast(`주문 거부: ${r.error}`, 'error', 7000);
        this.loadBalances(); this.loadOrders();
      }
    },
    async cancelOrder(id) { if (await guarded(() => API.del('/api/orders/' + id), '주문 취소 요청')) this.loadOrders(); },
  }));
});
