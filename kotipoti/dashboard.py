"""
dashboard.py — KotipotiBot v2 live dashboard
============================================
Flask server. Reads directly from SQLite.
Serves a self-contained vanilla JS dashboard — no CDN, no React.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import os, sys, json, logging, hashlib, secrets
from pathlib import Path

import db

app = Flask(__name__)
CORS(app)

# ── Admin auth ────────────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "CHANGE_ME")
# Simple in-memory token store: token → True (single-user, resets on restart)
_valid_tokens: set = set()

def _check_auth() -> bool:
    """Return True if the request carries a valid admin token."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:] in _valid_tokens
    return False

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



# ── Admin API routes ──────────────────────────────────────────────────────────

@app.route("/api/admin/auth", methods=["POST"])
def admin_auth():
    data = request.get_json(silent=True) or {}
    pw   = data.get("password", "")
    if pw == ADMIN_PASSWORD:
        token = secrets.token_hex(24)
        _valid_tokens.add(token)
        return jsonify({"ok": True, "token": token})
    return jsonify({"ok": False, "error": "Wrong password"}), 401

@app.route("/api/admin/bot/control", methods=["POST"])
def admin_bot_control():
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    data    = request.get_json(silent=True) or {}
    action  = data.get("action", "")  # stop | pause | resume
    if action not in ("stop", "pause", "resume"):
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    cmd_id = db.send_command(action)
    return jsonify({"ok": True, "command": action, "id": cmd_id})

@app.route("/api/admin/pairs", methods=["POST"])
def admin_pairs():
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    data  = request.get_json(silent=True) or {}
    pairs = data.get("pairs")  # full replacement list
    try:
        clean_pairs = db.validate_pairs(pairs)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    db.set_active_pairs(clean_pairs)
    db.send_command("update_pairs", {"pairs": clean_pairs})
    return jsonify({"ok": True, "pairs": clean_pairs})

@app.route("/api/admin/params", methods=["POST"])
def admin_params():
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    data = request.get_json(silent=True) or {}
    updates = data.get("updates", {})
    if not updates:
        return jsonify({"ok": False, "error": "No updates provided"}), 400
    cleaned = {}
    try:
        for k, v in updates.items():
            cleaned[k] = db.validate_param_update(k, v)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    for k, v in cleaned.items():
        db.set_param(k, v, updated_by="admin")
    return jsonify({"ok": True, "updated": list(cleaned.keys())})

@app.route("/api/admin/force_close/<int:trade_id>", methods=["POST"])
def admin_force_close(trade_id):
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    cmd_id = db.send_command("force_close", {"trade_id": trade_id})
    return jsonify({"ok": True, "trade_id": trade_id, "cmd_id": cmd_id})

@app.route("/api/admin/commands")
def admin_commands():
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    try:
        return jsonify(db.get_recent_commands(30))
    except Exception as e:
        return jsonify([])

@app.route("/api/admin/bot_state")
def admin_bot_state():
    if not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403
    try:
        return jsonify({
            "status":         db.get_bot_state("status", "unknown"),
            "last_heartbeat": db.get_bot_state("last_heartbeat", ""),
            "active_pairs":   db.get_active_pairs(),
        })
    except Exception as e:
        return jsonify({"status": "unknown", "last_heartbeat": "", "active_pairs": [],
                        "_error": str(e)})

# ── Health ─────────────────────────────────────────────────────────────────────

@app.route("/api/indicators")
def api_indicators():
    try:
        raw = db.get_bot_state("indicators", "{}")
        regime = db.get_bot_state("btc_regime", "unknown")
        return jsonify({"pairs": json.loads(raw), "btc_regime": regime})
    except Exception as e:
        return jsonify({"pairs": {}, "btc_regime": "unknown"})

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
<title>KotipotiBot - Claude</title>
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
/* Admin styles */
.admin-login{max-width:360px;margin:80px auto;text-align:center}
.admin-login h2{font-size:22px;font-weight:700;margin-bottom:8px}
.admin-login p{font-size:13px;color:#64748B;margin-bottom:24px}
.input{background:rgba(30,41,59,.9);border:1px solid rgba(148,163,184,.2);
       color:#F8FAFC;border-radius:8px;padding:10px 14px;font-size:14px;
       width:100%;outline:none;transition:border .2s}
.input:focus{border-color:rgba(59,130,246,.5)}
.btn-primary{background:linear-gradient(135deg,#3B82F6,#2563EB);border:none;
             color:#fff;border-radius:8px;padding:10px 20px;font-size:14px;
             font-weight:600;cursor:pointer;width:100%;margin-top:12px}
.btn-primary:hover{opacity:.9}
.btn-danger{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);
            color:#F87171;border-radius:8px;padding:6px 14px;font-size:12px;
            font-weight:600;cursor:pointer}
.btn-danger:hover{background:rgba(239,68,68,.25)}
.btn-warn{background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.3);
          color:#FCD34D;border-radius:8px;padding:6px 14px;font-size:12px;
          font-weight:600;cursor:pointer}
