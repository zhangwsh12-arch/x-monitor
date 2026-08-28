"""企业微信机器人推送：markdown 消息，>4096 字节自动分条，全文不截断。"""
import os

from common import log

WECHAT_LIMIT = 4000  # 企业微信 markdown content 上限约 4096 字节，留余量


def _post_markdown(webhook: str, content: str) -> bool:
    import requests
    try:
        r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode") not in (0, None):
            log.warning("企业微信返回错误: %s", data)
            return False
        return True
    except Exception as e:  # noqa
        log.error("推送失败: %s", e)
        return False


def _split(content: str, limit: int = WECHAT_LIMIT) -> list[str]:
    """按行分割，保证每条不超过字节上限。"""
    chunks, cur = [], ""
    for line in content.split("\n"):
        candidate = (cur + "\n" + line) if cur else line
        if len(candidate.encode("utf-8")) > limit:
            if cur:
                chunks.append(cur)
            # 单行超长则硬切
            if len(line.encode("utf-8")) > limit:
                b = line.encode("utf-8")
                for i in range(0, len(b), limit):
                    chunks.append(b[i:i + limit].decode("utf-8", "ignore"))
                cur = ""
            else:
                cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def push(content: str, webhook: str | None = None) -> bool:
    webhook = webhook or os.getenv("WECHAT_WEBHOOK_URL", "")
    if not webhook:
        log.warning("未配置 WECHAT_WEBHOOK_URL，跳过推送。以下为将推送的内容：\n%s", content)
        return False
    chunks = _split(content)
    total = len(chunks)
    ok = True
    for idx, ch in enumerate(chunks, 1):
        prefix = f"（{idx}/{total}）\n" if total > 1 else ""
        if not _post_markdown(webhook, prefix + ch):
            ok = False
    log.info("推送完成，共 %d 条", total)
    return ok


def build_daily_markdown(daily: dict) -> str:
    """日报：简洁 —— 每账号话题 + 缩进列表，无表格。"""
    lines = [f"# 📡 X 动态日报 · {daily['date']}", "> 过去 24 小时 · KST", ""]
    kind_label = {"post": "原创", "retweet": "转发", "quote": "引用", "reply": "回复"}
    for acc in daily["accounts"]:
        c = acc["counts"]
        lines.append(f"## {acc['name']} @{acc['handle']}")
        if acc.get("topic"):
            lines.append(f"💡 {acc['topic']}")
        if not acc["items"]:
            lines.append("> 今日无更新")
            lines.append("")
            continue
        lines.append(f"> 原创 {c['post']} · 转发 {c['retweet']} · 引用 {c['quote']}")
        for it in acc["items"]:
            body = it.get("text_zh") or it["text"]
            body = body.replace("\n", " ")
            if len(body) > 120:
                body = body[:120] + "…"
            lines.append(f"- **[{kind_label.get(it['kind'], it['kind'])}]** {body}")
            if it.get("url"):
                lines.append(f"  [原文↗]({it['url']})")
        lines.append("")
    return "\n".join(lines)


def _pages_base() -> str:
    return (os.getenv("PAGES_BASE_URL") or "https://zhangwsh12-arch.github.io/x-monitor").rstrip("/")


def _weekly_counts(snaps: list[dict]):
    """汇总近 7 日每账号条数（原创/转发/引用/回复）。返回 (agg, order)。"""
    agg: dict[str, dict] = {}
    order: list[str] = []
    for s in snaps:
        for a in s.get("accounts", []):
            h = a["handle"]
            if h not in agg:
                agg[h] = {"post": 0, "retweet": 0, "quote": 0, "reply": 0}
                order.append(h)
            c = a.get("counts", {}) or {}
            for k in ("post", "retweet", "quote", "reply"):
                agg[h][k] += c.get(k, 0)
    return agg, order


def _name_of(snaps: list[dict], handle: str) -> str:
    for s in snaps:
        for a in s.get("accounts", []):
            if a["handle"] == handle:
                return a.get("name") or handle
    return handle


def build_weekly_markdown(week_key: str, analysis: str, snaps: list | None = None) -> str:
    base = _pages_base()
    lines = [f"# 📈 X 账号周度趋势分析 · {week_key}", "> 覆盖近 7 日 · KST", ""]
    if snaps:
        agg, order = _weekly_counts(snaps)
        dmin = min((s["date"] for s in snaps), default="")
        dmax = max((s["date"] for s in snaps), default="")
        if dmin and dmax and dmin != dmax:
            lines[1] = f"> 覆盖近 7 日（{dmin}–{dmax}）· KST"
        lines.append("## 📊 本周概览")
        for h in order:
            c = agg[h]
            lines.append(
                f"- **{_name_of(snaps, h)}**：原创 {c['post']} / 转发 {c['retweet']} / "
                f"引用 {c['quote']} / 回复 {c['reply']}"
            )
        lines.append("")
    lines.append(analysis)
    lines.append("")
    lines.append(f"> 🏠 看板首页：[x-monitor↗]({base}/)")
    return "\n".join(lines)
