/* 전략 유형/파라미터 폼 공용 로직 (자동매매, 백테스트 페이지에서 사용) */
'use strict';

const StrategyForm = {
  types: [],
  async load() { if (!this.types.length) this.types = await API.get('/api/strategies/types'); return this.types; },
  spec(type) { return this.types.find(t => t.type === type); },
  defaults(type) {
    const s = this.spec(type); const p = {};
    (s ? s.params : []).forEach(x => { p[x.key] = x.default; });
    return p;
  },
  /* 그리드 전략은 현재가 기준으로 ±10% 구간을 기본값으로 제안 */
  async suggest(type, market, params) {
    if (type !== 'grid' || (params.lower > 0 && params.upper > 0)) return params;
    try {
      const t = (await API.get('/api/tickers')).find(x => x.market === market);
      if (t) { params.lower = Math.round(t.price * 0.9); params.upper = Math.round(t.price * 1.1); }
    } catch { /* 무시 */ }
    return params;
  },
};
