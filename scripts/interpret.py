"""AI 解读：DeepSeek(OpenAI 兼容) 翻译非中文推文 + 每账号话题标签 + 周趋势合成。
接口可插拔：LLM_API_BASE / LLM_API_KEY / LLM_MODEL。无 key 时降级为原文直出。"""
import json
import os

from common import log


def _llm_config():
    # 用 or 兜底：避免 Secret 存在但为空字符串时拿到 "" 导致 URL 无域名
    base = (os.getenv("LLM_API_BASE") or "https://api.deepseek.com").rstrip("/")
    model = os.getenv("LLM_MODEL") or "deepseek-chat"
    return {
        "base": base,
        "key": os.getenv("LLM_API_KEY", ""),
        "model": model,
    }


def _chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1500) -> str | None:
    cfg = _llm_config()
    if not cfg["key"]:
        return None
    import requests
    import time
    for attempt in range(3):
        try:
            r = requests.post(
                f"{cfg['base']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
                json={"model": cfg["model"], "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=90,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa
            log.warning("LLM 调用失败(第%d次): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
    return None


def _is_2026(it: dict) -> bool:
    """只翻译 2026 年的推文，更早的不翻以省额度。"""
    c = it.get("created_at") or ""
    # created_at 形如 "Wed Jun 17 10:34:13 +0000 2026"，年份在末尾
    return c.strip().endswith("2026")


def interpret_daily(fetch_result: dict) -> dict:
    """给每条非中文推文加中文译文；给每个账号加一句话话题概括。"""
    for acc in fetch_result["accounts"]:
        items = acc["items"]
        # 1) 批量翻译非中文推文（仅 2026 年、且 lang 非中文）
        to_translate = [
            it for it in items
            if _is_2026(it)
            and (it.get("lang") or "").lower() not in ("zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant")
        ]
        if to_translate:
            payload = [{"i": idx, "text": it["text"][:500]} for idx, it in enumerate(to_translate)]
            content = _chat([
                {"role": "system", "content": "你是专业的社交媒体翻译。将每条推文翻译成自然流畅的简体中文，保留专有名词与话题标签。只返回 JSON 数组，格式 [{\"i\":序号,\"zh\":\"译文\"}]，不要多余文字。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            trans_map = {}
            if content:
                try:
                    for row in json.loads(content[content.find("["):content.rfind("]") + 1]):
                        trans_map[row["i"]] = row["zh"]
                except Exception as e:  # noqa
                    log.warning("翻译解析失败: %s", e)
            for idx, it in enumerate(to_translate):
                if idx in trans_map:
                    it["text_zh"] = trans_map[idx]
        # 2) 账号话题概括
        if items:
            joined = "\n".join(f"- [{it['kind']}] {it.get('text_zh') or it['text']}" for it in items[:20])
            topic = _chat([
                {"role": "system", "content": "你是敏锐的行业观察者。用一句不超过40字的简体中文概括该账号今日在关注/讨论什么。只返回这句话本身。"},
                {"role": "user", "content": f"账号 @{acc['handle']} 今日内容：\n{joined}"},
            ], max_tokens=120)
            acc["topic"] = topic or ""
        else:
            acc["topic"] = "今日无更新"
    return fetch_result


def interpret_keywords(window_texts_by_handle: dict[str, list[str]]) -> dict | None:
    """基于近 30 天各账号文本，提炼每账号关键词 + 判断是否存在真实共同关注。
    无 key / 无内容 / 解析失败时返回 None（调用方据此隐藏整个区块）。"""
    if not any(window_texts_by_handle.values()):
        return None
    blocks = []
    for h, texts in window_texts_by_handle.items():
        if texts:
            blocks.append(f"账号 {h}:\n" + "\n".join(f"- {t}" for t in texts))
    if not blocks:
        return None
    content = _chat([
        {"role": "system", "content": (
            "你是资讯归纳专家。基于给定的X账号近30天内容，为每个账号提炼3-5个简体中文关键词/短语"
            "（每个不超过8字），概括其反复出现的持续关注主题，不要复述单条推文。"
            "同时判断账号之间是否存在真实的共同关注领域：只有当至少两个账号围绕同一具体主题"
            "明确反复出现交集时才输出 common；若没有真实重叠，common 必须返回空数组，"
            "禁止为了凑数给出宽泛/牵强的共同点。"
            'accounts 的键必须是账号原始 handle，不要带 @ 前缀。'
            '只返回JSON：{"accounts":{"handle":["kw1","kw2"]},"common":["kw"]}，不要多余文字。'
        )},
        {"role": "user", "content": "\n\n".join(blocks)},
    ], temperature=0.3, max_tokens=500)
    if not content:
        return None
    try:
        data = json.loads(content[content.find("{"):content.rfind("}") + 1])
        raw_accounts = data.get("accounts", {}) or {}
        # 防御性处理：不管 LLM 是否遵守"不带 @"的指示，统一去掉前缀再匹配
        norm_accounts = {k.lstrip("@"): v for k, v in raw_accounts.items()}
        return {"accounts": norm_accounts, "common": data.get("common", [])}
    except Exception as e:  # noqa
        log.warning("关键词解析失败: %s", e)
        return None


def interpret_weekly(daily_snapshots: list[dict], monitored: list[dict] | None = None) -> str:
    """基于近 7 日快照做结构化周报：每账号要点（编号）+ 整体趋势。

    monitored 为 config/accounts.json 中的账号列表，用于保证无更新的账号也出现。
    """
    by_account: dict[str, list[str]] = {}
    display_names: dict[str, str] = {}

    for snap in daily_snapshots:
        for acc in snap.get("accounts", []):
            handle = acc["handle"]
            name = acc.get("name") or handle
            display_names[handle] = name
            key = name  # 给模型的输入直接用昵称，降低其输出 @ID 的概率
            for it in acc.get("items", []):
                txt = it.get("text_zh") or it.get("text") or ""
                if txt:
                    by_account.setdefault(key, []).append(f"[{it['kind']}] {txt[:160]}")

    # 监控账号全名单：保证无更新的账号也出现在周报里
    if monitored:
        monitored_names = [acc.get("name") or acc["handle"] for acc in monitored]
    else:
        monitored_names = list(display_names.values())

    blocks = []
    for name, msgs in by_account.items():
        blocks.append(f"[{name}]:\n" + "\n".join(msgs[:15]))
    corpus = "\n\n".join(blocks) if blocks else "（本周三个账号均无新增内容）"

    # 动态构造「每账号一个小标题 + 分组列表」模板，覆盖全部监控账号
    parts = []
    for i, nm in enumerate(monitored_names):
        parts.append(f"### {nm}")
        parts.append("**《作品A》**")
        parts.append("- ① <该作品下动态1>")
        parts.append("- ② <该作品下动态2>")
        parts.append("**《作品B》**")
        parts.append("- ① <该作品下动态1>")
        parts.append("（若本周无更新：- 本周无更新）")
        if i < len(monitored_names) - 1:
            parts.append("——————")  # 账号间分隔线（纯文本，不用 > 避免企微左侧竖线）
    account_tpl = "\n".join(parts)
    prompt = (
        "你是游戏行业社媒观察员。基于给定的一周内容，输出一份结构清晰、可直接推送企业微信的中文周报。\n"
        "严格按以下格式输出（用三级标题 ### 分隔每个账号；账号内按作品/主题分组，"
        "每组先写一行加粗作品名 **《作品名》**，下面用无序列表 - 配合带圈序号 ① ② ③ 逐条列出；"
        "不要表格、不要「整体趋势」段落、不要 emoji 标题）：\n"
        "## 📌 主要内容\n"
        f"{account_tpl}\n"
        "要求：\n"
        "1. 账号名称必须严格使用输入中给出的昵称，禁止输出任何 @账号ID。\n"
        "2. 不要重复「本周概览」里已有的原创/转发/引用/回复条数统计，只描述内容本身。\n"
        "3. 当某账号围绕多个作品/主题有多条动态时，必须按作品分组（如《剑星》、NIKKE），"
        "同一作品的多条用 ① ② ③ 逐条列出，不同组之间空一行；不要所有动态平铺成一大段。\n"
        "4. 用具体、可感知的语言描述该账号本周实际发了/转了什么：点名具体游戏、作品、角色、活动、公告，"
        "或说明转发的是谁的什么内容。不要用『数条XX资讯』『若干动态』这类一笔带过的表述。\n"
        "5. 禁止使用抽象概括性表述（如『关注跨平台移植』『IP联动营销』『关注行业动态』『营销动作』等空泛词），"
        "只写看得见的具体事实。\n"
        "6. 若本周该账号无任何新增内容，该账号下写「- 本周无更新」。\n"
        "7. 每条动态控制在 60 字以内（一行能看完）。\n"
        "8. 语言精炼、干货、可直接阅读；总长度控制在 600 字以内。\n"
        "9. 术语规范：Stellar Blade 统一译为《剑星》，禁止用《星刃》；NIKKE 直接使用英文 NIKKE，不要写成《妮姬》或《胜利女神：妮姬》。\n"
        "10. 账号之间用纯文本横线 ———— 分隔，不要用 > 引用块格式（企微引用块会带左侧竖线）。"
    )
    analysis = _chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"本周各账号内容如下：\n{corpus}"},
    ], temperature=0.3, max_tokens=900)

    if not analysis:
        lines = ["## 📌 主要内容"]
        for nm in monitored_names:
            lines.append(f"### {nm}")
            lines.append("- 本周无更新" if nm not in by_account else "- 详见看板")
        analysis = "\n".join(lines)

    # 双保险：即使 LLM 仍写出 @ID，推送前也强制替换成昵称。
    for handle, name in display_names.items():
        analysis = analysis.replace(f"@{handle}", name)

    return analysis
