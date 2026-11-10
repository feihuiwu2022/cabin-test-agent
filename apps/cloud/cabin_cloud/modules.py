"""模块路由：模块缩写 → 文件夹 + 多维表格。

配置在 apps/cloud/modules.yaml（CABIN_MODULES_FILE 可改路径），保存即生效。文件缺失或写错时退回内置默认，
问题通过 problems() 和 /api/modules 提示，不影响闭环。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import get_settings

RE_CODE = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
RE_FOLDER_BAD = re.compile(r'[\\:*?"<>|]|^\.|/\.|^/|//')

# 与 modules.yaml 出厂内容一致；文件缺失时使用
_BUILTIN: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("VOICE", "语音助手", ("语音",)), ("DISP", "显示", ("显示",)), ("MEDIA", "媒体", ("媒体", "音乐")),
    ("SND", "声音", ("声音", "音效")), ("SYS", "系统", ("系统",)), ("NAV", "导航", ("导航",)),
    ("HVAC", "空调", ("空调",)), ("VEH", "车控", ("车控",)), ("BT", "蓝牙", ("蓝牙",)), ("PHONE", "电话", ("电话",)),
    ("CLU", "仪表", ("仪表",)), ("SEAT", "座椅", ("座椅",)), ("ACCT", "账号", ("账号",)), ("SET", "设置", ("设置",)),
)


@dataclass(frozen=True)
class Module:
    code: str
    name: str
    folder: str
    keywords: tuple[str, ...] = ()
    bitable: str = ""  # 空 = 默认表（FEISHU_BITABLE_URL）
    listed: bool = True  # False：没在 modules.yaml 里列出，按缩写兜底
    members: tuple[str, ...] = ()  # 模块成员：定时任务没有发现人时，待审核归他们

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "folder": self.folder, "keywords": list(self.keywords),
                "bitable": self.bitable, "bitable_effective": bitable_url(self.code), "listed": self.listed,
                "members": list(self.members)}


_cache: dict = {"key": None, "mods": (), "problems": []}


def _path() -> Path:
    return get_settings().modules_file


def _parse(raw: object) -> tuple[list[Module], list[str]]:
    mods: list[Module] = []
    errs: list[str] = []
    items = (raw or {}).get("modules") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        return [], ["modules.yaml 里没有 modules 列表"]
    seen: set[str] = set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errs.append(f"第 {i} 项不是 {{code, name, ...}} 格式")
            continue
        code = str(it.get("code") or "").strip().upper()
        if not RE_CODE.match(code):
            errs.append(f"第 {i} 项 code “{code}” 应为 1~8 位大写字母/数字，字母开头")
            continue
        if code in seen:
            errs.append(f"code {code} 重复，只取第一项")
            continue
        folder = str(it.get("folder") or code).strip().strip("/")
        if RE_FOLDER_BAD.search(folder) or ".." in folder.split("/"):
            errs.append(f"{code} 的 folder “{folder}” 不合法（不能含 .. \\ : * ? \" < > |），改用 {code}")
            folder = code
        url = str(it.get("bitable") or "").strip()
        if url:
            from .integ.bitable import BitableError, parse_url
            try:
                parse_url(url)
            except BitableError:
                errs.append(f"{code} 的 bitable 不是多维表格链接，先按默认表处理")
                url = ""
        kws = it.get("keywords") or []
        kws = tuple(str(k).strip() for k in (kws if isinstance(kws, list) else [kws]) if str(k).strip())
        seen.add(code)
        who = it.get("members") or []
        who = tuple(str(m).strip() for m in (who if isinstance(who, list) else [who]) if str(m).strip())
        mods.append(Module(code=code, name=str(it.get("name") or code).strip(), folder=folder, keywords=kws,
                           bitable=url, members=who))
    return mods, errs


def _load() -> tuple[tuple[Module, ...], list[str]]:
    p = _path()
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        key = (str(p), None)
    if _cache["key"] == key:
        return _cache["mods"], _cache["problems"]
    mods: list[Module] = []
    problems: list[str] = []
    if key[1] is None:
        problems.append(f"没找到 {p.name}，使用内置模块表")
    else:
        try:
            mods, problems = _parse(yaml.safe_load(p.read_text("utf8")))
        except yaml.YAMLError as e:
            problems = [f"{p.name} 格式错误：{str(e).splitlines()[0]}"]
    if not mods:
        mods = [Module(code=c, name=n, folder=n, keywords=k) for c, n, k in _BUILTIN]
        if key[1] is not None and not problems:
            problems.append("modules.yaml 没有可用的模块，使用内置模块表")
    _cache.update(key=key, mods=tuple(mods), problems=problems)
    return _cache["mods"], problems


def reload() -> None:
    _cache["key"] = None


def all_modules() -> list[Module]:
    return list(_load()[0])


def problems() -> list[str]:
    return list(_load()[1])


def get(code: str) -> Module:
    code = (code or "").upper()
    for m in _load()[0]:
        if m.code == code:
            return m
    return Module(code=code or "GEN", name=code or "GEN", folder=code or "GEN", listed=False)


def code_of_case(case_id: str) -> str:
    parts = (case_id or "").upper().split("-")
    return parts[1] if len(parts) >= 3 else ""


def of_case(case_id: str) -> Module:
    return get(code_of_case(case_id))


def code_for(module_path: str, suggested: str = "") -> str:
    """PRD 的模块路径 → 模块缩写：逐段按关键词匹配（先匹配到的生效），再整串匹配，最后用模型建议的缩写。"""
    segs = [x for x in (module_path or "").split("/") if x]
    if len(segs) > 1 and segs[0].startswith("设置"):  # “设置/显示/亮度” 按二级模块编号
        segs = segs[1:]
    mods = _load()[0]
    for seg in segs:
        for m in mods:
            if any(seg.startswith(k) for k in m.keywords):
                return m.code
    for m in mods:
        if any(k in (module_path or "") for k in m.keywords):
            return m.code
    return re.sub(r"[^A-Z0-9]", "", (suggested or "GEN").upper())[:8] or "GEN"


def find(text: str) -> str:
    """按缩写 / 模块名 / 文件夹名找模块（拉取过滤用）；找不到返回空串。"""
    t = (text or "").strip()
    if not t:
        return ""
    for m in _load()[0]:
        if t.upper() == m.code or t in (m.name, m.folder):
            return m.code
    return ""


def bitable_url(code: str) -> str:
    """该模块用例写哪张表：模块自己的表，否则默认表（可能为空 = 只落本地）。"""
    return get(code).bitable or get_settings().feishu_bitable_url
