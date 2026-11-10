"""cabin-cases：读用例取期望。

一档：飞书云文档（配置 FEISHU_APP_ID/SECRET/CASE_DOC 时，读 docx 纯文本并解析）
兜底：本地用例缓存 data/cases/*.yaml（飞书文档的离线副本）
另并入用例库中审核通过的用例（PRD 生成 → 审核 → 入库，见 caselib）。
"""
from __future__ import annotations

import os
import re

import httpx
import yaml

from .config import get_settings
from cabin_common.schema import Case


class CaseRepo:
    def __init__(self) -> None:
        self.s = get_settings()
        self._local: dict[str, Case] = {}
        self.load_local()

    def load_local(self) -> None:
        self._local.clear()
        for p in sorted((self.s.data_dir / "cases").glob("*.yaml")):
            doc = yaml.safe_load(p.read_text("utf8")) or {}
            src = doc.get("source", f"local:{p.name}")
            for c in doc.get("cases", []):
                c.setdefault("source", src)
                case = Case(**c)
                self._local[case.id] = case

    @property
    def feishu_enabled(self) -> bool:
        return bool(self.s.feishu_app_id and self.s.feishu_app_secret and self.s.feishu_case_doc)

    async def _feishu_cases(self) -> dict[str, Case]:
        """读飞书 docx 纯文本。约定文档内每条用例形如：
        用例编号：TC-xxx / 标题：… / 页面：… / 步骤：… / 预期结果：…
        """
        async with httpx.AsyncClient(timeout=20, trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
            t = await c.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                             json={"app_id": self.s.feishu_app_id, "app_secret": self.s.feishu_app_secret})
            token = t.json()["tenant_access_token"]
            r = await c.get(f"https://open.feishu.cn/open-apis/docx/v1/documents/{self.s.feishu_case_doc}/raw_content",
                            headers={"Authorization": f"Bearer {token}"})
            text = r.json()["data"]["content"]
        out: dict[str, Case] = {}
        for blk in re.split(r"(?=用例编号[:：])", text):
            f = dict(re.findall(r"(用例编号|标题|页面|包名|步骤|预期结果)[:：]\s*(.+)", blk))
            if "用例编号" in f and "预期结果" in f:
                cid = f["用例编号"].strip()
                out[cid] = Case(id=cid, title=f.get("标题", cid), page=f.get("页面", ""),
                                package=f.get("包名", "com.cabin.sim.settings"),
                                steps=re.split(r"[→,，]", f.get("步骤", "")), expected=f["预期结果"].strip(),
                                source=f"feishu:{self.s.feishu_case_doc}")
        return out

    @staticmethod
    def _library() -> dict[str, Case]:
        from .caselib import get_caselib
        out = {}
        for c in get_caselib().library():
            exp = c.expected if not c.is_voice else f"测试语句“{c.utterance}”；期望屏幕：{c.expect_screen}；期望播报：{c.expect_tts}"
            out[c.case_id] = Case(id=c.case_id, title=c.title, module=c.module, package=c.package, page=c.page,
                                  precondition=c.precondition, steps=c.steps, expected=exp, priority=c.priority,
                                  type=c.type, source=f"用例库:{c.batch_id}",
                                  keywords=[c.title, c.utterance, c.req_id] + c.module.split("/"))
        return out

    async def all(self) -> tuple[dict[str, Case], str]:
        """返回 (用例表, 来源说明)。飞书失败自动退回本地缓存；均并入用例库已入库用例。"""
        lib = self._library()
        if self.feishu_enabled:
            try:
                cs = await self._feishu_cases()
                if cs:
                    return {**cs, **lib}, "飞书云文档"
            except Exception as e:  # 降级
                return {**self._local, **lib}, f"本地缓存（飞书读取失败：{type(e).__name__}）"
        return {**self._local, **lib}, "本地缓存（飞书云文档离线副本）"

    async def get(self, case_id: str) -> tuple[Case | None, str]:
        cs, src = await self.all()
        return cs.get(case_id.upper()), src

    async def search(self, query: str, limit: int = 3) -> tuple[list[Case], str]:
        cs, src = await self.all()
        q = query.lower()
        scored = []
        for c in cs.values():
            hay = " ".join([c.id, c.title, c.module, c.page, " ".join(c.keywords)]).lower()
            score = 10 * (c.id.lower() in q)
            score += sum(3 for k in c.keywords if k.lower() in q)
            score += sum(1 for ch in set(q) if ch.strip() and ch in hay) * 0.05
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for s, c in scored[:limit] if s > 0.3], src


_repo: CaseRepo | None = None


def get_cases() -> CaseRepo:
    global _repo
    if _repo is None:
        _repo = CaseRepo()
    return _repo
