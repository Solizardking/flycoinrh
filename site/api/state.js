// Same-origin proxy for the public site.
//
// Robinhood Chain first: the public node's CORS is unreliable (it
// intermittently answers "Access-Control-Allow-Origin: *,*", which browsers
// refuse), so this runs server-side and the page talks only to its origin.
//
// Optional Solana fields ride along on the same JSON. They are GET-only
// windows on clawd-ws /health, the Phoenix mark, and Stonkfun's public
// listings. Nothing here loads a key, and there is no path that can sign
// or submit a launch.

const RPC = process.env.FLY_RH_RPC || 'https://rpc.mainnet.chain.robinhood.com';
const TOKEN = (process.env.FLY_TOKEN || '0x4eb990547bce4a982432ca88cf5fae7eed1a2d35').toLowerCase();
const WALLET = process.env.FLY_WALLET || '0x6ce4085EfB52a6eBDb7d6989beb8860847f4b42A';
const BIRTH = process.env.FLY_TOKEN_BLOCK || '0x38DA606';
const TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
const FEE_ETH = 0.00055;

const CLAWD_HEALTH = process.env.FLY_CLAWD_HEALTH || 'https://clawd-ws.fly.dev/health';
const PHOENIX_MARK = process.env.FLY_PX_MARK || 'https://perp-api.phoenix.trade/v1/market/SOL/mark-price';
const STK_TOKENS = 'https://www.stonkfun.xyz/api/public/v1/tokens?sort=newest';
const STK_PAIRS = 'https://www.stonkfun.xyz/api/public/v1/pairs?launchable=true';

let holdersCache = { at: 0, holders: null, transfers: null };
const HOLD_TTL = 120000;

function timedFetch(url, ms = 6000) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  return fetch(url, { cache: 'no-store', signal: ac.signal })
    .finally(() => clearTimeout(t));
}

async function rpc(method, params) {
  const r = await fetch(RPC, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error.message || 'rpc error');
  return j.result;
}

const int = (h) => (h ? parseInt(h, 16) : 0);

function abiString(x) {
  if (!x || x.length < 130) return '';
  const n = parseInt(x.slice(66, 130), 16);
  let out = '';
  for (let i = 0; i < n; i++) out += String.fromCharCode(parseInt(x.substr(130 + i * 2, 2), 16));
  return out;
}

async function holders() {
  const now = Date.now();
  if (holdersCache.holders != null && now - holdersCache.at < HOLD_TTL) return holdersCache;
  try {
    const logs = await rpc('eth_getLogs', [{
      address: TOKEN, fromBlock: BIRTH, toBlock: 'latest', topics: [TRANSFER],
    }]);
    const seen = new Set();
    for (const l of logs) if (l.topics.length >= 3) seen.add('0x' + l.topics[2].slice(-40));
    seen.delete('0x' + '0'.repeat(40));
    holdersCache = { at: now, holders: seen.size, transfers: logs.length };
  } catch (e) { /* keep the last good numbers */ }
  return holdersCache;
}

function cut(s, n) {
  const x = s == null ? '' : String(s);
  return x.length > n ? x.slice(0, n) : x;
}

function numOrNull(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// clawd-ws /health also echoes rpc URLs. Those are not for this page.
function pickHealth(j) {
  if (!j || typeof j !== 'object') return { ok: false };
  return {
    ok: true,
    status: cut(j.status, 24) || null,
    clients: numOrNull(j.clients),
    totalLaunches: numOrNull(j.totalLaunches),
    solana: typeof j.solana === 'boolean' ? j.solana : null,
  };
}

function pickMark(j) {
  if (!j || typeof j !== 'object') return { ok: false, symbol: 'SOL' };
  if (j.error) return { ok: false, symbol: 'SOL', error: cut(j.error, 80) };
  const mp = j.markPrice;
  let price = null;
  if (mp && typeof mp === 'object') price = numOrNull(mp.price);
  else price = numOrNull(mp);
  return {
    ok: price != null,
    symbol: cut(j.symbol || 'SOL', 12),
    mark: price,
    slot: numOrNull(j.slot),
  };
}

function pickTokens(j) {
  const data = (j && j.data) || j || {};
  const rows = Array.isArray(data.tokens) ? data.tokens : [];
  return rows.slice(0, 8).map((t) => ({
    name: cut(t && t.name, 40),
    symbol: cut(t && t.symbol, 16),
    mint: cut(t && t.mint, 64),
    quote: cut(t && t.quote && t.quote.symbol, 12),
    marketCapUsd: numOrNull(t && t.market && t.market.marketCapUsd),
    status: cut(t && t.status, 16),
    createdAt: cut(t && t.createdAt, 40),
  }));
}

function pickPairs(j) {
  const data = (j && j.data) || j || {};
  const rows = Array.isArray(data.pairs) ? data.pairs : [];
  return rows.filter((p) => p && p.launchable).slice(0, 24).map((p) => ({
    symbol: cut(p.symbol, 16),
    name: cut(p.name, 28),
    mint: cut(p.mint, 64),
    category: cut(p.categoryLabel || p.category, 20),
    launchable: true,
  }));
}

async function oneJson(url) {
  const r = await timedFetch(url);
  if (!r.ok) throw new Error('http ' + r.status);
  return r.json();
}

async function solanaWindow() {
  const out = { ok: true, tape: 'https://clawd-ws.fly.dev/' };
  const jobs = [
    oneJson(CLAWD_HEALTH).then((j) => { out.clawdws = pickHealth(j); })
      .catch((e) => { out.clawdws = { ok: false, error: cut(e.message || e, 120) }; }),
    oneJson(PHOENIX_MARK).then((j) => { out.phoenix = pickMark(j); })
      .catch((e) => { out.phoenix = { ok: false, symbol: 'SOL', error: cut(e.message || e, 120) }; }),
    oneJson(STK_TOKENS).then((j) => { out.tokens = pickTokens(j); })
      .catch((e) => { out.tokens = []; out.tokens_error = cut(e.message || e, 120); }),
    oneJson(STK_PAIRS).then((j) => { out.pairs = pickPairs(j); })
      .catch((e) => { out.pairs = []; out.pairs_error = cut(e.message || e, 120); }),
  ];
  await Promise.all(jobs);
  return out;
}

async function robinhood() {
  const [blk, sup, sym, bal] = await Promise.all([
    rpc('eth_blockNumber', []),
    rpc('eth_call', [{ to: TOKEN, data: '0x18160ddd' }, 'latest']),
    rpc('eth_call', [{ to: TOKEN, data: '0x95d89b41' }, 'latest']),
    rpc('eth_getBalance', [WALLET, 'latest']),
  ]);
  const h = await holders();
  const eth = int(bal) / 1e18;
  return {
    ok: true,
    block: int(blk),
    budget_eth: eth,
    launches_left: Math.floor(eth / FEE_ETH),
    token: {
      address: TOKEN,
      symbol: abiString(sym),
      supply: Number(BigInt(sup)) / 1e18,
      holders: h.holders,
      transfers: h.transfers,
      pair: 'GOOGL',
      creator_tax_pct: 1,
    },
  };
}

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 's-maxage=15, stale-while-revalidate=60');
  const [rh, sol] = await Promise.all([
    robinhood().catch((e) => ({ ok: false, error: String(e.message || e).slice(0, 160) })),
    solanaWindow().catch((e) => ({ ok: false, error: String(e.message || e).slice(0, 160) })),
  ]);
  res.status(200).json({
    ...rh,
    solana: sol,
    updated: Math.floor(Date.now() / 1000),
  });
}
