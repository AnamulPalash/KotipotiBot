#!/usr/bin/env python3
"""
Kotipoti Dashboard Server
=========================
Serves a self-contained HTML dashboard (no CDN, no React, no external deps).
Proxies /api/v1/* to the Freqtrade REST API with Basic Auth server-side.

Environment variables:
  FREQTRADE_API       Freqtrade base URL  (default: http://localhost:8090)
  FREQTRADE_USERNAME  REST API username   (default: kotipoti)
  FREQTRADE_PASSWORD  REST API password   (read from .env)
  DASHBOARD_PORT      Port for this app   (default: 5000)
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os
from pathlib import Path

app = Flask(__name__)
CORS(app)

FREQTRADE_API      = os.environ.get("FREQTRADE_API", "http://localhost:8090")
FREQTRADE_USERNAME = os.environ.get("FREQTRADE_USERNAME", "kotipoti")
FREQTRADE_PASSWORD = os.environ.get("FREQTRADE_PASSWORD", "")
DASHBOARD_PORT     = int(os.environ.get("DASHBOARD_PORT", 5000))

def _auth():
    return (FREQTRADE_USERNAME, FREQTRADE_PASSWORD) if FREQTRADE_PASSWORD else None


# ── Self-contained HTML (no external CDN) ─────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KotipotiBot Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:linear-gradient(160deg,#0F172A 0%,#1A2540 100%);
     color:#F8FAFC;min-height:100vh;padding:1.5rem}
.wrap{max-width:1400px;margin:0 auto}
h1{font-size:26px;font-weight:700;letter-spacing:-.5px}
.subtitle{font-size:13px;color:#94A3B8;margin-top:4px}
.header{display:flex;justify-content:space-between;align-items:flex-start;
        margin-bottom:1.5rem;flex-wrap:wrap;gap:12px}
.header-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.badge{display:flex;align-items:center;gap:6px;border-radius:999px;
       padding:5px 12px;font-size:12px;font-weight:600}
.badge-dot{width:7px;height:7px;border-radius:50%}
.btn{background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.3);
     color:#60A5FA;border-radius:8px;padding:6px 12px;font-size:12px;
     cursor:pointer;font-weight:500}
.btn:hover{background:rgba(59,130,246,.25)}
.ts{font-size:11px;color:#94A3B8}
.warn{font-size:12px;color:#FB923C;background:rgba(251,146,60,.1);
      border:1px solid rgba(251,146,60,.3);border-radius:6px;padding:4px 10px}

/* KPI grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
          gap:1rem;margin-bottom:1.5rem}
.kpi{border-radius:14px;padding:1.25rem;border:1px solid rgba(148,163,184,.12)}
.kpi-label{font-size:11px;color:#94A3B8;font-weight:500;
           text-transform:uppercase;letter-spacing:.6px}
.kpi-value{font-size:26px;font-weight:700;margin-top:.4rem;letter-spacing:-.5px}
.kpi-sub{font-size:12px;color:#94A3B8;margin-top:.5rem}
.bar-track{height:5px;background:rgba(148,163,184,.15);border-radius:3px;
           overflow:hidden;margin-top:8px}
.bar-fill{height:100%;border-radius:3px}

/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:1.25rem;
      background:rgba(30,41,59,.6);border-radius:10px;
      padding:4px;width:fit-content;flex-wrap:wrap}
.tab{border:none;padding:7px 14px;border-radius:7px;font-size:12px;
     font-weight:500;cursor:pointer;white-space:nowrap;
     background:transparent;color:#94A3B8}
.tab.active{background:linear-gradient(135deg,#3B82F6,#2563EB);color:#fff}

/* Cards / tables */
.card{background:rgba(30,41,59,.85);border:1px solid rgba(148,163,184,.12);
      border-radius:14px;padding:1.25rem;margin-bottom:1rem}
.card-title{font-size:14px;font-weight:600;color:#E2E8F0;margin-bottom:1rem}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}
.full-col{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:right;padding:8px 10px;color:#94A3B8;font-weight:500;
   font-size:11px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
th.left{text-align:left}
td{padding:10px 10px;color:#CBD5E1;text-align:right;
   border-bottom:1px solid rgba(148,163,184,.05)}
td.left{text-align:left}
tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:rgba(51,65,85,.2)}
.thead-row{border-bottom:1px solid rgba(148,163,184,.1)}

/* Trade rows */
.trade-row{display:flex;justify-content:space-between;align-items:center;
           padding:8px 10px;border-radius:8px;margin-bottom:7px;
           background:rgba(51,65,85,.6)}
.dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.pair-name{font-size:13px;font-weight:600;color:#F8FAFC}
.pair-sub{font-size:11px;color:#94A3B8;margin-left:6px}
.pnl-right{text-align:right}
.pnl-val{font-size:13px;font-weight:600}
.pnl-pct{font-size:11px;color:#94A3B8;margin-left:6px}

/* Config chips */
.cfg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px}
.cfg-chip{background:rgba(51,65,85,.4);border-radius:8px;padding:10px 12px}
.cfg-chip-label{font-size:11px;color:#94A3B8;text-transform:uppercase;
                letter-spacing:.6px;margin-bottom:4px}
.cfg-chip-val{font-size:13px;font-weight:600;color:#E2E8F0}

/* Badge SHORT/LONG */
.dir-short{background:rgba(239,68,68,.15);color:#F87171;font-size:11px;
           font-weight:700;padding:3px 8px;border-radius:5px}
.dir-long{background:rgba(16,185,129,.15);color:#34D399;font-size:11px;
          font-weight:700;padding:3px 8px;border-radius:5px}
.exit-badge{font-size:11px;padding:3px 8px;border-radius:5px;font-weight:500}
.exit-stop{background:rgba(239,68,68,.12);color:#F87171}
.exit-default{background:rgba(148,163,184,.1);color:#94A3B8}

.empty{text-align:center;padding:2rem 0;color:#94A3B8;font-size:13px}
.footer{text-align:center;margin-top:2rem;font-size:11px;color:rgba(148,163,184,.35)}
.spinner{width:40px;height:40px;border:3px solid rgba(148,163,184,.2);
         border-top:3px solid #3B82F6;border-radius:50%;
         animation:spin .8s linear infinite;margin:0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-wrap{display:flex;flex-direction:column;align-items:center;
              justify-content:center;min-height:100vh;gap:16px;color:#94A3B8}
.g{color:#10B981} .r{color:#EF4444} .m{color:#94A3B8}
.overflow-x{overflow-x:auto}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap" id="app">
  <div class="loading-wrap">
    <div class="spinner"></div>
    <p>Connecting to Freqtrade…</p>
  </div>
</div>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const el = (tag, attrs={}, ...children) => {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
};
const fmt  = (n, d=2) => n == null ? '—' : Number(n).toFixed(d);
const fmtU = n => n == null ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + ' USDT';
const fmtP = n => n == null ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
const colorClass = n => n == null ? '' : n >= 0 ? 'g' : 'r';
const exitLabel = r => ({roi:'ROI',stoploss:'Stop',trailing_stop_loss:'Trail',
  exit_signal:'Signal',force_exit:'Force',liquidation:'⚠ Liq'})[r] || r || '—';
const holdDur = d => {
  if (!d) return '—';
  const s = Math.floor((Date.now() - new Date(d).getTime()) / 1000);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};

// ── State ──────────────────────────────────────────────────────────────────
let state = {
  balance: null, openTrades: [], profit: null,
  performance: [], recentTrades: [], botStatus: null,
  error: null, lastUpdated: null, activeTab: 'overview'
};

// ── Fetch ──────────────────────────────────────────────────────────────────
async function apiFetch(path) {
  const r = await fetch('/api/v1/' + path);
  if (!r.ok) throw new Error(path + ': HTTP ' + r.status);
  return r.json();
}

async function fetchAll() {
  try {
    const [bal, status, prof, perf, trades, cfg] = await Promise.all([
      apiFetch('balance').catch(()=>null),
      apiFetch('status').catch(()=>[]),
      apiFetch('profit').catch(()=>null),
      apiFetch('performance').catch(()=>[]),
      apiFetch('trades?limit=30').catch(()=>({trades:[]})),
      apiFetch('show_config').catch(()=>null),
    ]);
    state.balance     = bal;
    state.openTrades  = Array.isArray(status) ? status : [];
    state.profit      = prof;
    state.performance = Array.isArray(perf) ? perf : [];
    state.recentTrades = ((trades && trades.trades) || [])
      .filter(t => !t.is_open).slice(-20).reverse();
    state.botStatus   = cfg;
    state.error       = null;
    state.lastUpdated = new Date();
  } catch(e) {
    state.error = e.message;
  }
  render();
}

// ── Render ─────────────────────────────────────────────────────────────────
function render() {
  const app = document.getElementById('app');
  app.innerHTML = '';
  app.appendChild(buildUI());
}

function buildUI() {
  const s = state;
  const wrap = el('div', {class:'wrap'});

  // ── Header ──
  const isRunning = s.botStatus && s.botStatus.state === 'running';
  const badgeColor = isRunning ? '#10B981' : '#EF4444';
  const badgeBg = isRunning
    ? 'background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);color:#10B981'
    : 'background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.3);color:#EF4444';

  const header = el('div', {class:'header'},
    el('div', {},
      el('h1', {}, '🤖 KotipotiBot'),
      el('p', {class:'subtitle'}, 'ScalpBot · Bybit Futures · Dry Run · 5m')
    ),
    el('div', {class:'header-right'},
      el('div', {class:'badge', style: badgeColor},
        el('div', {class:'badge-dot', style:`background:${badgeColor}`}),
        s.botStatus ? (isRunning ? 'Running' : (s.botStatus.state || 'Unknown')) : 'Unknown'
      ),
      s.error ? el('span', {class:'warn'}, '⚠ Partial data') : null,
      el('span', {class:'ts'}, s.lastUpdated ? 'Updated ' + s.lastUpdated.toLocaleTimeString() : ''),
      el('button', {class:'btn', onclick:'fetchAll()'}, '↻ Refresh')
    )
  );
  wrap.appendChild(header);

  // ── KPI row ──
  const bal    = s.balance;
  const walletVal   = bal && (bal.total != null ? bal.total : bal.value);
  const walletStart = bal && bal.starting_capital;
  const walletPnl   = (walletVal != null && walletStart != null) ? walletVal - walletStart : null;
  const walletPct   = walletStart && walletPnl != null ? walletPnl / walletStart * 100 : null;

  const p = s.profit || {};
  const totalPnl    = p.profit_all_coin;
  const totalPct    = p.profit_all_percent_mean;
  const tradeCount  = p.trade_count || 0;
  const wins        = p.winning_trades || 0;
  const losses      = p.losing_trades  || 0;
  const winRate     = tradeCount > 0 ? (wins / tradeCount * 100).toFixed(1) : null;
  const pf          = p.profit_factor;
  const maxDD       = p.max_drawdown;
  const avgDur      = p.avg_duration;
  const openPnl     = s.openTrades.reduce((a,t) => a + (t.profit_abs||0), 0);

  const kpiGrid = el('div', {class:'kpi-grid'});

  // Wallet
  kpiGrid.appendChild(kpiCard({
    label: 'Dry-Run Wallet',
    value: walletVal != null ? fmt(walletVal, 2) + ' USDT' : '—',
    sub: walletPct != null
      ? `<span class="${colorClass(walletPct)}">${fmtP(walletPct)} since start (${fmtU(walletPnl)})</span>`
      : 'No balance data',
    border: 'rgba(59,130,246,.25)',
    bg: 'linear-gradient(135deg,rgba(59,130,246,.08) 0%,rgba(139,92,246,.08) 100%)'
  }));

  // Total P&L
  kpiGrid.appendChild(kpiCard({
    label: 'Total Realised P&L',
    value: fmtU(totalPnl),
    valueClass: colorClass(totalPnl),
    sub: fmtP(totalPct) + ' avg · ' + tradeCount + ' trades',
    border: (totalPnl >= 0) ? 'rgba(16,185,129,.25)' : 'rgba(239,68,68,.25)',
    bg: (totalPnl >= 0)
      ? 'linear-gradient(135deg,rgba(16,185,129,.08) 0%,rgba(5,150,105,.08) 100%)'
      : 'linear-gradient(135deg,rgba(239,68,68,.08) 0%,rgba(185,28,28,.08) 100%)'
  }));

  // Win rate with bar
  const wrCard = kpiCard({
    label: 'Win Rate',
    value: winRate != null ? winRate + '%' : '—',
    sub: wins + 'W · ' + losses + 'L · PF: ' + (pf != null ? fmt(pf) : '—'),
    border: 'rgba(168,85,247,.25)',
    bg: 'linear-gradient(135deg,rgba(168,85,247,.08) 0%,rgba(236,72,153,.08) 100%)'
  });
  if (winRate != null) {
    const track = el('div', {class:'bar-track'});
    track.appendChild(el('div', {class:'bar-fill',
      style:`width:${winRate}%;background:linear-gradient(90deg,#A855F7,#EC4899)`}));
    wrCard.querySelector('.kpi-sub').before(track);
  }
  kpiGrid.appendChild(wrCard);

  // Open positions
  kpiGrid.appendChild(kpiCard({
    label: 'Open Positions',
    value: String(s.openTrades.length),
    sub: `<span class="${colorClass(openPnl)}">Unrealised: ${fmtU(openPnl)}</span>`,
    border: 'rgba(245,158,11,.25)',
    bg: 'linear-gradient(135deg,rgba(245,158,11,.08) 0%,rgba(251,146,60,.08) 100%)'
  }));

  // Max drawdown
  kpiGrid.appendChild(kpiCard({
    label: 'Max Drawdown',
    value: maxDD != null ? fmtP(maxDD * -100) : '—',
    valueClass: (maxDD != null && maxDD > 0.05) ? 'r' : '',
    sub: 'Avg hold: ' + (avgDur || '—')
  }));

  wrap.appendChild(kpiGrid);

  // ── Tabs ──
  const tabDefs = [
    {id:'overview', label:'📊 Overview'},
    {id:'open',     label:'📉 Open (' + s.openTrades.length + ')'},
    {id:'closed',   label:'✅ Closed'},
    {id:'pairs',    label:'🏅 Pairs'},
  ];
  const tabBar = el('div', {class:'tabs'});
  for (const t of tabDefs) {
    const btn = el('button', {
      class: 'tab' + (state.activeTab === t.id ? ' active' : ''),
      onclick: `setTab('${t.id}')`
    }, t.label);
    tabBar.appendChild(btn);
  }
  wrap.appendChild(tabBar);

  // ── Tab content ──
  const content = el('div', {});
  switch (state.activeTab) {
    case 'overview': content.appendChild(buildOverview()); break;
    case 'open':     content.appendChild(buildOpen());     break;
    case 'closed':   content.appendChild(buildClosed());   break;
    case 'pairs':    content.appendChild(buildPairs());    break;
  }
  wrap.appendChild(content);

  wrap.appendChild(el('p', {class:'footer'},
    'KotipotiBot Dashboard · auto-refreshes every 30s · read-only'));
  return wrap;
}

function kpiCard({label, value, valueClass='', sub='', border='rgba(148,163,184,.12)', bg='rgba(30,41,59,.85)'}) {
  const card = el('div', {class:'kpi', style:`border-color:${border};background:${bg}`});
  card.appendChild(el('div', {class:'kpi-label'}, label));
  card.appendChild(el('div', {class:`kpi-value ${valueClass}`}, value));
  card.appendChild(el('div', {class:'kpi-sub', html: sub}));
  return card;
}

// ── Overview tab ───────────────────────────────────────────────────────────
function buildOverview() {
  const wrap = el('div', {class:'two-col'});

  // Recent closed trades
  const recentCard = el('div', {class:'card'});
  recentCard.appendChild(el('div', {class:'card-title'}, 'Recent Closed Trades'));
  const rt = state.recentTrades.slice(0, 8);
  if (!rt.length) {
    recentCard.appendChild(el('p', {class:'empty'}, 'No closed trades yet'));
  } else {
    for (const t of rt) {
      const pnl = t.profit_abs || 0;
      const pct = (t.profit_ratio || 0) * 100;
      const cc  = colorClass(pnl);
      const row = el('div', {class:'trade-row',
        style:`border:1px solid ${pnl>=0 ? 'rgba(16,185,129,.15)' : 'rgba(239,68,68,.15)'}`});
      row.appendChild(el('div', {style:'display:flex;align-items:center;gap:8px'},
        el('div', {class:'dot', style:`background:${pnl>=0?'#10B981':'#EF4444'}`}),
        el('div', {},
          el('span', {class:'pair-name'}, (t.pair||'').split('/')[0]),
          el('span', {class:'pair-sub'}, exitLabel(t.exit_reason))
        )
      ));
      row.appendChild(el('div', {class:'pnl-right'},
        el('span', {class:`pnl-val ${cc}`}, fmtU(pnl)),
        el('span', {class:'pnl-pct'}, '(' + fmtP(pct) + ')')
      ));
      recentCard.appendChild(row);
    }
  }
  wrap.appendChild(recentCard);

  // Top pairs bar chart
  const pairsCard = el('div', {class:'card'});
  pairsCard.appendChild(el('div', {class:'card-title'}, 'Top Pairs by P&L'));
  const topPairs = [...state.performance]
    .sort((a,b) => (b.profit_abs||0) - (a.profit_abs||0)).slice(0,5);
  if (!topPairs.length) {
    pairsCard.appendChild(el('p', {class:'empty'}, 'No performance data yet'));
  } else {
    const maxAbs = Math.max(...topPairs.map(p => Math.abs(p.profit_abs||0)), 1);
    for (const p of topPairs) {
      const isPos = (p.profit_abs||0) >= 0;
      const barW  = Math.abs(p.profit_abs||0) / maxAbs * 100;
      const pDiv  = el('div', {style:'margin-bottom:12px'});
      pDiv.appendChild(el('div', {style:'display:flex;justify-content:space-between;margin-bottom:4px'},
        el('span', {style:'font-size:13px;font-weight:600;color:#E2E8F0'},
          (p.pair||'').split('/')[0]),
        el('span', {class: isPos?'g':'r', style:'font-size:12px;font-weight:600'},
          fmtU(p.profit_abs) + ' · ' + (p.count||0) + ' trades')
      ));
      const track = el('div', {class:'bar-track'});
      track.appendChild(el('div', {class:'bar-fill',
        style:`width:${barW}%;background:${isPos
          ? 'linear-gradient(90deg,#10B981,#059669)'
          : 'linear-gradient(90deg,#EF4444,#B91C1C)'}`}));
      pDiv.appendChild(track);
      pairsCard.appendChild(pDiv);
    }
  }
  wrap.appendChild(pairsCard);

  // Config chips (full width)
  const cfg = state.botStatus || {};
  const cfgCard = el('div', {class:'card full-col'});
  cfgCard.appendChild(el('div', {class:'card-title'}, 'Bot Configuration'));
  const cfgGrid = el('div', {class:'cfg-grid'});
  const cfgItems = [
    ['Strategy',       cfg.strategy       || 'ShortScalper'],
    ['Mode',           cfg.dry_run != null ? (cfg.dry_run ? 'Dry Run' : 'Live') : '—'],
    ['Exchange',       cfg.exchange        || 'Bybit'],
    ['Timeframe',      cfg.timeframe       || '5m'],
    ['Max Trades',     cfg.max_open_trades ?? 3],
    ['Stake Amount',   cfg.stake_amount    ? cfg.stake_amount + ' USDT' : '200 USDT'],
    ['State',          cfg.state           || '—'],
    ['Trading Mode',   cfg.trading_mode    || 'futures'],
  ];
  for (const [lbl, val] of cfgItems) {
    const chip = el('div', {class:'cfg-chip'});
    chip.appendChild(el('div', {class:'cfg-chip-label'}, lbl));
    chip.appendChild(el('div', {class:'cfg-chip-val'}, String(val)));
    cfgGrid.appendChild(chip);
  }
  cfgCard.appendChild(cfgGrid);
  wrap.appendChild(cfgCard);

  return wrap;
}

// ── Open trades tab ────────────────────────────────────────────────────────
function buildOpen() {
  const card = el('div', {class:'card'});
  card.appendChild(el('div', {class:'card-title'}, 'Active Positions'));
  if (!state.openTrades.length) {
    card.appendChild(el('p', {class:'empty'}, 'No open positions right now'));
    return card;
  }
  const wrap = el('div', {class:'overflow-x'});
  const tbl = el('table', {});
  tbl.appendChild(el('thead', {},
    el('tr', {class:'thead-row'},
      el('th',{class:'left'},'Pair'), el('th',{},'Direction'),
      el('th',{},'Entry'), el('th',{},'Current'),
      el('th',{},'Stake'), el('th',{},'Unrealised P&L'),
      el('th',{},'Holding'), el('th',{},'Stop Loss')
    )
  ));
  const tbody = el('tbody', {});
  for (const t of state.openTrades) {
    const pnl = t.profit_abs || 0;
    const pct = (t.profit_ratio || 0) * 100;
    const isShort = t.is_short;
    const dirEl = el('span', {class: isShort ? 'dir-short' : 'dir-long'},
      isShort ? 'SHORT' : 'LONG');
    const pnlEl = el('div', {},
      el('span', {class:`pnl-val ${colorClass(pnl)}`}, fmtU(pnl)),
      el('br',{}),
      el('span', {class: colorClass(pnl), style:'font-size:11px'}, fmtP(pct))
    );
    const row = el('tr', {},
      el('td', {class:'left', html:`<span style="font-weight:700;color:#F8FAFC">${t.pair}</span><br><span style="font-size:11px;color:#94A3B8">#${t.trade_id}</span>`}),
      el('td', {}, dirEl),
      el('td', {}, fmt(t.open_rate, 4)),
      el('td', {}, fmt(t.current_rate, 4)),
      el('td', {}, fmt(t.stake_amount, 2) + ' USDT'),
      el('td', {}, pnlEl),
      el('td', {}, holdDur(t.open_date)),
      el('td', {style:'color:#FB923C;font-size:12px'}, t.stop_loss_abs ? fmt(t.stop_loss_abs, 4) : '—')
    );
    tbody.appendChild(row);
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

// ── Closed trades tab ──────────────────────────────────────────────────────
function buildClosed() {
  const card = el('div', {class:'card'});
  card.appendChild(el('div', {class:'card-title'},
    'Last ' + state.recentTrades.length + ' Closed Trades'));
  if (!state.recentTrades.length) {
    card.appendChild(el('p', {class:'empty'}, 'No closed trades yet'));
    return card;
  }
  const wrap = el('div', {class:'overflow-x'});
  const tbl  = el('table', {});
  tbl.appendChild(el('thead', {},
    el('tr', {class:'thead-row'},
      el('th',{class:'left'},'#'), el('th',{class:'left'},'Pair'),
      el('th',{class:'left'},'Dir'), el('th',{},'Entry'),
      el('th',{},'Exit'), el('th',{},'P&L (USDT)'),
      el('th',{},'P&L %'), el('th',{},'Exit Reason'), el('th',{},'Duration')
    )
  ));
  const tbody = el('tbody', {});
  for (const t of state.recentTrades) {
    const pnl  = t.profit_abs || 0;
    const pct  = (t.profit_ratio || 0) * 100;
    const cc   = colorClass(pnl);
    const isStop = t.exit_reason === 'stoploss';
    const row = el('tr', {},
      el('td', {class:'left', style:'color:#94A3B8;font-size:11px'}, '#' + t.trade_id),
      el('td', {class:'left', style:'font-weight:600;color:#F8FAFC'}, t.pair),
      el('td', {class:'left'}, el('span', {class: t.is_short ? 'dir-short' : 'dir-long'},
        t.is_short ? 'S' : 'L')),
      el('td', {}, fmt(t.open_rate, 4)),
      el('td', {}, fmt(t.close_rate, 4)),
      el('td', {class: cc, style:'font-weight:600'}, fmtU(pnl)),
      el('td', {class: cc, style:'font-weight:600'}, fmtP(pct)),
      el('td', {}, el('span', {class:`exit-badge ${isStop?'exit-stop':'exit-default'}`},
        exitLabel(t.exit_reason))),
      el('td', {}, t.trade_duration_s ? Math.floor(t.trade_duration_s/60) + 'm' : '—')
    );
    tbody.appendChild(row);
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

// ── Pairs tab ──────────────────────────────────────────────────────────────
function buildPairs() {
  const card = el('div', {class:'card'});
  card.appendChild(el('div', {class:'card-title'}, 'Performance by Pair'));
  if (!state.performance.length) {
    card.appendChild(el('p', {class:'empty'}, 'No performance data yet'));
    return card;
  }
  const sorted = [...state.performance].sort((a,b)=>(b.profit_abs||0)-(a.profit_abs||0));
  const wrap = el('div', {class:'overflow-x'});
  const tbl  = el('table', {});
  tbl.appendChild(el('thead', {},
    el('tr', {class:'thead-row'},
      el('th',{class:'left'},'Pair'), el('th',{},'Trades'),
      el('th',{},'Win Rate'), el('th',{},'Total P&L'),
      el('th',{},'Avg P&L / trade'), el('th',{},'Avg %')
    )
  ));
  const tbody = el('tbody', {});
  for (const p of sorted) {
    const wr   = p.count > 0 ? ((p.wins||0) / p.count * 100).toFixed(1) : null;
    const isPos = (p.profit_abs||0) >= 0;
    const cc   = isPos ? 'g' : 'r';
    const row = el('tr', {},
      el('td', {class:'left', style:'font-weight:700;color:#F8FAFC'}, p.pair),
      el('td', {}, p.count || 0),
      el('td', {class: wr != null ? (parseFloat(wr)>=50?'g':'r') : '',
               style:'font-weight:600'}, wr != null ? wr + '%' : '—'),
      el('td', {class:cc, style:'font-weight:600'}, fmtU(p.profit_abs)),
      el('td', {class:cc}, p.count ? fmtU((p.profit_abs||0)/p.count) : '—'),
      el('td', {class:cc}, p.profit_mean != null ? fmtP(p.profit_mean*100) : '—')
    );
    tbody.appendChild(row);
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

// ── Tab switch & auto-refresh ──────────────────────────────────────────────
function setTab(id) {
  state.activeTab = id;
  render();
}

fetchAll();
setInterval(fetchAll, 30000);
</script>
</body>
</html>
"""


