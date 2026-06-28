#!/usr/bin/env python3.9
"""Claude CLI Token Usage Dashboard — reads ~/.claude/stats-cache.json"""

import json
import queue
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

STATS_FILE    = Path.home() / ".claude" / "stats-cache.json"
PLAN_FILE     = Path.home() / ".claude" / "plan-limits.json"
PROJECTS_DIR  = Path.home() / ".claude" / "projects"
WATCH_INTERVAL = 60  # seconds

MODEL_ALIASES = {
    "claude-opus-4-6":            "Opus 4.6",
    "claude-opus-4-5-20251101":   "Opus 4.5",
    "claude-sonnet-4-6":          "Sonnet 4.6",
    "claude-sonnet-4-5-20250929": "Sonnet 4.5",
    "claude-haiku-4-5-20251001":  "Haiku 4.5",
}

MODEL_COLORS = {
    "Opus 4.6":   "bright_magenta",
    "Opus 4.5":   "magenta",
    "Sonnet 4.6": "bright_cyan",
    "Sonnet 4.5": "cyan",
    "Haiku 4.5":  "bright_green",
}

BAR_CHARS = " ▁▂▃▄▅▆▇█"


# ─── Formatters ───────────────────────────────────────────────────────────────

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_num(n: int) -> str:
    return f"{n:,}"


def trunc_display(text: str, max_width: int) -> str:
    """Truncate to max_width display columns (CJK = 2 cols each)."""
    result, width = [], 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if width + cw > max_width - 1:
            result.append("…")
            break
        result.append(ch)
        width += cw
    return "".join(result)


def spark(value: int, max_value: int, width: int = 14, color: str = "cyan") -> Text:
    if max_value == 0:
        return Text(" " * width)
    filled = int(value / max_value * width * 8)
    full, rem = filled // 8, filled % 8
    s = "█" * full
    if rem and full < width:
        s += BAR_CHARS[rem]
    return Text(s.ljust(width), style=color)


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_stats() -> dict:
    with open(STATS_FILE) as f:
        return json.load(f)


def decode_project_name(dir_name: str) -> str:
    parts = dir_name.split("-")
    return parts[-1] if parts else dir_name


def load_sessions(n: int = 30) -> list:
    results = []
    for jsonl_path in PROJECTS_DIR.glob("*/*.jsonl"):
        project   = decode_project_name(jsonl_path.parent.name)
        title     = None
        date_str  = None
        model_tokens: dict = {}

        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    t = entry.get("type", "")
                    if t == "custom-title":
                        title = entry.get("title") or title
                    elif t == "agent-name" and not title:
                        title = entry.get("agentName") or title
                    elif t == "user":
                        if date_str is None:
                            ts = entry.get("timestamp")
                            if ts:
                                date_str = ts[:10]
                        if not title:
                            msg     = entry.get("message", {})
                            content = msg.get("content", "") if isinstance(msg, dict) else ""
                            if isinstance(content, str) and content.strip():
                                title = content.strip()[:80]
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        t2 = part.get("text", "").strip()
                                        if t2:
                                            title = t2[:80]
                                            break
                    elif t == "assistant":
                        msg   = entry.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        model = msg.get("model", "unknown")
                        usage = msg.get("usage", {})
                        if not usage:
                            continue
                        alias = MODEL_ALIASES.get(model, model)
                        if alias not in model_tokens:
                            model_tokens[alias] = {"input": 0, "output": 0, "cache_r": 0, "cache_w": 0}
                        model_tokens[alias]["input"]   += usage.get("input_tokens", 0)
                        model_tokens[alias]["output"]  += usage.get("output_tokens", 0)
                        model_tokens[alias]["cache_r"] += usage.get("cache_read_input_tokens", 0)
                        model_tokens[alias]["cache_w"] += usage.get("cache_creation_input_tokens", 0)
        except (OSError, UnicodeDecodeError):
            continue

        if not model_tokens:
            continue

        total = sum(
            v["input"] + v["output"] + v["cache_r"] + v["cache_w"]
            for v in model_tokens.values()
        )
        primary = max(model_tokens, key=lambda m: sum(model_tokens[m].values()))
        results.append({
            "date":         date_str or "?",
            "project":      project,
            "title":        title or "",
            "model":        primary,
            "model_tokens": model_tokens,
            "total":        total,
            "mtime":        jsonl_path.stat().st_mtime,
        })

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results[:n]


