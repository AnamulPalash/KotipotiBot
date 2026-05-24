"""
dashboard.py — KotipotiBot v2 live dashboard
============================================
Flask server. Reads directly from SQLite.
Serves a self-contained vanilla JS dashboard — no CDN, no React.
"""

from flask import Flask, jsonify
from flask_cors import CORS
import os, sys, json, logging
from pathlib import Path

import db

app = Flask(__name__)
CORS(app)

PORT = int(os.environ.get("DASHBOARD_PORT", 5000))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("kotipoti.dashboard")

# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    return jsonify(db.get_profit_summary())

@app.route("/api/open_trades")
def api_open_trades():
    return jsonify(db.get_open_trades())

@app.route("/api/recent_trades")
def api_recent_trades():
    return jsonify(db.get_recent_trades(50))

@app.route("/api/signals")
def api_signals():
    return jsonify(db.get_recent_signals(100))

@app.route("/api/params")
def api_params():
    return jsonify(db.get_all_params())

@app.route("/api/hermes")
def api_hermes():
    logs = db.get_hermes_logs(5)
    result = []
    for h in logs:
        result.append({
            "ts":          h["ts"],
            "analysis":    json.loads(h["analysis"]) if h["analysis"] else {},
            "suggestions": json.loads(h["suggestions"]) if h["suggestions"] else {},
            "applied":     h["applied"],
        })
    return jsonify(result)

@app.route("/api/charts")
def api_charts():
    """Pre-computed chart data so the frontend doesn't need to crunch numbers."""
    trades = db.get_all_closed_trades()

    # Cumulative P&L curve — one point per trade, sorted by exit time
    sorted_trades = sorted(
        [t for t in trades if t.get("exit_time") and t.get("profit_usdt") is not None],
        key=lambda t: t["exit_time"]
    )
    cumulative = 0.0
    pnl_curve = []
    for t in sorted_trades:
        cumulative += t["profit_usdt"]
        pnl_curve.append({"t": t["exit_time"][:16], "v": round(cumulative, 2),
                           "p": round(t["profit_usdt"], 2)})

    # Win rate rolling (last 20 trades)
    rolling_wr = []
    for i, t in enumerate(sorted_trades):
        window = sorted_trades[max(0, i-19):i+1]
        wins = sum(1 for x in window if x["profit_usdt"] > 0)
        rolling_wr.append({"t": t["exit_time"][:16],
                            "v": round(wins / len(window) * 100, 1)})

    # By session
    from collections import defaultdict
    sess: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        s = t.get("session") or "Unknown"
        sess[s]["pnl"] += t.get("profit_usdt") or 0
        if (t.get("profit_usdt") or 0) > 0:
            sess[s]["wins"] += 1
        else:
            sess[s]["losses"] += 1
    by_session = [
        {"label": s,
         "wins": v["wins"], "losses": v["losses"],
         "pnl": round(v["pnl"], 2),
         "wr": round(v["wins"] / max(v["wins"]+v["losses"], 1) * 100, 1)}
        for s, v in sess.items()
    ]

    # By pair
    pair_map: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        p = t["pair"].split("/")[0]
        pair_map[p]["pnl"] += t.get("profit_usdt") or 0
        if (t.get("profit_usdt") or 0) > 0:
            pair_map[p]["wins"] += 1
        else:
            pair_map[p]["losses"] += 1
    by_pair = sorted([
        {"label": p,
         "wins": v["wins"], "losses": v["losses"],
         "pnl": round(v["pnl"], 2),
         "wr": round(v["wins"] / max(v["wins"]+v["losses"], 1) * 100, 1)}
        for p, v in pair_map.items()
    ], key=lambda x: x["pnl"], reverse=True)

    # By entry tag
    tag_map: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        tag = t.get("entry_tag") or "unknown"
        tag_map[tag]["pnl"] += t.get("profit_usdt") or 0
        if (t.get("profit_usdt") or 0) > 0:
            tag_map[tag]["wins"] += 1
        else:
            tag_map[tag]["losses"] += 1
    by_tag = [
        {"label": tag,
         "wins": v["wins"], "losses": v["losses"],
         "pnl": round(v["pnl"], 2),
         "wr": round(v["wins"] / max(v["wins"]+v["losses"], 1) * 100, 1)}
        for tag, v in tag_map.items()
    ]

    return jsonify({
        "pnl_curve":   pnl_curve,
        "rolling_wr":  rolling_wr,
        "by_session":  by_session,
        "by_pair":     by_pair,
        "by_tag":      by_tag,
    })


