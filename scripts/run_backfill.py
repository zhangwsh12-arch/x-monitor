"""一次性回填 + 诊断：抓每个账号最近 N 天推文，按真实发布日期分装成多天日报快照。
修复：不再因单条旧推文提前停止翻页（转发的时间戳可能是原推文时间，会乱序）。
诊断：把每个账号第一页原始返回摘要 dump 到 data/_diag.json 便于排查。
用法：python run_backfill.py [天数，默认30]
"""
import sys
from collections import defaultdict
from datetime import timedelta, timezone

from common import (
    TwitterApiClient, load_accounts, load_state, save_state,
    now_kst, KST, DAILY_DIR, DATA_DIR, write_json, read_json, log,
)
from fetch import parse_created_at, classify, simplify
from interpret import interpret_daily
from render import render_daily


def fetch_recent(client: TwitterApiClient, handle: str, since_utc,
                 max_pages: int = 30, diag: list | None = None):
    """抓最近推文。收集所有页内推文，最后统一按时间过滤（不提前 break，避免转发乱序漏抓）。"""
    collected = []
    cursor = ""
    pages = 0
    for _ in range(max_pages):
        resp = client.last_tweets(handle, cursor=cursor)
        tweets = resp.get("tweets") or []
        pages += 1
        if diag is not None and pages == 1:
            for t in tweets[:8]:
                diag.append({
                    "handle": handle,
                    "id": t.get("id"),
                    "type": t.get("type"),
                    "createdAt": t.get("createdAt"),
                    "isReply": t.get("isReply"),
                    "has_retweeted": bool(t.get("retweeted_tweet")),
                    "has_quoted": bool(t.get("quoted_tweet")),
                    "author": (t.get("author") or {}).get("userName"),
                    "text": (t.get("text") or "")[:60],
                })
        if not tweets:
            break
        for t in tweets:
            created = parse_created_at(t.get("createdAt", ""))
            if created and created >= since_utc:
                collected.append((created, simplify(t, classify(t))))
        if not resp.get("has_next_page"):
            break
        cursor = resp.get("next_cursor") or ""
        if not cursor:
            break
    if diag is not None:
        diag.append({"handle": handle, "_summary": f"翻了{pages}页, 窗口内收集{len(collected)}条"})
    return collected


def main(days: int = 30):
    accounts = load_accounts()
    client = TwitterApiClient()
    since_utc = now_kst().astimezone(timezone.utc) - timedelta(days=days)

    per_date = defaultdict(dict)
    newest_ids = {}
    diag = []

    for a in accounts:
        handle = a["handle"].lstrip("@")
        try:
            rows = fetch_recent(client, handle, since_utc, diag=diag)
        except Exception as e:  # noqa
            log.error("回填 @%s 失败: %s", handle, e)
            rows = []
        ids = [it["id"] for _, it in rows if it.get("id")]
        if ids:
            newest_ids[handle] = max(ids)
        for created, it in rows:
            if not created:
                continue
            dk = created.astimezone(KST).strftime("%Y-%m-%d")
            per_date[dk].setdefault(handle, []).append(it)
        log.info("@%s 回填抓到 %d 条", handle, len(rows))

    write_json(DATA_DIR / "_diag.json", {"generated_at": now_kst().isoformat(), "records": diag})

    generated_days = []
    for i in range(days):
        d = (now_kst() - timedelta(days=i)).strftime("%Y-%m-%d")
        accounts_data = []
        for a in accounts:
            handle = a["handle"].lstrip("@")
            items = per_date.get(d, {}).get(handle, [])
            items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
            counts = {"post": 0, "retweet": 0, "quote": 0, "reply": 0}
            for it in items:
                counts[it["kind"]] = counts.get(it["kind"], 0) + 1
            accounts_data.append({
                "handle": handle, "name": a.get("name", handle),
                "counts": counts, "items": items,
            })
        daily = {"date": d, "generated_at": now_kst().isoformat(), "accounts": accounts_data}
        daily = interpret_daily(daily)
        write_json(DAILY_DIR / f"{d}.json", daily)
        generated_days.append(d)
        log.info("已生成 %s 快照", d)

    state = load_state()
    for h, mid in newest_ids.items():
        cur = (state.get(h) or {}).get("last_seen_id")
        if not cur or mid > cur:
            state[h] = {"last_seen_id": mid}
    save_state(state)

    for d in sorted(generated_days):
        snap = read_json(DAILY_DIR / f"{d}.json")
        if snap:
            render_daily(snap)
    log.info("=== 回填完成，共 %d 天 ===", len(generated_days))


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    main(n)