def rebuild_cache():
    """JSONL 파일에서 새 데이터만 읽어 stats-cache.json에 증분 병합.

    기존 stats-cache.json의 누적 집계(modelUsage, hourCounts, totalSessions 등)를
    보존하고, lastComputedDate 이후의 날짜만 추가/갱신한다.
    JSONL 파일이 없는 과거 구간은 건드리지 않는다.
    """
    console = Console()

    # ── 기존 캐시 로드 ──────────────────────────────────────────────────────
    try:
        with open(STATS_FILE) as f:
            cache = json.load(f)
    except Exception:
        cache = {"version": 3}

    last_computed = cache.get("lastComputedDate", "1970-01-01")
    console.print(f"[yellow]증분 재계산 중 — {last_computed} 이후 JSONL 파일 분석...[/]")

    # ── 기존 dailyActivity / dailyModelTokens 를 dict로 변환 ────────────────
    existing_daily: dict = {
        e["date"]: {
            "msgCount":   e.get("messageCount", 0),
            "sessionIds": set(),          # 집합은 복원 불가 → 0으로 유지
            "toolCount":  e.get("toolCallCount", 0),
            "sessionCount": e.get("sessionCount", 0),
        }
        for e in cache.get("dailyActivity", [])
    }
    existing_dmt: dict = {
        e["date"]: dict(e.get("tokensByModel", {}))
        for e in cache.get("dailyModelTokens", [])
    }
    existing_mu: dict = {
        k: dict(v) for k, v in cache.get("modelUsage", {}).items()
    }
    existing_hc: dict = {str(h): int(cache.get("hourCounts", {}).get(str(h), 0))
                         for h in range(24)}
    existing_total_sessions = cache.get("totalSessions", 0)
    existing_total_messages = cache.get("totalMessages", 0)

    # ── JSONL 에서 새 데이터 집계 ────────────────────────────────────────────
    new_daily: dict = {}          # date -> {msgCount, sessionIds, toolCount, modelTokens}
    new_mu: dict    = {}
    new_hc          = {str(h): 0 for h in range(24)}
    new_session_ids: set = set()
    new_messages    = 0

    for jsonl_path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        current_date = None
        hour_recorded = False

        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    t = entry.get("type", "")

                    if t == "user":
                        ts = entry.get("timestamp", "")
                        if not ts:
                            continue
                        current_date = ts[:10]
                        if current_date <= last_computed:
                            continue          # 이미 집계된 날짜는 건너뜀

                        d = new_daily.setdefault(current_date, {
                            "msgCount": 0, "sessionIds": set(),
                            "toolCount": 0, "modelTokens": {},
                        })
                        d["msgCount"] += 1
                        new_messages  += 1

                        if not hour_recorded:
                            new_hc[str(int(ts[11:13]))] += 1
                            hour_recorded = True

                        sid = entry.get("sessionId", "")
                        if sid:
                            new_session_ids.add(sid)
                            d["sessionIds"].add(sid)

                    elif t == "assistant":
                        if current_date is None or current_date <= last_computed:
                            continue
                        msg = entry.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        model = msg.get("model", "")
                        usage = msg.get("usage", {})
                        if not usage or not model:
                            continue

                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cr  = usage.get("cache_read_input_tokens", 0)
                        cw  = usage.get("cache_creation_input_tokens", 0)

                        mu = new_mu.setdefault(model, {
                            "inputTokens": 0, "outputTokens": 0,
                            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                        })
                        mu["inputTokens"]              += inp
                        mu["outputTokens"]             += out
                        mu["cacheReadInputTokens"]     += cr
                        mu["cacheCreationInputTokens"] += cw

                        tok_map = new_daily[current_date]["modelTokens"]
                        tok_map[model] = tok_map.get(model, 0) + inp + out + cr + cw

                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "tool_use":
                                    new_daily[current_date]["toolCount"] += 1

        except (OSError, UnicodeDecodeError):
            continue

    if not new_daily:
        console.print("[green]✓ 새 데이터 없음 — 이미 최신 상태입니다.[/]")
        console.print(f"  lastComputedDate: {last_computed}")
        return

    # ── 기존 데이터에 증분 병합 ──────────────────────────────────────────────
    for date, nd in new_daily.items():
        existing_daily[date] = {
            "msgCount":     nd["msgCount"],
            "sessionIds":   nd["sessionIds"],
            "toolCount":    nd["toolCount"],
            "sessionCount": len(nd["sessionIds"]),
        }
        if nd["modelTokens"]:
            existing_dmt[date] = nd["modelTokens"]

    for model, delta in new_mu.items():
        em = existing_mu.setdefault(model, {
            "inputTokens": 0, "outputTokens": 0,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        })
        for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
            em[k] = em.get(k, 0) + delta.get(k, 0)

    for h in range(24):
        existing_hc[str(h)] += new_hc[str(h)]

    new_total_sessions = existing_total_sessions + len(new_session_ids)
    new_total_messages = existing_total_messages + new_messages

    sorted_dates = sorted(existing_daily.keys())
    daily_activity_list = [
        {
            "date":         d,
            "messageCount": existing_daily[d]["msgCount"],
            "sessionCount": existing_daily[d].get("sessionCount", 0),
            "toolCallCount": existing_daily[d]["toolCount"],
        }
        for d in sorted_dates
    ]
    daily_model_tokens_list = [
        {"date": d, "tokensByModel": existing_dmt[d]}
        for d in sorted_dates
        if d in existing_dmt and existing_dmt[d]
    ]

    # ── 저장 ────────────────────────────────────────────────────────────────
    cache["lastComputedDate"]  = time.strftime("%Y-%m-%d")
    cache["firstSessionDate"]  = sorted_dates[0] if sorted_dates else cache.get("firstSessionDate", "")
    cache["totalSessions"]     = new_total_sessions
    cache["totalMessages"]     = new_total_messages
    cache["dailyActivity"]     = daily_activity_list
    cache["dailyModelTokens"]  = daily_model_tokens_list
    cache["hourCounts"]        = existing_hc
    cache["modelUsage"]        = existing_mu

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    new_dates = sorted(new_daily.keys())
    console.print(f"[green]✓ 증분 재계산 완료[/]")
    console.print(f"  추가된 날짜: {new_dates[0]} → {new_dates[-1]} ({len(new_dates)}일)")
    console.print(f"  신규 세션: +{len(new_session_ids):,}  신규 메시지: +{new_messages:,}")
    console.print(f"  누적 세션: {new_total_sessions:,}  누적 메시지: {new_total_messages:,}")
    console.print(f"  lastComputedDate: {cache['lastComputedDate']}")


# ─── Panel builders ───────────────────────────────────────────────────────────

def make_summary(data: dict) -> Panel:
    mu = data.get("modelUsage", {})
    total_in  = sum(v.get("inputTokens", 0) for v in mu.values())
    total_out = sum(v.get("outputTokens", 0) for v in mu.values())
    cache_r   = sum(v.get("cacheReadInputTokens", 0) for v in mu.values())
    cache_w   = sum(v.get("cacheCreationInputTokens", 0) for v in mu.values())
    total_tok = total_in + total_out + cache_r + cache_w

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim",  justify="right")
    t.add_column(style="bold", justify="left")
    t.add_column(style="dim",  justify="right")
    t.add_column(style="bold", justify="left")

    t.add_row("Sessions", f"[bright_white]{fmt_num(data.get('totalSessions', 0))}[/]",
              "Input",    f"[green]{fmt_tokens(total_in)}[/]")
    t.add_row("Messages", f"[bright_white]{fmt_num(data.get('totalMessages', 0))}[/]",
              "Output",   f"[yellow]{fmt_tokens(total_out)}[/]")
    t.add_row("", "",     "Cache R",  f"[blue]{fmt_tokens(cache_r)}[/]")
    t.add_row("Since",    f"[dim]{data.get('firstSessionDate','?')}[/]",
              "Cache W",  f"[cyan]{fmt_tokens(cache_w)}[/]")
    t.add_row("Updated",  f"[dim]{data.get('lastComputedDate','?')}[/]",
              "Total",    f"[bold bright_white]{fmt_tokens(total_tok)}[/]")
    return Panel(t, title="[bold yellow]⚡ Summary[/]", border_style="yellow")


def make_model_table(data: dict) -> Panel:
    mu = data.get("modelUsage", {})
    sorted_models = sorted(
        mu.items(),
        key=lambda x: x[1].get("inputTokens",0)+x[1].get("outputTokens",0)
                     +x[1].get("cacheReadInputTokens",0)+x[1].get("cacheCreationInputTokens",0),
        reverse=True,
    )
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True)
    tbl.add_column("Model",   style="bold",      min_width=12)
    tbl.add_column("Input",   justify="right",   style="green",      min_width=8)
    tbl.add_column("Output",  justify="right",   style="yellow",     min_width=8)
    tbl.add_column("Cache R", justify="right",   style="blue",       min_width=8)
    tbl.add_column("Cache W", justify="right",   style="cyan",       min_width=8)
    tbl.add_column("Total",   justify="right",   style="bold white", min_width=8)

    for model_id, stats in sorted_models:
        alias = MODEL_ALIASES.get(model_id, model_id)
        color = MODEL_COLORS.get(alias, "white")
        inp = stats.get("inputTokens", 0);     out = stats.get("outputTokens", 0)
        cr  = stats.get("cacheReadInputTokens", 0); cw = stats.get("cacheCreationInputTokens", 0)
        tbl.add_row(f"[{color}]{alias}[/]", fmt_tokens(inp), fmt_tokens(out),
                    fmt_tokens(cr), fmt_tokens(cw), fmt_tokens(inp+out+cr+cw))
    return Panel(tbl, title="[bold]② Model Breakdown[/]", border_style="dim")


