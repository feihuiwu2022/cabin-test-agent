"""用例编写规范包：按模块的领域规则、正反示例、额外自检规则，改完保存即生效。

存放在测试代码 git 仓库（data/plans/_skills/*.md），每次在网页上保存就是一次提交（作者、说明、时间），
历史、差异、回滚都靠 git。首次使用时从 apps/cloud/casegen_skills/（出厂默认）复制过去。

一份规范包是一个 Markdown 文件，开头 YAML 声明适用模块和额外自检规则，正文分节：
  ---
  name: 空调
  modules: [HVAC]          # 用 modules.yaml 的模块缩写；["*"] 表示默认包（没有专门规范包的模块用它）
  version: 1               # 改了规则就加 1，审核统计按版本分开看
  checks:                  # 额外自检（确定性，不调模型）：字段命中 forbid 或缺 require 就提示
    - {field: expect_tts, forbid: "^(好的|已执行)[。！!]?$", msg: 期望播报只写“好的”，判不出对错, suggest: 修改, scope: voice}
  ---
  ## 抽取        → 追加到需求抽取的提示词
  ## 编写        → 追加到用例生成的提示词
  ## 示例        → 正反示例，追加到用例生成的提示词

输出格式（JSON 字段）、编号、自检的固定规则仍在代码里（casegen），规范包只能补充，改坏了也不会让解析失败。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import BACKEND_DIR

SEED_DIR = BACKEND_DIR / "casegen_skills"  # 出厂默认，只在首次使用时复制
SKILLS_DIR: Path | None = None  # 测试或特殊部署可指定；默认在测试代码仓库里
REPO_SUBDIR = "_skills"
MAX_PATTERN = 200  # 正则长度上限
FIELDS = ("title", "precondition", "steps", "expected", "utterance", "expect_screen", "expect_tts", "page")
SECTIONS = {"抽取": "extract", "编写": "write", "示例": "examples"}
MAX_SECTION = 3000  # 每节上限，防止规范包把提示词撑爆


@dataclass
class Rule:
    field: str
    msg: str
    forbid: str = ""
    require: str = ""
    suggest: str = "修改"
    scope: str = "all"  # all | voice | ui


@dataclass
class Skill:
    name: str
    file: str
    modules: list[str]
    version: int = 1
    extract: str = ""
    write: str = ""
    examples: str = ""
    rules: list[Rule] = field(default_factory=list)
    sha: str = ""
    problems: list[str] = field(default_factory=list)
    parts: dict[str, str] = field(default_factory=dict)  # 组成这份规范的文件 → 各自的内容哈希

    @property
    def is_default(self) -> bool:
        return "*" in self.modules

    def meta(self) -> dict[str, Any]:
        return {"name": self.name, "file": self.file, "version": self.version, "sha": self.sha,
                "parts": dict(self.parts or {self.file: self.sha})}  # 记在批次里，审核统计按 sha 分


def parse(text: str, file: str = "") -> Skill:
    problems: list[str] = []
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.S)
    head, body = (m.group(1), m.group(2)) if m else ("", text)
    try:
        meta = yaml.safe_load(head) or {} if head else {}
    except yaml.YAMLError as e:
        meta, problems = {}, [f"开头的 YAML 写错了：{e}"]
    if not isinstance(meta, dict):
        meta, problems = {}, problems + ["开头的 YAML 应该是键值对"]
    mods = meta.get("modules") or []
    mods = [str(x).upper() if x != "*" else "*" for x in (mods if isinstance(mods, list) else [mods])]
    if not mods:
        problems.append("没写 modules（适用的模块缩写），这份规范包不会被用到")
    rules: list[Rule] = []
    for i, r in enumerate(meta.get("checks") or [], 1):
        if not isinstance(r, dict) or r.get("field") not in FIELDS or not r.get("msg") or not (r.get("forbid") or r.get("require")):
            problems.append(f"第 {i} 条自检规则不完整：需要 field（{'/'.join(FIELDS)}）、msg，以及 forbid 或 require")
            continue
        bad = next((why for k in ("forbid", "require") if r.get(k) and (why := unsafe_pattern(str(r[k])))), "")
        if bad:
            problems.append(f"第 {i} 条自检规则的正则{bad}")
            continue
        rules.append(Rule(field=r["field"], msg=str(r["msg"])[:80], forbid=str(r.get("forbid") or ""),
                          require=str(r.get("require") or ""),
                          suggest=r.get("suggest") if r.get("suggest") in ("修改", "驳回", "确认", "转需求方") else "修改",
                          scope=r.get("scope") if r.get("scope") in ("all", "voice", "ui") else "all"))
    parts: dict[str, list[str]] = {}
    cur = ""
    for line in body.splitlines():
        h = re.match(r"^##\s+(\S+)", line)
        if h:
            cur = SECTIONS.get(h.group(1), "")
            continue
        if cur:
            parts.setdefault(cur, []).append(line)
    sec = {k: "\n".join(v).strip()[:MAX_SECTION] for k, v in parts.items()}
    try:
        version = int(meta.get("version") or 1)
    except (TypeError, ValueError):
        version, problems = 1, problems + ["version 应该是整数"]
    return Skill(name=str(meta.get("name") or Path(file).stem), file=file, modules=mods, version=version,
                 extract=sec.get("extract", ""), write=sec.get("write", ""), examples=sec.get("examples", ""), rules=rules,
                 sha=hashlib.sha256(text.encode("utf8")).hexdigest()[:10], problems=problems)


RE_NESTED = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*{]")  # (a+)+ 这类嵌套量词：可能回溯到卡死


def unsafe_pattern(p: str) -> str:
    """正则不能用的原因；能用返回空串。自检会对每条用例跑，不允许可能卡死的写法。"""
    if len(p) > MAX_PATTERN:
        return f"太长（{len(p)} 字，上限 {MAX_PATTERN}）"
    try:
        re.compile(p)
    except re.error as e:
        return f"写错了：{e}"
    if RE_NESTED.search(p):
        return "里有嵌套的重复（如 (a+)+），可能让自检卡住，请改写"
    return ""


def skills_dir() -> Path:
    """规范包目录：默认在测试代码仓库的 _skills/，首次使用时从出厂默认复制并提交。"""
    if SKILLS_DIR is not None:
        return SKILLS_DIR
    from . import planner
    d = planner.plans_dir() / REPO_SUBDIR
    if not d.is_dir():
        d.mkdir(parents=True)
        for f in sorted(SEED_DIR.glob("*.md")):
            (d / f.name).write_bytes(f.read_bytes())
        try:
            if planner._ensure_repo():
                planner._git("add", REPO_SUBDIR)
                planner._git("commit", "-q", "-m", "规范包：出厂默认")
        except Exception:  # 没有 git 也能用，只是没有历史
            pass
    return d


def load_all(root: Path | None = None) -> list[Skill]:
    """每次生成都重新读，改完保存即生效。"""
    root = root or skills_dir()
    if not root.is_dir():
        return []
    return [parse(p.read_text("utf8"), p.name) for p in sorted(root.glob("*.md")) if p.name.lower() != "readme.md"]


def for_module(code: str, root: Path | None = None) -> Skill | None:
    """默认包（modules: ["*"]）始终生效，模块专属的规范包叠加在它后面：规则、示例、自检都是两份合在一起。"""
    skills = [s for s in load_all(root) if s.modules]
    code = (code or "").upper()
    base = next((s for s in skills if s.is_default), None)
    own = next((s for s in skills if code and code in s.modules and not s.is_default), None)
    return combine(base, own)


def combine(base: Skill | None, own: Skill | None) -> Skill | None:
    """默认包 + 模块包合成一份生效的规范（试生成时用草稿替换其中一份）。"""
    if not (base and own):
        return own or base
    join = lambda a, b: "\n".join(x for x in (a, b) if x)  # noqa: E731
    return Skill(name=f"{base.name}+{own.name}", file=f"{base.file}+{own.file}", modules=own.modules,
                 version=own.version, extract=join(base.extract, own.extract), write=join(base.write, own.write),
                 examples=join(base.examples, own.examples), rules=base.rules + own.rules,
                 sha=hashlib.sha256((base.sha + own.sha).encode()).hexdigest()[:10], problems=base.problems + own.problems,
                 parts={base.file: base.sha, own.file: own.sha})


def prompt_extra(s: Skill | None, part: str) -> str:
    """追加到系统提示词后面的一段；part = extract | write。"""
    if not s:
        return ""
    if part == "extract":
        return f"\n\n本模块（{s.name}）的补充规则：\n{s.extract}" if s.extract else ""
    out = f"\n\n本模块（{s.name}）的补充规则：\n{s.write}" if s.write else ""
    return out + (f"\n\n示例（照着它的写法，不要照抄内容）：\n{s.examples}" if s.examples else "")


def check(case: Any, s: Skill | None) -> list[tuple[str, str]]:
    """额外自检：返回 [(msg, suggest)]。"""
    out = []
    for r in (s.rules if s else []):
        if (r.scope == "voice" and not case.is_voice) or (r.scope == "ui" and case.is_voice):
            continue
        v = getattr(case, r.field, "")
        text = "；".join(v) if isinstance(v, list) else str(v or "")
        if (r.forbid and re.search(r.forbid, text)) or (r.require and not re.search(r.require, text)):
            out.append((r.msg, r.suggest))
    return out


def review_stats(store, limit: int = 2000, by_part: bool = False) -> dict[str, dict[str, Any]]:
    """按规范版本汇总审核结论：通过 / 修改 / 驳回 / 转需求方，以及最常见的驳回原因。用来看改规范后质量是升是降。
    by_part=False：按生效规范（默认包 + 模块包合起来）的哈希；True：按单个文件的哈希（编辑页的历史版本用）。"""
    by_batch: dict[str, list[str]] = {}
    for r in store.q("SELECT id, doc FROM case_batch ORDER BY created DESC LIMIT ?", (limit,)):
        sk = (json.loads(r["doc"]).get("params") or {}).get("skill") or {}
        keys = list((sk.get("parts") or {}).values()) if by_part else [sk.get("sha")]
        if any(keys):
            by_batch[r["id"]] = [k for k in keys if k]
    out: dict[str, dict[str, Any]] = {}
    for r in store.q("SELECT batch_id, decision, reason FROM case_review"):
        for sha in by_batch.get(r["batch_id"], []):
            st = out.setdefault(sha, {"pass": 0, "edit": 0, "reject": 0, "to_req": 0, "reasons": {}})
            st[r["decision"]] = st.get(r["decision"], 0) + 1
            if r["decision"] == "reject" and r["reason"]:
                st["reasons"][r["reason"]] = st["reasons"].get(r["reason"], 0) + 1
    for st in out.values():
        n = st["pass"] + st["edit"] + st["reject"] + st["to_req"]
        st["total"], st["pass_rate"] = n, round(st["pass"] / n, 3) if n else 0
        st["reasons"] = sorted(st["reasons"].items(), key=lambda x: -x[1])[:5]
    return out
