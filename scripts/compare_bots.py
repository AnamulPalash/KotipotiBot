#!/usr/bin/env python3
"""
Compare two dry-run bot SQLite databases.

Usage:
  python scripts/compare_bots.py --claude-db /path/claude.db --codex-db /path/codex.db

The reader supports the custom Kotipoti schema and a Freqtrade trades table.
It prints a markdown report to stdout, or to --output when provided.
"""

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _connect(path: Path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value)
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _session(dt):
    if dt is None:
        return "unknown"
    h = dt.astimezone(timezone.utc).hour
    if 0 <= h < 8:
        return "Asia"
    if 8 <= h < 13:
        return "London"
    return "US"


def _load_custom(conn, label):
    cols = _columns(conn, "trades")
    select_cols = [
        "id", "pair", "side", "entry_time", "exit_time", "entry_price",
        "exit_price", "stake_usdt", "leverage", "profit_usdt", "profit_pct",
        "entry_tag", "exit_reason", "session",
    ]
    optional = [
        "gross_profit_usdt", "estimated_fee_usdt", "estimated_slippage_usdt",
        "bot_variant",
    ]
    select_cols.extend([c for c in optional if c in cols])
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM trades WHERE is_open=0"
    ).fetchall()
    trades = []
    for row in rows:
        d = dict(row)
        entry_dt = _parse_dt(d.get("entry_time"))
        exit_dt = _parse_dt(d.get("exit_time"))
        trades.append({
            "source": label,
            "id": d.get("id"),
            "pair": d.get("pair") or "unknown",
            "side": d.get("side") or "unknown",
            "entry_time": entry_dt,
            "exit_time": exit_dt,
            "profit_usdt": float(d.get("profit_usdt") or 0),
            "gross_profit_usdt": float(d.get("gross_profit_usdt") or d.get("profit_usdt") or 0),
            "estimated_fee_usdt": float(d.get("estimated_fee_usdt") or 0),
            "estimated_slippage_usdt": float(d.get("estimated_slippage_usdt") or 0),
            "profit_pct": float(d.get("profit_pct") or 0),
            "entry_tag": d.get("entry_tag") or "unknown",
            "exit_reason": d.get("exit_reason") or "unknown",
            "session": d.get("session") or _session(entry_dt),
            "bot_variant": d.get("bot_variant") or label,
        })
    return trades


def _load_freqtrade(conn, label):
    cols = _columns(conn, "trades")
    wanted = [
        "id", "pair", "is_short", "open_date", "close_date", "profit_abs",
        "profit_ratio", "exit_reason", "enter_tag",
    ]
    select_cols = [c for c in wanted if c in cols]
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM trades WHERE is_open=0"
    ).fetchall()
    trades = []
    for row in rows:
        d = dict(row)
        entry_dt = _parse_dt(d.get("open_date"))
        exit_dt = _parse_dt(d.get("close_date"))
        ratio = float(d.get("profit_ratio") or 0)
        trades.append({
            "source": label,
            "id": d.get("id"),
            "pair": d.get("pair") or "unknown",
            "side": "short" if d.get("is_short") else "long",
            "entry_time": entry_dt,
            "exit_time": exit_dt,
            "profit_usdt": float(d.get("profit_abs") or 0),
            "gross_profit_usdt": float(d.get("profit_abs") or 0),
            "estimated_fee_usdt": 0.0,
            "estimated_slippage_usdt": 0.0,
            "profit_pct": ratio * 100,
            "entry_tag": d.get("enter_tag") or "unknown",
            "exit_reason": d.get("exit_reason") or "unknown",
            "session": _session(entry_dt),
            "bot_variant": label,
        })
    return trades


def load_trades(path, label):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    with _connect(path) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "trades" not in tables:
            raise ValueError(f"{path} has no trades table")
        cols = _columns(conn, "trades")
        if "profit_usdt" in cols and "entry_time" in cols:
            return _load_custom(conn, label)
        if "profit_abs" in cols and "open_date" in cols:
            return _load_freqtrade(conn, label)
    raise ValueError(f"Unsupported trades schema in {path}")