def make_daily(data: dict) -> Panel:
    daily  = data.get("dailyActivity", [])
    recent = daily[-14:]
    max_msgs = max((d.get("messageCount", 0) for d in recent), default=1)

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(width=11, justify="right", style="dim")
    tbl.add_column(width=16)
    tbl.add_column(width=8,  justify="right", style="bright_white")
    tbl.add_column(width=4,  justify="right", style="dim")
    tbl.add_column(width=9,  justify="right", style="dim")

    for entry in recent:
        msgs = entry.get("messageCount", 0)
        tbl.add_row(
            entry.get("date", ""),
            spark(msgs, max_msgs, 14, "bright_cyan" if msgs == max_msgs else "cyan"),
            fmt_num(msgs),
            f"{entry.get('sessionCount',0)}s",
            fmt_num(entry.get("toolCallCount", 0)),
        )
    return Panel(tbl,
        title="[bold]③ Daily Activity[/] [dim]msgs · sessions · tools (14d)[/]",
        border_style="dim")



def make_hourly(data: dict) -> Panel:
    hc = data.get("hourCounts", {})
    counts = [int(hc.get(str(h), 0)) for h in range(24)] if isinstance(hc, dict) \
             else list(hc) + [0]*(24-len(hc))
    max_h = max(counts) if counts else 1
    peak  = counts.index(max(counts)) if counts else 0

    BAR_WIDTH = 18
    # 2열 (00-11 / 12-23) 배치로 높이 절반
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=2, justify="right")   # hour L
    grid.add_column(width=BAR_WIDTH)             # bar L
    grid.add_column(width=3, justify="right")    # count L
    grid.add_column(width=1)                     # spacer
    grid.add_column(width=2, justify="right")    # hour R
    grid.add_column(width=BAR_WIDTH)             # bar R
    grid.add_column(width=3, justify="right")    # count R

    for row in range(12):
        cells = []
        for h in (row, row + 12):
            c = counts[h]
            ratio = c / max_h if max_h else 0
            filled = int(ratio * BAR_WIDTH)
            style = (
                "bold bright_yellow" if c == max_h and c > 0 else
                "bright_cyan" if ratio > 0.6 else
                "cyan" if ratio > 0.3 else "dim cyan"
            )
            label = Text(f"{h:02d}", style="bold" if c == max_h and c > 0 else "dim")
            bar_text = Text("█" * filled + "░" * (BAR_WIDTH - filled), style=style)
            count_text = Text(str(c), style=style) if c > 0 else Text("·", style="dim")
            cells.extend([label, bar_text, count_text])
        grid.add_row(cells[0], cells[1], cells[2], Text("│", style="dim"),
                     cells[3], cells[4], cells[5])

    return Panel(grid,
        title=f"[bold]④ Hour Distribution[/] [dim]peak: {peak:02d}:00[/]",
        border_style="dim")


def make_session_table(sessions: list, n: int) -> Panel:
    grid = Table.grid(padding=(0, 0))
    grid.add_column()

    for i, s in enumerate(sessions, 1):
        mt      = s["model_tokens"]
        primary = s["model"]
        color   = MODEL_COLORS.get(primary, "white")
        inp  = sum(v["input"]   for v in mt.values())
        out  = sum(v["output"]  for v in mt.values())
        cr   = sum(v["cache_r"] for v in mt.values())
        cw   = sum(v["cache_w"] for v in mt.values())
        total = inp + out + cr + cw
        model_label = primary + (f" +{len(mt)-1}" if len(mt) > 1 else "")
        title = trunc_display((s["title"] or "").split("\n")[0].strip(), 50)

        line1 = Text()
        line1.append(f"{i:>2} ", style="dim")
        line1.append(s["date"] + "  ", style="dim")
        line1.append(f"{model_label:<13}", style=f"bold {color}")
        line1.append(f"{fmt_tokens(inp):>7}", style="green")
        line1.append("/", style="dim")
        line1.append(f"{fmt_tokens(out):<7}", style="yellow")
        line1.append(" ¢", style="dim")
        line1.append(f"{fmt_tokens(cr+cw):>8}", style="blue")
        line1.append("  =", style="dim")
        line1.append(f"{fmt_tokens(total):>8}", style="bold white")

        line2 = Text()
        line2.append("     ", style="")
        line2.append(f"[{s['project']}] ", style="cyan")
        line2.append(title, style="dim")

        grid.add_row(line1)
        grid.add_row(line2)
        if i < len(sessions):
            grid.add_row(Text(""))

    return Panel(grid,
        title=f"[bold]Per-Session Token Usage[/] [dim](last {n}, recent first)[/]",
        border_style="yellow")


# ─── Help & briefing overlays ─────────────────────────────────────────────────

# HELP_KEYS is defined later near main(), before make_help_panel() is called.
def make_help_panel() -> Panel:
    return Panel(
        HELP_KEYS,
        title="[bold]Help[/]",
        border_style="yellow",
        padding=(1, 2),
    )


def make_brief_summary(data: dict) -> Panel:
    mu = data.get("modelUsage", {})
    total_in  = sum(v.get("inputTokens", 0) for v in mu.values())
    total_out = sum(v.get("outputTokens", 0) for v in mu.values())
    cache_r   = sum(v.get("cacheReadInputTokens", 0) for v in mu.values())
    cache_w   = sum(v.get("cacheCreationInputTokens", 0) for v in mu.values())
    total     = total_in + total_out + cache_r + cache_w

    sessions  = data.get("totalSessions", 0)
    messages  = data.get("totalMessages", 0)
    daily     = data.get("dailyActivity", [])

    # avg messages/day over last 14 days
    recent    = [d.get("messageCount", 0) for d in daily[-14:]]
    avg_day   = sum(recent) / len(recent) if recent else 0
    busiest   = max(daily, key=lambda d: d.get("messageCount", 0), default={})

    # cache hit rate (cache_r / total_in+cache_r)
    cache_hit = cache_r / (total_in + cache_r) * 100 if (total_in + cache_r) else 0

    txt = Text()
    txt.append(f"총 {fmt_num(sessions)}개 세션", style="bold white")
    txt.append(f"에서 {fmt_num(messages)}개 메시지를 주고받았습니다.\n\n")
    txt.append(f"최근 14일 평균 ", style="dim")
    txt.append(f"{avg_day:.0f}개/일", style="bright_white")
    txt.append(" 메시지 사용.\n")
    if busiest:
        txt.append(f"가장 바쁜 날: ", style="dim")
        txt.append(f"{busiest.get('date','')} ({fmt_num(busiest.get('messageCount',0))}개)\n\n")

    txt.append(f"누적 토큰: ", style="dim")
    txt.append(f"{fmt_tokens(total)}", style="bold bright_white")
    txt.append(f"  (캐시 히트율 {cache_hit:.1f}%)\n", style="dim")
    txt.append(f"캐시 덕분에 실제 과금 토큰은 훨씬 적습니다.", style="dim")

    return Panel(txt, title="[bold]① Summary — 현황 브리핑[/]", border_style="yellow", padding=(1, 2))


