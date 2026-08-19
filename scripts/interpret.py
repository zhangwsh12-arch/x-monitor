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


def interpret_weekly(daily_snapshots: list[dict]) -> str:
    """基于近 7 日快照做简洁周报，适合直接推送企业微信。"""
    by_account: dict[str, list[str]] = {}
    for snap in daily_snapshots:
        for acc in snap.get("accounts", []):
            key = f"@{acc['handle']} ({acc['name']})"
            for it in acc.get("items", []):
                txt = it.get("text_zh") or it.get("text") or ""
                if txt:
                    by_account.setdefault(key, []).append(f"[{it['kind']}] {txt[:160]}")
    if not by_account:
        return "本周监控账号无新增动态。"
    blocks = []
    for k, msgs in by_account.items():
        blocks.append(f"### {k}\n" + "\n".join(msgs[:12]))
    corpus = "\n\n".join(blocks)
    analysis = _chat([
        {"role": "system", "content": (
            "你是游戏行业社媒观察员。基于给定的一周内容，输出一份供企业微信直接推送的极简中文周报。"
            "严格遵守：总字数不超过350字；只写有新增内容的账号；每个账号只用1条项目符号，"
            "限45字以内，按“账号：核心动态；值得关注的信号”表达；最后只用1条“整体”项目符号，"
            "限50字以内。不要复述推文原文、不要背景铺陈、不要空泛评价、不要表格、不要使用标题或段落。"
            "如无真实共同趋势，整体项写“整体：账号关注点分散，暂无明确共同主题。”"
        )},
        {"role": "user", "content": corpus[:6000]},
    ], temperature=0.2, max_tokens=500)
    return analysis or "本周动态已更新；中文分析暂不可用，请查看看板。"
