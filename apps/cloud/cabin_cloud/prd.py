"""cabin-prd：PRD 导入与解析。

来源：对话框上传的本地文件、对话中输入的本机路径、在线链接（飞书云文档 / 普通网页 / 文件直链）。
格式：.docx / .pdf / .md / .txt / .html。
解析结果统一为带标题层级的段落列表：标题保留层级与编号，表格按行转文本并带表头，图片只记占位与题注。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

from .config import get_settings

SUPPORTED = (".docx", ".pdf", ".md", ".markdown", ".txt", ".html", ".htm")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
RE_SECNO = re.compile(r"^\s*§?\s*((?:\d+\.)*\d+)\.?\s+")
RE_URL = re.compile(r"https?://[^\s，。,；;“”\"'<>]+")
RE_LOCAL = re.compile(r"(?:[A-Za-z]:[\\/]|/|~[\\/]|\.{1,2}[\\/])[^\s，。,；;“”\"'<>]*?\.(?:docx|pdf|md|markdown|txt|html?|xlsx?)\b", re.I)
RE_SECTION = re.compile(r"(?:§\s*|第\s*)((?:\d+\.)*\d+)(?:\s*[节章])?|((?:\d+\.)+\d+)\s*[节章]")


class PrdError(ValueError):
    pass


@dataclass
class Block:
    kind: str  # heading / para / table / image
    text: str
    level: int = 0  # heading 层级（1 起）
    section: str = ""  # 所属章节编号，如 4.1.2
    bid: str = ""  # 飞书块 ID（跨版本稳定）；其它来源为空

    @property
    def digest(self) -> str:
        """内容哈希（忽略空白）：块级增量比对用。"""
        flat = re.sub(r"\s+", "", self.text)
        return hashlib.sha256(f"{self.kind}|{flat}".encode()).hexdigest()[:12]

    @property
    def key(self) -> str:
        """块的身份：飞书用块 ID（内容改了身份不变），其它来源用内容哈希。"""
        return self.bid or "h:" + self.digest


@dataclass
class PrdDoc:
    name: str
    source: str  # upload:<文件名> | path:<路径> | url:<链接> | feishu:<token>
    blocks: list[Block] = field(default_factory=list)
    sha: str = ""
    raw: bytes = field(default=b"", repr=False)  # 原文件内容（存档用）；在线文档为空
    revision: str = ""  # 飞书文档 revision_id：改一次 +1，用来判断内容变没变

    @property
    def title(self) -> str:
        h = next((b for b in self.blocks if b.kind == "heading"), None)
        return h.text if h else self.name

    def outline(self) -> list[str]:
        return [("  " * (b.level - 1)) + b.text for b in self.blocks if b.kind == "heading"]

    def text(self, section: str = "") -> str:
        """渲染成带层级的纯文本（给模型）；section 非空时只取该章节及其子章节。"""
        return render([b for b in self.blocks if not section or _in_section(b.section, section)])

    def has_section(self, section: str) -> bool:
        return any(_in_section(b.section, section) for b in self.blocks)


def render(blocks: list[Block]) -> str:
    """块 → 带层级的纯文本。"""
    out = []
    for b in blocks:
        if b.kind == "heading":
            out.append(f"{'#' * min(b.level, 6)} {b.text}")
        elif b.kind == "table":
            out.append(b.text)
        elif b.kind == "image":
            out.append(f"[图片：{b.text or '无题注'}]")
        else:
            out.append(b.text)
    return "\n".join(out)


def _in_section(sec: str, want: str) -> bool:
    return sec == want or sec.startswith(want + ".")


# ---------- 定位：对话文本里的链接 / 本机路径 / 章节 ----------
def find_ref(text: str) -> tuple[str, str] | None:
    """返回 ("url", 链接) 或 ("path", 本机路径)。"""
    m = RE_URL.search(text)
    if m:
        return "url", m.group(0).rstrip(")）】]")
    m = RE_LOCAL.search(text)
    if m:
        return "path", m.group(0).strip()
    return None


def find_section(text: str) -> str:
    m = RE_SECTION.search(text)
    return (m.group(1) or m.group(2)) if m else ""


# ---------- 解析 ----------
def _number_headings(blocks: list[Block]) -> list[Block]:
    """给每个块标出所属章节号：标题自带编号用编号，否则按层级自动编号。"""
    counters: list[int] = []
    cur = ""
    for b in blocks:
        if b.kind == "heading":
            m = RE_SECNO.match(b.text)
            if m:
                cur = m.group(1)
                counters = [int(x) for x in cur.split(".")]
            else:
                lvl = max(1, b.level)
                counters = (counters + [0] * lvl)[:lvl]
                counters[-1] += 1
                cur = ".".join(map(str, counters))
            b.section = cur
        else:
            m = RE_SECNO.match(b.text)
            # 段落自带更细的编号（如 "4.1.2 说'空调调到 N 度'……"）时按段落编号归属
            b.section = m.group(1) if m and cur and m.group(1).startswith(cur + ".") else cur
    return blocks


def parse_markdown(text: str) -> list[Block]:
    blocks: list[Block] = []
    table: list[list[str]] = []

    def flush_table():
        if not table:
            return
        head, rows = table[0], [r for r in table[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
        lines = ["表格：" + " | ".join(head)] + ["；".join(f"{h}={c}" for h, c in zip(head, r) if c) for r in rows]
        blocks.append(Block("table", "\n".join(lines)))
        table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("|"):
            table.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush_table()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            blocks.append(Block("heading", m.group(2).strip(), level=len(m.group(1))))
            continue
        m = re.match(r"^!\[([^\]]*)\]\([^)]*\)", line.strip())
        if m:
            blocks.append(Block("image", m.group(1)))
            continue
        blocks.append(Block("para", re.sub(r"^\s*[-*+]\s+", "", line).strip()))
    flush_table()
    return blocks


def _docx_styles(z: zipfile.ZipFile) -> dict[str, int]:
    """styleId → 标题层级（兼容中文样式名“标题 1”与 outlineLvl）。"""
    out: dict[str, int] = {}
    try:
        root = ET.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return out
    for st in root.iter(f"{W}style"):
        sid = st.get(f"{W}styleId", "")
        name_el = st.find(f"{W}name")
        name = (name_el.get(f"{W}val", "") if name_el is not None else "").lower()
        lvl_el = st.find(f"{W}pPr/{W}outlineLvl")
        m = re.match(r"(heading|标题)\s*(\d)", name) or re.match(r"(heading)(\d)", sid.lower())
        if m:
            out[sid] = int(m.group(2))
        elif lvl_el is not None and name != "normal":
            out[sid] = int(lvl_el.get(f"{W}val", "0")) + 1
        elif name == "title":
            out[sid] = 1
    return out


def _p_text(p: ET.Element) -> str:
    parts = []
    for el in p.iter():
        if el.tag == f"{W}t":
            parts.append(el.text or "")
        elif el.tag == f"{W}tab":
            parts.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def parse_docx(data: bytes) -> list[Block]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError) as e:
        raise PrdError(f"不是有效的 .docx 文件（{e}）") from e
    styles = _docx_styles(z)
    body = root.find(f"{W}body")
    blocks: list[Block] = []
    pending_img = False
    for el in list(body if body is not None else []):
        if el.tag == f"{W}p":
            ps = el.find(f"{W}pPr/{W}pStyle")
            sid = ps.get(f"{W}val", "") if ps is not None else ""
            lvl_el = el.find(f"{W}pPr/{W}outlineLvl")
            level = styles.get(sid) or (int(lvl_el.get(f"{W}val", "9")) + 1 if lvl_el is not None else 0)
            has_img = any(x.tag.endswith("}drawing") or x.tag.endswith("}pict") for x in el.iter())
            txt = _p_text(el)
            if has_img:
                blocks.append(Block("image", txt))
                pending_img = not txt
                continue
            if not txt:
                continue
            if pending_img and re.match(r"^(图|Figure)\s*\d", txt):  # 图片题注
                blocks[-1].text = txt
                pending_img = False
                continue
            pending_img = False
            if 1 <= level <= 9:
                blocks.append(Block("heading", txt, level=level))
            else:
                blocks.append(Block("para", txt))
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.iter(f"{W}tr"):
                rows.append([" ".join(_p_text(p) for p in tc.iter(f"{W}p")).strip() for tc in tr.iter(f"{W}tc")])
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            head = rows[0]
            lines = ["表格：" + " | ".join(head)]
            for r in rows[1:]:
                lines.append("；".join(f"{h}={c}" if h else c for h, c in zip(head, r) if c))
            blocks.append(Block("table", "\n".join(lines)))
    return blocks


def parse_pdf(data: bytes) -> list[Block]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise PrdError("解析 PDF 需要 pypdf") from e
    reader = PdfReader(io.BytesIO(data))
    blocks: list[Block] = []
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = RE_SECNO.match(line)
            # PDF 没有样式：以“编号 + 短标题”识别标题，层级取编号段数
            if m and len(line) <= 40 and not re.search(r"[，。；：:,;]", line[m.end():]):
                blocks.append(Block("heading", line, level=m.group(1).count(".") + 1))
            else:
                blocks.append(Block("para", line))
    return blocks


def parse_html(text: str) -> list[Block]:
    text = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", lambda m: f"\n{'#' * int(m.group(1))} {m.group(2)}\n", text)
    text = re.sub(r"(?is)<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"\n![\1]()\n", text)
    text = re.sub(r"(?is)<tr[^>]*>(.*?)</tr>", lambda m: "\n|" + re.sub(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", r"\1|", m.group(1)) + "\n", text)
    text = re.sub(r"(?is)<(br|/p|/div|/li)[^>]*>", "\n", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "", text))
    return parse_markdown(text)


def parse_bytes(name: str, data: bytes, source: str) -> PrdDoc:
    ext = Path(name).suffix.lower()
    if ext == ".docx":
        blocks = parse_docx(data)
    elif ext == ".pdf":
        blocks = parse_pdf(data)
    elif ext in (".html", ".htm"):
        blocks = parse_html(data.decode("utf8", "ignore"))
    elif ext in (".md", ".markdown", ".txt"):
        blocks = parse_markdown(data.decode("utf8", "ignore"))
    else:
        raise PrdError(f"不支持的格式 {ext or '（无扩展名）'}，支持 .docx / .pdf / .md / .txt")
    if not any(b.kind != "image" for b in blocks):
        raise PrdError("文档中没有读到文字内容")
    return PrdDoc(name=Path(name).name, source=source, blocks=_number_headings(blocks),
                  sha=hashlib.sha256(data).hexdigest()[:12], raw=data)


# ---------- 读取 ----------
def load_path(path: str) -> PrdDoc:
    p = Path(os.path.expanduser(path.strip().strip('"')))
    if not p.is_file():
        raise PrdError(f"本机找不到文件：{p}")
    if p.suffix.lower() not in SUPPORTED:
        raise PrdError(f"不支持的格式 {p.suffix}，支持 .docx / .pdf / .md / .txt")
    return parse_bytes(p.name, p.read_bytes(), f"path:{p}")


RE_BITABLE = re.compile(r"https?://[^/\s]*(?:feishu\.cn|larksuite\.com)/base/[A-Za-z0-9]+")
RE_FEISHU = re.compile(r"https?://[^/]*(?:feishu\.cn|larksuite\.com)/(?:docx|wiki|docs)/([A-Za-z0-9]+)")


FEISHU = "https://open.feishu.cn/open-apis"
RE_FOLDER = re.compile(r"/folder/([A-Za-z0-9]+)")
IMPORT_POLL_S, IMPORT_TIMEOUT_S = 1.0, 60
_transport: httpx.AsyncBaseTransport | None = None  # 测试注入


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, transport=_transport, trust_env=os.getenv("CABIN_USE_PROXY") == "1")


async def _tenant_token(c: httpx.AsyncClient) -> str:
    s = get_settings()
    t = (await c.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      json={"app_id": s.feishu_app_id, "app_secret": s.feishu_app_secret})).json()
    if not t.get("tenant_access_token"):
        raise PrdError(f"飞书鉴权失败：{t.get('msg')}")
    return t["tenant_access_token"]


def word_import_folder() -> str:
    """Word 转飞书文档的目标文件夹 token；没配飞书应用或文件夹时为空（不转，本地解析）。"""
    s = get_settings()
    if not (s.feishu_app_id and s.feishu_app_secret and s.feishu_prd_folder):
        return ""
    m = RE_FOLDER.search(s.feishu_prd_folder)
    return m.group(1) if m else s.feishu_prd_folder.strip()


async def import_word(name: str, data: bytes) -> tuple[str, str]:
    """Word → 飞书云文档（drive 导入任务）：上传素材 → 创建导入任务 → 轮询结果。返回 (文档 token, 文档链接)。
    每次导入都会新建一份文档，调用方按文件哈希去重。"""
    folder = word_import_folder()
    if not folder:
        raise PrdError("没有配置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_PRD_FOLDER，不能转成飞书文档")
    stem = Path(name).stem or "PRD"
    async with _client() as c:
        hdr = {"Authorization": f"Bearer {await _tenant_token(c)}"}
        up = (await c.post(f"{FEISHU}/drive/v1/medias/upload_all", headers=hdr,
                           data={"file_name": Path(name).name, "parent_type": "ccm_import_open", "size": str(len(data)),
                                 "extra": json.dumps({"obj_type": "docx", "file_extension": "docx"})},
                           files={"file": (Path(name).name, data, "application/octet-stream")})).json()
        if up.get("code") != 0:
            raise PrdError(f"上传 Word 到飞书失败：{up.get('msg')}")
        task = (await c.post(f"{FEISHU}/drive/v1/import_tasks", headers=hdr, json={
            "file_extension": "docx", "file_token": up["data"]["file_token"], "type": "docx", "file_name": stem,
            "point": {"mount_type": 1, "mount_key": folder}})).json()
        if task.get("code") != 0:
            raise PrdError(f"创建飞书导入任务失败：{task.get('msg')}（确认应用有该文件夹的编辑权限）")
        ticket = task["data"]["ticket"]
        waited = 0.0
        while waited <= IMPORT_TIMEOUT_S:
            r = (await c.get(f"{FEISHU}/drive/v1/import_tasks/{ticket}", headers=hdr)).json()
            res = (r.get("data") or {}).get("result") or {}
            status = res.get("job_status")
            if r.get("code") == 0 and status == 0 and res.get("token"):
                return res["token"], res.get("url", "")
            if r.get("code") != 0 or status not in (1, 2, None):  # 1 初始化 / 2 处理中，其余为失败
                raise PrdError(f"飞书导入失败：{res.get('job_error_msg') or r.get('msg') or status}")
            await asyncio.sleep(IMPORT_POLL_S)
            waited += IMPORT_POLL_S
    raise PrdError(f"飞书导入超过 {IMPORT_TIMEOUT_S} 秒没有完成，请稍后重试")


async def load_feishu_token(token: str, url: str = "") -> PrdDoc:
    return await _feishu_doc(url or f"feishu:{token}", token)


async def _feishu_doc(url: str, token_id: str) -> PrdDoc:
    """飞书云文档：一期按应用身份读取（需把应用加为文档协作者）；按用户身份授权待接入飞书登录。"""
    s = get_settings()
    if not (s.feishu_app_id and s.feishu_app_secret):
        raise PrdError("这是飞书云文档链接，需配置 FEISHU_APP_ID / FEISHU_APP_SECRET 才能读取；"
                       "也可以导出为 .docx 后上传")
    async with _client() as c:
        hdr = {"Authorization": f"Bearer {await _tenant_token(c)}"}
        if "/wiki/" in url:  # 知识库节点 → 实际文档 token
            n = (await c.get("https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
                             params={"token": token_id}, headers=hdr)).json()
            token_id = ((n.get("data") or {}).get("node") or {}).get("obj_token") or token_id
        # 块接口保留标题层级、表格与块 ID；失败时退回纯文本
        items: list[dict] = []
        page_token = ""
        try:
            while True:
                r = (await c.get(f"https://open.feishu.cn/open-apis/docx/v1/documents/{token_id}/blocks",
                                 params={"page_size": 500, "page_token": page_token}, headers=hdr)).json()
                if r.get("code") != 0:
                    raise PrdError(r.get("msg", "读取失败"))
                items += r["data"].get("items", [])
                if not r["data"].get("has_more"):
                    break
                page_token = r["data"].get("page_token", "")
            blocks = feishu_blocks(items, token_id)
        except PrdError:
            r = (await c.get(f"https://open.feishu.cn/open-apis/docx/v1/documents/{token_id}/raw_content",
                             headers=hdr)).json()
            if r.get("code") != 0:
                raise PrdError(f"飞书文档读取失败：{r.get('msg')}（确认应用已加为文档协作者）")
            blocks = parse_markdown(r["data"]["content"])
        title = (await c.get(f"https://open.feishu.cn/open-apis/docx/v1/documents/{token_id}", headers=hdr)).json()
        meta = (title.get("data") or {}).get("document") or {}
        name = meta.get("title") or token_id
    raw = "\n".join(b.text for b in blocks).encode()
    return PrdDoc(name=name, source=f"feishu:{token_id}", blocks=_number_headings(blocks),
                  sha=hashlib.sha256(raw).hexdigest()[:12], revision=str(meta.get("revision_id") or ""))


def _feishu_text(obj: dict | None) -> str:
    """文本元素：普通文字、@文档 / @人 的标题、行内公式。"""
    out = []
    for e in (obj or {}).get("elements", []):
        if "text_run" in e:
            out.append((e["text_run"] or {}).get("content", ""))
        elif "mention_doc" in e:
            out.append((e["mention_doc"] or {}).get("title", ""))
        elif "mention_user" in e:
            out.append("@" + ((e["mention_user"] or {}).get("user_name") or ""))
        elif "equation" in e:
            out.append((e["equation"] or {}).get("content", ""))
    return "".join(out).strip()


# 飞书 docx 块类型（open-apis/docx/v1 block_type）
FS_PAGE, FS_TEXT, FS_IMAGE, FS_TABLE, FS_CELL = 1, 2, 27, 31, 32
FS_TEXTLIKE = {2: "text", 12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo"}
FS_SKIP = {22}  # 分割线


def feishu_blocks(items: list[dict], doc_id: str = "") -> list[Block]:
    """飞书块列表 → Block：按 children 还原文档顺序；表格按表头 + 行输出；高亮块、分栏、引用容器等展开子块。
    每个 Block 带飞书块 ID（表格整体一个块）。"""
    by_id = {b.get("block_id"): b for b in items if b.get("block_id")}
    root = by_id.get(doc_id) or next((b for b in items if b.get("block_type") == FS_PAGE), None)
    out: list[Block] = []
    seen: set[str] = set()

    def text_of(bid: str) -> str:
        """单元格等容器里的全部文字（子块按顺序拼接）。"""
        b = by_id.get(bid)
        if not b:
            return ""
        own = _block_text(b)
        kids = " ".join(t for k in b.get("children") or [] if (t := text_of(k)))
        return " ".join(x for x in (own, kids) if x)

    def table(b: dict) -> Block | None:
        t = b.get("table") or {}
        prop = t.get("property") or {}
        cells = t.get("cells") or [k for k in b.get("children") or []]
        ncol = int(prop.get("column_size") or 0) or 1
        rows = [[text_of(cid).replace("\n", " ").strip() for cid in cells[i:i + ncol]] for i in range(0, len(cells), ncol)]
        rows = [r for r in rows if any(r)]
        if not rows:
            return None
        head = rows[0]
        lines = ["表格：" + " | ".join(head)]
        for r in rows[1:]:
            lines.append("；".join(f"{h}={c}" if h else c for h, c in zip(head, r) if c))
        return Block("table", "\n".join(lines), bid=b.get("block_id", ""))

    def walk(bid: str) -> None:
        if bid in seen:
            return
        seen.add(bid)
        b = by_id.get(bid)
        if not b:
            return
        bt = b.get("block_type")
        if bt == FS_TABLE:
            tb = table(b)
            if tb:
                out.append(tb)
            seen.update(_descendants(by_id, bid))
            return
        if 3 <= (bt or 0) <= 11:  # heading1..9
            t_ = _feishu_text(b.get(f"heading{bt - 2}"))
            if t_:
                out.append(Block("heading", t_, level=bt - 2, bid=bid))
        elif bt in FS_TEXTLIKE:
            t_ = _feishu_text(b.get(FS_TEXTLIKE[bt]))
            if t_:
                out.append(Block("para", t_, bid=bid))
        elif bt == FS_IMAGE:
            out.append(Block("image", "", bid=bid))
        for k in b.get("children") or []:  # 列表子项、高亮块、分栏、引用容器……
            if bt not in FS_SKIP:
                walk(k)

    if root:
        seen.add(root["block_id"])
        for k in root.get("children") or []:
            walk(k)
    # 树里没挂上的块（接口只返回了部分层级时）按返回顺序补上
    for b in items:
        if b.get("block_id") not in seen and b.get("block_type") != FS_PAGE and b.get("parent_id") not in by_id:
            walk(b["block_id"])
    return out


def _block_text(b: dict) -> str:
    bt = b.get("block_type") or 0
    if 3 <= bt <= 11:
        return _feishu_text(b.get(f"heading{bt - 2}"))
    if bt in FS_TEXTLIKE:
        return _feishu_text(b.get(FS_TEXTLIKE[bt]))
    return ""


def _descendants(by_id: dict, bid: str) -> set[str]:
    out: set[str] = set()
    stack = list((by_id.get(bid) or {}).get("children") or [])
    stack += list(((by_id.get(bid) or {}).get("table") or {}).get("cells") or [])
    while stack:
        k = stack.pop()
        if k in out:
            continue
        out.add(k)
        stack += list((by_id.get(k) or {}).get("children") or [])
    return out


async def load_url(url: str) -> PrdDoc:
    if re.search(r"(?:feishu\.cn|larksuite\.com)/(?:sheets|base)/", url):  # 需登录的页面，抓下来只有空壳
        raise PrdError("这是飞书表格链接，不是 PRD 文档。多维表格直接发链接即可读取用例；电子表格请导出 .xlsx 后上传。")
    m = RE_FEISHU.match(url)
    if m:
        return await _feishu_doc(url, m.group(1))
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 CabinPilot"})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise PrdError(f"在线文档读取失败：{type(e).__name__} {e}") from e
    ctype = r.headers.get("content-type", "").lower()
    name = Path(httpx.URL(url).path).name or httpx.URL(url).host
    if "wordprocessingml" in ctype:
        name = name if name.endswith(".docx") else name + ".docx"
    elif "pdf" in ctype:
        name = name if name.endswith(".pdf") else name + ".pdf"
    elif "markdown" in ctype or name.lower().endswith((".md", ".markdown")):
        name = name if name.lower().endswith((".md", ".markdown")) else name + ".md"
    elif "text/plain" in ctype and not name.lower().endswith(".txt"):
        name += ".txt"
    elif Path(name).suffix.lower() not in SUPPORTED:
        name += ".html"
    doc = parse_bytes(name, r.content, f"url:{url}")
    if doc.name.endswith(".html"):
        t = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
        if t:
            doc.name = html.unescape(t.group(1)).strip()[:60]
    return doc


def uploads_dir() -> Path:
    d = get_settings().data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d
