"""规范包编辑接口（/api/skills）：查看、预览（校验 + 自检规则在最近用例上的命中）、保存、历史、查看某一版、回滚。

存放在测试代码 git 仓库 _skills/，每次保存一个提交（作者是登录用户，说明必填）。
权限：默认包只有管理员能改；模块包是该模块成员（modules.yaml 的 members）和管理员能改；
没配管理员（CABIN_ADMINS 为空）时所有登录用户都能改，页面会提示。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import modules, planner, skills
from .auth import current_user
from .caselib import get_caselib
from .config import get_settings
from .store import get_store

router = APIRouter(prefix="/api/skills")
RE_FILE = re.compile(r"^[\w一-鿿\-]{1,30}\.md$")
PREVIEW_CASES = 300
_lock = asyncio.Lock()


class SaveReq(BaseModel):
    text: str
    base_sha: str = ""  # 读取时的内容哈希；新建文件留空
    message: str


class PreviewReq(BaseModel):
    text: str


class TrialReq(BaseModel):
    text: str  # 草稿全文
    code: str = ""  # 用哪个模块的需求点试；模块包缺省取它的第一个模块，默认包必填
    reqs: list[dict] = []  # 手填的需求点 [{section, quote, summary}]；为空时取该模块最近一版 PRD 的需求点


class SuggestReq(BaseModel):
    text: str
    limit: int = 30


class RollbackReq(BaseModel):
    rev: str
    message: str = ""


def _user(req: Request) -> str:
    u = current_user(req)
    if not u:
        raise HTTPException(401, "请先登录")
    return u.name


def _admins() -> tuple[str, ...]:
    return get_settings().admins


def can_edit(name: str, mods: list[str]) -> tuple[bool, str]:
    """能不能改一份适用于 mods 的规范包；不能时给原因。"""
    admins = _admins()
    if not admins or name in admins:
        return True, ""
    if not mods or "*" in mods:
        return False, "默认包影响所有模块，只有管理员能改"
    outside = [m for m in mods if name not in modules.get(m).members]
    if outside:
        return False, f"你不是模块 {'、'.join(outside)} 的成员（modules.yaml 的 members），改不了"
    return True, ""


def _path(file: str) -> Path:
    if not RE_FILE.fullmatch(file) or file.lower() == "readme.md":
        raise HTTPException(400, "文件名只能用中文、字母、数字、下划线、连字符，以 .md 结尾，最长 30 字")
    return skills.skills_dir() / file


def _git(*args: str) -> str:
    return planner._git(*args) if planner._ensure_repo() else ""


def _show(rev: str, file: str) -> str:
    """某一版的原文（不去掉首尾空白：哈希要和当时保存的内容一致）。"""
    import subprocess
    r = subprocess.run(["git", "-C", str(planner.plans_dir()), "show", f"{rev}:{skills.REPO_SUBDIR}/{file}"],
                       capture_output=True, text=True, encoding="utf8", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def _history(file: str, limit: int = 30) -> list[dict[str, Any]]:
    """这个文件的每一版：提交、作者、时间、说明、内容哈希。"""
    rel = f"{skills.REPO_SUBDIR}/{file}"
    try:
        out = _git("log", f"-{limit}", "--format=%H%x1f%an%x1f%at%x1f%s", "--", rel)
    except RuntimeError:
        return []
    rows = []
    for line in filter(None, out.splitlines()):
        h, who, at, msg = line.split("\x1f")
        try:
            sha = skills.parse(_show(h, file), file).sha
        except RuntimeError:  # 这一版删除了文件
            sha = ""
        rows.append({"rev": h, "short": h[:8], "author": who, "at": float(at), "message": msg, "sha": sha})
    return rows


def _commit(file: str, message: str, author: str) -> str:
    rel = f"{skills.REPO_SUBDIR}/{file}"
    if not planner._ensure_repo():
        return ""
    _git("add", rel)
    if not _git("status", "--porcelain", rel):
        return ""
    _git("commit", "-q", "-m", message[:200], f"--author={author} <{author}@cabin.local>", "--", rel)
    return _git("rev-parse", "HEAD")


def _recent_cases(mods: list[str]) -> list:
    cases = get_caselib().library()
    if mods and "*" not in mods:
        cases = [c for c in cases if modules.code_of_case(c.case_id) in mods]
    return cases[-PREVIEW_CASES:]


def preview(text: str, file: str = "") -> dict[str, Any]:
    """校验 + 每条自检规则在最近已入库用例上的命中：标出的如果是好用例，说明规则写宽了。"""
    sk = skills.parse(text, file)
    cases = _recent_cases(sk.modules)
    rules = []
    for r in sk.rules:
        one = skills.Skill(name="", file="", modules=sk.modules, rules=[r])
        hits = [c for c in cases if skills.check(c, one)]
        rules.append({"field": r.field, "msg": r.msg, "hits": len(hits),
                      "samples": [{"case_id": c.case_id, "title": c.title, "value": str(getattr(c, r.field, ""))[:80]}
                                  for c in hits[:5]]})
    return {"name": sk.name, "modules": sk.modules, "version": sk.version, "sha": sk.sha, "problems": sk.problems,
            "rules": rules, "checked_cases": len(cases),
            "chars": {"extract": len(sk.extract), "write": len(sk.write), "examples": len(sk.examples)},
            "max_chars": skills.MAX_SECTION}


@router.get("")
async def list_skills(req: Request):
    me = _user(req)
    stats = skills.review_stats(get_store())
    files = []
    for x in skills.load_all():
        ok, why = can_edit(me, x.modules)
        files.append({"name": x.name, "file": x.file, "modules": x.modules, "version": x.version, "sha": x.sha,
                      "rules": len(x.rules), "problems": x.problems, "can_edit": ok, "why": why})
    mods = []
    for m in modules.all_modules():
        sk = skills.for_module(m.code)
        mods.append({"code": m.code, "name": m.name, "members": list(m.members),
                     "skill": sk.meta() if sk else None, "review": stats.get(sk.sha) if sk else None})
    return {"me": me, "admins": list(_admins()), "open_to_all": not _admins(), "files": files, "modules": mods,
            "max_chars": skills.MAX_SECTION, "fields": list(skills.FIELDS)}


@router.get("/{file}")
async def get_skill(file: str, req: Request):
    me = _user(req)
    p = _path(file)
    if not p.is_file():
        raise HTTPException(404, "没有这份规范包")
    text = p.read_text("utf8")
    sk = skills.parse(text, file)
    ok, why = can_edit(me, sk.modules)
    by_part = skills.review_stats(get_store(), by_part=True)
    hist = _history(file)
    for h in hist:
        h["review"] = by_part.get(h["sha"])
    return {"file": file, "text": text, "parsed": preview(text, file), "can_edit": ok, "why": why, "history": hist,
            "review": by_part.get(sk.sha), "form": form(sk)}


def form(sk: "skills.Skill") -> dict[str, Any]:
    """页面表单用的结构化内容（页面按这个格式拼回 Markdown 保存）。"""
    return {"name": sk.name, "modules": sk.modules, "version": sk.version, "extract": sk.extract, "write": sk.write,
            "examples": sk.examples,
            "checks": [{"field": r.field, "mode": "forbid" if r.forbid else "require", "pattern": r.forbid or r.require,
                        "msg": r.msg, "suggest": r.suggest, "scope": r.scope} for r in sk.rules]}


@router.post("/{file}/preview")
async def preview_skill(file: str, body: PreviewReq, req: Request):
    _user(req)
    _path(file)
    return preview(body.text, file)


@router.get("/{file}/rev/{rev}")
async def show_rev(file: str, rev: str, req: Request):
    """某一版的全文（看差异由页面比对）。"""
    _user(req)
    _path(file)
    if not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        raise HTTPException(400, "版本号不对")
    try:
        return {"rev": rev, "text": _show(rev, file)}
    except RuntimeError as e:
        raise HTTPException(404, f"没有这一版：{e}") from e


async def _save(file: str, text: str, base_sha: str, message: str, me: str) -> dict[str, Any]:
    p = _path(file)
    if not message.strip():
        raise HTTPException(400, "请写改动说明（会记在历史里）")
    new = skills.parse(text, file)
    if new.problems:
        raise HTTPException(400, "；".join(new.problems))
    async with _lock:
        cur = skills.parse(p.read_text("utf8"), file) if p.is_file() else None
        if (cur.sha if cur else "") != base_sha:
            raise HTTPException(409, "这份规范包在你打开之后被别人改过了，请刷新后再改")
        for mods in ([cur.modules] if cur else []) + [new.modules]:  # 原来和改后适用的模块都要有权限
            ok, why = can_edit(me, mods)
            if not ok:
                raise HTTPException(403, why)
        taken = {m for s in skills.load_all() if s.file != file for m in s.modules if m != "*"}
        clash = sorted(set(new.modules) & taken)
        if clash:
            raise HTTPException(400, f"模块 {'、'.join(clash)} 已经有规范包了，一个模块只能有一份")
        if "*" in new.modules and any(s.is_default for s in skills.load_all() if s.file != file):
            raise HTTPException(400, "已经有默认包了")
        p.write_text(text, "utf8")
        try:
            async with planner._git_lock:  # 测试代码也往这个仓库提交
                rev = await asyncio.to_thread(_commit, file, f"规范包 {new.name}：{message.strip()}", me)
        except RuntimeError as e:
            rev = ""
            get_store().append("system", "skill.commit_failed", {"file": file, "error": str(e)})
    get_store().append("system", "skill.saved", {"file": file, "by": me, "sha": new.sha, "rev": rev, "at": time.time()})
    return {"file": file, "sha": new.sha, "rev": rev, "version": new.version}


@router.put("/{file}")
async def save_skill(file: str, body: SaveReq, req: Request):
    return await _save(file, body.text, body.base_sha, body.message, _user(req))


@router.post("/{file}/rollback")
async def rollback(file: str, body: RollbackReq, req: Request):
    """把某一版的内容存成新的一版（不改写历史）。"""
    me = _user(req)
    old = (await show_rev(file, body.rev, req))["text"]
    p = _path(file)
    cur = skills.parse(p.read_text("utf8"), file).sha if p.is_file() else ""
    return await _save(file, old, cur, body.message or f"回滚到 {body.rev[:8]}", me)


# ---------- 第二期：试生成对比、按驳回原因建议规则（都会调用模型，由页面按钮手动触发） ----------
TRIAL_REQS = 3


def _sample_reqs(code: str) -> tuple[list, str]:
    """该模块最近一版 PRD 的前几个需求点。"""
    from cabin_common.schema import PrdVersion
    for r in get_store().q("SELECT doc FROM prd_version ORDER BY created DESC LIMIT 200"):
        v = PrdVersion.model_validate_json(r["doc"])
        if v.module_code == code and v.requirements:
            return v.requirements[:TRIAL_REQS], f"{v.name} {v.version}"
    return [], ""


def _case_view(c) -> dict[str, Any]:
    return {"req_id": c.req_id, "title": c.title, "type": c.type, "precondition": c.precondition, "steps": c.steps,
            "utterance": c.utterance, "expected": c.expected, "expect_screen": c.expect_screen, "expect_tts": c.expect_tts,
            "checks": [x.model_dump() for x in c.checks], "suggestion": c.suggestion}


@router.post("/{file}/trial")
async def trial(file: str, body: TrialReq, req: Request):
    """同一组需求点，用当前生效的规范和这份草稿各生成一次，并排对比（不入库）。"""
    from cabin_common.schema import Requirement

    from . import casegen
    _user(req)
    _path(file)
    draft = skills.parse(body.text, file)
    if draft.problems:
        raise HTTPException(400, "；".join(draft.problems))
    code = (body.code or next((m for m in draft.modules if m != "*"), "")).upper()
    if not code:
        raise HTTPException(400, "默认包要选一个模块来试")
    if body.reqs:
        reqs = [Requirement(req_id=f"REQ-{i:02d}", section=str(x.get("section") or ""), quote=str(x.get("quote") or "")[:300],
                            summary=str(x.get("summary") or x.get("quote") or "")[:30]) for i, x in enumerate(body.reqs[:5], 1)
                if str(x.get("quote") or "").strip()]
        source = "手填"
    else:
        reqs, source = _sample_reqs(code)
    if not reqs:
        raise HTTPException(400, f"模块 {code} 还没有 PRD 需求点，请手填一两个需求点再试")
    saved = {s_.file: s_ for s_ in skills.load_all()}
    base = next((x for x in saved.values() if x.is_default), None)
    own = next((x for x in saved.values() if code in x.modules and not x.is_default), None)
    old = skills.combine(base, own)
    new = skills.combine(draft, own) if draft.is_default else skills.combine(base if base and base.file != file else None, draft)
    mod = modules.get(code).name
    try:
        a, b = await asyncio.gather(casegen.draft_cases(reqs, mod, old), casegen.draft_cases(reqs, mod, new))
    except ValueError as e:
        raise HTTPException(502, f"模型没有给出可用结果：{e}") from e
    flag = lambda cs: sum(1 for c in cs if c.checks)  # noqa: E731
    return {"code": code, "source": source,
            "reqs": [{"req_id": r.req_id, "section": r.section, "quote": r.quote} for r in reqs],
            "old": {"skill": old.meta() if old else None, "cases": [_case_view(c) for c in a], "flagged": flag(a)},
            "new": {"skill": new.meta() if new else None, "cases": [_case_view(c) for c in b], "flagged": flag(b)}}


SYS_SUGGEST = """你在帮测试团队改进“AI 生成测试用例”的编写规范。下面是当前规范，以及最近被审核人驳回或修改的用例（含原因和改动）。
找出反复出现的问题，提出对规范的补充。只输出一个 JSON：
{"summary": "一句话总结主要问题",
 "extract": ["补充到“抽取”节的规则，每条一句"], "write": ["补充到“编写”节的规则，每条一句"],
 "examples": ["补充的正反示例（一条一个 JSON 用例或一句说明）"],
 "checks": [{"field": "title|precondition|steps|expected|utterance|expect_screen|expect_tts|page", "mode": "forbid|require",
             "pattern": "Python 正则", "msg": "提示语（≤30 字）", "suggest": "修改|驳回", "scope": "all|voice|ui", "why": "依据哪些驳回"}]}