@app.route("/health")
def health():
    try:
        summary = db.get_profit_summary()
        return jsonify({"status": "ok", "trades": summary["trade_count"]})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KotipotiBot v2</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:linear-gradient(160deg,#0F172A 0%,#1A2540 100%);
     color:#F8FAFC;min-height:100vh;padding:1.5rem}
.wrap{max-width:1500px;margin:0 auto}
h1{font-size:26px;font-weight:700;letter-spacing:-.5px}
.subtitle{font-size:13px;color:#94A3B8;margin-top:4px}
.header{display:flex;justify-content:space-between;align-items:flex-start;
        margin-bottom:1.5rem;flex-wrap:wrap;gap:12px}
.header-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.badge{display:flex;align-items:center;gap:6px;border-radius:999px;
       padding:5px 12px;font-size:12px;font-weight:600}
.badge-dot{width:7px;height:7px;border-radius:50%}
/* Charts */
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem}
.chart-full{grid-column:1/-1}
.chart-wrap{background:rgba(30,41,59,.85);border:1px solid rgba(148,163,184,.12);
            border-radius:14px;padding:1.25rem}
.chart-title{font-size:13px;font-weight:600;color:#E2E8F0;margin-bottom:12px}
canvas{display:block;width:100%!important}
.bar-label{font-size:11px;color:#94A3B8}
.no-data{text-align:center;padding:40px 0;color:#475569;font-size:13px}
.btn{background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.3);
     color:#60A5FA;border-radius:8px;padding:6px 12px;font-size:12px;
     cursor:pointer;font-weight:500}
.btn:hover{background:rgba(59,130,246,.25)}
.ts{font-size:11px;color:#94A3B8}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:1rem;margin-bottom:1.5rem}
.kpi{border-radius:14px;padding:1.25rem;border:1px solid}
.kpi-label{font-size:11px;color:#94A3B8;font-weight:500;
           text-transform:uppercase;letter-spacing:.6px}
.kpi-value{font-size:26px;font-weight:700;margin-top:.4rem;letter-spacing:-.5px}
.kpi-sub{font-size:12px;color:#94A3B8;margin-top:.5rem}
.tabs{display:flex;gap:4px;margin-bottom:1.25rem;
      background:rgba(30,41,59,.6);border-radius:10px;
      padding:4px;width:fit-content;flex-wrap:wrap}
.tab{border:none;padding:7px 14px;border-radius:7px;font-size:12px;
     font-weight:500;cursor:pointer;white-space:nowrap;
     background:transparent;color:#94A3B8}
.tab.active{background:linear-gradient(135deg,#3B82F6,#2563EB);color:#fff}
.card{background:rgba(30,41,59,.85);border:1px solid rgba(148,163,184,.12);
      border-radius:14px;padding:1.25rem;margin-bottom:1rem}
.card-title{font-size:14px;font-weight:600;color:#E2E8F0;margin-bottom:1rem}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:right;padding:8px 10px;color:#94A3B8;font-weight:500;
   font-size:11px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
th.left{text-align:left}
td{padding:10px;color:#CBD5E1;text-align:right;
   border-bottom:1px solid rgba(148,163,184,.05)}
td.left{text-align:left}
tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:rgba(51,65,85,.2)}
.dir-short{background:rgba(239,68,68,.15);color:#F87171;font-size:11px;
           font-weight:700;padding:2px 7px;border-radius:5px}
.dir-long{background:rgba(16,185,129,.15);color:#34D399;font-size:11px;
          font-weight:700;padding:2px 7px;border-radius:5px}
.tag{background:rgba(99,102,241,.15);color:#A5B4FC;font-size:10px;
     padding:2px 6px;border-radius:4px;font-weight:500}
.empty{text-align:center;padding:2rem 0;color:#94A3B8;font-size:13px}
.g{color:#10B981}.r{color:#EF4444}.m{color:#94A3B8}
.overflow-x{overflow-x:auto}
.hermes-card{background:rgba(245,158,11,.05);border:1px solid rgba(245,158,11,.2);
             border-radius:10px;padding:12px 14px;margin-bottom:10px}
.hermes-ts{font-size:11px;color:#FCD34D;margin-bottom:6px;font-weight:600}
.hermes-row{display:flex;justify-content:space-between;font-size:12px;
            color:#CBD5E1;padding:3px 0;border-bottom:1px solid rgba(245,158,11,.08)}
.hermes-row:last-child{border-bottom:none}
.param-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.param-chip{background:rgba(51,65,85,.4);border-radius:8px;padding:8px 12px}
.param-label{font-size:10px;color:#64748B;text-transform:uppercase;letter-spacing:.5px}
.param-val{font-size:13px;font-weight:600;color:#E2E8F0;margin-top:2px}
.param-by{font-size:10px;color:#F59E0B;margin-top:1px}
.signal-row{display:flex;align-items:center;gap:8px;padding:6px 10px;
            border-radius:7px;margin-bottom:5px;background:rgba(51,65,85,.4)}
.signal-fired{border-left:3px solid #10B981}
.signal-skip{border-left:3px solid rgba(148,163,184,.3)}
.footer{text-align:center;margin-top:2rem;font-size:11px;color:rgba(148,163,184,.35)}
.spinner{width:36px;height:36px;border:3px solid rgba(148,163,184,.2);
         border-top:3px solid #3B82F6;border-radius:50%;
         animation:spin .8s linear infinite;margin:0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-wrap{display:flex;flex-direction:column;align-items:center;
              justify-content:center;min-height:100vh;gap:16px;color:#94A3B8}
.bar-track{height:4px;background:rgba(148,163,184,.15);border-radius:2px;margin-top:6px}
.bar-fill{height:100%;border-radius:2px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap" id="app">
  <div class="loading-wrap">
    <div class="spinner"></div>
    <p>Loading KotipotiBot dashboard…</p>
  </div>
</div>

<script>
const el = (tag, attrs={}, ...children) => {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k==='class') e.className=v;
    else if (k==='html') e.innerHTML=v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c==null) continue;
    e.appendChild(typeof c==='string'?document.createTextNode(c):c);
  }
  return e;
};
const fmt  = (n,d=2) => n==null?'—':Number(n).toFixed(d);
const fmtU = n => n==null?'—':(n>=0?'+':'')+Number(n).toFixed(2)+' USDT';
const fmtP = n => n==null?'—':(n>=0?'+':'')+Number(n).toFixed(2)+'%';
const cc   = n => n==null?'':n>=0?'g':'r';
const holdDur = d => {
  if (!d) return '—';
  const s = Math.floor((Date.now()-new Date(d).getTime())/1000);
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h>0?`${h}h ${m}m`:`${m}m`;
};
const fmtDur = s => {
  if (!s) return '—';
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h>0?`${h}h ${m}m`:`${m}m`;
};

let state = {
  summary:{}, openTrades:[], recentTrades:[],
  signals:[], params:{}, hermes:[], charts:{},
  lastUpdated:null, activeTab:'overview'
};

async function fetchAll() {
  try {
    const [sum, open, closed, sigs, params, hermes, charts] = await Promise.all([
      fetch('/api/summary').then(r=>r.json()).catch(()=>({})),
      fetch('/api/open_trades').then(r=>r.json()).catch(()=>[]),
      fetch('/api/recent_trades').then(r=>r.json()).catch(()=>[]),
      fetch('/api/signals').then(r=>r.json()).catch(()=>[]),
      fetch('/api/params').then(r=>r.json()).catch(()=>({})),
      fetch('/api/hermes').then(r=>r.json()).catch(()=>[]),
      fetch('/api/charts').then(r=>r.json()).catch(()=>({})),
    ]);
    state.summary=sum; state.openTrades=open; state.recentTrades=closed;
    state.signals=sigs; state.params=params; state.hermes=hermes;
    state.charts=charts;
    state.lastUpdated=new Date();
  } catch(e) { console.error(e); }
  render();
}

function render() {
  const app = document.getElementById('app');
  app.innerHTML='';
  app.appendChild(buildUI());
}

function buildUI() {
  const s = state;
  const wrap = el('div',{class:'wrap'});

  // Header
  const dryRun = (s.params.leverage != null);
  wrap.appendChild(el('div',{class:'header'},
    el('div',{},
      el('h1',{},'🤖 KotipotiBot v2'),
      el('p',{class:'subtitle'},'Custom Bot · Bybit Futures · 5m · No Freqtrade')
    ),
    el('div',{class:'header-right'},
      el('div',{class:'badge',style:'background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);color:#10B981'},
        el('div',{class:'badge-dot',style:'background:#10B981'}),
        'Running'
      ),
      el('span',{class:'ts'}, s.lastUpdated?'Updated '+s.lastUpdated.toLocaleTimeString():''),
      el('button',{class:'btn',onclick:fetchAll},'↻ Refresh')
    )
  ));

  // KPIs
  const sum = s.summary;
  const kpi = el('div',{class:'kpi-grid'});

  const openPnl = s.openTrades.reduce((a,t)=>{
    const cur = parseFloat(t.current_price||t.entry_price);
    const ep  = parseFloat(t.entry_price);
    const amt = parseFloat(t.amount);
    const lev = parseFloat(t.leverage||5);
    if (t.side==='long') return a+(cur-ep)*amt;
    else return a+(ep-cur)*amt;
  },0);

  kpi.appendChild(kpiCard({
    label:'Total P&L', value:fmtU(sum.total_profit_usdt),
    valueClass:cc(sum.total_profit_usdt),
    sub:`${sum.trade_count||0} closed trades`,
    border:(sum.total_profit_usdt>=0)?'rgba(16,185,129,.25)':'rgba(239,68,68,.25)',
    bg:(sum.total_profit_usdt>=0)?'linear-gradient(135deg,rgba(16,185,129,.08),rgba(5,150,105,.08))':'linear-gradient(135deg,rgba(239,68,68,.08),rgba(185,28,28,.08))'
  }));
  kpi.appendChild(kpiCard({
    label:'Win Rate', value: sum.win_rate!=null?sum.win_rate+'%':'—',
    sub:`avg ${fmtP(sum.avg_profit_pct)} per trade`,
    border:'rgba(168,85,247,.25)',
    bg:'linear-gradient(135deg,rgba(168,85,247,.08),rgba(236,72,153,.08))'
  }));
  kpi.appendChild(kpiCard({
    label:'Open Positions', value:String(s.openTrades.length),
    sub:`<span class="${cc(openPnl)}">Unrealised: ${fmtU(openPnl)}</span>`,
    border:'rgba(245,158,11,.25)',
    bg:'linear-gradient(135deg,rgba(245,158,11,.08),rgba(251,146,60,.08))'
  }));
  kpi.appendChild(kpiCard({
    label:'Best Trade', value:fmtU(sum.best_trade),
    valueClass:'g',
    sub:`Worst: <span class="r">${fmtU(sum.worst_trade)}</span>`,
    border:'rgba(59,130,246,.25)',
    bg:'linear-gradient(135deg,rgba(59,130,246,.08),rgba(139,92,246,.08))'
  }));
  wrap.appendChild(kpi);

  // Tabs
  const tabs = [
    {id:'overview',label:'📊 Overview'},
    {id:'charts',  label:'📈 Charts'},
    {id:'open',    label:`📉 Open (${s.openTrades.length})`},
    {id:'closed',  label:'✅ Closed'},
    {id:'signals', label:'🔔 Signals'},
    {id:'hermes',  label:'🧠 Hermes'},
    {id:'params',  label:'⚙️ Params'},
  ];
  const tabBar = el('div',{class:'tabs'});
  for (const t of tabs) {
    tabBar.appendChild(el('button',{
      class:'tab'+(state.activeTab===t.id?' active':''),
      onclick:()=>{state.activeTab=t.id;render();}
    },t.label));
  }
  wrap.appendChild(tabBar);

  const content = el('div',{});
  switch(state.activeTab) {
    case 'overview': content.appendChild(buildOverview()); break;
    case 'charts':   content.appendChild(buildCharts());   break;
    case 'open':     content.appendChild(buildOpen());     break;
    case 'closed':   content.appendChild(buildClosed());   break;
    case 'signals':  content.appendChild(buildSignals());  break;
    case 'hermes':   content.appendChild(buildHermes());   break;
    case 'params':   content.appendChild(buildParams());   break;
  }
  wrap.appendChild(content);
  wrap.appendChild(el('p',{class:'footer'},'KotipotiBot v2 · auto-refreshes every 30s · read-only'));
  return wrap;
}

function kpiCard({label,value,valueClass='',sub='',border='rgba(148,163,184,.12)',bg='rgba(30,41,59,.85)'}) {
  const card = el('div',{class:'kpi',style:`border-color:${border};background:${bg}`});
  card.appendChild(el('div',{class:'kpi-label'},label));
  card.appendChild(el('div',{class:`kpi-value ${valueClass}`},value));
  card.appendChild(el('div',{class:'kpi-sub',html:sub}));
  return card;
}

// ── Pure-canvas chart helpers ──────────────────────────────────────────────

function lineChart(data, {width=600,height=180,color='#3B82F6',fillColor='rgba(59,130,246,.08)',zeroLine=true}={}) {
  if (!data || data.length < 2) { const d=document.createElement('div'); d.className='no-data'; d.textContent='Not enough data yet'; return d; }
  const canvas=document.createElement('canvas');
  canvas.width=width; canvas.height=height;
  canvas.style.width='100%'; canvas.style.height=height+'px';
  const ctx=canvas.getContext('2d');
  const pad={t:10,r:10,b:28,l:52};
  const W=width-pad.l-pad.r, H=height-pad.t-pad.b;
  const vals=data.map(d=>d.v);
  const minV=Math.min(...vals), maxV=Math.max(...vals);
  const range=maxV-minV||1;
  const xS=i=>pad.l+i/(data.length-1)*W;
  const yS=v=>pad.t+H-(v-minV)/range*H;
  ctx.clearRect(0,0,width,height);
  if (zeroLine && minV<0 && maxV>0) {
    ctx.strokeStyle='rgba(148,163,184,.2)'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
    const y0=yS(0); ctx.beginPath(); ctx.moveTo(pad.l,y0); ctx.lineTo(pad.l+W,y0); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.beginPath(); ctx.moveTo(xS(0),yS(vals[0]));
  for (let i=1;i<data.length;i++) ctx.lineTo(xS(i),yS(vals[i]));
  ctx.lineTo(xS(data.length-1),pad.t+H); ctx.lineTo(xS(0),pad.t+H); ctx.closePath();
  ctx.fillStyle=fillColor; ctx.fill();
  ctx.beginPath(); ctx.moveTo(xS(0),yS(vals[0]));
  for (let i=1;i<data.length;i++) ctx.lineTo(xS(i),yS(vals[i]));
  ctx.strokeStyle=color; ctx.lineWidth=2; ctx.stroke();
  ctx.fillStyle='#475569'; ctx.font='10px sans-serif'; ctx.textAlign='right';
  for (let i=0;i<=4;i++) {
    const v=minV+(range/4)*i, y=yS(v);
    ctx.fillText(v.toFixed(1),pad.l-4,y+3);
    ctx.strokeStyle='rgba(148,163,184,.06)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+W,y); ctx.stroke();
  }
  ctx.fillStyle='#475569'; ctx.textAlign='center'; ctx.font='9px sans-serif';
  for (const i of [0,Math.floor(data.length/2),data.length-1])
    ctx.fillText((data[i].t||'').slice(5), xS(i), height-6);
  return canvas;
}

function barChart(data, {width=600,height=160,colorPos='#10B981',colorNeg='#EF4444',valueKey='pnl',labelKey='label'}={}) {
  if (!data||!data.length) { const d=document.createElement('div'); d.className='no-data'; d.textContent='Not enough data yet'; return d; }
  const canvas=document.createElement('canvas');
  canvas.width=width; canvas.height=height;
  canvas.style.width='100%'; canvas.style.height=height+'px';
  const ctx=canvas.getContext('2d');
  const pad={t:14,r:10,b:32,l:52};
  const W=width-pad.l-pad.r, H=height-pad.t-pad.b;
  const vals=data.map(d=>d[valueKey]);
  const maxAbs=Math.max(...vals.map(Math.abs),0.01);
  const barW=Math.max(8,W/data.length*0.6);
  const gap=(W-barW*data.length)/(data.length+1);
  const midY=pad.t+H/2;
  ctx.clearRect(0,0,width,height);
  ctx.strokeStyle='rgba(148,163,184,.2)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(pad.l,midY); ctx.lineTo(pad.l+W,midY); ctx.stroke();
  data.forEach((d,i)=>{
    const v=d[valueKey], x=pad.l+gap+(barW+gap)*i;
    const bH=Math.abs(v)/maxAbs*(H/2-4);
    ctx.fillStyle=v>=0?colorPos:colorNeg;
    ctx.beginPath(); ctx.roundRect(x,v>=0?midY-bH:midY,barW,bH,3); ctx.fill();
    ctx.fillStyle='#64748B'; ctx.font='10px sans-serif'; ctx.textAlign='center';
    ctx.fillText(d[labelKey],x+barW/2,height-8);
    ctx.fillStyle=v>=0?'#34D399':'#F87171'; ctx.font='bold 10px sans-serif';
    ctx.fillText((v>=0?'+':'')+v.toFixed(1),x+barW/2,v>=0?midY-bH-4:midY+bH+12);
  });
  return canvas;
}

function winRateBar(wins,losses) {
  const total=wins+losses; if(!total){const d=document.createElement('div');d.className='no-data';d.textContent='—';return d;}
  const wr=wins/total*100;
  const wrap=el('div',{style:'padding:2px 0'});
  wrap.appendChild(el('div',{style:'display:flex;justify-content:space-between;margin-bottom:4px'},
    el('span',{style:'font-size:11px;color:#94A3B8'},`${wins}W/${losses}L`),
    el('span',{style:`font-size:12px;font-weight:700;color:${wr>=50?'#10B981':'#EF4444'}`},wr.toFixed(1)+'%')
  ));
  const track=el('div',{style:'height:6px;background:rgba(239,68,68,.2);border-radius:3px;overflow:hidden'});
  track.appendChild(el('div',{style:`height:100%;width:${wr}%;background:linear-gradient(90deg,#10B981,#059669);border-radius:3px`}));
  wrap.appendChild(track);
  return wrap;
}

// ── Charts tab ────────────────────────────────────────────────────────────
function buildCharts() {
  const c=state.charts;
  const wrap=el('div',{});

  // P&L curve — full width
  const pnlBox=el('div',{class:'chart-wrap',style:'margin-bottom:1rem'});
  pnlBox.appendChild(el('div',{class:'chart-title'},'Cumulative P&L (USDT)'));
  if (c.pnl_curve&&c.pnl_curve.length>=2) {
    const last=c.pnl_curve[c.pnl_curve.length-1].v;
    pnlBox.appendChild(lineChart(c.pnl_curve,{width:1200,height:200,
      color:last>=0?'#10B981':'#EF4444',
      fillColor:last>=0?'rgba(16,185,129,.08)':'rgba(239,68,68,.08)',
      zeroLine:true}));
  } else pnlBox.appendChild(el('div',{class:'no-data'},'Waiting for closed trades…'));
  wrap.appendChild(pnlBox);

  // Row 2: rolling win rate + by session
  const row2=el('div',{class:'chart-grid'});
  const wrBox=el('div',{class:'chart-wrap'});
  wrBox.appendChild(el('div',{class:'chart-title'},'Rolling Win Rate % (last 20 trades)'));
  if (c.rolling_wr&&c.rolling_wr.length>=2)
    wrBox.appendChild(lineChart(c.rolling_wr,{width:600,height:160,color:'#A78BFA',fillColor:'rgba(167,139,250,.08)',zeroLine:false}));
  else wrBox.appendChild(el('div',{class:'no-data'},'Waiting for closed trades…'));
  row2.appendChild(wrBox);

  const sessBox=el('div',{class:'chart-wrap'});
  sessBox.appendChild(el('div',{class:'chart-title'},'P&L by Session (USDT)'));
  if (c.by_session&&c.by_session.length) {
    sessBox.appendChild(barChart(c.by_session,{width:600,height:160}));
    const g=el('div',{style:'display:flex;gap:12px;margin-top:10px;flex-wrap:wrap'});
    for (const s of c.by_session){const chip=el('div',{style:'flex:1;min-width:90px'});chip.appendChild(el('div',{style:'font-size:11px;color:#94A3B8;margin-bottom:3px'},s.label));chip.appendChild(winRateBar(s.wins,s.losses));g.appendChild(chip);}
    sessBox.appendChild(g);
  } else sessBox.appendChild(el('div',{class:'no-data'},'Waiting for closed trades…'));
  row2.appendChild(sessBox);
  wrap.appendChild(row2);

  // Row 3: by pair + by tag
  const row3=el('div',{class:'chart-grid'});
  const pairBox=el('div',{class:'chart-wrap'});
  pairBox.appendChild(el('div',{class:'chart-title'},'P&L by Pair (USDT)'));
  if (c.by_pair&&c.by_pair.length) {
    pairBox.appendChild(barChart(c.by_pair,{width:600,height:160}));
    const g=el('div',{style:'display:flex;gap:10px;margin-top:10px;flex-wrap:wrap'});
    for (const p of c.by_pair){const chip=el('div',{style:'flex:1;min-width:80px'});chip.appendChild(el('div',{style:'font-size:11px;color:#94A3B8;margin-bottom:3px'},p.label));chip.appendChild(winRateBar(p.wins,p.losses));g.appendChild(chip);}
    pairBox.appendChild(g);
  } else pairBox.appendChild(el('div',{class:'no-data'},'Waiting for closed trades…'));
  row3.appendChild(pairBox);

  const tagBox=el('div',{class:'chart-wrap'});
  tagBox.appendChild(el('div',{class:'chart-title'},'P&L by Signal Type (USDT)'));
  if (c.by_tag&&c.by_tag.length) {
    tagBox.appendChild(barChart(c.by_tag,{width:600,height:160}));
    const g=el('div',{style:'display:flex;gap:10px;margin-top:10px;flex-wrap:wrap'});
    for (const t of c.by_tag){const chip=el('div',{style:'flex:1;min-width:100px'});chip.appendChild(el('div',{style:'font-size:11px;color:#94A3B8;margin-bottom:3px'},t.label));chip.appendChild(winRateBar(t.wins,t.losses));g.appendChild(chip);}
    tagBox.appendChild(g);
  } else tagBox.appendChild(el('div',{class:'no-data'},'Waiting for closed trades…'));
  row3.appendChild(tagBox);
  wrap.appendChild(row3);
  return wrap;
}

function buildOverview() {
  const wrap = el('div',{class:'two-col'});

  // Recent closed
  const rc = el('div',{class:'card'});
  rc.appendChild(el('div',{class:'card-title'},'Recent Closed Trades'));
  const rt = state.recentTrades.slice(0,8);
  if (!rt.length) rc.appendChild(el('p',{class:'empty'},'No closed trades yet'));
  else for (const t of rt) {
    const p = t.profit_usdt||0;
    const row = el('div',{style:`display:flex;justify-content:space-between;align-items:center;
      padding:8px 10px;border-radius:8px;margin-bottom:6px;
      background:rgba(51,65,85,.5);border:1px solid ${p>=0?'rgba(16,185,129,.15)':'rgba(239,68,68,.15)'}`});
    row.appendChild(el('div',{style:'display:flex;align-items:center;gap:8px'},
      el('div',{style:`width:6px;height:6px;border-radius:50%;background:${p>=0?'#10B981':'#EF4444'}`}),
      el('div',{},
        el('span',{style:'font-size:13px;font-weight:600;color:#F8FAFC'},t.pair.split('/')[0]),
        el('span',{style:'font-size:11px;color:#94A3B8;margin-left:6px'},t.exit_reason||'—')
      )
    ));
    row.appendChild(el('span',{class:cc(p),style:'font-size:13px;font-weight:600'},fmtU(p)));
    rc.appendChild(row);
  }
  wrap.appendChild(rc);

  // Hermes latest insight
  const hc = el('div',{class:'card'});
  hc.appendChild(el('div',{class:'card-title'},'🧠 Latest Hermes Insight'));
  const latest = state.hermes[0];
  if (!latest) {
    hc.appendChild(el('p',{class:'empty'},'No analysis yet — Hermes runs every 6h'));
  } else {
    const a = latest.analysis;
    hc.appendChild(el('div',{class:'hermes-card'},
      el('div',{class:'hermes-ts'},new Date(latest.ts).toLocaleString()),
      el('div',{class:'hermes-row'},
        el('span',{},'Overall Win Rate'),
        el('span',{class:cc(a.overall_win_rate-50)},`${(a.overall_win_rate||0).toFixed(1)}%`)
      ),
      el('div',{class:'hermes-row'},
        el('span',{},'Total P&L'),
        el('span',{class:cc(a.total_profit_usdt)},fmtU(a.total_profit_usdt))
      ),
      el('div',{class:'hermes-row'},
        el('span',{},'Trades analysed'),
        el('span',{style:'color:#E2E8F0'},a.total_trades)
      ),
      Object.keys(latest.suggestions||{}).length>0
        ? el('div',{style:'margin-top:8px;font-size:11px;color:#FCD34D'},
            '⚡ Applied '+Object.keys(latest.suggestions).length+' parameter update(s)')
        : el('div',{style:'margin-top:8px;font-size:11px;color:#64748B'},'No changes needed')
    ));
  }
  wrap.appendChild(hc);
  return wrap;
}

function buildOpen() {
  const card = el('div',{class:'card'});
  card.appendChild(el('div',{class:'card-title'},'Active Positions'));
  if (!state.openTrades.length) {
    card.appendChild(el('p',{class:'empty'},'No open positions'));
    return card;
  }
  const wrap = el('div',{class:'overflow-x'});
  const tbl  = el('table',{});
  tbl.appendChild(el('thead',{},el('tr',{},
    el('th',{class:'left'},'Pair'),el('th',{},'Dir'),el('th',{},'Entry'),
    el('th',{},'Stake'),el('th',{},'Holding'),el('th',{},'Tag'),el('th',{},'Session')
  )));
  const tb = el('tbody',{});
  for (const t of state.openTrades) {
    tb.appendChild(el('tr',{},
      el('td',{class:'left',html:`<span style="font-weight:700;color:#F8FAFC">${t.pair}</span>`}),
      el('td',{},el('span',{class:t.side==='short'?'dir-short':'dir-long'},t.side.toUpperCase())),
      el('td',{},fmt(t.entry_price,4)),
      el('td',{},fmt(t.stake_usdt,0)+' USDT'),
      el('td',{},holdDur(t.entry_time)),
      el('td',{},el('span',{class:'tag'},t.entry_tag||'—')),
      el('td',{style:'color:#94A3B8'},t.session||'—')
    ));
  }
  tbl.appendChild(tb);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

function buildClosed() {
  const card = el('div',{class:'card'});
  card.appendChild(el('div',{class:'card-title'},`Last ${state.recentTrades.length} Closed Trades`));
  if (!state.recentTrades.length) {
    card.appendChild(el('p',{class:'empty'},'No closed trades yet'));
    return card;
  }
  const wrap = el('div',{class:'overflow-x'});
  const tbl  = el('table',{});
  tbl.appendChild(el('thead',{},el('tr',{},
    el('th',{class:'left'},'#'),el('th',{class:'left'},'Pair'),el('th',{},'Dir'),
    el('th',{},'Entry'),el('th',{},'Exit'),el('th',{},'P&L'),
    el('th',{},'Exit Reason'),el('th',{},'Session'),el('th',{},'BTC Regime')
  )));
  const tb = el('tbody',{});
  for (const t of state.recentTrades) {
    const p = t.profit_usdt||0;
    tb.appendChild(el('tr',{},
      el('td',{class:'left',style:'color:#64748B;font-size:11px'},'#'+t.id),
      el('td',{class:'left',style:'font-weight:600;color:#F8FAFC'},t.pair),
      el('td',{},el('span',{class:t.side==='short'?'dir-short':'dir-long'},
        t.side==='short'?'S':'L')),
      el('td',{},fmt(t.entry_price,4)),
      el('td',{},fmt(t.exit_price,4)),
      el('td',{class:cc(p),style:'font-weight:600'},fmtU(p)),
      el('td',{style:'color:#94A3B8;font-size:12px'},t.exit_reason||'—'),
      el('td',{style:'color:#94A3B8'},t.session||'—'),
      el('td',{style:'color:#94A3B8'},t.btc_regime||'—')
    ));
  }
  tbl.appendChild(tb);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

function buildSignals() {
  const card = el('div',{class:'card'});
  card.appendChild(el('div',{class:'card-title'},'Recent Signals (last 100)'));
  if (!state.signals.length) {
    card.appendChild(el('p',{class:'empty'},'No signals logged yet'));
    return card;
  }
  const wrap = el('div',{class:'overflow-x'});
  const tbl = el('table',{});
  tbl.appendChild(el('thead',{},el('tr',{},
    el('th',{class:'left'},'Time'),el('th',{class:'left'},'Pair'),
    el('th',{},'Dir'),el('th',{},'Fired'),el('th',{},'Price'),
    el('th',{},'RSI'),el('th',{},'ATR%'),el('th',{},'VWAP dev'),
    el('th',{},'Vol ratio'),el('th',{},'Skip reason')
  )));
  const tb = el('tbody',{});
  for (const s of state.signals) {
    const fired = s.fired===1;
    tb.appendChild(el('tr',{style:fired?'':'opacity:.55'},
      el('td',{class:'left',style:'font-size:11px;color:#64748B'},
        new Date(s.ts).toLocaleTimeString()),
      el('td',{class:'left',style:'font-weight:600;color:#F8FAFC'},s.pair),
      el('td',{},el('span',{class:s.direction==='short'?'dir-short':'dir-long'},
        s.direction.toUpperCase())),
      el('td',{},fired
        ? el('span',{style:'color:#10B981;font-size:12px'},'✅ fired')
        : el('span',{style:'color:#475569;font-size:12px'},'— skip')),
      el('td',{},fmt(s.price,4)),
      el('td',{class:s.rsi>70?'r':s.rsi<30?'g':'m'},fmt(s.rsi,1)),
      el('td',{},fmt(s.atr_pct,2)),
      el('td',{class:cc(s.vwap_dev?-s.vwap_dev:null)},fmt(s.vwap_dev,2)),
      el('td',{},fmt(s.volume_ratio,2)),
      el('td',{style:'color:#94A3B8;font-size:11px'},s.skip_reason||'—')
    ));
  }
  tbl.appendChild(tb);
  wrap.appendChild(tbl);
  card.appendChild(wrap);
  return card;
}

function buildHermes() {
  const wrap = el('div',{});
  if (!state.hermes.length) {
    const card = el('div',{class:'card'});
    card.appendChild(el('p',{class:'empty'},
      '🧠 Hermes hasn\'t run yet — it analyses performance every 6h.\n\nMake sure the bot has been running and has at least 10 closed trades.'));
    wrap.appendChild(card);
    return wrap;
  }
  for (const h of state.hermes) {
    const card = el('div',{class:'card'});
    card.appendChild(el('div',{class:'card-title'},
      '🧠 Analysis — '+new Date(h.ts).toLocaleString()));
    const a = h.analysis;

    // Overall stats
    card.appendChild(el('div',{style:'display:flex;gap:20px;margin-bottom:12px;flex-wrap:wrap'},
      el('div',{},el('div',{class:'kpi-label'},'Win Rate'),
        el('div',{style:'font-size:20px;font-weight:700;color:#A78BFA'},
          (a.overall_win_rate||0).toFixed(1)+'%')),
      el('div',{},el('div',{class:'kpi-label'},'Total P&L'),
        el('div',{style:`font-size:20px;font-weight:700;${a.total_profit_usdt>=0?'color:#10B981':'color:#EF4444'}`},
          fmtU(a.total_profit_usdt))),
      el('div',{},el('div',{class:'kpi-label'},'Trades'),
        el('div',{style:'font-size:20px;font-weight:700;color:#60A5FA'},
          a.total_trades||0))
    ));

    // By session
    if (a.by_session && Object.keys(a.by_session).length) {
      card.appendChild(el('div',{class:'card-title',style:'margin-top:12px;font-size:13px'},'By Session'));
      const tbl = el('table',{});
      tbl.appendChild(el('thead',{},el('tr',{},
        el('th',{class:'left'},'Session'),el('th',{},'Trades'),
        el('th',{},'Win Rate'),el('th',{},'Avg P&L')
      )));
      const tb = el('tbody',{});
      for (const [s,st] of Object.entries(a.by_session)) {
        tb.appendChild(el('tr',{},
          el('td',{class:'left'},s),
          el('td',{},st.count),
          el('td',{class:st.win_rate>=50?'g':'r'},st.win_rate+'%'),
          el('td',{class:cc(st.avg_profit)},fmtU(st.avg_profit))
        ));
      }
      tbl.appendChild(tb);
      card.appendChild(tbl);
    }

    // Suggestions applied
    if (Object.keys(h.suggestions||{}).length) {
      card.appendChild(el('div',{style:'margin-top:12px;padding:10px;background:rgba(245,158,11,.08);border-radius:8px;border:1px solid rgba(245,158,11,.2)'},
        el('div',{style:'font-size:12px;font-weight:600;color:#FCD34D;margin-bottom:6px'},'⚡ Parameter Updates Applied'),
        ...Object.entries(h.suggestions).map(([k,v])=>
          el('div',{style:'font-size:12px;color:#CBD5E1;padding:2px 0'},
            el('span',{style:'color:#94A3B8'},k+': '),
            el('span',{style:'color:#FCD34D'},String(v))
          )
        )
      ));
    }

    wrap.appendChild(card);
  }
  return wrap;
}

function buildParams() {
  const card = el('div',{class:'card'});
  card.appendChild(el('div',{class:'card-title'},'⚙️ Active Parameters'));
  card.appendChild(el('p',{style:'font-size:12px;color:#64748B;margin-bottom:12px'},
    'Parameters updated by Hermes are shown in amber. All values are live.'));
  const grid = el('div',{class:'param-grid'});
  const p = state.params;
  const hermes_keys = new Set(
    (state.hermes||[]).flatMap(h=>Object.keys(h.suggestions||{}))
  );
  for (const [k,v] of Object.entries(p).sort()) {
    const chip = el('div',{class:'param-chip'});
    chip.appendChild(el('div',{class:'param-label'},k));
    chip.appendChild(el('div',{class:'param-val'},v));
    if (hermes_keys.has(k)) {
      chip.appendChild(el('div',{class:'param-by'},'⚡ Hermes-tuned'));
    }
    grid.appendChild(chip);
  }
  card.appendChild(grid);
  return card;
}

fetchAll();
setInterval(fetchAll, 30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    # Don't call init_db() here — the bot container owns schema creation.
    # We just wait until the DB file exists before starting.
    import time
    db_path = os.environ.get("DB_PATH", "/data/kotipoti.db")
    for _ in range(30):
        if os.path.exists(db_path):
            break
        log.info(f"Waiting for DB at {db_path}...")
        time.sleep(2)
    log.info(f"KotipotiBot Dashboard starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