.btn-warn:hover{background:rgba(245,158,11,.22)}
.btn-green{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);
           color:#34D399;border-radius:8px;padding:6px 14px;font-size:12px;
           font-weight:600;cursor:pointer}
.btn-green:hover{background:rgba(16,185,129,.22)}
.btn-sm{padding:4px 10px;font-size:11px;border-radius:6px}
.admin-section{margin-bottom:1.25rem}
.admin-label{font-size:12px;font-weight:600;color:#94A3B8;
             text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.pair-chip{display:inline-flex;align-items:center;gap:6px;
           background:rgba(51,65,85,.6);border:1px solid rgba(148,163,184,.15);
           border-radius:20px;padding:4px 10px 4px 12px;font-size:12px;
           color:#E2E8F0;margin:3px}
.pair-x{background:none;border:none;color:#94A3B8;cursor:pointer;font-size:14px;
        line-height:1;padding:0 2px}
.pair-x:hover{color:#F87171}
.ctrl-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.status-pill{display:inline-flex;align-items:center;gap:5px;
             border-radius:999px;padding:5px 14px;font-size:12px;font-weight:700}
.param-edit-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.param-edit-row{display:flex;flex-direction:column;gap:4px}
.param-edit-label{font-size:10px;color:#64748B;text-transform:uppercase;letter-spacing:.5px}
.param-edit-input{background:rgba(30,41,59,.9);border:1px solid rgba(148,163,184,.15);
                  color:#F8FAFC;border-radius:6px;padding:6px 10px;font-size:13px;
                  outline:none;width:100%}
.param-edit-input:focus{border-color:rgba(59,130,246,.4)}
.cmd-row{display:flex;align-items:center;gap:8px;padding:6px 10px;
         border-radius:7px;background:rgba(51,65,85,.35);margin-bottom:4px;font-size:12px}
.cmd-done{color:#34D399}.cmd-error{color:#F87171}.cmd-pending{color:#FCD34D}
.alert{padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:12px}
.alert-ok{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.25);color:#34D399}
.alert-err{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.25);color:#F87171}
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
    e.appendChild((typeof c==='string'||typeof c==='number')?document.createTextNode(String(c)):c);
  }
  return e;
};
const fmt  = (n,d=2) => n==null?'—':Number(n).toFixed(d);
const fmtU = n => n==null?'—':(n>=0?'+':'')+Number(n).toFixed(2)+' USDT';
const fmtP = n => n==null?'—':(n>=0?'+':'')+Number(n).toFixed(2)+'%';
const cc   = n => n==null?'':n>=0?'g':'r';
const sydneyTime = d => {
  if (!d) return '—';
  const dt = new Date(d);
  if (Number.isNaN(dt.getTime())) return '—';
  return dt.toLocaleString('en-AU', {
    timeZone:'Australia/Sydney',
    day:'2-digit',
    month:'short',
    hour:'2-digit',
    minute:'2-digit',
    hour12:false
  });
};
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
  signals:[], params:{}, hermes:[], charts:{}, indicators:{},
  lastUpdated:null, activeTab:'overview',
  // Admin
  adminToken: sessionStorage.getItem('adminToken')||null,
  adminState: {},   // bot_state + pairs fetched after login
  adminCmds:  [],
  adminAlert: null, // {type:'ok'|'err', msg}
  adminParamEdits: {}, // live edits before save
  adminNewPair: '',
};

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
      el('h1',{},'🤖 KotipotiBot - Claude'),
      el('p',{class:'subtitle'},'Kotipoti Trading Bot - Claude Edition - ByBit Futures - 5m')
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
    {id:'admin',   label:'🔐 Admin'},
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
    case 'admin':    content.appendChild(buildAdmin());    break;
  }
  wrap.appendChild(content);
  wrap.appendChild(el('p',{class:'footer'},'KotipotiBot Claude Edition · auto-refreshes every 30s · build 2026-05-25'));
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
    ctx.fillRect(x,v>=0?midY-bH:midY,barW,bH);
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
  const wrap = el('div',{});

  // ── Live indicators card ───────────────────────────────────────────────────
  const ind = state.indicators || {};
  const pairs = ind.pairs || {};
  const regime = ind.btc_regime || 'unknown';
  const regimeCol = regime==='bull'?'#10B981':regime==='bear'?'#EF4444':'#FCD34D';

  const ic = el('div',{class:'card'});
  ic.appendChild(el('div',{style:'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px'},
    el('div',{class:'card-title',style:'margin-bottom:0'},'📡 Live Indicators'),
    el('div',{style:`font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;
        background:rgba(30,41,59,.8);border:1px solid ${regimeCol};color:${regimeCol}`},
      'BTC: '+regime.toUpperCase())
  ));

  if (!Object.keys(pairs).length) {
    ic.appendChild(el('p',{class:'empty'},'Waiting for first bot loop…'));
  } else {
    const tbl = el('table',{});
    tbl.appendChild(el('thead',{},el('tr',{},
      el('th',{class:'left'},'Pair'),
      el('th',{},'Price'),
      el('th',{},'RSI'),
      el('th',{},'ATR%'),
      el('th',{},'VWAP dev'),
      el('th',{},'Vol ratio'),
      el('th',{},'BB pos'),
      el('th',{},'EMA'),
    )));
    const tb = el('tbody',{});
    for (const [pair, d] of Object.entries(pairs)) {
      const rsiCol = d.rsi>70?'#F87171':d.rsi<30?'#34D399':'#CBD5E1';
      const vwapCol = d.vwap_dev>0.5?'#F87171':d.vwap_dev<-0.5?'#34D399':'#CBD5E1';
      const bbBadge = d.bb_pos==='above'
        ? el('span',{style:'background:rgba(239,68,68,.15);color:#F87171;font-size:10px;padding:2px 6px;border-radius:4px'},'▲ above')
        : d.bb_pos==='below'
        ? el('span',{style:'background:rgba(16,185,129,.15);color:#34D399;font-size:10px;padding:2px 6px;border-radius:4px'},'▼ below')
        : el('span',{style:'background:rgba(100,116,139,.15);color:#94A3B8;font-size:10px;padding:2px 6px;border-radius:4px'},'inside');
      const emaBadge = el('span',{
        style:`font-size:10px;padding:2px 6px;border-radius:4px;${d.ema_trend==='bull'?'background:rgba(16,185,129,.15);color:#34D399':'background:rgba(239,68,68,.15);color:#F87171'}`
      }, d.ema_trend);
      tb.appendChild(el('tr',{},
        el('td',{class:'left',style:'font-weight:600;color:#F8FAFC'},pair.split('/')[0]),
        el('td',{},fmt(d.close,4)),
        el('td',{style:`color:${rsiCol};font-weight:600`},fmt(d.rsi,1)),
        el('td',{},fmt(d.atr_pct,2)+'%'),
        el('td',{style:`color:${vwapCol}`},(d.vwap_dev>=0?'+':'')+fmt(d.vwap_dev,2)+'%'),
        el('td',{style:d.vol_ratio>=1.2?'color:#FCD34D;font-weight:600':''},fmt(d.vol_ratio,2)+'x'),
        el('td',{},bbBadge),
        el('td',{},emaBadge),
      ));
    }
    tbl.appendChild(tb);
    ic.appendChild(el('div',{class:'overflow-x'},tbl));
  }
  wrap.appendChild(ic);

  // ── Bottom row: recent trades + hermes ────────────────────────────────────
  const row2 = el('div',{class:'two-col'});

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
        el('span',{style:'font-size:11px;color:#94A3B8;margin-left:6px'},t.exit_reason||'—'),
        el('div',{style:'font-size:10px;color:#64748B;margin-top:2px'},sydneyTime(t.exit_time)+' Sydney')
      )
    ));
    row.appendChild(el('span',{class:cc(p),style:'font-size:13px;font-weight:600'},fmtU(p)));
    rc.appendChild(row);
  }
  row2.appendChild(rc);

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
  row2.appendChild(hc);
  wrap.appendChild(row2);
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
    el('th',{},'Opened'),el('th',{},'Stake'),el('th',{},'Holding'),el('th',{},'Tag'),el('th',{},'Session')
  )));
  const tb = el('tbody',{});
  for (const t of state.openTrades) {
    tb.appendChild(el('tr',{},
      el('td',{class:'left',html:`<span style="font-weight:700;color:#F8FAFC">${t.pair}</span>`}),
      el('td',{},el('span',{class:t.side==='short'?'dir-short':'dir-long'},t.side.toUpperCase())),
      el('td',{},fmt(t.entry_price,4)),
      el('td',{style:'color:#94A3B8;font-size:12px'},sydneyTime(t.entry_time)),
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
    el('th',{},'Entry'),el('th',{},'Exit'),el('th',{},'Opened'),el('th',{},'Closed'),el('th',{},'P&L'),
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
      el('td',{style:'color:#94A3B8;font-size:12px'},sydneyTime(t.entry_time)),
      el('td',{style:'color:#94A3B8;font-size:12px'},sydneyTime(t.exit_time)),
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

// ── Admin helpers ─────────────────────────────────────────────────────────────

async function adminFetch(path, opts={}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer '+(state.adminToken||''),
      ...(opts.headers||{})
    }
  });
  return res.json();
}

