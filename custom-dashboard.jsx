/**
 * KotipotiBot Custom Dashboard
 * ==============================
 * Fetches live data from the Freqtrade REST API via the Flask proxy at /api/v1/*.
 *
 * The Flask server in dashboard_app.py handles authentication so this component
 * uses relative URLs — no credentials in the browser, no CORS issues.
 *
 * Auto-refreshes every 30 seconds.
 *
 * NOTE: This file is served as a raw JS function by dashboard_app.py.
 * Do NOT use ES module syntax (import/export). Use React globals instead.
 */

function KotipotiDashboard() {
  // ─── State ───────────────────────────────────────────────────────────────
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [lastUpdated, setLastUpdated] = React.useState(null);

  // API data
  const [balance, setBalance] = React.useState(null);
  const [openTrades, setOpenTrades] = React.useState([]);
  const [profit, setProfit] = React.useState(null);
  const [performance, setPerformance] = React.useState([]);
  const [recentTrades, setRecentTrades] = React.useState([]);
  const [botStatus, setBotStatus] = React.useState(null);

  // UI state
  const [activeTab, setActiveTab] = React.useState('overview');

  // ─── API fetch ───────────────────────────────────────────────────────────
  async function apiFetch(path) {
    const resp = await fetch('/api/v1/' + path, { credentials: 'include' });
    if (!resp.ok) throw new Error(path + ': HTTP ' + resp.status);
    return resp.json();
  }

  async function fetchAll() {
    try {
      const [bal, status, prof, perf, trades, config] = await Promise.all([
        apiFetch('balance').catch(() => null),
        apiFetch('status').catch(() => []),
        apiFetch('profit').catch(() => null),
        apiFetch('performance').catch(() => []),
        apiFetch('trades?limit=30').catch(() => ({ trades: [] })),
        apiFetch('show_config').catch(() => null),
      ]);

      setBalance(bal);
      setOpenTrades(Array.isArray(status) ? status : []);
      setProfit(prof);
      setPerformance(Array.isArray(perf) ? perf : []);
      setRecentTrades(
        ((trades && trades.trades) || [])
          .filter(function(t) { return !t.is_open; })
          .slice(-20)
          .reverse()
      );
      setBotStatus(config);
      setError(null);
      setLastUpdated(new Date());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(function() {
    fetchAll();
    const interval = setInterval(fetchAll, 30000);
    return function() { clearInterval(interval); };
  }, []);

  // ─── Derived stats ───────────────────────────────────────────────────────
  const walletValue = balance && (balance.total != null ? balance.total : balance.value);
  const walletStart = balance && balance.starting_capital;
  const walletPnl = (walletValue != null && walletStart != null) ? walletValue - walletStart : null;
  const walletPnlPct = (walletStart && walletPnl != null) ? (walletPnl / walletStart * 100) : null;

  const totalProfit = profit && profit.profit_all_coin;
  const totalProfitPct = profit && profit.profit_all_percent_mean;
  const tradeCount = (profit && profit.trade_count) || 0;
  const winCount = (profit && profit.winning_trades) || 0;
  const lossCount = (profit && profit.losing_trades) || 0;
  const winRate = tradeCount > 0 ? (winCount / tradeCount * 100).toFixed(1) : null;
  const avgDuration = profit && profit.avg_duration;
  const profitFactor = profit && profit.profit_factor;
  const maxDrawdown = profit && profit.max_drawdown;

  const openPnl = openTrades.reduce(function(sum, t) { return sum + (t.profit_abs || 0); }, 0);

  const topPairs = performance.slice().sort(function(a, b) {
    return (b.profit_abs || 0) - (a.profit_abs || 0);
  }).slice(0, 5);

  // ─── Helpers ─────────────────────────────────────────────────────────────
  function fmt(n, decimals) {
    if (n == null) return '—';
    return Number(n).toFixed(decimals != null ? decimals : 2);
  }
  function fmtUsdt(n) {
    if (n == null) return '—';
    var sign = n >= 0 ? '+' : '';
    return sign + Number(n).toFixed(2) + ' USDT';
  }
  function fmtPct(n) {
    if (n == null) return '—';
    var sign = n >= 0 ? '+' : '';
    return sign + Number(n).toFixed(2) + '%';
  }
  function holdDuration(openDate) {
    if (!openDate) return '—';
    var diff = Math.floor((Date.now() - new Date(openDate).getTime()) / 1000);
    var h = Math.floor(diff / 3600);
    var m = Math.floor((diff % 3600) / 60);
    return h > 0 ? (h + 'h ' + m + 'm') : (m + 'm');
  }
  function exitLabel(reason) {
    var map = { roi: 'ROI', stoploss: 'Stop', trailing_stop_loss: 'Trail', exit_signal: 'Signal', force_exit: 'Force', liquidation: '⚠ Liq' };
    return map[reason] || reason || '—';
  }

  // ─── Design tokens ───────────────────────────────────────────────────────
  var green = '#10B981';
  var red = '#EF4444';
  var muted = '#94A3B8';
  var surface = 'rgba(30, 41, 59, 0.85)';
  var surfaceAlt = 'rgba(51, 65, 85, 0.6)';
  var borderStyle = '1px solid rgba(148, 163, 184, 0.12)';
  var isRunning = botStatus && botStatus.state === 'running';

  var card = { background: surface, border: borderStyle, borderRadius: '14px', padding: '1.25rem' };
  var labelStyle = { margin: 0, fontSize: '11px', color: muted, fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.6px' };
  var bigNum = { margin: '0.4rem 0 0 0', fontSize: '26px', fontWeight: 700, color: '#F8FAFC', letterSpacing: '-0.5px' };
  var subText = { margin: '0.5rem 0 0 0', fontSize: '12px', color: muted };

  // ─── Loading screen ───────────────────────────────────────────────────────
  if (loading) {
    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '100vh', gap: '16px', color: muted }
    },
      React.createElement('style', null, '@keyframes spin{to{transform:rotate(360deg)}}'),
      React.createElement('div', { style: { width: '40px', height: '40px', border: '3px solid rgba(148,163,184,0.2)', borderTop: '3px solid #3B82F6', borderRadius: '50%', animation: 'spin 0.8s linear infinite' } }),
      React.createElement('p', { style: { margin: 0, fontSize: '14px' } }, 'Connecting to Freqtrade…')
    );
  }

  // ─── Error screen ─────────────────────────────────────────────────────────
  if (error && !balance && openTrades.length === 0) {
    return React.createElement('div', {
      style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '100vh', gap: '12px', color: muted, textAlign: 'center', padding: '2rem' }
    },
      React.createElement('div', { style: { fontSize: '48px' } }, '🔌'),
      React.createElement('p', { style: { margin: 0, fontSize: '18px', color: '#F8FAFC', fontWeight: 600 } }, 'Cannot reach Freqtrade API'),
      React.createElement('p', { style: { margin: 0, fontSize: '13px', maxWidth: '420px' } },
        'Make sure freqtrade is running and the REST API is enabled on port 8090. ',
        React.createElement('br', null),
        'Error: ', React.createElement('code', { style: { color: '#FB923C' } }, error)
      ),
      React.createElement('button', {
        onClick: fetchAll,
        style: { marginTop: '8px', background: '#3B82F6', color: '#fff', border: 'none', padding: '8px 20px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }
      }, '↻ Retry')
    );
  }

  // ─── Tab definitions ──────────────────────────────────────────────────────
  var tabs = [
    { id: 'overview', label: '📊 Overview' },
    { id: 'open', label: '📉 Open (' + openTrades.length + ')' },
    { id: 'closed', label: '✅ Closed' },
    { id: 'pairs', label: '🏅 Pairs' },
  ];

  // ─── Helpers for table headers ────────────────────────────────────────────
  function th(text, align) {
    return React.createElement('th', {
      style: { textAlign: align || 'right', padding: '8px 10px', color: muted, fontWeight: 500, fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.5px', whiteSpace: 'nowrap' }
    }, text);
  }
  function td(content, extra) {
    return React.createElement('td', Object.assign({ style: { padding: '11px 10px', color: '#CBD5E1', textAlign: 'right' } }, extra || {}), content);
  }

  // ─── Main render ──────────────────────────────────────────────────────────
  return React.createElement('div', {
    style: { background: 'linear-gradient(160deg, #0F172A 0%, #1A2540 100%)', minHeight: '100vh', padding: '1.5rem', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif', color: '#F8FAFC' }
  },
    React.createElement('div', { style: { maxWidth: '1400px', margin: '0 auto' } },

      // ── Header ──
      React.createElement('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '1.5rem', flexWrap: 'wrap', gap: '12px' }
      },
        React.createElement('div', null,
          React.createElement('h1', { style: { margin: 0, fontSize: '26px', fontWeight: 700, letterSpacing: '-0.5px' } }, '🤖 KotipotiBot'),
          React.createElement('p', { style: { margin: '4px 0 0 0', fontSize: '13px', color: muted } }, 'Short Scalper · Bybit Futures · Dry Run · 5m')
        ),
        React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' } },
          React.createElement('div', {
            style: { display: 'flex', alignItems: 'center', gap: '6px', background: isRunning ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)', border: '1px solid ' + (isRunning ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'), borderRadius: '999px', padding: '5px 12px', fontSize: '12px', fontWeight: 600, color: isRunning ? green : red }
          },
            React.createElement('div', { style: { width: '7px', height: '7px', borderRadius: '50%', background: isRunning ? green : red } }),
            botStatus ? (isRunning ? 'Running' : (botStatus.state || 'Unknown')) : 'Unknown'
          ),
          error && React.createElement('div', {
            style: { fontSize: '12px', color: '#FB923C', background: 'rgba(251,146,60,0.1)', border: '1px solid rgba(251,146,60,0.3)', borderRadius: '6px', padding: '4px 10px' }
          }, '⚠ Partial data'),
          React.createElement('p', { style: { margin: 0, fontSize: '11px', color: muted } },
            lastUpdated ? ('Updated ' + lastUpdated.toLocaleTimeString()) : ''
          ),
          React.createElement('button', {
            onClick: fetchAll,
            style: { background: 'rgba(59,130,246,0.15)', border: '1px solid rgba(59,130,246,0.3)', color: '#60A5FA', borderRadius: '8px', padding: '6px 12px', fontSize: '12px', cursor: 'pointer', fontWeight: 500 }
          }, '↻ Refresh')
        )
      ),

      // ── KPI row ──
      React.createElement('div', {
        style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }
      },
        // Wallet
        React.createElement('div', {
          style: Object.assign({}, card, { borderColor: 'rgba(59,130,246,0.25)', background: 'linear-gradient(135deg, rgba(59,130,246,0.08) 0%, rgba(139,92,246,0.08) 100%)' })
        },
          React.createElement('p', { style: labelStyle }, 'Dry-Run Wallet'),
          React.createElement('p', { style: bigNum }, walletValue != null ? (fmt(walletValue, 2) + ' USDT') : '—'),
          walletPnlPct != null
            ? React.createElement('p', { style: Object.assign({}, subText, { color: walletPnlPct >= 0 ? green : red, fontWeight: 600 }) },
                fmtPct(walletPnlPct) + ' since start (' + fmtUsdt(walletPnl) + ')')
            : React.createElement('p', { style: subText }, 'No balance data')
        ),
        // Total P&L
        React.createElement('div', {
          style: Object.assign({}, card, { borderColor: (totalProfit >= 0) ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)', background: (totalProfit >= 0) ? 'linear-gradient(135deg,rgba(16,185,129,0.08) 0%,rgba(5,150,105,0.08) 100%)' : 'linear-gradient(135deg,rgba(239,68,68,0.08) 0%,rgba(185,28,28,0.08) 100%)' })
        },
          React.createElement('p', { style: labelStyle }, 'Total Realised P&L'),
          React.createElement('p', { style: Object.assign({}, bigNum, { color: totalProfit == null ? '#F8FAFC' : (totalProfit >= 0 ? green : red) }) }, fmtUsdt(totalProfit)),
          React.createElement('p', { style: subText }, fmtPct(totalProfitPct) + ' avg · ' + tradeCount + ' trades')
        ),
        // Win rate
        React.createElement('div', {
          style: Object.assign({}, card, { borderColor: 'rgba(168,85,247,0.25)', background: 'linear-gradient(135deg,rgba(168,85,247,0.08) 0%,rgba(236,72,153,0.08) 100%)' })
        },
          React.createElement('p', { style: labelStyle }, 'Win Rate'),
          React.createElement('p', { style: bigNum }, winRate != null ? (winRate + '%') : '—'),
          React.createElement('div', { style: { marginTop: '8px', height: '5px', background: 'rgba(148,163,184,0.15)', borderRadius: '3px', overflow: 'hidden' } },
            winRate != null && React.createElement('div', { style: { width: winRate + '%', height: '100%', background: 'linear-gradient(90deg,#A855F7,#EC4899)' } })
          ),
          React.createElement('p', { style: subText }, winCount + 'W · ' + lossCount + 'L · PF: ' + (profitFactor != null ? fmt(profitFactor) : '—'))
        ),
        // Open positions
        React.createElement('div', {
          style: Object.assign({}, card, { borderColor: 'rgba(245,158,11,0.25)', background: 'linear-gradient(135deg,rgba(245,158,11,0.08) 0%,rgba(251,146,60,0.08) 100%)' })
        },
          React.createElement('p', { style: labelStyle }, 'Open Positions'),
          React.createElement('p', { style: bigNum }, openTrades.length),
          React.createElement('p', { style: Object.assign({}, subText, { color: openPnl >= 0 ? green : red, fontWeight: openPnl !== 0 ? 600 : 400 }) },
            'Unrealised: ' + fmtUsdt(openPnl))
        ),
        // Max drawdown
        React.createElement('div', { style: card },
          React.createElement('p', { style: labelStyle }, 'Max Drawdown'),
          React.createElement('p', { style: Object.assign({}, bigNum, { color: (maxDrawdown != null && maxDrawdown > 0.05) ? '#FB923C' : '#F8FAFC' }) },
            maxDrawdown != null ? fmtPct(maxDrawdown * -100) : '—'),
          React.createElement('p', { style: subText }, 'Avg hold: ' + (avgDuration || '—'))
        )
      ),

      // ── Tabs ──
      React.createElement('div', {
        style: { display: 'flex', gap: '4px', marginBottom: '1.25rem', background: 'rgba(30,41,59,0.6)', borderRadius: '10px', padding: '4px', width: 'fit-content', flexWrap: 'wrap' }
      },
        tabs.map(function(tab) {
          return React.createElement('button', {
            key: tab.id,
            onClick: function() { setActiveTab(tab.id); },
            style: { background: activeTab === tab.id ? 'linear-gradient(135deg,#3B82F6,#2563EB)' : 'transparent', color: activeTab === tab.id ? '#fff' : muted, border: 'none', padding: '7px 14px', borderRadius: '7px', fontSize: '12px', fontWeight: 500, cursor: 'pointer', whiteSpace: 'nowrap' }
          }, tab.label);
        })
      ),

      // ── OVERVIEW TAB ──
      activeTab === 'overview' && React.createElement('div', {
        style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.25rem' }
      },
        // Recent closed trades
        React.createElement('div', { style: card },
          React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } }, 'Recent Closed Trades'),
          recentTrades.length === 0
            ? React.createElement('p', { style: Object.assign({}, subText, { textAlign: 'center', padding: '1.5rem 0' }) }, 'No closed trades yet')
            : React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: '7px' } },
                recentTrades.slice(0, 8).map(function(t, i) {
                  var pnl = t.profit_abs || 0;
                  var pct = (t.profit_ratio || 0) * 100;
                  var isWin = pnl >= 0;
                  return React.createElement('div', {
                    key: i,
                    style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 10px', background: surfaceAlt, borderRadius: '8px', border: '1px solid ' + (isWin ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)') }
                  },
                    React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
                      React.createElement('div', { style: { width: '6px', height: '6px', borderRadius: '50%', background: isWin ? green : red, flexShrink: 0 } }),
                      React.createElement('div', null,
                        React.createElement('span', { style: { fontSize: '13px', fontWeight: 600, color: '#F8FAFC' } }, t.pair ? t.pair.split('/')[0] : '—'),
                        React.createElement('span', { style: { fontSize: '11px', color: muted, marginLeft: '6px' } }, exitLabel(t.exit_reason))
                      )
                    ),
                    React.createElement('div', { style: { textAlign: 'right' } },
                      React.createElement('span', { style: { fontSize: '13px', fontWeight: 600, color: isWin ? green : red } }, fmtUsdt(pnl)),
                      React.createElement('span', { style: { fontSize: '11px', color: muted, marginLeft: '6px' } }, '(' + fmtPct(pct) + ')')
                    )
                  );
                })
              )
        ),
        // Top pairs
        React.createElement('div', { style: card },
          React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } }, 'Top Pairs by P&L'),
          topPairs.length === 0
            ? React.createElement('p', { style: Object.assign({}, subText, { textAlign: 'center', padding: '1.5rem 0' }) }, 'No performance data yet')
            : React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px' } },
                topPairs.map(function(p, i) {
                  var maxAbs = Math.max.apply(null, topPairs.map(function(x) { return Math.abs(x.profit_abs || 0); }).concat([1]));
                  var barW = Math.abs(p.profit_abs || 0) / maxAbs * 100;
                  var isPos = (p.profit_abs || 0) >= 0;
                  return React.createElement('div', { key: i },
                    React.createElement('div', { style: { display: 'flex', justifyContent: 'space-between', marginBottom: '4px' } },
                      React.createElement('span', { style: { fontSize: '13px', fontWeight: 600, color: '#E2E8F0' } }, p.pair ? p.pair.split('/')[0] : p.pair),
                      React.createElement('span', { style: { fontSize: '12px', color: isPos ? green : red, fontWeight: 600 } },
                        fmtUsdt(p.profit_abs) + ' · ' + (p.count || 0) + ' trades')
                    ),
                    React.createElement('div', { style: { height: '5px', background: 'rgba(148,163,184,0.1)', borderRadius: '3px', overflow: 'hidden' } },
                      React.createElement('div', { style: { width: barW + '%', height: '100%', background: isPos ? 'linear-gradient(90deg,#10B981,#059669)' : 'linear-gradient(90deg,#EF4444,#B91C1C)', borderRadius: '3px' } })
                    )
                  );
                })
              )
        ),
        // Config summary (full width)
        React.createElement('div', { style: Object.assign({}, card, { gridColumn: '1 / -1' }) },
          React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } }, 'Bot Configuration'),
          React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(155px, 1fr))', gap: '10px' } },
            [
              { label: 'Strategy', value: (botStatus && botStatus.strategy) || 'ShortScalper' },
              { label: 'Mode', value: botStatus ? (botStatus.dry_run ? 'Dry Run' : 'Live') : '—' },
              { label: 'Exchange', value: (botStatus && botStatus.exchange) || 'Bybit' },
              { label: 'Timeframe', value: (botStatus && botStatus.timeframe) || '5m' },
              { label: 'Max Open Trades', value: (botStatus && botStatus.max_open_trades) || 3 },
              { label: 'Stake Amount', value: botStatus && botStatus.stake_amount ? (botStatus.stake_amount + ' USDT') : '200 USDT' },
              { label: 'State', value: (botStatus && botStatus.state) || '—' },
              { label: 'Trading Mode', value: (botStatus && botStatus.trading_mode) || 'futures' },
            ].map(function(item) {
              return React.createElement('div', {
                key: item.label,
                style: { background: 'rgba(51,65,85,0.4)', borderRadius: '8px', padding: '10px 12px' }
              },
                React.createElement('p', { style: Object.assign({}, labelStyle, { marginBottom: '4px' }) }, item.label),
                React.createElement('p', { style: { margin: 0, fontSize: '13px', fontWeight: 600, color: '#E2E8F0' } }, String(item.value))
              );
            })
          )
        )
      ),

      // ── OPEN TRADES TAB ──
      activeTab === 'open' && React.createElement('div', { style: card },
        React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } }, 'Active Short Positions'),
        openTrades.length === 0
          ? React.createElement('p', { style: Object.assign({}, subText, { textAlign: 'center', padding: '2rem 0' }) }, 'No open positions right now')
          : React.createElement('div', { style: { overflowX: 'auto' } },
              React.createElement('table', { style: { width: '100%', borderCollapse: 'collapse', fontSize: '13px' } },
                React.createElement('thead', null,
                  React.createElement('tr', { style: { borderBottom: '1px solid rgba(148,163,184,0.1)' } },
                    th('Pair', 'left'), th('Direction'), th('Entry Rate'), th('Current Rate'), th('Stake'), th('Unrealised P&L'), th('Holding'), th('Stop Loss')
                  )
                ),
                React.createElement('tbody', null,
                  openTrades.map(function(t, i) {
                    var pnl = t.profit_abs || 0;
                    var pct = (t.profit_ratio || 0) * 100;
                    var isWin = pnl >= 0;
                    return React.createElement('tr', { key: i, style: { borderBottom: '1px solid rgba(148,163,184,0.06)' } },
                      React.createElement('td', { style: { padding: '12px 10px' } },
                        React.createElement('span', { style: { fontWeight: 700, color: '#F8FAFC' } }, t.pair),
                        React.createElement('span', { style: { display: 'block', fontSize: '11px', color: muted } }, '#' + t.trade_id)
                      ),
                      React.createElement('td', { style: { padding: '12px 10px', textAlign: 'right' } },
                        React.createElement('span', { style: { background: 'rgba(239,68,68,0.15)', color: '#F87171', fontSize: '11px', fontWeight: 700, padding: '3px 8px', borderRadius: '5px' } }, 'SHORT')
                      ),
                      td(fmt(t.open_rate, 4)),
                      td(fmt(t.current_rate, 4)),
                      td(fmt(t.stake_amount, 2) + ' USDT'),
                      React.createElement('td', { style: { textAlign: 'right', padding: '12px 10px' } },
                        React.createElement('span', { style: { fontWeight: 600, color: isWin ? green : red, display: 'block' } }, fmtUsdt(pnl)),
                        React.createElement('span', { style: { fontSize: '11px', color: isWin ? green : red } }, fmtPct(pct))
                      ),
                      td(holdDuration(t.open_date)),
                      td(t.stop_loss_abs ? fmt(t.stop_loss_abs, 4) : '—', { style: { textAlign: 'right', padding: '12px 10px', color: '#FB923C', fontSize: '12px' } })
                    );
                  })
                )
              )
            )
      ),

      // ── CLOSED TRADES TAB ──
      activeTab === 'closed' && React.createElement('div', { style: card },
        React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } },
          'Last ' + recentTrades.length + ' Closed Trades'),
        recentTrades.length === 0
          ? React.createElement('p', { style: Object.assign({}, subText, { textAlign: 'center', padding: '2rem 0' }) }, 'No closed trades yet')
          : React.createElement('div', { style: { overflowX: 'auto' } },
              React.createElement('table', { style: { width: '100%', borderCollapse: 'collapse', fontSize: '13px' } },
                React.createElement('thead', null,
                  React.createElement('tr', { style: { borderBottom: '1px solid rgba(148,163,184,0.1)' } },
                    th('#', 'left'), th('Pair', 'left'), th('Entry'), th('Exit'), th('P&L (USDT)'), th('P&L %'), th('Exit Reason'), th('Duration')
                  )
                ),
                React.createElement('tbody', null,
                  recentTrades.map(function(t, i) {
                    var pnl = t.profit_abs || 0;
                    var pct = (t.profit_ratio || 0) * 100;
                    var isWin = pnl >= 0;
                    return React.createElement('tr', { key: i, style: { borderBottom: '1px solid rgba(148,163,184,0.05)', background: i % 2 === 0 ? 'transparent' : 'rgba(51,65,85,0.2)' } },
                      React.createElement('td', { style: { padding: '10px 10px', color: muted, fontSize: '11px' } }, '#' + t.trade_id),
                      React.createElement('td', { style: { padding: '10px 10px', fontWeight: 600, color: '#F8FAFC' } }, t.pair),
                      td(fmt(t.open_rate, 4)),
                      td(fmt(t.close_rate, 4)),
                      React.createElement('td', { style: { textAlign: 'right', padding: '10px 10px', fontWeight: 600, color: isWin ? green : red } }, fmtUsdt(pnl)),
                      React.createElement('td', { style: { textAlign: 'right', padding: '10px 10px', fontWeight: 600, color: isWin ? green : red } }, fmtPct(pct)),
                      React.createElement('td', { style: { textAlign: 'right', padding: '10px 10px' } },
                        React.createElement('span', {
                          style: { background: t.exit_reason === 'stoploss' ? 'rgba(239,68,68,0.12)' : 'rgba(148,163,184,0.1)', color: t.exit_reason === 'stoploss' ? '#F87171' : muted, fontSize: '11px', padding: '3px 8px', borderRadius: '5px', fontWeight: 500 }
                        }, exitLabel(t.exit_reason))
                      ),
                      td(t.trade_duration_s ? (Math.floor(t.trade_duration_s / 60) + 'm') : '—')
                    );
                  })
                )
              )
            )
      ),

      // ── PAIRS TAB ──
      activeTab === 'pairs' && React.createElement('div', { style: card },
        React.createElement('h3', { style: { margin: '0 0 1rem 0', fontSize: '14px', fontWeight: 600, color: '#E2E8F0' } }, 'Performance by Pair'),
        performance.length === 0
          ? React.createElement('p', { style: Object.assign({}, subText, { textAlign: 'center', padding: '2rem 0' }) }, 'No performance data yet')
          : React.createElement('div', { style: { overflowX: 'auto' } },
              React.createElement('table', { style: { width: '100%', borderCollapse: 'collapse', fontSize: '13px' } },
                React.createElement('thead', null,
                  React.createElement('tr', { style: { borderBottom: '1px solid rgba(148,163,184,0.1)' } },
                    th('Pair', 'left'), th('Trades'), th('Win Rate'), th('Total P&L'), th('Avg P&L / trade'), th('Avg %')
                  )
                ),
                React.createElement('tbody', null,
                  performance.slice().sort(function(a, b) { return (b.profit_abs || 0) - (a.profit_abs || 0); }).map(function(p, i) {
                    var wr = p.count > 0 ? ((p.wins || 0) / p.count * 100).toFixed(1) : null;
                    var isPos = (p.profit_abs || 0) >= 0;
                    return React.createElement('tr', { key: i, style: { borderBottom: '1px solid rgba(148,163,184,0.05)', background: i % 2 === 0 ? 'transparent' : 'rgba(51,65,85,0.2)' } },
                      React.createElement('td', { style: { padding: '11px 10px', fontWeight: 700, color: '#F8FAFC' } }, p.pair),
                      td(p.count || 0),
                      React.createElement('td', { style: { textAlign: 'right', padding: '11px 10px' } },
                        wr != null && React.createElement('span', { style: { color: parseFloat(wr) >= 50 ? green : red, fontWeight: 600 } }, wr + '%')
                      ),
                      React.createElement('td', { style: { textAlign: 'right', padding: '11px 10px', fontWeight: 600, color: isPos ? green : red } }, fmtUsdt(p.profit_abs)),
                      React.createElement('td', { style: { textAlign: 'right', padding: '11px 10px', color: isPos ? green : red } },
                        p.count ? fmtUsdt((p.profit_abs || 0) / p.count) : '—'),
                      React.createElement('td', { style: { textAlign: 'right', padding: '11px 10px', color: isPos ? green : red } },
                        p.profit_mean != null ? fmtPct(p.profit_mean * 100) : '—')
                    );
                  })
                )
              )
            )
      ),

      // ── Footer ──
      React.createElement('p', {
        style: { textAlign: 'center', marginTop: '2rem', fontSize: '11px', color: 'rgba(148,163,184,0.35)' }
      }, 'KotipotiBot Dashboard · auto-refreshes every 30s · read-only view')
    )
  );
}
