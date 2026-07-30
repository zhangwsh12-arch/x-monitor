"""一次性回填：抓每个账号最近 N 天推文，按真实发布日期分装成过去 N 天的日报快照，
并生成看板。绕过 24h 窗口，但抓完会更新 state 游标，避免日报重复抓。
用法：python run_backfill.py [天数，默认30]
"""
import sys
from collections import defaultdict
from datetime import timedelta, timezone

from common import (
    TwitterApiClient, load_accounts, load_state, save_state,
    now_kst, KST, DAILY_DIR, write_json, read_json, log,
)
from fetch import parse_created_at, classify, simplify
from interpret import interpret_daily
from render import render_daily


def fetch_recent(client: TwitterApiClient, handle: str, since_utc, max_pages: int = 30):
    """抓最近推文直到超过 since_utc。返回 [(created_dt, item), ...]。"""
    collected = []
    cursor = ""
    for _ in range(max_pages):
        resp = client.last_tweets(handle, cursor=cursor)
        tweets = resp.get("tweets") or []
        if not tweets:
            break
        stop = False
        for t in tweets:
            created = parse_created_at(t.get("createdAt", ""))
            if created and created < since_utc:
                stop = True
                break
            collected.append((created, simplify(t, classify(t))))
        if stop or not resp.get("has_next_page"):
            break
        cursor = resp.get("next_cursor") or ""
        if not cursor:
            break
    return collected


def main(days: int = 30):
    accounts = load_accounts()
    client = TwitterApiClient()
    since_utc = now_kst().astimezone(timezone.utc) - timedelta(days=days)

    per_date = defaultdict(dict)
    newest_ids = {}

    for a in accounts:
        handle = a["handle"].lstrip("@")
        try:
            rows = fetch_recent(client, handle, since_utc)
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
