"""渲染看板 HTML 到 docs/。"""
import re
from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import TEMPLATES_DIR, DOCS_DIR, DAILY_DIR, WEEKLY_DIR, now_kst

KIND_LABEL = {"post": "原创", "retweet": "转发", "quote": "引用", "reply": "回复"}


def _env():
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["kind_label"] = lambda k: KIND_LABEL.get(k, k)
    return env


def _history_links():
    dates = sorted([p.stem for p in DAILY_DIR.glob("*.json")], reverse=True)[:14]
    weeks = sorted([p.stem for p in WEEKLY_DIR.glob("*.json")], reverse=True)[:8]
    return dates, weeks


def render_daily(daily: dict) -> None:
    env = _env()
    # 全部日期(倒序，新→旧)供下拉；周报单独下拉
    all_dates = sorted([p.stem for p in DAILY_DIR.glob("*.json")], reverse=True)
    weeks = sorted([p.stem for p in WEEKLY_DIR.glob("*.json")], reverse=True)[:12]
    cur = daily["date"]
    if cur not in all_dates:
        all_dates = sorted(set(all_dates + [cur]), reverse=True)
    idx = all_dates.index(cur)
    next_date = all_dates[idx - 1] if idx > 0 else None          # 更近的一天(后一天)
    prev_date = all_dates[idx + 1] if idx < len(all_dates) - 1 else None  # 更早的一天(前一天)
    ctx = {
        "date": cur,
        "accounts": daily["accounts"],
        "all_dates": all_dates,
        "weeks": weeks,
        "prev_date": prev_date,
        "next_date": next_date,
        "keywords": daily.get("keywords"),
        "generated_at": now_kst().strftime("%Y-%m-%d %H:%M KST"),
    }
    html = env.get_template("dashboard.html.j2").render(**ctx)
    # 首页 = 最新日报；同时留存 daily-YYYY-MM-DD.html
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (DOCS_DIR / f"daily-{daily['date']}.html").write_text(html, encoding="utf-8")


def _analysis_to_html(text: str) -> str:
    """把 LLM 输出的 markdown-ish 文本转成简单 HTML（###标题 / 段落）。"""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("###"):
            out.append(f"<h3>{line.lstrip('#').strip()}</h3>")
        elif line.startswith("##"):
            out.append(f"<h3>{line.lstrip('#').strip()}</h3>")
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            out.append(f"<p>{line}</p>")
    return "\n".join(out)


def render_weekly(week_key: str, analysis: str) -> None:
    env = _env()
    html = env.get_template("weekly.html.j2").render(
        week=week_key,
        analysis_html=_analysis_to_html(analysis),
        generated_at=now_kst().strftime("%Y-%m-%d %H:%M KST"),
    )
    (DOCS_DIR / f"weekly-{week_key}.html").write_text(html, encoding="utf-8")
