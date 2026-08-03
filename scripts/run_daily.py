"""日流程：抓取 → 解读 → 采集近30天窗口 → 提炼关键词 → 存快照 → 渲染看板
（日报仅更新看板，不推送微信）。"""
from common import DAILY_DIR, kst_date_key, write_json, read_json, now_kst, log
from fetch import run_fetch
from interpret import interpret_daily, interpret_keywords
from render import render_daily


def _collect_window_texts(accounts_today, days=30, max_per_acc=12, max_chars=160):
    """采集近 days 天各账号文本（今日内存数据 + 历史快照文件），供关键词提炼使用。
    上限：每账号 max_per_acc 条、每条 max_chars 字符，控制 LLM 输入体积与成本。"""
    texts = {a["handle"]: [] for a in accounts_today}
    for acc in accounts_today:  # 今日内存数据优先，最新鲜
        for it in acc["items"]:
            texts[acc["handle"]].append((it.get("text_zh") or it["text"])[:max_chars])
    today_key = kst_date_key()
    files = sorted(DAILY_DIR.glob("*.json"), reverse=True)[:days]
    for f in files:
        if f.stem == today_key:
            continue  # 今日文件此时尚未写入，避免重复
        snap = read_json(f)
        if not snap:
            continue  # 缺失/损坏文件安静跳过
        for acc in snap.get("accounts", []):
            h = acc.get("handle")
            if h in texts:
                for it in acc.get("items", []):
                    t = (it.get("text_zh") or it.get("text") or "")[:max_chars]
                    if t:
                        texts[h].append(t)
    return {h: v[:max_per_acc] for h, v in texts.items()}


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

    render_daily(daily)
    log.info("已渲染看板 docs/index.html（日报不推送微信，每周推送一次）")
    log.info("=== 日报流程结束 ===")


if __name__ == "__main__":
    main()