def make_brief_models(data: dict) -> Panel:
    mu = data.get("modelUsage", {})
    totals = {
        MODEL_ALIASES.get(k, k): (
            v.get("inputTokens", 0) + v.get("outputTokens", 0)
            + v.get("cacheReadInputTokens", 0) + v.get("cacheCreationInputTokens", 0)
        )
        for k, v in mu.items()
    }
    grand = sum(totals.values()) or 1
    sorted_m = sorted(totals.items(), key=lambda x: x[1], reverse=True)

    txt = Text()
    for alias, tok in sorted_m:
        pct   = tok / grand * 100
        color = MODEL_COLORS.get(alias, "white")
        bar   = "█" * int(pct / 5)
        txt.append(f"{alias:<12}", style=f"bold {color}")
        txt.append(f" {bar:<20}", style=color)
        txt.append(f" {pct:5.1f}%  {fmt_tokens(tok)}\n")

    # Model characteristics
    txt.append("\n[bold yellow]모델 특성[/]\n", style="")
    txt.append("Opus 4.6   ", style="bold bright_magenta")
    txt.append("가장 강력, 복잡한 코딩·분석에 최적\n", style="dim")
    txt.append("Sonnet 4.6 ", style="bold bright_cyan")
    txt.append("속도·품질 균형, 일반 개발 작업\n", style="dim")
    txt.append("Haiku 4.5  ", style="bold bright_green")
    txt.append("가장 빠름, 단순 작업·도구 호출\n", style="dim")

    return Panel(txt, title="[bold]② Model Breakdown — 현황 브리핑[/]", border_style="yellow", padding=(1, 2))


def make_brief_daily(data: dict) -> Panel:
    daily  = data.get("dailyActivity", [])
    recent = daily[-14:]
    if not recent:
        return Panel("데이터 없음", title="③ Daily", border_style="yellow")

    msgs   = [d.get("messageCount", 0) for d in recent]
    avg    = sum(msgs) / len(msgs)
    trend  = msgs[-1] - msgs[-7] if len(msgs) >= 7 else 0
    busiest = max(recent, key=lambda d: d.get("messageCount", 0))
    quietest = min(recent, key=lambda d: d.get("messageCount", 0))
    total_tools = sum(d.get("toolCallCount", 0) for d in recent)
    tool_ratio  = total_tools / sum(msgs) if sum(msgs) else 0

    txt = Text()
    txt.append(f"최근 14일 평균 ", style="dim")
    txt.append(f"{avg:.0f}개/일\n", style="bold white")
    trend_sym = "↑" if trend > 0 else "↓" if trend < 0 else "→"
    trend_col = "green" if trend > 0 else "red" if trend < 0 else "white"
    txt.append(f"7일 전 대비 트렌드: ", style="dim")
    txt.append(f"{trend_sym} {abs(trend):,}개\n\n", style=trend_col)
    txt.append(f"가장 바쁜 날  ", style="dim")
    txt.append(f"{busiest.get('date','')} — {fmt_num(busiest.get('messageCount',0))}개 메시지\n")
    txt.append(f"가장 조용한 날 ", style="dim")
    txt.append(f"{quietest.get('date','')} — {fmt_num(quietest.get('messageCount',0))}개 메시지\n\n")
    txt.append(f"도구 호출 비율: 메시지 1개당 평균 ", style="dim")
    txt.append(f"{tool_ratio:.1f}회", style="bright_white")
    txt.append(" 툴 사용\n", style="dim")

    return Panel(txt, title="[bold]③ Daily Activity — 현황 브리핑[/]", border_style="yellow", padding=(1, 2))


def make_brief_hourly(data: dict) -> Panel:
    hc     = data.get("hourCounts", {})
    counts = [int(hc.get(str(h), 0)) for h in range(24)] if isinstance(hc, dict) \
             else list(hc) + [0]*(24-len(hc))
    total  = sum(counts) or 1
    peak   = counts.index(max(counts))

    # time-of-day buckets
    dawn    = sum(counts[5:9])
    morning = sum(counts[9:12])
    afternoon = sum(counts[12:18])
    evening = sum(counts[18:23])
    night   = sum(counts[0:5]) + counts[23]

    buckets = [
        ("새벽 (00-05)", night,     "dim cyan"),
        ("오전 (05-09)", dawn,      "cyan"),
        ("낮  (09-12)", morning,   "bright_cyan"),
        ("오후 (12-18)", afternoon, "bright_yellow"),
        ("저녁 (18-23)", evening,   "yellow"),
    ]
    main_bucket = max(buckets, key=lambda x: x[1])

    txt = Text()
    txt.append(f"가장 많이 사용하는 시간: ", style="dim")
    txt.append(f"{peak:02d}:00\n", style="bold bright_yellow")
    txt.append(f"주요 활동 시간대: ", style="dim")
    txt.append(f"{main_bucket[0]}\n\n", style=f"bold {main_bucket[2]}")

    for name, cnt, color in buckets:
        pct = cnt / total * 100
        bar = "█" * int(pct / 3)
        txt.append(f"{name}  ", style="dim")
        txt.append(f"{bar:<20}", style=color)
        txt.append(f" {pct:4.1f}%\n")

    return Panel(txt, title="[bold]④ Hour Distribution — 현황 브리핑[/]", border_style="yellow", padding=(1, 2))


# ─── Keyboard input thread ────────────────────────────────────────────────────

def start_key_reader(key_queue: queue.Queue, stop_event: threading.Event):
    """Read single keypresses (cbreak mode — Ctrl+C still generates SIGINT)."""
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        # stdin이 터미널이 아닌 경우 (파이프, 백그라운드 등) — 키 리더 비활성
        return threading.Thread(target=lambda: None, daemon=True), None

    def _run():
        try:
            # setcbreak: 단일 키 즉시 읽기 + Ctrl+C SIGINT 유지 (setraw와 달리)
            tty.setcbreak(fd)
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    ch = sys.stdin.read(1)
                    key_queue.put(ch)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, old  # old 반환 → 메인에서도 복원 가능


