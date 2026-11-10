"""用例闭环编排：对话导入 PRD → 需求点 → 用例 + 自检 → 审核 → 入库通知 → 拉取 → 测试代码。

网关把对话里的文件 / 链接 / 指令交给这里；进度通过 WebSocket 推给界面（文档工作台、追溯表、审核台、代码卡片）。
生成在后台任务里跑，不随下一句对话被打断。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

import httpx
from pathlib import Path
from typing import Protocol

from cabin_common import vehicles
from . import modules, planner, rigplan
from .bus import bus
from .casegen import PrdBusy, VersionConflict, coverage, find_existing, generate, reqs_msg
from .caselib import IN_LIBRARY, default_models, get_caselib, model_code, reject_reasons
from .casexls import RE_XLS_SAMPLE, XLSX_EXT, import_workbook, sample_xlsx
from .config import BACKEND_DIR, get_settings
from .integ.bitable import BitableClient, all_bitables, get_bitable, sync_back, table_key
from .integ.feishu import notify
from .prd import (RE_BITABLE, PrdDoc, PrdError, find_ref, find_section, import_word, load_feishu_token, load_path,
                  load_url, uploads_dir, word_import_folder)
from .store import get_store

# 审核通过即自动生成测试代码并派台架首次试跑；“拉取”是台架侧的事（A-15 / 认领），不在用例流水线里
CASE_STAGES = ["导入 PRD", "需求提取", "用例生成", "AI 自检", "用例审核", "入库通知", "测试代码", "首次试跑"]
SAMPLES_DIR = BACKEND_DIR / "data" / "samples"
RE_SAMPLE = re.compile(r"(示例|样例|演示)\s*(PRD|需求|文档)", re.I)
RE_GEN = re.compile(r"(PRD|需求文档|需求).{0,12}(转成|生成|写).{0,6}用例|生成.{0,4}用例|导入.{0,4}(PRD|需求)", re.I)
RE_PULL = re.compile(r"拉取.{0,12}用例|用例.{0,4}拉取")
RE_CODE = re.compile(r"生成.{0,6}(测试代码|TaskPlan|脚本)|测试代码", re.I)
RE_SUBMIT = re.compile(r"(采纳|按).{0,4}建议.{0,6}提交|提交.{0,4}审核")
RE_SYNC = re.compile(r"同步.{0,6}(多维表格|飞书|用例库)|从.{0,4}(多维表格|飞书).{0,4}(同步|读取|拉取)")
RE_VERSION = re.compile(r"(?:版本号?|[Vv](?=\d))\s*[:：=]?\s*[Vv]?(\d+(?:\.\d+){1,3})")
RE_FORCE = re.compile(r"重新生成|强制生成|再生成一[次遍]")
RE_AS_REV = re.compile(r"作为修订|按修订|当作修订|修订版")
RE_CANCEL = re.compile(r"^(取消|算了|不用了|先不)")
RE_BASELINE = re.compile(r"(PRD-[A-Z0-9]+-[A-Z0-9]+@[\w.\-]+)")


class Sess(Protocol):
    id: str
    state: dict

    async def send(self, msg: dict) -> None: ...

    async def log(self, text: str, kind: str = "") -> None: ...

    async def say(self, text: str, src: str = "") -> None: ...


class LiveSession:
    """后台任务用：按会话编号找当前连接，刷新页面后新连接接着收进度。没有连接时消息丢弃，结果都在用例库里。"""

    def __init__(self, s: Sess):
        self.origin, self.id, self.state = s, s.id, s.state
        self.user = getattr(s, "user", "")

    @property
    def _now(self) -> Sess:
        return bus.sessions.get(self.id) or self.origin  # type: ignore[return-value]

    async def send(self, msg: dict) -> None:
        await self._now.send(msg)

    async def log(self, text: str, kind: str = "") -> None:
        await self._now.log(text, kind)

    async def say(self, text: str, src: str = "") -> None:
        await self._now.say(text, src)


async def stage(s: Sess, n: int) -> None:
    await s.send({"type": "stage", "flow": "case", "stage": n, "name": CASE_STAGES[n]})


def sample_path() -> Path | None:
    files = sorted(SAMPLES_DIR.glob("*.docx")) + sorted(SAMPLES_DIR.glob("*.md"))
    return files[0] if files else None


def wants(text: str, has_attachment: bool) -> str:
    """判断这句话是否属于用例闭环：import / pull / code / submit / ''。"""
    if RE_BITABLE.search(text):
        return "bitable"
    if has_attachment or find_ref(text) or RE_SAMPLE.search(text) or RE_XLS_SAMPLE.search(text):
        return "import"
    if RE_SYNC.search(text):
        return "sync"
    if RE_SUBMIT.search(text):
        return "submit"
    if RE_PULL.search(text):
        return "pull"
    if RE_CODE.search(text):
        return "code"
    if RE_GEN.search(text):
        return "import"
    return ""


# ---------- 1. 导入 + 生成 ----------
async def load_doc(text: str, attachment: dict | None) -> PrdDoc:
    if attachment:
        p = uploads_dir() / Path(attachment.get("id", "")).name
        if not p.is_file():
            raise PrdError("上传的文件已失效，请重新上传")
        doc = load_path(str(p))
        doc.name, doc.source = attachment.get("name") or doc.name, f"upload:{attachment.get('name')}"
        return doc
    ref = find_ref(text)
    if ref and ref[0] == "url":
        return await load_url(ref[1])
    if ref:
        return load_path(ref[1])
    sp = sample_path()
    if RE_SAMPLE.search(text) and sp:
        doc = load_path(str(sp))
        doc.source = f"sample:{sp.name}"
        return doc
    raise PrdError("请上传 PRD 文件（📎），或在对话里贴本机路径 / 在线文档链接")


async def _xlsx_source(text: str, attachment: dict | None) -> tuple[bytes, str, str] | str | None:
    """用例表来源：(内容, 文件名, 来源)；不是用例表返回 None；有问题返回提示语。"""
    if attachment:
        name, path = attachment.get("name", ""), uploads_dir() / Path(attachment.get("id", "")).name
        source = f"upload:{name}"
    else:
        ref = find_ref(text)
        if ref:
            name, path, source = ref[1], ref[1], f"{ref[0]}:{ref[1]}"
        elif RE_XLS_SAMPLE.search(text):
            p = sample_xlsx()
            return (p.read_bytes(), p.name, f"sample:{p.name}") if p.is_file() else "没有找到示例用例表。"
        else:
            return None
    ext = Path(str(name).split("?")[0]).suffix.lower()
    if ext == ".xls":
        return "暂不支持旧版 .xls，请在表格软件里另存为 .xlsx 后再导入。"
    if ext not in XLSX_EXT:
        return None
    if source.startswith("url:"):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(str(path))
                r.raise_for_status()
            return r.content, Path(str(name).split("?")[0]).name, source
        except httpx.HTTPError as e:
            return f"用例表下载失败：{type(e).__name__}"
    p = Path(path).expanduser()
    if not p.is_file():
        return f"本机找不到文件：{p}"
    return p.read_bytes(), Path(str(name)).name, source


async def _import_xlsx(s: Sess, data: bytes, name: str, source: str) -> str:
    try:
        batch, cases, rows, bad = import_workbook(data, name, source, session=s.id, created_by=getattr(s, "user", ""))
    except ValueError as e:
        return str(e)
    await stage(s, 0)
    await s.send({"type": "casegen.doc", "name": name, "source": source, "section": "", "n_blocks": len(rows),
                  "blocks": [{"kind": "para", "text": r, "level": 0, "section": ""} for r in rows[:400]]})
    await s.log(f"<b>read_xlsx</b> 《{name}》· {len(rows)} 行 · 可导入 {len(cases)} · 不合格 {len(bad)}", "md")
    for b in bad[:10]:
        await s.log(f"<b>xlsx_row</b> 第 {b['row']} 行：{'；'.join(b['errors'])}", "err")
    get_store().append(s.id, "cases.import", {"name": name, "source": source, "n": len(cases), "bad": len(bad)})
    if not cases:
        return "这张表里没有可导入的用例。" + ("；".join(bad[0]["errors"]) if bad else "")
    await s.send({"type": "casegen.batch", "batch": batch.model_dump()})
    await s.send({"type": "casegen.reqs", "batch_id": batch.batch_id, "requirements": [r.model_dump() for r in batch.requirements],
                  "ambiguities": [], "module": batch.module, "models": batch.models})
    for c in cases:
        await s.send({"type": "casegen.case", "batch_id": batch.batch_id, "case": c.model_dump()})
    await _open_review(s, batch, cases)
    return f"已读取《{name}》：{len(cases)} 条可导入" + (f"，{len(bad)} 行不合格未导入" if bad else "") + "。"


async def start_import(s: Sess, text: str, attachment: dict | None) -> str:
    """返回立即回复的话；生成在后台进行。"""
    job = s.state.get("casegen_job")
    if job and not job.done():
        return "上一份 PRD 还在生成用例，等它完成再导入下一份。"
    xls = await _xlsx_source(text, attachment)
    if isinstance(xls, str):
        return xls
    if xls:
        return await _import_xlsx(s, *xls)
    try:
        doc = await load_doc(text, attachment)
    except PrdError as e:
        return str(e)
    note = ""
    if doc.name.lower().endswith(".docx") and doc.raw and word_import_folder():
        got = await _word_to_feishu(s, doc, force=bool(RE_FORCE.search(text)))
        if isinstance(got, str):
            return got
        doc, note = got
    section = find_section(text)
    if section and not doc.has_section(section):
        heads = "、".join(h.strip() for h in doc.outline()[:6])
        return f"《{doc.name}》里没找到 §{section}。文档的章节有：{heads}。"
    await stage(s, 0)
    blocks = [{"kind": b.kind, "text": b.text[:300], "level": b.level, "section": b.section} for b in doc.blocks
              if not section or b.section == section or b.section.startswith(section + ".")][:400]
    await s.send({"type": "casegen.doc", "name": doc.name, "source": doc.source, "section": section,
                  "blocks": blocks, "n_blocks": len(doc.blocks)})
    await s.log(f"<b>read_prd</b> {doc.source.split(':')[0]} → 《{doc.name}》{('§' + section) if section else ''}"
                f" · {len(doc.blocks)} 段 · {len(doc.outline())} 个标题", "md")
    get_store().append(s.id, "prd.import", {"name": doc.name, "source": doc.source, "sha": doc.sha,
                                             "section": section, "blocks": len(doc.blocks), "revision": doc.revision})
    s.state.pop("prd_choice", None)
    if not RE_FORCE.search(text) and (dup := find_existing(doc, section)):
        return _duplicate_reply(s, doc, dup)
    vm = RE_VERSION.search(text)
    s.state["casegen_job"] = bus.spawn(_run_generate(LiveSession(s), doc, section, vm.group(1) if vm else ""))
    scope = f"第 {section} 节" if section else "全文"
    return f"{note}收到《{doc.name}》，我先读{scope}、提取需求点，再生成用例，大约需要一分钟。"


async def _word_to_feishu(s: Sess, doc: PrdDoc, force: bool = False) -> tuple[PrdDoc, str] | str:
    """Word PRD 先转成飞书文档再导入，以后以飞书文档为准（身份绑定、块级增量都按飞书文档走）。
    同一份 Word（按文件哈希）只转一次；再发时给出已有的飞书链接。转换失败时退回本地解析。"""
    key = "word2feishu:" + hashlib.sha256(doc.raw).hexdigest()
    prev = get_store().get_kv(key)
    if prev and not force:
        when = time.strftime("%m-%d %H:%M", time.localtime(prev.get("at", 0)))
        return (f"这份 Word 在 {when} 已经转成飞书文档：{prev.get('url') or prev.get('token')}。PRD 以飞书文档为准，"
                f"有改动请直接在飞书文档里改，再把链接发给我。确实要按这份 Word 重新转一份，请说“重新生成”并再发一次。")
    try:
        token, url = await import_word(doc.name, doc.raw)
        fdoc = await load_feishu_token(token, url)
    except PrdError as e:
        await s.log(f"<b>word_to_feishu</b> 转飞书文档失败，按本地 Word 解析：{e}", "err")
        return doc, f"（没能转成飞书文档：{e}。这次先按本地 Word 解析。）"
    get_store().set_kv(key, {"token": token, "url": url, "name": doc.name, "at": time.time()})
    await s.log(f"<b>word_to_feishu</b> 《{doc.name}》→ 飞书文档 {url or token}", "md")
    return fdoc, (f"已把《{doc.name}》转成飞书文档：{url or token}。以后这份 PRD 请在飞书里修改，"
                  f"再把链接发给我，我会按改动的部分增量生成。")


def _duplicate_reply(s: Sess, doc: PrdDoc, b) -> str:
    """同一内容已经生成过：不再调模型，告诉测试结果在哪。"""
    when = time.strftime("%m-%d %H:%M", time.localtime(b.created_at))
    scope = f"§{b.section}" if b.section else "全文"
    head = f"《{doc.name}》（{scope}）内容和 {when} 导入的一样，"
    tail = "如果确实要重新生成，请说“重新生成”并再发一次。"
    if b.status == "已审核":
        return head + f"用例已审核并发布为基线 {b.prd_id}@{b.prd_version}，不再重复生成。" + tail
    s.state["case_batch"] = b.batch_id
    who = f"（{b.created_by} 发起）" if b.created_by and b.created_by != getattr(s, "user", "") else ""
    return head + f"批次 {b.batch_id}{who}还在{b.status}，请接着审核这一批。" + tail


async def resolve_choice(s: Sess, text: str) -> str | None:
    """版本冲突后测试的回答：「作为修订」/「新版本 0.7」/「取消」。不是在回答时返回 None，交给其它流程。"""
    ch = s.state.get("prd_choice")
    if not ch:
        return None
    plain = text.strip()
    if RE_CANCEL.match(plain):
        s.state.pop("prd_choice", None)
        return f"好的，《{ch['doc'].name}》这次不生成用例。"
    vm = RE_VERSION.search(plain)
    if RE_AS_REV.search(plain):
        kw = {"as_revision": True}
    elif vm and vm.group(1).lstrip("Vv") != ch["version"]:
        kw = {"version_override": vm.group(1)}
    elif vm:
        return f"{vm.group(1)} 就是已发布的版本号。请给一个新的版本号，或者回复“作为修订”。"
    else:
        return None
    job = s.state.get("casegen_job")
    if job and not job.done():
        return "上一份 PRD 还在生成用例，等它完成再继续。"
    s.state.pop("prd_choice", None)
    s.state["casegen_job"] = bus.spawn(_run_generate(LiveSession(s), ch["doc"], ch["section"], ch["chat_version"],
                                                     extracted=ch["extracted"], **kw))
    what = f"按新版本 {kw['version_override']}" if "version_override" in kw else f"作为 {ch['version']} 的修订"
    return f"好的，{what}生成，只为变化的需求点出用例。"


async def _run_generate(s: Sess, doc: PrdDoc, section: str, version: str = "", **kw) -> None:
    async def emit(kind: str, data: dict) -> None:
        await s.send({"type": kind, **data})
        if kind == "casegen.reqs":
            await stage(s, 2)
            for r in data["requirements"]:
                await s.log(f"<b>extract_req</b> {r['req_id']} {r['summary']} ← §{r['section']}", "md")
            for a in data["ambiguities"]:
                await s.log(f"<b>ambiguity</b> §{a['section']} {a['question']} → 待确认", "ai")
            n, q = len(data["requirements"]), len(data["ambiguities"])
            ver = data.get("version", "")
            await s.log(f"<b>prd_version</b> {data.get('prd_id', '')}@{ver}"
                        f"（{data.get('version_source', '')}）" + (f" · 对比 {data['prev_version']}" if data.get("prev_version") else ""), "md")
            d = data.get("diff") or {}
            if inc := data.get("incremental"):
                secs = "、".join("§" + x for x in inc["dirty"]) or "无"
                await s.log(f"<b>incremental</b> 对比 {inc['base']}：{inc['changed_blocks']} 个块有变化，只重读 {secs}；"
                            f"{inc['carried']} 条需求点不经模型直接沿用"
                            + (f"；丢弃范围外抽取 {inc['dropped']} 条" if inc.get("dropped") else ""), "md")
            for p in data.get("params") or []:
                vals = "，".join(f"{k} {v}" for k, v in p["values"].items())
                await s.log(f"<b>model_param</b> {p['name']} 默认 {p['default']}" + (f"，{vals}" if vals else ""), "md")
            if data.get("prev_version"):
                await s.say(f"这是 {ver} 版本，和 {data['prev_version']} 比：新增 {d.get('新增', 0)}、修改 {d.get('修改', 0)}、"
                            f"未改 {d.get('未改', 0)}、删除 {d.get('删除', 0)} 条需求点。只为新增和修改的生成用例，未改的直接继承。")
            else:
                await s.say(f"读到 {n} 条需求点" + (f"，另有 {q} 处待确认" if q else "") + "，正在生成用例。")
        elif kind == "casegen.case":
            c = data["case"]
            await s.log(f"<b>gen_case</b> {c['case_id']} {c['title']} ← {c['req_id']}", "md")

    try:
        await stage(s, 1)
        batch = await generate(doc, section=section, session=s.id, emit=emit, created_by=s.user, version=version, **kw)
    except VersionConflict as e:
        s.state["prd_choice"] = {"doc": doc, "section": section, "chat_version": version, "extracted": e.extracted,
                                 "prd_id": e.prd_id, "version": e.version}
        await s.log(f"<b>prd_version</b> {e.prd_id}@{e.version} 已发布，本次内容不同 → 等测试选择升版本号或作为修订", "ai")
        await s.say(f"《{doc.name}》的版本号还是 {e.version}，但内容和已发布的 {e.version} 不一样。已发布的版本不能覆盖，请选：\n"
                    f"1. 回复“新版本 x.y.z”：按新版本号生成，并通知产品部在 PRD 里补上版本号；\n"
                    f"2. 回复“作为修订”：记为 {e.version} 的修订，以它为基准只为变化的需求点生成用例；\n"
                    f"3. 回复“取消”。")
        return
    except PrdBusy as e:
        await s.log(f"<b>prd_version</b> {e}", "ai")
        await s.say(str(e) + "。")
        return
    except Exception as e:
        await s.log(f"<b>casegen</b> 失败：{type(e).__name__} {e}", "err")
        await s.say(f"用例生成失败：{e}")
        return
    if batch.status == "失败":
        await s.say("没能从这份文档里提取出可测试的需求点，请确认文档内容或指定章节。")
        return
    sk = batch.params.get("skill")
    if sk:
        await s.log(f"<b>skill</b> 用例编写规范：{sk['name']} v{sk['version']}（{sk['file']}）", "md")
    g = batch.params.get("generate_groups") or []
    if g:  # 时间花在哪：提取一次 + 分组生成（并发）
        await s.log(f"<b>timing</b> 需求提取 {batch.params.get('extract_s', '—')} 秒 · 用例生成 {batch.params.get('generate_s', '—')} 秒"
                    f"（{len(g)} 组并发，单组最长 {max(x['s'] for x in g)} 秒，提示词平均 {sum(x['chars'] for x in g) // len(g)} 字）", "md")
    try:  # PRD 原文按版本存进模块文件夹（失败不影响用例闭环）
        rel = await planner.archive_prd(batch, doc)
        if rel:
            await s.log(f"<b>archive_prd</b> PRD 原文存档 → {rel}", "md")
    except Exception as e:
        await s.log(f"<b>archive_prd</b> PRD 原文存档失败：{type(e).__name__} {e}", "err")
    if batch.version_source == "测试预填":
        stale = bool(kw.get("version_override"))  # PRD 写了旧版本号、内容却变了
        what = "版本号未更新" if stale else "缺少版本号"
        fs = await notify(f"PRD {what} · 《{doc.name}》",
                          [("预填版本", batch.prd_version), ("PRD", doc.source),
                           ("请产品部", f"把 PRD 里的版本号改为 {batch.prd_version}" if stale else "在 PRD 中补上版本号")],
                          template="orange")
        await s.send({"type": "feishu.result", **fs})
        await s.log(f"<b>prd_version</b> PRD {what}，已预填 {batch.prd_version}，飞书通知产品部补正", "ai")
    if batch.status == "已审核":  # 只有未改的需求：直接继承发布
        await s.say(f"{batch.prd_version} 与 {batch.prev_version} 相比没有需要重新审核的需求，已继承 "
                    f"{len(batch.inherited)} 条用例并发布基线 {batch.prd_id}@{batch.prd_version}。")
        return
    await _open_review(s, batch, get_caselib().batch_cases(batch.batch_id))


async def _open_review(s: Sess, batch, cases: list) -> None:
    """自检结果汇总，进入审核门。PRD 生成与 xlsx 导入共用。"""
    cov = coverage(batch, cases)
    await stage(s, 3)
    t = cov["by_type"]
    await s.log(f"<b>coverage</b> {cov['covered']}/{cov['total_reqs']} 需求已覆盖 · 正向 {t['正向']} · 边界 {t['边界']}"
                f" · 异常 {t['异常']}")
    await s.log(f"<b>self_check</b> {len(cases)} 条用例自检 · {len(cov['flagged'])} 条有意见", "ai")
    await s.log("<b>review_gate</b> 等待人工审核用例 · 未审核不写入用例库")
    await stage(s, 4)
    s.state["case_batch"] = batch.batch_id
    # 多维表格是审核入口之一：整批以“待审核”写入
    ok, note = await push_bitable(s, cases, "写入批次（待审核）")
    bt = get_bitable(modules.code_of_case(cases[0].case_id)) if cases else get_bitable()
    await s.send(_review_msg(batch, {"url": bt.link(), "ok": ok, "note": note} if bt.enabled else None))
    flag = f"其中 {len(cov['flagged'])} 条自检有意见，" if cov["flagged"] else ""
    where = "可以在审核台或飞书多维表格里审核" if ok else "请在审核台逐条审核后提交"
    inh = f"另继承未改需求的用例 {cov['n_inherited']} 条（不用审）。" if cov.get("n_inherited") else ""
    await s.say(f"生成 {len(cases)} 条用例，覆盖 {cov['covered']}/{cov['total_reqs']} 条需求。{inh}{flag}"
                f"{where}，审核通过的才会入库。")


def _review_msg(batch, bitable: dict | None) -> dict:
    cases = get_caselib().batch_cases(batch.batch_id)
    return {"type": "casereview.open", "batch": batch.model_dump(), "cases": [c.model_dump() for c in cases],
            "reasons": reject_reasons(), "coverage": coverage(batch, cases), "bitable": bitable}


async def resume(s: Sess) -> None:
    """重新打开页面：恢复本人生成中或待审核的批次。生成中的后续进度由 LiveSession 继续发过来。"""
    b = get_caselib().open_batch(getattr(s, "user", ""))
    if not b:
        return
    s.state["case_batch"] = b.batch_id
    await s.send({"type": "casegen.doc", "name": b.doc_name, "source": b.doc_source, "section": b.section, "blocks": [],
                  "n_blocks": 0})
    await s.send({"type": "casegen.batch", "batch": b.model_dump()})
    if b.requirements:
        await s.send(reqs_msg(b))
    if b.status == "待审核":
        await stage(s, 4)
        await s.send(_review_msg(b, None))
    else:
        for c in get_caselib().batch_cases(b.batch_id):
            await s.send({"type": "casegen.case", "batch_id": b.batch_id, "case": c.model_dump()})


async def push_bitable(s: Sess | None, cases: list, what: str) -> tuple[bool, str]:
    """把用例按模块写入 / 更新到各自的飞书多维表格（modules.yaml；未配置时跳过）。
    失败不阻断闭环，本地库照常可用。返回 (全部写入, 问题说明)。"""
    cases = [c for c in cases if c]
    if not cases:
        return False, ""
    groups: dict[tuple[str, str], tuple[BitableClient, list]] = {}
    for c in cases:
        bt = get_bitable(modules.code_of_case(c.case_id))
        groups.setdefault(bt.key, (bt, []))[1].append(c)
    lib = get_caselib()
    pushed, notes = 0, []
    for bt, cs in groups.values():
        mods = "、".join(sorted({modules.code_of_case(c.case_id) for c in cs}))
        if not bt.enabled:
            notes.append(f"{mods} 未配置飞书多维表格（FEISHU_BITABLE_URL 或 modules.yaml）")
            continue
        try:
            await bt.upsert(cs)
        except Exception as e:
            if s:
                await s.log(f"<b>bitable</b> {mods} {what}失败：{e}，已保留在本地用例库", "err")
            notes.append(f"{mods}：{e}")
            continue
        for c in cs:
            lib.save_case(c)  # 记下 record_id / 所在表 / 同步时间
        pushed += len(cs)
        if s:
            where = "默认表" if "*" in bt.modules() else f"{mods} 模块表"
            await s.log(f"<b>bitable</b> 多维表格（{where}）{what} {len(cs)} 条", "md")
    return pushed == len(cases), "；".join(notes)


# ---------- 2. 审核提交 → 入库 → 通知 ----------
async def submit_review(s: Sess, batch_id: str, decisions: list[dict]) -> None:
    lib = get_caselib()
    try:
        n = lib.submit_review(batch_id, decisions, reviewer=getattr(s, "user", "") or "测试工程师")
    except (KeyError, ValueError) as e:
        await s.send({"type": "error", "text": str(e)})
        return
    b = lib.get_batch(batch_id)
    cases = lib.batch_cases(batch_id)
    ok = [c for c in cases if c.review_status in IN_LIBRARY]
    get_store().append(s.id, "review.case_batch", {"batch_id": batch_id, **n})
    await s.log(f"<b>review_gate</b> 人工审核：通过 {n['pass']} · 修改 {n['edit']} · 驳回 {n['reject']}"
                f" · 转需求方 {n['to_req']}")
    pushed, note = await push_bitable(s, cases, "回写审核结论")
    where = "飞书多维表格 + 本地副本" if pushed else f"本地用例库（{note or '多维表格未写入'}）"
    await s.log(f"<b>write_case</b> {where}写入 {len(ok)} 条 · 驳回 {n['reject']} 条计入评估集", "md")
    await s.send({"type": "caselib.result", "batch_id": batch_id, "counts": n, "written": [c.case_id for c in ok],
                  "tier": "bitable" if pushed else "local", "note": note,
                  "url": get_bitable(modules.code_of_case(ok[0].case_id) if ok else "").link() if pushed else "",
                  "cases": [c.model_dump() for c in lib.batch_cases(batch_id)]})
    await stage(s, 5)
    asks = [f"§{a.section} {a.question}" for a in b.ambiguities]
    ids = [c.case_id for c in ok]
    fields = [("入库", f"{len(ok)} 条" + (f"（{ids[0]} ~ {ids[-1]}）" if ids else "")),
              ("驳回", f"{n['reject']} 条 · 计入评估集"),
              ("批次", f"{batch_id} · 《{b.doc_name}》{('§' + b.section) if b.section else ''}")]
    if asks:
        fields.append(("待确认", "；".join(asks)[:200] + " · 已 @需求方"))
    fs = await notify(f"用例评审完成 · {b.module or b.doc_name}", fields, template="green")
    await s.send({"type": "feishu.result", **fs})
    await s.log(f"<b>notify</b> 飞书群卡片：用例评审完成{' · 待确认 %d 条已 @需求方' % len(asks) if asks else ''}", "md")
    s.state["last_written"] = ids
    tail = f"待确认的 {len(asks)} 个问题已转给需求方。" if asks else ""
    base = f"基线 {b.prd_id}@{b.prd_version} 已发布。" if b.prd_id else ""
    need = claim(ok)
    nxt = f"现在生成 {len(need)} 条测试代码，通过静态校验的自动派台架首次试跑。" if need else ""
    await s.say(f"已写入用例库 {len(ok)} 条，驳回 {n['reject']} 条。{base}{tail}{nxt}")
    if need:
        s.state["plan_job"] = bus.spawn(_run_codegen(LiveSession(s), "", need))


async def submit_by_suggestion(s: Sess) -> str:
    bid = s.state.get("case_batch")
    b = get_caselib().get_batch(bid) if bid else None
    if not b or b.status != "待审核":
        return "当前没有待审核的用例批次。"
    decisions = []
    for c in get_caselib().batch_cases(bid):
        if c.suggestion == "驳回":
            reason = "与已有用例重复" if any(x.kind == "重复" for x in c.checks) else "预期不可判定"
            decisions.append({"case_id": c.case_id, "decision": "reject", "reason": reason})
        else:
            decisions.append({"case_id": c.case_id, "decision": "pass"})
    await s.send({"type": "casereview.closed", "batch_id": bid})
    await submit_review(s, bid, decisions)
    return ""


# ---------- 3. 拉取（A-15）→ 4. 测试代码 ----------
def _model_from(text: str) -> str:
    found = vehicles.find_all(text)
    return found[0] if found else get_settings().rig_model


async def pull_and_code(s: Sess, text: str, only_codegen: bool = False, only: set[str] | None = None) -> str:
    lib = get_caselib()
    model = _model_from(text)
    cid = re.search(r"TC-[A-Z0-9]+-\d{3}", text.upper())
    bm = RE_BASELINE.search(text)
    try:
        cases = lib.pull(model, case_id=cid.group(0) if cid else "", baseline=bm.group(1) if bm else "")
    except ValueError as e:
        return str(e)
    if only_codegen and s.state.get("last_written"):
        cases = [c for c in cases if c.case_id in s.state["last_written"]] or cases
    if only is not None:
        cases = [c for c in cases if c.case_id in only]
    if not cases:
        return f"用例库里还没有适用 {model} 的已入库用例。先导入 PRD 生成用例并审核通过。"
    await stage(s, 6)
    need = [c for c in cases if _codegen_wanted(c)]
    await s.log(f"<b>code_status</b> 车型={model} → {len(cases)} 条已入库用例 · 无测试代码 {len(need)} 条（补生成）", "md")
    await s.send({"type": "casepull.result", "model": model, "cases": [lib.a15_row(c) for c in cases]})
    if only is not None:  # 从表格读取时，把已有的 YAML 也展示出来
        for c in cases:
            view = planner.plan_view(c) if c.plan_status == "ready" and c not in need else None
            if view:
                await s.send({"type": "plan.result", **view})
    busy = [c.case_id for c in cases if c.case_id in _inflight]
    if not need:
        if busy:
            return f"{model} 的 {len(cases)} 条已入库用例里有 {len(busy)} 条正在生成测试代码，完成后自动派去试跑。"
        return (f"{model} 的 {len(cases)} 条已入库用例测试代码都已就绪（审核通过时已自动生成并派去试跑）。"
                f"台架在本地页面认领 / 拉取用例。")
    job = s.state.get("plan_job")
    if job and not job.done():
        return "测试代码还在生成中。"
    s.state["plan_job"] = bus.spawn(_run_codegen(LiveSession(s), model, claim(need)))
    return f"{model} 的 {len(cases)} 条已入库用例里有 {len(need)} 条还没有测试代码，我现在补生成并做静态校验。"


_inflight: set[str] = set()  # 正在生成测试代码的用例：网页审核、多维表格同步、台架 A-15 可能同时触发


def _codegen_wanted(c) -> bool:
    return planner.needs_generation(c) and c.auto_status != "不适合自动化" and c.case_id not in _inflight


def claim(cases: list) -> list:
    """派发生成任务之前同步占位：后台任务还没开始跑时，另一条入口不会再生成同一条用例。"""
    got = [c for c in cases if _codegen_wanted(c)]
    _inflight.update(c.case_id for c in got)
    return got


async def code_and_trial(cases: list, s: Sess | None = None) -> dict:
    """审核通过后的固定动作：生成测试代码 + 静态校验 → 通过的派台架首次试跑；没有能跑的台架时留“已生成”，
    台架上线后由 dispatch_pending_trials 补派。s 为空时（多维表格后台同步）只记事件日志。"""
    _inflight.update(c.case_id for c in cases)  # 调用方没先 claim 时（直接调用）也占位
    sem = asyncio.Semaphore(4)
    ok = bad = 0
    started, waiting = [], []

    async def one(c):
        nonlocal ok, bad
        async with sem:
            r = await planner.generate_for(c, s.id if s else "")
        ok, bad = ok + r["ok"], bad + (not r["ok"])
        if s:
            warn = f" · 告警 {len(r['warnings'])}" if r.get("warnings") else ""
            await s.log(f"<b>gen_plan</b> {c.case_id} → {r.get('generated_by', '')} · 静态校验"
                        f"{'通过' if r['ok'] else '未通过：' + '；'.join(r['errors'][:2])}{warn}"
                        + (f" · git {r['plan_version']}" if r.get("plan_version") else ""), "md" if r["ok"] else "err")
            await s.send({"type": "plan.result", **r})

    try:
        if s:
            await stage(s, 6)
        await asyncio.gather(*(one(c) for c in cases))
    finally:
        _inflight.difference_update(c.case_id for c in cases)
    lib = get_caselib()
    if s and any(lib.get_case(c.case_id).plan_status == "ready" for c in cases):
        await stage(s, 7)
    for c in cases:  # 通过静态校验的直接派去试跑；没有台架时留着，台架上线后自动补派
        if lib.get_case(c.case_id).plan_status != "ready":
            continue
        try:
            rig = await rigplan.start_trial(c.case_id, getattr(s, "user", "") if s else "", s.id if s else "system")
            started.append(c.case_id)
            if s:
                await s.log(f"<b>trial</b> {c.case_id} 派到台架 {rig} 首次试跑")
                await s.send({"type": "plan.trial", "case_id": c.case_id, "auto_status": "试跑中", "trial": {"rig": rig}})
        except ValueError as e:
            waiting.append(c.case_id)
            if s:
                await s.log(f"<b>trial</b> 待试跑：{e}（台架上线后自动补派）", "ai")
    await push_bitable(s, [lib.get_case(c.case_id) for c in cases], "回写自动化状态")
    out = {"ok": ok, "bad": bad, "started": started, "waiting": waiting}
    get_store().append(s.id if s else "system", "plan.auto", {**out, "cases": [c.case_id for c in cases]})
    return out


async def _run_codegen(s: Sess, model: str, cases: list) -> None:
    r = await code_and_trial(cases, s)
    lib = get_caselib()
    rows = lib.pull(model) if model else [c for x in cases if (c := lib.get_case(x.case_id))]
    await s.send({"type": "casepull.result", "model": model or "本批", "cases": [lib.a15_row(c) for c in rows]})
    msg = f"测试代码生成完成：{r['ok']} 条通过静态校验" + (f"，{r['bad']} 条未通过，需要人工处理" if r["bad"] else "")
    if r["waiting"]:
        msg += f"。{len(r['waiting'])} 条暂时没有能跑的台架，待试跑，台架上线后自动补派。"
    elif r["started"]:
        msg += "，已派去首次试跑，通过后才能进台架用例集。"
    else:
        msg += "。"
    await s.say(msg)


async def dispatch_pending_trials() -> list[str]:
    """测试代码已就绪、还没试跑过（“已生成”）的用例：有能跑的台架时补派首次试跑。定时循环里调用。"""
    lib = get_caselib()
    started = []
    for c in lib.library():
        if c.plan_status != "ready" or c.auto_status != "已生成" or c.case_id in _inflight:
            continue
        try:
            await rigplan.start_trial(c.case_id, "", "system")
            started.append(c.case_id)
        except ValueError:
            continue
    if started:
        get_store().append("system", "trial.dispatch_pending", {"cases": started})
    return started


async def trial(s: Sess, case_id: str) -> None:
    """首次试跑：通过才算已验证，不能人工跳过。"""
    try:
        rig = await rigplan.start_trial(case_id, getattr(s, "user", ""), s.id)
    except ValueError as e:
        await s.send({"type": "error", "text": str(e)})
        return
    await s.log(f"<b>trial</b> {case_id} 派到台架 {rig} 首次试跑")
    await s.send({"type": "plan.trial", "case_id": case_id, "auto_status": "试跑中", "trial": {"rig": rig}})


def model_codes_hint() -> str:
    return "、".join(f"{vehicles.name(m)}（{m}）" for m in default_models())


# ---------- 多维表格 → CabinPilot 同步 ----------
LAST_SYNC: dict = {}
_LISTS = ("updated", "created", "invalid", "replan", "batches_done", "case_ids")


async def sync_now(s: Sess | None = None, client: BitableClient | None = None) -> dict:
    """反向同步。不指定表时同步全部已配置的表（默认表 + 各模块表），结果合并，tables 里是每张表的情况。"""
    clients = [client] if client else all_bitables()
    if not clients or not all(c.enabled for c in clients):
        r = {"ok": False, "error": "未配置飞书多维表格（FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_URL）"}
    else:
        configured = {c.key for c in all_bitables()}
        r = {"ok": False, "rows": 0, "tables": [], "at": time.time(), **{k: [] for k in _LISTS}}
        errs = []
        for bt in clients:
            row = {"url": bt.link(), "modules": bt.modules()}
            try:
                one = await sync_back(bt, route=bt.key in configured)
            except Exception as e:
                errs.append(str(e))
                r["tables"].append({**row, "ok": False, "error": str(e)})
                continue
            r["ok"] = True
            r["rows"] += one["rows"]
            for k in _LISTS:
                r[k] += one[k]
            r["tables"].append({**row, "ok": True, "rows": one["rows"]})
        if not r["ok"]:
            r["error"] = errs[0] if errs else "没有可同步的表"
        elif errs:
            r["errors"] = errs
    if not client:
        LAST_SYNC.clear()
        LAST_SYNC.update({k: (len(v) if isinstance(v, list) and k != "tables" else v) for k, v in r.items()})
        LAST_SYNC["invalid_rows"] = (r.get("invalid") or [])[:20]
    if r.get("ok") and (r.get("batches_done") or r.get("replan")):  # 在多维表格里审完的批次 / 改了内容的用例：自动生成
        lib = get_caselib()
        ids = {c.case_id for b in r.get("batches_done") or [] for c in lib.batch_cases(b)} | set(r.get("replan") or [])
        todo = claim([c for i in sorted(ids) if (c := lib.get_case(i)) and c.review_status in IN_LIBRARY])
        if todo:
            bus.spawn(code_and_trial(todo, LiveSession(s) if s else None))
    if s:
        if r["ok"]:
            n = len(r["tables"])
            await s.log(f"<b>bitable_sync</b> {f'{n} 张表 · ' if n > 1 else ''}读 {r['rows']} 行 · 更新 {len(r['updated'])}"
                        f" · 新增 {len(r['created'])} · 校验不通过 {len(r['invalid'])} · 测试代码需重新生成 {len(r['replan'])}", "md")
            for e in r.get("errors") or []:
                await s.log(f"<b>bitable_sync</b> 有一张表失败：{e}", "err")
        else:
            await s.log(f"<b>bitable_sync</b> 失败：{r['error']}", "err")
        await s.send({"type": "caselib.sync", **r})
    return r


def _client_for(url: str) -> BitableClient:
    """对话里发来的表格：是已配置的某张表（默认表或模块表）就用它，否则按链接临时读取。"""
    k = table_key(url)
    for bt in all_bitables():
        if bt.key == k:
            return bt
    return BitableClient(url=url)


async def read_bitable(s: Sess, text: str) -> str:
    """对话框发多维表格链接：读用例 → 审核通过的拉取并生成 YAML。"""
    url = RE_BITABLE.search(text).group(0)
    full = next((u for u in text.split() if u.startswith(url)), url)
    await stage(s, 5)
    r = await sync_now(s, _client_for(full))
    if not r["ok"]:
        return f"读取多维表格失败：{r['error'][:120]}"
    lib = get_caselib()
    ids = r["case_ids"]
    passed = [i for i in ids if (c := lib.get_case(i)) and c.review_status in IN_LIBRARY]
    head = f"读到 {r['rows']} 行用例：审核通过 {len(passed)} 条"
    if len(ids) > len(passed):
        head += f"，未通过或待审核 {len(ids) - len(passed)} 条"
    if r["invalid"]:
        head += f"，不合格 {len(r['invalid'])} 行"
    if not passed:
        return head + "。审核通过后才能生成测试代码。"
    return head + "。" + await pull_and_code(s, "拉取用例", only=set(passed))


async def sync_loop() -> None:
    """周期反向同步：默认 5 分钟一轮，每轮同步全部已配置的表（modules.yaml 改了下一轮即生效）；有变化时推给所有在线会话。"""
    while all_bitables():
        await asyncio.sleep(max(30, get_settings().bitable_sync_s))
        r = await sync_now()
        if r.get("ok") and (r["updated"] or r["created"] or r["invalid"]):
            await bus.broadcast("caselib.sync", r)
            get_store().append("system", "bitable.sync", {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