规则：
- 只提有驳回 / 修改记录支撑的建议，不要泛泛而谈；当前规范已经写了的不要重复。
- 自检规则要能被正则判定（如“期望播报不能只写好的”），正则尽量简单，不要用嵌套重复。
- 每类最多 5 条。"""


def _eval_samples(mods: list[str], limit: int) -> list[dict[str, Any]]:
    """该模块近期的驳回 / 修改记录（评估集）。"""
    import json as _json
    out = []
    for r in get_store().q("SELECT defect_id, reason, doc FROM eval_set WHERE rule='case-gen' ORDER BY at DESC LIMIT 500"):
        if mods and "*" not in mods and modules.code_of_case(r["defect_id"]) not in mods:
            continue
        doc = _json.loads(r["doc"] or "{}")
        c = doc.get("case") or {}
        out.append({"case_id": r["defect_id"], "reason": r["reason"], "diff": doc.get("diff") or {},
                    "case": {k: c.get(k) for k in ("title", "type", "precondition", "steps", "utterance", "expected",
                                                   "expect_screen", "expect_tts", "channel")}})
        if len(out) >= limit:
            break
    return out


@router.post("/{file}/suggest")
async def suggest(file: str, body: SuggestReq, req: Request):
    """按近期驳回 / 修改记录让模型提出规范补充；自检规则先校验，再算能拦住几条被驳回的、会误伤几条已通过的。人工勾选后并入草稿。"""
    import json as _json

    from cabin_common.schema import TestCase

    from . import casegen
    _user(req)
    _path(file)
    draft = skills.parse(body.text, file)
    samples = _eval_samples(draft.modules, max(5, min(body.limit, 60)))
    if not samples:
        raise HTTPException(400, "这个模块还没有被驳回或修改过的用例，没有可参考的记录")
    prompt = (f"当前规范（{draft.name}）：\n{body.text[:6000]}\n\n最近被驳回 / 修改的用例（{len(samples)} 条）：\n"
              + "\n".join(_json.dumps(x, ensure_ascii=False) for x in samples)[:20000])
    try:
        got = await casegen._llm_json(SYS_SUGGEST, prompt, "skills.suggest", "skills.suggest", max_tokens=4000)
    except ValueError as e:
        raise HTTPException(502, f"模型没有给出可用结果：{e}") from e
    bad_cases = []
    for x in samples:
        if x["reason"] == "修改后通过":
            continue
        try:
            bad_cases.append(TestCase(case_id=x["case_id"], title=x["case"].get("title") or "",
                                      channel=x["case"].get("channel") or [],
                                      **{k: v for k, v in x["case"].items() if k not in ("title", "channel") and v is not None}))
        except ValueError:  # 旧记录字段不合法：只用来给模型看，不参与命中统计
            continue
    passed = _recent_cases(draft.modules)
    checks = []
    for c in (got.get("checks") or [])[:5]:
        if not isinstance(c, dict):
            continue
        field_, mode, pattern = c.get("field"), c.get("mode"), str(c.get("pattern") or "")
        problem = ("字段不认识" if field_ not in skills.FIELDS else "方式应为 forbid / require" if mode not in ("forbid", "require")
                   else skills.unsafe_pattern(pattern) and f"正则{skills.unsafe_pattern(pattern)}")
        item = {"field": field_, "mode": mode, "pattern": pattern, "msg": str(c.get("msg") or "")[:80],
                "suggest": c.get("suggest") if c.get("suggest") in ("修改", "驳回", "确认", "转需求方") else "修改",
                "scope": c.get("scope") if c.get("scope") in ("all", "voice", "ui") else "all", "why": str(c.get("why") or "")[:120],
                "problem": problem or ""}
        if not problem:
            rule = skills.Rule(field=field_, msg=item["msg"] or "x", suggest=item["suggest"], scope=item["scope"],
                               **{mode: pattern})
            one = skills.Skill(name="", file="", modules=draft.modules, rules=[rule])
            item["catches"] = sum(1 for x in bad_cases if skills.check(x, one))
            item["false_hits"] = sum(1 for x in passed if skills.check(x, one))
        checks.append(item)
    lines = lambda k: [str(x)[:300] for x in (got.get(k) or [])[:5] if str(x).strip()]  # noqa: E731
    return {"summary": str(got.get("summary") or "")[:200], "extract": lines("extract"), "write": lines("write"),
            "examples": lines("examples"), "checks": checks, "samples": len(samples), "rejected": len(bad_cases),
            "passed_checked": len(passed)}