# ─── Plan limits ──────────────────────────────────────────────────────────────

def _get_weekly_tokens(stats=None) -> dict:
    """현재 주간 토큰 사용량 계산 (모든 모델 합산 + Sonnet만)."""
    if stats is None:
        try:
            stats = load_stats()
        except Exception:
            return {"all": 0, "sonnet": 0}
    daily = stats.get("dailyModelTokens", [])
    total_all = 0
    total_sonnet = 0
    for day in daily[-7:]:
        by_model = day.get("tokensByModel", {})
        for model, tokens in by_model.items():
            total_all += tokens
            if "sonnet" in model.lower():
                total_sonnet += tokens
    return {"all": total_all, "sonnet": total_sonnet}


def load_plan_limits(auto_interpolate: bool = True) -> dict:
    if not PLAN_FILE.exists():
        return {}
    try:
        with open(PLAN_FILE) as f:
            limits = json.load(f)
    except Exception:
        return {}

    if not auto_interpolate:
        return limits

    # 스냅샷 토큰이 있으면 자동 보간
    snap = limits.get("_snapshot_tokens")
    if not snap:
        return limits

    try:
        current = _get_weekly_tokens()
    except Exception:
        return limits

    snap_all    = snap.get("all", 0)
    snap_sonnet = snap.get("sonnet", 0)
    cur_all     = current["all"]
    cur_sonnet  = current["sonnet"]

    # 보정 계수 계산 (보정 데이터가 있으면 추정 정확도 향상)
    calibration = limits.get("_calibration", [])
    adj_all = 1.0
    adj_son = 1.0
    if len(calibration) >= 2:
        # 최근 보정 데이터에서 추정 대비 실제 비율의 평균
        ratios_all = []
        ratios_son = []
        for c in calibration[-10:]:
            if c.get("est_all") and c.get("actual_all") and c["est_all"] > 0:
                ratios_all.append(c["actual_all"] / c["est_all"])
            if c.get("est_son") and c.get("actual_son") and c["est_son"] > 0:
                ratios_son.append(c["actual_son"] / c["est_son"])
        if ratios_all:
            adj_all = sum(ratios_all) / len(ratios_all)
        if ratios_son:
            adj_son = sum(ratios_son) / len(ratios_son)

    # weekly_all 보간
    wall = limits.get("weekly_all")
    if wall and snap_all > 0:
        base_pct = wall.get("_base_pct", wall.get("used_pct", 0))
        if base_pct > 0 and base_pct < 100:
            tokens_per_100 = snap_all / (base_pct / 100)
            delta_tokens = cur_all - snap_all
            delta_pct = (delta_tokens / tokens_per_100) * 100 if tokens_per_100 > 0 else 0
            raw_est = base_pct + delta_pct
            wall["used_pct"] = min(100, round(raw_est * adj_all))

    # weekly_sonnet 보간
    wson = limits.get("weekly_sonnet")
    if wson and snap_sonnet > 0:
        base_pct = wson.get("_base_pct", wson.get("used_pct", 0))
        if base_pct > 0 and base_pct < 100:
            tokens_per_100 = snap_sonnet / (base_pct / 100)
            delta_tokens = cur_sonnet - snap_sonnet
            delta_pct = (delta_tokens / tokens_per_100) * 100 if tokens_per_100 > 0 else 0
            raw_est = base_pct + delta_pct
            wson["used_pct"] = min(100, round(raw_est * adj_son))

    return limits


def _plan_stale_warning(limits: dict) -> str:
    """스냅샷 이후 토큰 변화량 기반으로 갱신 필요 여부 판단."""
    snap = limits.get("_snapshot_tokens")
    updated = limits.get("updated_at")
    if not snap or not updated:
        return ""
    try:
        from datetime import datetime
        age_h = (time.time() - datetime.fromisoformat(updated).timestamp()) / 3600
    except Exception:
        age_h = 0

    try:
        cur = _get_weekly_tokens()
    except Exception:
        return ""

    delta = cur["all"] - snap.get("all", 0)
    # 토큰 2M 이상 증가 또는 3시간 이상 경과 시 알림
    if delta > 2_000_000 or age_h >= 3:
        return (f"💡 마지막 보정 {int(age_h)}시간 전 · 토큰 +{fmt_tokens(delta)} "
                f"→ cs --set-plan S,W,N 으로 갱신 권장")
    return ""