def get_dashboard_html():
    return DASHBOARD_HTML


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return get_dashboard_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/v1/<path:endpoint>", methods=["GET", "POST", "DELETE"])
def proxy_freqtrade_api(endpoint):
    freqtrade_url = f"{FREQTRADE_API}/api/v1/{endpoint}"
    params = request.args.to_dict()
    auth = _auth()

    try:
        if request.method == "GET":
            resp = requests.get(freqtrade_url, params=params, auth=auth, timeout=8)
        elif request.method == "POST":
            resp = requests.post(freqtrade_url, json=request.get_json(silent=True),
                                 params=params, auth=auth, timeout=8)
        else:
            resp = requests.delete(freqtrade_url, params=params, auth=auth, timeout=8)

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return jsonify(data), resp.status_code

    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot connect to Freqtrade API",
                        "url": freqtrade_url}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Freqtrade API timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    ft_ok, ft_state = False, None
    try:
        r = requests.get(f"{FREQTRADE_API}/api/v1/ping", auth=_auth(), timeout=3)
        ft_ok    = r.status_code == 200
        ft_state = r.json().get("status")
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "freqtrade_api": FREQTRADE_API,
        "freqtrade_reachable": ft_ok,
        "freqtrade_ping": ft_state,
        "dashboard_port": DASHBOARD_PORT,
        "auth_enabled": bool(FREQTRADE_PASSWORD),
    })


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    auth_status = "enabled" if FREQTRADE_PASSWORD else "disabled"
    print(f"""
  ╔══════════════════════════════════════════════════════════════╗
  ║          KotipotiBot Dashboard Server                        ║
  ╠══════════════════════════════════════════════════════════════╣
  ║  Dashboard:    http://localhost:{DASHBOARD_PORT}
  ║  Health:       http://localhost:{DASHBOARD_PORT}/health
  ║  Freqtrade:    {FREQTRADE_API}
  ║  Auth:         {auth_status}
  ╚══════════════════════════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)
