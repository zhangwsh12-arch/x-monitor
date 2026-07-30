"""一次性回填：抓每个账号最近若干页的全部推文，按真实发布日期分装成快照，
为「每个真的有推文的日期」生成日报，并渲染看板。抓多少展多少，不限时间窗口。
用法：python run_backfill.py [最多翻页数，默认40]
"""
import sys
from collections import defaultdict

from common import (
    TwitterApiClient, load_accounts, load_state, save_state,
    now_kst, KST, DAILY_DIR, DATA_DIR, write_json, read_json, log,
)
from fetch import parse_created_at, classify, simplify
from interpret import interpret_daily
from render import render_daily


def fetch_all(client: TwitterApiClient, handle: str, max_pages: int = 40, diag: list | None = None):
    """抓最近 max_pages 页的全部推文，不做时间过滤。返回 [(created_dt, item), ...]。"""
    collected = []
    cursor = ""
    pages = 0
    for _ in range(max_pages):
        resp = client.last_tweets(handle, cursor=cursor)
        tweets = resp.get("tweets") or []
        pages += 1
        if diag is not None and pages == 1:
            diag.append({
                "handle": handle,
                "tweets_len": len(tweets),
                "has_next_page": resp.get("has_next_page"),
            })
        if not tweets:
            break
        for t in tweets:
            created = parse_created_at(t.get("createdAt", ""))
            collected.append((created, simplify(t, classify(t))))
        if not resp.get("has_next_page"):
            break
        cursor = resp.get("next_cursor") or ""
        if not cursor:
            break
    if diag is not None:
        diag.append({"handle": handle, "_summary": f"翻了{pages}页, 共{len(collected)}条"})
    return collected


def main(max_pages: int = 40):
    accounts = load_accounts()
    client = TwitterApiClient()

    per_date = defaultdict(dict)   # date_key -> handle -> [items]
    newest_ids = {}
    diag = []

    for a in accounts:
        handle = a["handle"].lstrip("@")
        try:
            rows = fetch_all(client, handle, max_pages, diag=diag)
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

    # 为「每个真的有推文的日期」生成快照
    generated_days = []
    for d in sorted(per_date.keys()):
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

    # 更新去重游标
    state = load_state()
    for h, mid in newest_ids.items():
        cur = (state.get(h) or {}).get("last_seen_id")
        if not cur or mid > cur:
            state[h] = {"last_seen_id": mid}
    save_state(state)

    # 渲染（升序，index.html 最终指向最新一天）
    for d in sorted(generated_days):
        snap = read_json(DAILY_DIR / f"{d}.json")
        if snap:
            render_daily(snap)
    log.info("=== 回填完成，共 %d 天有内容 ===", len(generated_days))


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    main(n)