async function refreshAdminState() {
  if (!state.adminToken) return;
  const [s, c] = await Promise.all([
    adminFetch('/api/admin/bot_state'),
    adminFetch('/api/admin/commands'),
  ]);
  if (s.ok !== false) state.adminState = s;
  if (Array.isArray(c)) state.adminCmds = c;
}

function buildAdmin() {
  const wrap = el('div',{});

  // Not logged in → show login form
  if (!state.adminToken) {
    const box = el('div',{class:'admin-login'});
    box.appendChild(el('div',{style:'font-size:40px;margin-bottom:12px'},'🔐'));
    box.appendChild(el('h2',{},'Admin Panel'));
    box.appendChild(el('p',{},'Enter your admin password to continue'));

    if (state.adminAlert) {
      box.appendChild(el('div',{class:`alert alert-${state.adminAlert.type}`},
        state.adminAlert.msg));
    }

    const pwInput = el('input',{class:'input',type:'password',placeholder:'Admin password…'});
    const loginBtn = el('button',{class:'btn-primary'}, 'Unlock');

    const doLogin = async () => {
      const res = await fetch('/api/admin/auth', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({password: pwInput.value})
      }).then(r=>r.json());
      if (res.ok) {
        state.adminToken = res.token;
        sessionStorage.setItem('adminToken', res.token);
        state.adminAlert = null;
        await refreshAdminState();
      } else {
        state.adminAlert = {type:'err', msg:'❌ Wrong password'};
      }
      render();
    };
    pwInput.addEventListener('keydown', e=>{ if(e.key==='Enter') doLogin(); });
    loginBtn.addEventListener('click', doLogin);
    box.appendChild(pwInput);
    box.appendChild(loginBtn);
    wrap.appendChild(box);
    return wrap;
  }

  // ── Logged in ──────────────────────────────────────────────────────────────
  const s  = state.adminState;
  const pairs = Array.isArray(s.active_pairs) ? s.active_pairs : db_active_pairs_fallback();

  // Header row with logout
  const hdr = el('div',{style:'display:flex;justify-content:space-between;align-items:center;margin-bottom:1.25rem'});
  hdr.appendChild(el('div',{},
    el('h2',{style:'font-size:18px;font-weight:700'},'🔐 Admin Panel'),
    el('p',{style:'font-size:12px;color:#64748B;margin-top:2px'},'Changes take effect immediately via command queue')
  ));
  hdr.appendChild(el('button',{class:'btn',onclick:()=>{
    state.adminToken=null;
    sessionStorage.removeItem('adminToken');
    state.adminAlert=null;
    render();
  }},'🚪 Logout'));
  wrap.appendChild(hdr);

  if (state.adminAlert) {
    const alert = el('div',{class:`alert alert-${state.adminAlert.type}`},state.adminAlert.msg);
    wrap.appendChild(alert);
    setTimeout(()=>{ state.adminAlert=null; render(); }, 4000);
  }

  // ── Position & Risk (top, full width) ────────────────────────────────────
  const riskCard = el('div',{class:'card'});
  riskCard.appendChild(el('div',{class:'card-title'},'💰 Position & Risk Settings'));
  riskCard.appendChild(el('p',{style:'font-size:12px;color:#64748B;margin-bottom:14px'},
    'Core trading parameters. Changes apply immediately on the next bot loop.'));

  const riskFields = [
    {key:'stake_usdt',     label:'Stake per trade (USDT)',  hint:'Amount risked per position'},
    {key:'wallet_start',   label:'Dry-run wallet (USDT)',   hint:'Starting balance used for daily loss limit'},
    {key:'leverage',       label:'Leverage',                hint:'Multiplier (e.g. 5 = 5×)'},
    {key:'max_open_trades',label:'Max open trades',         hint:'How many positions at once'},
    {key:'stoploss_pct',   label:'Stop loss %',             hint:'e.g. 2.5 = 2.5% from entry'},
    {key:'daily_loss_limit',label:'Daily loss limit %',     hint:'Halt if wallet drops this much'},
    {key:'max_consec_losses',label:'Max consecutive losses',hint:'Circuit breaker before cooldown'},
  ];

  const riskGrid = el('div',{style:'display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:14px'});
  for (const {key, label, hint} of riskFields) {
    const curVal = state.adminParamEdits[key] !== undefined
      ? state.adminParamEdits[key]
      : (state.params[key] || '');
    const row = el('div',{class:'param-edit-row'});
    row.appendChild(el('label',{class:'param-edit-label',title:hint}, label));
    const inp = el('input',{class:'param-edit-input', value: curVal,
      style:'font-size:15px;font-weight:600;padding:8px 12px'});
    inp.addEventListener('input', e => { state.adminParamEdits[key] = e.target.value; });
    row.appendChild(inp);
    row.appendChild(el('div',{style:'font-size:10px;color:#475569;margin-top:2px'}, hint));
    riskGrid.appendChild(row);
  }
  riskCard.appendChild(riskGrid);

  const saveRiskBtn = el('button',{class:'btn-primary',style:'width:auto;padding:9px 24px'},
    '💾 Save Risk Settings');
  saveRiskBtn.addEventListener('click', async () => {
    const edits = {};
    for (const {key} of riskFields) {
      if (state.adminParamEdits[key] !== undefined) edits[key] = state.adminParamEdits[key];
    }
    if (!Object.keys(edits).length) { state.adminAlert={type:'err',msg:'No changes to save'}; render(); return; }
    const res = await adminFetch('/api/admin/params',{method:'POST',body:JSON.stringify({updates:edits})});
    if (res.ok) {
      state.adminAlert={type:'ok',msg:`✅ Saved: ${res.updated.join(', ')}`};
      for (const k of res.updated) delete state.adminParamEdits[k];
      await fetchAll();
    } else { state.adminAlert={type:'err',msg:'❌ '+res.error}; }
    render();
  });
  riskCard.appendChild(saveRiskBtn);
  wrap.appendChild(riskCard);

  const grid = el('div',{style:'display:grid;grid-template-columns:1fr 1fr;gap:1.25rem'});

  // ── Bot Control ────────────────────────────────────────────────────────────
  const ctrlCard = el('div',{class:'card'});
  ctrlCard.appendChild(el('div',{class:'card-title'},'⚙️ Bot Control'));

  const statusVal = s.status||'unknown';
  const dotCol = statusVal==='running'?'#10B981':statusVal==='paused'?'#FCD34D':'#EF4444';
  const hbAgo  = s.last_heartbeat
    ? Math.round((Date.now()-new Date(s.last_heartbeat).getTime())/1000)+'s ago'
    : '—';
  ctrlCard.appendChild(el('div',{style:'display:flex;align-items:center;gap:10px;margin-bottom:16px'},
    el('div',{style:`width:10px;height:10px;border-radius:50%;background:${dotCol};box-shadow:0 0 6px ${dotCol}`}),
    el('span',{style:'font-size:15px;font-weight:700;color:#F8FAFC'},statusVal.toUpperCase()),
    el('span',{style:'font-size:12px;color:#64748B'},'Last heartbeat: '+hbAgo)
  ));

  const sendCtrl = async (action, label) => {
    const res = await adminFetch('/api/admin/bot/control', {
      method:'POST', body:JSON.stringify({action})
    });
    if (res.ok) {
      state.adminAlert = {type:'ok', msg:`✅ Command "${label}" queued (id #${res.id})`};
    } else {
      state.adminAlert = {type:'err', msg:'❌ '+res.error};
    }
    await refreshAdminState();
    render();
  };

  const ctrlRow = el('div',{class:'ctrl-row'});
  ctrlRow.appendChild(el('button',{class:'btn-green',
    onclick:()=>sendCtrl('resume','Resume')},'▶ Resume'));
  ctrlRow.appendChild(el('button',{class:'btn-warn',
    onclick:()=>sendCtrl('pause','Pause')},'⏸ Pause'));
  ctrlRow.appendChild(el('button',{class:'btn-danger',
    onclick:()=>{
      if(confirm('Stop the bot? It will exit after the current loop.')) sendCtrl('stop','Stop');
    }},'⏹ Stop'));
  ctrlCard.appendChild(ctrlRow);
  grid.appendChild(ctrlCard);

  // ── Active Pairs ───────────────────────────────────────────────────────────
  const pairsCard = el('div',{class:'card'});
  pairsCard.appendChild(el('div',{class:'card-title'},'💱 Active Pairs'));
  pairsCard.appendChild(el('p',{style:'font-size:12px;color:#64748B;margin-bottom:10px'},
    'Format: BTC/USDT:USDT · changes take ~1 min'));

  const pairList = el('div',{style:'margin-bottom:12px;min-height:32px'});
  const renderPairs = (pairArr) => {
    pairList.innerHTML='';
    for (const p of pairArr) {
      const chip = el('span',{class:'pair-chip'}, p);
      const x = el('button',{class:'pair-x',title:'Remove'}, '×');
      x.addEventListener('click', async () => {
        const newPairs = pairArr.filter(pp=>pp!==p);
        const res = await adminFetch('/api/admin/pairs',{
          method:'POST', body:JSON.stringify({pairs:newPairs})
        });
        if (res.ok) {
          state.adminAlert={type:'ok',msg:'✅ Pair removed: '+p};
          await refreshAdminState();
        } else {
          state.adminAlert={type:'err',msg:'❌ '+res.error};
        }
        render();
      });
      chip.appendChild(x);
      pairList.appendChild(chip);
    }
  };
  renderPairs(pairs);
  pairsCard.appendChild(pairList);

  const addRow = el('div',{style:'display:flex;gap:8px'});
  const pairInput = el('input',{class:'input',placeholder:'SOL/USDT:USDT',
    style:'flex:1;padding:7px 12px;font-size:13px'});
  pairInput.value = state.adminNewPair;
  pairInput.addEventListener('input', e=>{ state.adminNewPair=e.target.value; });
  const addBtn = el('button',{class:'btn-primary',style:'width:auto;margin-top:0;padding:7px 16px;font-size:13px'},
    '+ Add');
  addBtn.addEventListener('click', async () => {
    const newPair = (state.adminNewPair||'').trim().toUpperCase();
    if (!newPair) return;
    const newPairs = [...pairs, newPair];
    const res = await adminFetch('/api/admin/pairs',{
      method:'POST', body:JSON.stringify({pairs:newPairs})
    });
    if (res.ok) {
      state.adminAlert={type:'ok',msg:'✅ Pair added: '+newPair};
      state.adminNewPair='';
      await refreshAdminState();
    } else {
      state.adminAlert={type:'err',msg:'❌ '+res.error};
    }
    render();
  });
  addRow.appendChild(pairInput);
  addRow.appendChild(addBtn);
  pairsCard.appendChild(addRow);
  grid.appendChild(pairsCard);

  wrap.appendChild(grid);

  // ── Open Trades — Force Close ──────────────────────────────────────────────
  const openCard = el('div',{class:'card',style:'margin-top:0'});
  openCard.appendChild(el('div',{class:'card-title'},'📉 Open Positions — Force Close'));
  if (!state.openTrades.length) {
    openCard.appendChild(el('p',{class:'empty'},'No open positions'));
  } else {
    const tbl = el('table',{});
    tbl.appendChild(el('thead',{},el('tr',{},
      el('th',{class:'left'},'#'),el('th',{class:'left'},'Pair'),el('th',{},'Dir'),
      el('th',{},'Entry'),el('th',{},'Opened'),el('th',{},'Stake'),el('th',{},'Holding'),el('th',{},'Action')
    )));
    const tb = el('tbody',{});
    for (const t of state.openTrades) {
      const fc = el('button',{class:'btn-danger btn-sm'},'Force Close');
      fc.addEventListener('click', async ()=>{
        if (!confirm(`Force close trade #${t.id} (${t.pair})?`)) return;
        const res = await adminFetch(`/api/admin/force_close/${t.id}`,{method:'POST'});
        if (res.ok) {
          state.adminAlert={type:'ok',msg:`✅ Force-close queued for trade #${t.id}`};
        } else {
          state.adminAlert={type:'err',msg:'❌ '+res.error};
        }
        render();
      });
      tb.appendChild(el('tr',{},
        el('td',{class:'left',style:'color:#64748B;font-size:11px'},'#'+t.id),
        el('td',{class:'left',style:'font-weight:600'},t.pair),
        el('td',{},el('span',{class:t.side==='short'?'dir-short':'dir-long'},t.side.toUpperCase())),
        el('td',{},fmt(t.entry_price,4)),
        el('td',{style:'color:#94A3B8;font-size:12px'},sydneyTime(t.entry_time)),
        el('td',{},fmt(t.stake_usdt,0)+' USDT'),
        el('td',{},holdDur(t.entry_time)),
        el('td',{},fc)
      ));
    }
    tbl.appendChild(tb);
    openCard.appendChild(el('div',{class:'overflow-x'},tbl));
  }
  wrap.appendChild(openCard);

  // ── Strategy Parameters Editor ────────────────────────────────────────────
  const paramCard = el('div',{class:'card'});
  paramCard.appendChild(el('div',{class:'card-title'},'⚙️ Strategy Parameters'));
  paramCard.appendChild(el('p',{style:'font-size:12px;color:#64748B;margin-bottom:12px'},
    'Edit values below then click Save. Hermes records suggestions; auto-apply is disabled unless explicitly enabled.'));

  const paramGroups = {
    'Signal Thresholds': ['signal_profile','confirmation_mode','rsi_short_entry','rsi_long_entry','vwap_rsi_short','vwap_rsi_long','trend_rsi_long','trend_rsi_short','trend_rsi_max_long','trend_rsi_min_short','active_min_volume_ratio','rsi_exit_short','rsi_exit_long','volume_multiplier','vwap_dev_min','min_signal_confidence','min_setup_trades','min_setup_win_rate'],
    'Bollinger / EMA':   ['bb_period','bb_std','ema_fast','ema_slow'],
    'ATR Filter':        ['atr_period','atr_min_pct','atr_max_pct'],
    'Risk / Position':   ['wallet_start','stake_usdt','max_risk_per_trade_pct','stoploss_pct','trailing_pct','trailing_offset','max_trade_duration_min','time_stop_min_profit_pct','leverage','max_open_trades'],
    'Comparison / Costs': ['bot_variant','taker_fee_bps','slippage_bps'],
    'Circuit Breakers':  ['daily_loss_limit','max_consec_losses','pair_cooldown_min','max_trades_per_pair_per_day','max_trades_per_day','blocked_sessions','hermes_auto_apply'],
  };

  for (const [groupName, keys] of Object.entries(paramGroups)) {
    const grpWrap = el('div',{class:'admin-section'});
    grpWrap.appendChild(el('div',{class:'admin-label'},groupName));
    const pgrid = el('div',{class:'param-edit-grid'});
    for (const k of keys) {
      const curVal = state.adminParamEdits[k] !== undefined
        ? state.adminParamEdits[k]
        : (state.params[k]||'');
      const row = el('div',{class:'param-edit-row'});
      row.appendChild(el('label',{class:'param-edit-label'},k.replace(/_/g,' ')));
      const inp = el('input',{class:'param-edit-input', value: curVal});
      inp.addEventListener('input', e=>{
        state.adminParamEdits[k] = e.target.value;
      });
      row.appendChild(inp);
      pgrid.appendChild(row);
    }
    grpWrap.appendChild(pgrid);
    paramCard.appendChild(grpWrap);
  }

  const saveBtn = el('button',{class:'btn-primary',style:'width:auto;margin-top:4px;padding:9px 24px'},
    '💾 Save Parameters');
  saveBtn.addEventListener('click', async ()=>{
    const edits = state.adminParamEdits;
    if (!Object.keys(edits).length) {
      state.adminAlert={type:'err',msg:'No changes to save'};
      render();
      return;
    }
    const res = await adminFetch('/api/admin/params',{
      method:'POST', body:JSON.stringify({updates: edits})
    });
    if (res.ok) {
      state.adminAlert={type:'ok',msg:`✅ Saved ${res.updated.length} parameter(s)`};
      state.adminParamEdits={};
      await fetchAll();
    } else {
      state.adminAlert={type:'err',msg:'❌ '+res.error};
    }
    render();
  });
  paramCard.appendChild(saveBtn);
  wrap.appendChild(paramCard);

  // ── Recent Commands ────────────────────────────────────────────────────────
  const cmdsCard = el('div',{class:'card'});
  cmdsCard.appendChild(el('div',{class:'card-title'},'📋 Recent Commands'));
  const cmds = state.adminCmds;
  if (!cmds.length) {
    cmdsCard.appendChild(el('p',{class:'empty'},'No commands yet'));
  } else {
    for (const c of cmds.slice(0,15)) {
      const statusCls = c.status==='done'?'cmd-done':c.status==='error'?'cmd-error':'cmd-pending';
      const icon = c.status==='done'?'✅':c.status==='error'?'❌':'⏳';
      cmdsCard.appendChild(el('div',{class:'cmd-row'},
        el('span',{style:'color:#64748B;font-size:11px;min-width:80px'},
          new Date(c.ts).toLocaleTimeString()),
        el('span',{style:'font-size:12px;color:#E2E8F0;font-weight:600'},c.command),
        el('span',{class:statusCls},icon+' '+c.status),
        c.result?el('span',{style:'color:#94A3B8;font-size:11px;flex:1;text-align:right'},c.result):null
      ));
    }
  }
  cmdsCard.appendChild(el('button',{class:'btn',style:'margin-top:8px;font-size:11px',
    onclick: async()=>{ await refreshAdminState(); render(); }
  },'↻ Refresh'));
  wrap.appendChild(cmdsCard);

  return wrap;
}