def _stats(trades):
    profits = [t["profit_usdt"] for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = abs(sum(p for p in profits if p < 0))
    equity = []
    cumulative = 0.0
    max_equity = 0.0
    max_drawdown = 0.0
    for t in sorted(trades, key=lambda x: x["exit_time"] or datetime.min.replace(tzinfo=timezone.utc)):
        cumulative += t["profit_usdt"]
        max_equity = max(max_equity, cumulative)
        max_drawdown = min(max_drawdown, cumulative - max_equity)
        equity.append(cumulative)
    return {
        "count": len(trades),
        "net": sum(profits),
        "gross": sum(t["gross_profit_usdt"] for t in trades),
        "fees": sum(t["estimated_fee_usdt"] for t in trades),
        "slippage": sum(t["estimated_slippage_usdt"] for t in trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
        "avg": (sum(profits) / len(trades)) if trades else 0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else math.inf if gross_profit else 0,
        "max_drawdown": max_drawdown,
    }


def _group_stats(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[t.get(key) or "unknown"].append(t)
    return sorted(
        ((k, _stats(v)) for k, v in groups.items()),
        key=lambda item: item[1]["net"],
        reverse=True,
    )


def _fmt_money(value):
    return f"{value:+.2f}"


def _fmt_pf(value):
    return "inf" if value == math.inf else f"{value:.2f}"


def _summary_table(rows):
    out = [
        "| Bot | Trades | Net USDT | Gross USDT | Fees | Slippage | Win Rate | Avg | Profit Factor | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, stats in rows:
        out.append(
            f"| {label} | {stats['count']} | {_fmt_money(stats['net'])} | "
            f"{_fmt_money(stats['gross'])} | {stats['fees']:.2f} | "
            f"{stats['slippage']:.2f} | {stats['win_rate']:.1f}% | "
            f"{_fmt_money(stats['avg'])} | {_fmt_pf(stats['profit_factor'])} | "
            f"{_fmt_money(stats['max_drawdown'])} |"
        )
    return "\n".join(out)


def _top_group_table(title, label, trades, key):
    lines = [f"### {title}: {label}", "", "| Group | Trades | Net USDT | Win Rate | Avg |", "|---|---:|---:|---:|---:|"]
    for group, stats in _group_stats(trades, key)[:8]:
        lines.append(
            f"| {group} | {stats['count']} | {_fmt_money(stats['net'])} | "
            f"{stats['win_rate']:.1f}% | {_fmt_money(stats['avg'])} |"
        )
    return "\n".join(lines)


def _overlap_key(trade):
    dt = trade["entry_time"]
    if dt is None:
        bucket = "unknown"
    else:
        minute = dt.minute - (dt.minute % 5)
        bucket = dt.replace(minute=minute, second=0, microsecond=0).isoformat()
    return (trade["pair"], trade["side"], bucket)


def build_report(claude_trades, codex_trades):
    claude_stats = _stats(claude_trades)
    codex_stats = _stats(codex_trades)
    claude_keys = Counter(_overlap_key(t) for t in claude_trades)
    codex_keys = Counter(_overlap_key(t) for t in codex_trades)
    overlap = sum((claude_keys & codex_keys).values())
    only_claude = len(claude_trades) - overlap
    only_codex = len(codex_trades) - overlap

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Kotipoti Bot Comparison ({now})",
        "",
        _summary_table([("Claude", claude_stats), ("Codex", codex_stats)]),
        "",
        "## Trade Overlap",
        "",
        f"- Approx overlapping trades: {overlap}",
        f"- Claude-only trades: {only_claude}",
        f"- Codex-only trades: {only_codex}",
        "",
        _top_group_table("Pair Performance", "Claude", claude_trades, "pair"),
        "",
        _top_group_table("Pair Performance", "Codex", codex_trades, "pair"),
        "",
        _top_group_table("Session Performance", "Claude", claude_trades, "session"),
        "",
        _top_group_table("Session Performance", "Codex", codex_trades, "session"),
        "",
        _top_group_table("Exit Reason Performance", "Claude", claude_trades, "exit_reason"),
        "",
        _top_group_table("Exit Reason Performance", "Codex", codex_trades, "exit_reason"),
        "",
        "## Notes",
        "",
        "- Overlap uses pair + side + 5-minute entry bucket, so it is approximate.",
        "- Codex net PnL includes estimated fees/slippage when those columns exist.",
        "- Freqtrade dry-run DBs may already include fee assumptions depending on config.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-db", required=True)
    parser.add_argument("--codex-db", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    claude = load_trades(args.claude_db, "claude")
    codex = load_trades(args.codex_db, "codex")
    report = build_report(claude, codex)
    if args.output:
        Path(args.output).write_text(report)
    else:
        print(report)


if __name__ == "__main__":
    main()