def save_plan_limits(data: dict):
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(PLAN_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def usage_bar(pct: int, width: int = 20) -> Text:
    """Colored progress bar based on usage %."""
    filled = int(pct / 100 * width)
    empty  = width - filled
    color  = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    t = Text()
    t.append("█" * filled, style=f"bold {color}")
    t.append("░" * empty,  style="dim")
    t.append(f"  {pct}%",  style=f"bold {color}")
    return t


def make_plan_panel(limits: dict) -> Panel:
    if not limits:
        txt = Text()
        txt.append("플랜 한도 정보 없음\n\n", style="dim")
        txt.append("업데이트 방법:\n", style="bold")
        txt.append("  cs --set-plan   ", style="bright_yellow")
        txt.append("대화형 입력\n", style="dim")
        txt.append("  cs --fetch-plan  ", style="bright_cyan")
        txt.append("브라우저에서 자동 가져오기\n", style="dim")
        return Panel(txt, title="[bold]⑤ Plan Usage Limits[/]", border_style="dim")

    updated_at = limits.get("updated_at", "?")
    # how old
    try:
        from datetime import datetime
        age_s   = time.time() - datetime.fromisoformat(updated_at).timestamp()
        age_str = f"{int(age_s/60)}분 전" if age_s < 3600 else f"{int(age_s/3600)}시간 전"
    except Exception:
        age_str = "?"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim",  justify="right", width=18)
    grid.add_column(min_width=24)
    grid.add_column(style="dim",  justify="left")

    def add_row(label: str, info: dict, reset_label: str):
        pct   = info.get("used_pct", 0)
        bar   = usage_bar(pct)
        reset = info.get("resets_in_mins")
        if reset is not None:
            h, m = divmod(int(reset), 60)
            reset_str = f"{h}h {m}m 후 재설정" if h else f"{m}m 후 재설정"
        else:
            day  = info.get("resets_day", "")
            tstr = info.get("resets_time", "")
            reset_str = f"{day} {tstr}에 재설정"
        grid.add_row(label, bar, reset_str)

    sess = limits.get("session")
    if sess:
        label = f"현재 세션 ({sess.get('label','Max')})"
        add_row(label, sess, "")

    wall = limits.get("weekly_all")
    if wall:
        add_row("주간 (모든 모델)", wall, "")

    wson = limits.get("weekly_sonnet")
    if wson:
        add_row("주간 (Sonnet만)", wson, "")

    has_snap = bool(limits.get("_snapshot_tokens"))
    mode_str = "자동 추정" if has_snap else "수동"

    stale = _plan_stale_warning(limits)
    content = Group(grid, Text(stale, style="bright_yellow")) if stale else grid

    return Panel(
        content,
        title=f"[bold]⑤ Plan Usage Limits[/] [dim]({mode_str} · 기준: {age_str})[/]",
        border_style="dim",
    )


def make_brief_analysis(data: dict) -> Panel:
    """Usage analysis briefing — patterns & insights."""
    daily  = data.get("dailyActivity", [])
    mu     = data.get("modelUsage", {})
    hc     = data.get("hourCounts", {})

    # weekly comparison
    this_week = [d.get("messageCount", 0) for d in daily[-7:]]
    prev_week = [d.get("messageCount", 0) for d in daily[-14:-7]]
    tw_sum, pw_sum = sum(this_week), sum(prev_week)
    week_delta = (tw_sum - pw_sum) / pw_sum * 100 if pw_sum else 0

    # cache efficiency
    total_in  = sum(v.get("inputTokens", 0) for v in mu.values())
    cache_r   = sum(v.get("cacheReadInputTokens", 0) for v in mu.values())
    cache_eff = cache_r / (total_in + cache_r) * 100 if (total_in + cache_r) else 0

    # dominant model
    totals = {
        MODEL_ALIASES.get(k, k):
        v.get("inputTokens",0)+v.get("outputTokens",0)
        +v.get("cacheReadInputTokens",0)+v.get("cacheCreationInputTokens",0)
        for k, v in mu.items()
    }
    grand = sum(totals.values()) or 1
    top_model = max(totals, key=totals.get)
    top_pct   = totals[top_model] / grand * 100

    # hour pattern
    counts = [int(hc.get(str(h), 0)) for h in range(24)] if isinstance(hc, dict) \
             else list(hc) + [0]*(24 - len(hc))
    peak_h = counts.index(max(counts)) if counts else 0
    dawn   = sum(counts[22:]) + sum(counts[:5])  # 22시~04시
    day_   = sum(counts[9:18])
    night_ratio = dawn / sum(counts) * 100 if sum(counts) else 0

    # busy days
    avg_msgs = sum(d.get("messageCount",0) for d in daily) / len(daily) if daily else 0
    heavy_days = sum(1 for d in daily[-30:] if d.get("messageCount",0) > avg_msgs * 1.5)

    txt = Text()

    # weekly trend
    arrow = "↑" if week_delta > 5 else "↓" if week_delta < -5 else "→"
    color = "green" if week_delta > 5 else "red" if week_delta < -5 else "white"
    txt.append("📊 이번 주 vs 지난 주\n", style="bold yellow")
    txt.append(f"   {arrow} {abs(week_delta):.0f}%  ", style=f"bold {color}")
    txt.append(f"이번 주 {sum(this_week):,}개 / 지난 주 {sum(prev_week):,}개 메시지\n\n")

    # cache
    txt.append("⚡ 캐시 효율\n", style="bold yellow")
    cache_color = "green" if cache_eff > 90 else "yellow" if cache_eff > 70 else "red"
    txt.append(f"   {cache_eff:.1f}% ", style=f"bold {cache_color}")
    txt.append("캐시 히트 — ", style="dim")
    if cache_eff > 90:
        txt.append("매우 효율적. 긴 대화를 재활용하고 있음\n\n")
    elif cache_eff > 70:
        txt.append("양호. 세션당 맥락이 적당히 누적됨\n\n")
    else:
        txt.append("낮음. 짧은 세션이 많거나 맥락 재활용 적음\n\n")

    # model usage
    txt.append("🤖 모델 패턴\n", style="bold yellow")
    txt.append(f"   {top_model} ", style=f"bold {MODEL_COLORS.get(top_model, 'white')}")
    txt.append(f"가 전체의 {top_pct:.0f}% 사용\n")
    if "Opus" in top_model:
        txt.append("   → 복잡한 개발·분석 중심 사용 패턴\n\n", style="dim")
    elif "Sonnet" in top_model:
        txt.append("   → 속도·품질 균형 잡힌 일반 개발 패턴\n\n", style="dim")
    else:
        txt.append("   → 빠른 반복 작업 위주 패턴\n\n", style="dim")

    # time pattern
    txt.append("🕐 사용 시간대\n", style="bold yellow")
    txt.append(f"   피크: {peak_h:02d}:00  ")
    if night_ratio > 30:
        txt.append(f"심야 비중 {night_ratio:.0f}% — ", style="dim")
        txt.append("야간 집중 개발형\n", style="bright_magenta")
    elif day_ > sum(counts) * 0.5:
        txt.append("업무 시간 집중형\n", style="bright_cyan")
    else:
        txt.append("혼합형 (낮+밤)\n", style="white")

    txt.append(f"\n   최근 30일 중 ", style="dim")
    txt.append(f"{heavy_days}일", style="bold white")
    txt.append(f"이 평균(일 {avg_msgs:.0f}개) 대비 1.5배 이상 사용일\n", style="dim")

    return Panel(txt, title="[bold]⑤ 사용량 분석 브리핑[/]", border_style="yellow", padding=(1, 2))


def _restore_terminal():
    """터미널이 raw 모드로 남아있을 경우 복원."""
    try:
        import subprocess
        subprocess.run(["stty", "sane"], check=False)
    except Exception:
        pass


def _readline(prompt: str) -> str:
    """raw 모드 잔재 방지 + \r 처리 안전한 input."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return line.rstrip("\r\n")


def set_plan_interactive(console: Console):
    """Interactive prompt to set plan limits."""
    # cs-watch 후 raw 모드가 남아있을 수 있으므로 복원
    _restore_terminal()

    console.print(Panel(
        "[bold yellow]플랜 한도 수동 입력[/]\n\n"
        "Claude.ai 설정 화면의 수치를 입력하세요.\n"
        "숫자만 입력 (%, 분 등 단위 제외). 없으면 Enter로 건너뜀.",
        border_style="yellow",
    ))

    limits = load_plan_limits()

    def ask(prompt: str, key: str, sub: str, default=None):
        val = _readline(f"  {prompt} [{default or ''}]: ").strip()
        if val:
            try:
                limits.setdefault(key, {})[sub] = int(val)
            except ValueError:
                pass

    console.print("\n[bold]① 현재 세션[/]")
    ask("사용량 %", "session", "used_pct", limits.get("session", {}).get("used_pct"))
    ask("재설정까지 남은 분 (예: 285)", "session", "resets_in_mins",
        limits.get("session", {}).get("resets_in_mins"))
    label = _readline("  라벨 [Max 5x]: ").strip() or "Max 5x"
    limits.setdefault("session", {})["label"] = label

    console.print("\n[bold]② 주간 한도 — 모든 모델[/]")
    ask("사용량 %", "weekly_all", "used_pct",
        limits.get("weekly_all", {}).get("used_pct"))
    day_all  = _readline("  재설정 요일 (예: 금): ").strip()
    time_all = _readline("  재설정 시각 (예: 15:00): ").strip()
    if day_all:  limits.setdefault("weekly_all", {})["resets_day"]  = day_all
    if time_all: limits.setdefault("weekly_all", {})["resets_time"] = time_all

    console.print("\n[bold]③ 주간 한도 — Sonnet만[/]")
    ask("사용량 %", "weekly_sonnet", "used_pct",
        limits.get("weekly_sonnet", {}).get("used_pct"))
    day_son  = _readline("  재설정 요일 (예: 토): ").strip()
    time_son = _readline("  재설정 시각 (예: 16:00): ").strip()
    if day_son:  limits.setdefault("weekly_sonnet", {})["resets_day"]  = day_son
    if time_son: limits.setdefault("weekly_sonnet", {})["resets_time"] = time_son

    # 현재 토큰 스냅샷 저장 (자동 보간용)
    snap = _get_weekly_tokens()
    limits["_snapshot_tokens"] = snap
    # 기준 % 저장 (보간의 기준점)
    if limits.get("weekly_all", {}).get("used_pct"):
        limits["weekly_all"]["_base_pct"] = limits["weekly_all"]["used_pct"]
    if limits.get("weekly_sonnet", {}).get("used_pct"):
        limits["weekly_sonnet"]["_base_pct"] = limits["weekly_sonnet"]["used_pct"]

    save_plan_limits(limits)
    console.print(f"\n[green]✓ 저장됨: {PLAN_FILE}[/]")
    console.print(f"[dim]  토큰 스냅샷: all={fmt_tokens(snap['all'])}, sonnet={fmt_tokens(snap['sonnet'])}[/]")
    console.print(f"[dim]  이후 사용량 변화에 따라 자동 보간됩니다.[/]")


# ─── Main ─────────────────────────────────────────────────────────────────────

HELP_TEXT = """usage: claude-stats.py [options]

Options:
  (none)               Dashboard: summary, models, daily, hourly, plan limits
  -s, --sessions       Per-session token breakdown
  -n N                 Number of sessions to show (default: 30)
  -w, --watch          Live full-screen mode (htop style)
  --set-plan           Interactive input for plan usage limits
  --set-plan S,W,N     Quick set: session%, weekly%, sonnet% (e.g. 10,94,65)
  --rebuild            Recompute stats-cache.json from JSONL files
  -h, --help           Show this help

Watch mode keys:
  h / ?    Toggle help overlay
  1        Brief: Summary
  2        Brief: Model Breakdown
  3        Brief: Daily Activity
  4        Brief: Hour Distribution
  5        Brief: Usage Analysis (패턴 분석)
  p        Brief: Plan Limits
  s        Sessions view
  d        Dashboard view
  r        Force refresh
  q        Quit

Examples:
  python3.9 ~/claude-stats.py
  python3.9 ~/claude-stats.py --set-plan
  python3.9 ~/claude-stats.py --sessions -n 20
  python3.9 ~/claude-stats.py --watch
  python3.9 ~/claude-stats.py -s -w"""

HELP_KEYS = """[bold yellow]Keys[/]

  [bold]h[/]  [bold]?[/]       Toggle this help
  [bold]1[/]          Brief: ① Summary
  [bold]2[/]          Brief: ② Model Breakdown
  [bold]3[/]          Brief: ③ Daily Activity
  [bold]4[/]          Brief: ④ Hour Distribution
  [bold]5[/]          Brief: 사용량 패턴 분석
  [bold]p[/]          Brief: ⑤ Plan Usage Limits
  [bold]s[/]          Sessions view
  [bold]d[/]          Dashboard view
  [bold]r[/]          Force refresh now
  [bold]q[/]  Ctrl+C  Quit

[bold yellow]Metrics[/]

  [green]Input[/]     Prompts + context you sent to Claude
  [yellow]Output[/]    Tokens Claude generated (responses)
  [blue]Cache R[/]   Context reused from cache [dim](near-zero cost)[/]
  [cyan]Cache W[/]   Context written to cache [dim](moderate cost)[/]
  [white]Total[/]     Input + Output + Cache R + Cache W

[bold yellow]Session view symbols[/]

  [green]N[/][dim]/[/][yellow]N[/]   Input / Output
  [dim]¢[/]       Cache R + Cache W combined
  [dim]=[/]       Session total
  [dim]+N[/]      Multiple models in one session

[bold yellow]Plan limits[/]

  Update:  [bright_yellow]cs --set-plan[/]   (대화형 입력)"""


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(HELP_TEXT)
        return

    console = Console()

    if "--rebuild" in args:
        rebuild_cache()
        return

    if "--set-plan" in args:
        idx = args.index("--set-plan")
        # cs --set-plan 10,94,65  (세션%,주간전체%,소넷%)
        if idx + 1 < len(args) and "," in args[idx + 1]:
            parts = args[idx + 1].split(",")
            if len(parts) >= 3:
                try:
                    s_pct, w_pct, sn_pct = int(parts[0]), int(parts[1]), int(parts[2])
                    cur_tokens = _get_weekly_tokens()

                    # 이전 추정치와 비교 → 보정 데이터 수집
                    old_limits = load_plan_limits(auto_interpolate=True)
                    limits = load_plan_limits(auto_interpolate=False)
                    calibration = limits.get("_calibration", [])

                    old_est_all = old_limits.get("weekly_all", {}).get("used_pct", 0)
                    old_est_son = old_limits.get("weekly_sonnet", {}).get("used_pct", 0)
                    if old_est_all > 0 and limits.get("_snapshot_tokens"):
                        calibration.append({
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "est_all": old_est_all, "actual_all": w_pct,
                            "est_son": old_est_son, "actual_son": sn_pct,
                            "tokens": cur_tokens,
                        })
                        calibration = calibration[-20:]  # 최근 20개만

                    limits.setdefault("session", {"label": "Max 5x"})["used_pct"] = s_pct
                    limits.setdefault("weekly_all", {})["used_pct"] = w_pct
                    limits["weekly_all"]["_base_pct"] = w_pct
                    limits.setdefault("weekly_sonnet", {})["used_pct"] = sn_pct
                    limits["weekly_sonnet"]["_base_pct"] = sn_pct
                    limits["_snapshot_tokens"] = cur_tokens
                    limits["_calibration"] = calibration
                    save_plan_limits(limits)

                    console.print(f"[green]✓ 세션 {s_pct}% · 주간 {w_pct}% · Sonnet {sn_pct}%[/]")
                    if old_est_all > 0:
                        diff_all = w_pct - old_est_all
                        diff_son = sn_pct - old_est_son
                        sign_a = "+" if diff_all >= 0 else ""
                        sign_s = "+" if diff_son >= 0 else ""
                        color_a = "green" if abs(diff_all) <= 2 else "yellow" if abs(diff_all) <= 5 else "red"
                        color_s = "green" if abs(diff_son) <= 2 else "yellow" if abs(diff_son) <= 5 else "red"
                        console.print(f"  보정: 주간 추정 {old_est_all}% → 실제 {w_pct}% [{color_a}]({sign_a}{diff_all})[/]"
                                      f"  Sonnet 추정 {old_est_son}% → 실제 {sn_pct}% [{color_s}]({sign_s}{diff_son})[/]")
                        console.print(f"  [dim]보정 데이터 {len(calibration)}건 축적 중[/]")
                except ValueError:
                    console.print("[red]형식: --set-plan 세션%,주간%,소넷%  (예: 10,94,65)[/]")
                return
        set_plan_interactive(console)
        return

    watch_mode   = "--watch" in args or "-w" in args
    session_mode = "--sessions" in args or "-s" in args

    n_sessions = 30
    if "-n" in args:
        idx = args.index("-n")
        if idx + 1 < len(args):
            try:
                n_sessions = int(args[idx + 1])
            except ValueError:
                pass

    # ── One-shot mode ──────────────────────────────────────────────────────
    if not watch_mode:
        try:
            if session_mode:
                console.print(make_session_table(load_sessions(n_sessions), n_sessions))
            else:
                data   = load_stats()
                limits = load_plan_limits()
                for p in [make_summary(data), make_model_table(data),
                          make_daily(data), make_hourly(data),
                          make_plan_panel(limits)]:
                    console.print(p)
        except FileNotFoundError:
            console.print(f"[red]Stats file not found: {STATS_FILE}[/]")
            sys.exit(1)
        return

    # ── Watch (full-screen, htop-style) mode ──────────────────────────────
    overlay  = None  # None|"help"|"brief1"|"brief2"|"brief3"|"brief4"|"brief5"|"plan"
    cur_mode = "sessions" if session_mode else "dashboard"
    last_refresh = 0.0
    _data:    dict = {}
    _sessions: list = []
    _limits:  dict = {}

    def refresh():
        nonlocal _data, _sessions, _limits, last_refresh
        _data     = load_stats()
        _sessions = load_sessions(n_sessions)
        _limits   = load_plan_limits()
        last_refresh = time.time()

    def build_footer() -> Text:
        elapsed = int(time.time() - last_refresh)
        nxt     = max(0, WATCH_INTERVAL - elapsed)
        t = Text(justify="right")
        t.append(f" refresh in {nxt}s  ", style="dim")
        t.append("h", style="bold yellow"); t.append("=help  ", style="dim")
        t.append("1-5", style="bold yellow"); t.append("=brief  ", style="dim")
        t.append("p", style="bold yellow"); t.append("=plan  ", style="dim")
        t.append("d/s", style="bold yellow"); t.append("=view  ", style="dim")
        t.append("r", style="bold yellow"); t.append("=refresh  ", style="dim")
        t.append("q", style="bold yellow"); t.append("=quit", style="dim")
        return t

    # Layout은 항상 content(가변) + footer(1줄) 2단 고정 — 구조 변경 시 스크롤 방지
    _layout = Layout()
    _layout.split_column(
        Layout(name="content"),
        Layout(name="footer", size=1),
    )

    def build_content():
        """현재 상태에 맞는 content renderable 반환."""
        overlays = {
            "help":   make_help_panel,
            "brief1": lambda: make_brief_summary(_data),
            "brief2": lambda: make_brief_models(_data),
            "brief3": lambda: make_brief_daily(_data),
            "brief4": lambda: make_brief_hourly(_data),
            "brief5": lambda: make_brief_analysis(_data),
            "plan":   lambda: make_plan_panel(_limits),
        }
        if overlay in overlays:
            return overlays[overlay]()

        if cur_mode == "sessions":
            return make_session_table(_sessions, n_sessions)

        # 대시보드: Group으로 묶어서 단일 렌더러블로
        return Group(
            make_summary(_data),
            make_model_table(_data),
            make_daily(_data),
            make_hourly(_data),
            make_plan_panel(_limits),
        )

    def build_screen() -> Layout:
        _layout["content"].update(build_content())
        _layout["footer"].update(build_footer())
        return _layout

    key_q   = queue.Queue()
    stop_ev = threading.Event()

    _old_term = None
    try:
        refresh()
        _, _old_term = start_key_reader(key_q, stop_ev)

        with Live(build_screen(), screen=True, refresh_per_second=2, console=console) as live:
            while True:
                # Keypresses
                try:
                    while True:
                        ch = key_q.get_nowait()
                        if ch in ('q', 'Q', '\x03'):
                            raise KeyboardInterrupt
                        elif ch in ('h', '?'):
                            overlay = None if overlay == "help" else "help"
                        elif ch == '1':
                            overlay = None if overlay == "brief1" else "brief1"
                        elif ch == '2':
                            overlay = None if overlay == "brief2" else "brief2"
                        elif ch == '3':
                            overlay = None if overlay == "brief3" else "brief3"
                        elif ch == '4':
                            overlay = None if overlay == "brief4" else "brief4"
                        elif ch == '5':
                            overlay = None if overlay == "brief5" else "brief5"
                        elif ch in ('p', 'P'):
                            overlay = None if overlay == "plan" else "plan"
                        elif ch in ('s', 'S'):
                            cur_mode = "sessions"; overlay = None
                        elif ch in ('d', 'D'):
                            cur_mode = "dashboard"; overlay = None
                        elif ch in ('r', 'R'):
                            refresh(); overlay = None
                        live.update(build_screen())
                except queue.Empty:
                    pass

                # Auto-refresh
                if time.time() - last_refresh >= WATCH_INTERVAL:
                    refresh()
                live.update(build_screen())
                time.sleep(0.25)

    except KeyboardInterrupt:
        pass
    finally:
        stop_ev.set()
        # 터미널 상태 완전 복원 (cbreak → 정상 모드)
        try:
            if _old_term is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _old_term)
        except Exception:
            pass
        _restore_terminal()  # stty sane 추가 보험


if __name__ == "__main__":
    main()