function db_active_pairs_fallback() {
  // Fallback if admin state not loaded yet
  return ["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","DOGE/USDT:USDT","XRP/USDT:USDT"];
}

// Auto-refresh admin state when on admin tab
async function fetchAll() {
  try {
    const [sum, open, closed, sigs, params, hermes, charts, indicators] = await Promise.all([
      fetch('/api/summary').then(r=>r.json()).catch(()=>({})),
      fetch('/api/open_trades').then(r=>r.json()).catch(()=>[]),
      fetch('/api/recent_trades').then(r=>r.json()).catch(()=>[]),
      fetch('/api/signals').then(r=>r.json()).catch(()=>[]),
      fetch('/api/params').then(r=>r.json()).catch(()=>({})),
      fetch('/api/hermes').then(r=>r.json()).catch(()=>[]),
      fetch('/api/charts').then(r=>r.json()).catch(()=>({})),
      fetch('/api/indicators').then(r=>r.json()).catch(()=>({})),
    ]);
    state.summary=sum; state.openTrades=open; state.recentTrades=closed;
    state.signals=sigs; state.params=params; state.hermes=hermes;
    state.charts=charts; state.indicators=indicators;
    state.lastUpdated=new Date();
    if (state.activeTab==='admin') await refreshAdminState();
  } catch(e) { console.error(e); }
  render();
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
    import time
    db_path = os.environ.get("DB_PATH", "/data/kotipoti.db")
    # Wait for DB file to appear (bot container creates it first)
    for _ in range(30):
        if os.path.exists(db_path):
            break
        log.info(f"Waiting for DB at {db_path}...")
        time.sleep(2)
    # Run init_db() to ensure any new tables (commands, bot_state) exist
    # even if the DB was originally created by an older bot version.
    # init_db() uses CREATE TABLE IF NOT EXISTS — safe to call any time.
    try:
        db.init_db()
        log.info("DB schema verified/upgraded OK")
    except Exception as e:
        log.warning(f"init_db warning (non-fatal): {e}")
    log.info(f"KotipotiBot Dashboard starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
