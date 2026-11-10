"""cabin-integ / 飞书通知：群自定义机器人 Webhook。

一档：卡片（interactive）；二档：同一 Webhook 推纯文本；三档：界面内模拟卡片（未配置 Webhook）。
"""
from __future__ import annotations

import os

import httpx

from ..config import get_settings


def build_card(title: str, fields: list[tuple[str, str]], link: str = "", template: str = "red") -> dict:
    els = [{"tag": "div", "fields": [{"is_short": False, "text": {"tag": "lark_md", "content": f"**{k}**：{v}"}}
                                     for k, v in fields]}]
    if link:
        els.append({"tag": "action", "actions": [{"tag": "button", "type": "primary", "url": link,
                                                  "text": {"tag": "plain_text", "content": "查看报告 / 证据"}}]})
    return {"msg_type": "interactive", "card": {"header": {"template": template,
                                                           "title": {"tag": "plain_text", "content": title}},
                                                "elements": els}}


async def notify(title: str, fields: list[tuple[str, str]], link: str = "", template: str = "red") -> dict:
    """返回 {tier, ok, title, fields, note}，界面据此显示真实/模拟卡片。"""
    s = get_settings()
    res = {"title": title, "fields": fields, "link": link}
    if not s.feishu_webhook:
        return {**res, "tier": "simulated", "ok": True, "note": "未配置 FEISHU_WEBHOOK，界面内模拟卡片"}
    async with httpx.AsyncClient(timeout=15, trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
        try:
            r = await c.post(s.feishu_webhook, json=build_card(title, fields, link, template))
            j = r.json()
            if j.get("code", j.get("StatusCode", 0)) == 0:
                return {**res, "tier": "card", "ok": True}
            note = f"卡片推送失败：{j.get('msg')}"
        except Exception as e:
            note = f"卡片推送失败：{type(e).__name__}"
        try:
            text = title + "\n" + "\n".join(f"{k}：{v}" for k, v in fields) + (f"\n{link}" if link else "")
            r = await c.post(s.feishu_webhook, json={"msg_type": "text", "content": {"text": text}})
            if r.json().get("code", 0) == 0:
                return {**res, "tier": "text", "ok": True, "note": note + "，已降级为纯文本"}
        except Exception:
            pass
    return {**res, "tier": "simulated", "ok": False, "note": note + "，界面内模拟卡片"}
