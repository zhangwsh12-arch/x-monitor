"""日流程：抓取 → 解读 → 采集近30天窗口 → 提炼关键词 → 存快照 → 渲染看板
（日报仅更新看板，不推送微信）。"""
from common import DAILY_DIR, kst_date_key, write_json, read_json, now_kst, log
from fetch import run_fetch
from interpret import interpret_daily, interpret_keywords
from render import render_daily, refresh_recent_daily_pages


def _collect_window_texts(accounts_today, days=30, max_per_acc=12, max_chars=160):
    """采集近 days 天各账号文本（今日内存数据 + 历史快照文件），供关键词提炼使用。"""
    texts = {a["handle"]: [] for a in accounts_today}

    for acc in accounts_today:
        for it in acc["items"]:
            texts[acc["handle"]].append((it.get("text_zh") or it["text"])[:max_chars])

    today_key = kst_date_key()
    files = sorted(DAILY_DIR.glob("*.json"), reverse=True)[:days]

    for f in files:
        if f.stem == today_key:
            continue

        snap = read_json(f)
        if not snap:
            continue

        for acc in snap.get("accounts", []):
            handle = acc.get("handle")
            if handle in texts:
                for it in acc.get("items", []):
                    text = (it.get("text_zh") or it.get("text") or "")[:max_chars]
                    if text:
                        texts[handle].append(text)

    return {handle: texts[:max_per_acc] for handle, texts in texts.items()}


def main():
    date_key = kst_date_key()
    log.info("=== 日报流程开始 %s ===", date_key)

    result = run_fetch(window_hours=24)
    result = interpret_daily(result)

    window_texts = _collect_window_texts(result["accounts"])
    keywords = interpret_keywords(window_texts)

    if keywords:
        log.info("已提炼近30天关键词")
    else:
        log.info("关键词区块跳过（无LLM key/无内容/解析失败）")

    daily = {
        "date": date_key,
        "generated_at": now_kst().isoformat(),
        "accounts": result["accounts"],
    }

    if keywords:
        daily["keywords"] = keywords

    write_json(DAILY_DIR / f"{date_key}.json", daily)
    log.info("已保存快照 data/daily/%s.json", date_key)

    # 更新最近31天历史页面，使旧日期也能正确跳转到“后一天”。
    refreshed = refresh_recent_daily_pages(days=31)

    # 最后再渲染当天，确保首页始终指向当天。
    render_daily(daily, update_index=True)

    log.info("已渲染首页，并刷新 %d 个近期历史日期导航", refreshed)
    log.info("=== 日报流程结束 ===")


if __name__ == "__main__":
    main()
