"""一次性修复历史日报：
1) 用 config/accounts.json 中最新 name 更新所有历史快照的显示昵称；
2) 补译指定日期中缺少中文译文的 2026 年非中文内容；
3) 重渲染所有历史日报 HTML，修复昵称和前后日期导航。

用法：python repair_history.py [YYYY-MM-DD]
默认修复 2026-08-17。
"""
import json
import sys

from common import DAILY_DIR, WEEKLY_DIR, load_accounts, read_json, write_json, log
from interpret import _chat, _is_2026
from render import render_daily, render_weekly

TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-08-17"
CHINESE_LANGS = {"zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant"}


def translate_missing_items(daily: dict) -> int:
    """只补译当前快照中缺失译文的非中文内容，返回成功补译条数。"""
    targets = []
    for acc in daily.get("accounts", []):
        for item in acc.get("items", []):
            lang = (item.get("lang") or "").lower()
            if (
                _is_2026(item)
                and lang not in CHINESE_LANGS
                and not item.get("text_zh")
                and item.get("text")
            ):
                targets.append(item)

    if not targets:
        log.info("%s 没有需要补译的内容", daily.get("date"))
        return 0

    payload = [{"i": idx, "text": item["text"][:800]} for idx, item in enumerate(targets)]
    content = _chat([
        {
            "role": "system",
            "content": (
                "你是专业的社交媒体翻译。将每条推文翻译成自然流畅的简体中文，"
                "保留专有名词、账号、话题标签和链接。"
                "只返回 JSON 数组，格式 [{\"i\":序号,\"zh\":\"译文\"}]，不要多余文字。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ])

    if not content:
        log.warning("%s 补译失败：LLM 没有返回内容", daily.get("date"))
        return 0

    try:
        rows = json.loads(content[content.find("["):content.rfind("]") + 1])
        translated = {row["i"]: row["zh"] for row in rows}
    except Exception as e:  # noqa
        log.warning("%s 补译解析失败：%s", daily.get("date"), e)
        return 0

    count = 0
    for idx, item in enumerate(targets):
        if idx in translated:
            item["text_zh"] = translated[idx]
            count += 1
    return count


def main():
    display_names = {account["handle"]: account["name"] for account in load_accounts()}
    paths = sorted(DAILY_DIR.glob("*.json"))
    if not paths:
        log.warning("没有找到历史日报快照，跳过修复")
        return

    repaired_names = 0
    repaired_translations = 0
    repaired_weekly_names = 0
    latest_daily = None

    for path in paths:
        daily = read_json(path)
        if not daily:
            continue

        changed = False
        for account in daily.get("accounts", []):
            desired_name = display_names.get(account.get("handle"))
            if desired_name and account.get("name") != desired_name:
                account["name"] = desired_name
                repaired_names += 1
                changed = True

        if daily.get("date") == TARGET_DATE:
            translated = translate_missing_items(daily)
            if translated:
                repaired_translations += translated
                changed = True

        if changed:
            write_json(path, daily)

        # 所有历史页面均重新生成：昵称、前后日期导航一起更新。
        render_daily(daily, update_index=False)
        latest_daily = daily

    # 首页保持指向最新日期。
    if latest_daily:
        render_daily(latest_daily, update_index=True)

    # 同步替换历史周报中的 @ID，并重新生成历史周报页面。
    for path in sorted(WEEKLY_DIR.glob("*.json")):
        weekly = read_json(path)
        if not weekly:
            continue
        analysis = weekly.get("analysis") or ""
        updated_analysis = analysis
        for handle, name in display_names.items():
            updated_analysis = updated_analysis.replace(f"@{handle}", name)
        if updated_analysis != analysis:
            weekly["analysis"] = updated_analysis
            write_json(path, weekly)
            repaired_weekly_names += 1
        render_weekly(weekly.get("week", path.stem), weekly.get("analysis", ""))

    log.info(
        "历史修复完成：更新 %d 个日报昵称、%d 个周报昵称，补译 %d 条内容，重渲染 %d 个日报页面",
        repaired_names,
        repaired_weekly_names,
        repaired_translations,
        len(paths),
    )


if __name__ == "__main__":
    main()
