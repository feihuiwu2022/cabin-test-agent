"""Word PRD 先转飞书文档再导入：上传素材 → 导入任务 → 轮询 → 按飞书文档读取；同一份 Word 只转一次；失败退回本地解析。"""
import json

import httpx
import pytest

from cabin_cloud import casegen, casewf, prd
from cabin_cloud.bus import bus
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from test_casegen import _docx
from test_versioning import C_SET, R_SET, _extract


class FakeFeishu:
    """飞书开放平台的最小模拟：鉴权、素材上传、导入任务（第一次轮询处理中）、文档块与元信息。"""

    def __init__(self, fail_import: bool = False):
        self.calls: list[str] = []
        self.polls = 0
        self.fail_import = fail_import

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.removeprefix("/open-apis")
        self.calls.append(f"{req.method} {p}")
        if p == "/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1"})
        assert req.headers["authorization"] == "Bearer t-1"
        if p == "/drive/v1/medias/upload_all":
            body = req.content.decode("utf8", "ignore")
            assert "ccm_import_open" in body and '"obj_type": "docx"' in body
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "box-1"}})
        if p == "/drive/v1/import_tasks" and req.method == "POST":
            j = json.loads(req.content)
            assert j["file_token"] == "box-1" and j["point"] == {"mount_type": 1, "mount_key": "fldPRD"}
            if self.fail_import:
                return httpx.Response(200, json={"code": 1061004, "msg": "forbidden"})
            return httpx.Response(200, json={"code": 0, "data": {"ticket": "tk-1"}})
        if p == "/drive/v1/import_tasks/tk-1":
            self.polls += 1
            res = {"job_status": 2} if self.polls == 1 else {
                "job_status": 0, "token": "DocW1", "url": "https://x.feishu.cn/docx/DocW1"}
            return httpx.Response(200, json={"code": 0, "data": {"result": res}})
        if p == "/docx/v1/documents/DocW1/blocks":
            t = lambda bid, bt, key, txt, parent="DocW1": {  # noqa: E731
                "block_id": bid, "block_type": bt, "parent_id": parent, key: {"elements": [{"text_run": {"content": txt}}]}}
            items = [{"block_id": "DocW1", "block_type": 1, "children": ["h", "v", "p1"]},
                     t("h", 3, "heading1", "空调控制 PRD"), t("v", 2, "text", "文档版本：1.0"),
                     t("p1", 2, "text", "3.1 说空调调到 N 度时设为 N 度并播报")]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})
        if p == "/docx/v1/documents/DocW1":
            return httpx.Response(200, json={"code": 0, "data": {"document": {"title": "空调控制 PRD", "revision_id": 3}}})
        return httpx.Response(404, json={"code": 404, "msg": p})


@pytest.fixture
def feishu(monkeypatch):
    s = get_settings()
    for k, v in (("feishu_app_id", "cli_x"), ("feishu_app_secret", "sec"),
                 ("feishu_prd_folder", "https://x.feishu.cn/drive/folder/fldPRD")):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(prd, "IMPORT_POLL_S", 0)

    def use(fake: FakeFeishu) -> FakeFeishu:
        monkeypatch.setattr(prd, "_transport", httpx.MockTransport(fake))
        return fake
    return use


class _Sess:
    def __init__(self, sid):
        self.id, self.state, self.user, self.said, self.logs = sid, {}, "张工", [], []

    async def send(self, msg):
        pass

    async def log(self, text, kind=""):
        self.logs.append(text)

    async def say(self, text, src=""):
        self.said.append(text)


def _word(tmp_path, name="空调控制_PRD_V1.0.docx", extra=""):
    p = tmp_path / name
    p.write_bytes(_docx([("1", "空调控制 PRD"), ("", "文档版本：1.0"), ("", "3.1 说空调调到 N 度时设为 N 度并播报" + extra)]))
    return p


def _fake_llm(monkeypatch):
    async def fake(system, prompt, source, session, max_tokens=4000):
        return _extract([{**R_SET, "quote": "3.1 说空调调到 N 度时设为 N 度并播报"}], []) if source == "casegen.extract" \
            else {"cases": [C_SET]}
    monkeypatch.setattr(casegen, "_llm_json", fake)


async def test_word_is_converted_then_read_as_feishu(feishu, monkeypatch, tmp_path):
    fake = feishu(FakeFeishu())
    _fake_llm(monkeypatch)
    s = _Sess("S-word-1")
    bus.sessions.pop(s.id, None)
    p = _word(tmp_path)
    reply = await casewf.start_import(s, f"把 {p} 转成用例", None)
    assert "已把《空调控制_PRD_V1.0.docx》转成飞书文档：https://x.feishu.cn/docx/DocW1" in reply
    await s.state["casegen_job"]
    b = get_caselib().get_batch(s.state["case_batch"])
    assert b.doc_source == "feishu:DocW1" and b.doc_revision == "3" and b.prd_version == "1.0"
    assert get_caselib().prd_for_source("feishu:DocW1") == b.prd_id  # 以后发飞书链接就是同一份 PRD
    assert fake.polls == 2 and "POST /drive/v1/medias/upload_all" in fake.calls

    # 同一份 Word 再发：不再新建飞书文档，提示以飞书文档为准
    n = len(fake.calls)
    s.state.pop("casegen_job")
    again = await casewf.start_import(s, f"把 {p} 转成用例", None)
    assert "已经转成飞书文档：https://x.feishu.cn/docx/DocW1" in again and "飞书文档里改" in again
    assert len(fake.calls) == n and "casegen_job" not in s.state


async def test_import_failure_falls_back_to_local_word(feishu, monkeypatch, tmp_path):
    feishu(FakeFeishu(fail_import=True))
    _fake_llm(monkeypatch)
    s = _Sess("S-word-2")
    bus.sessions.pop(s.id, None)
    p = _word(tmp_path, "回退_PRD.docx", "（回退）")
    reply = await casewf.start_import(s, f"把 {p} 转成用例", None)
    assert reply.startswith("（没能转成飞书文档：创建飞书导入任务失败：forbidden") and "按本地 Word 解析" in reply
    await s.state["casegen_job"]
    b = get_caselib().get_batch(s.state["case_batch"])
    assert b.doc_source.startswith("path:") and any("word_to_feishu" in x for x in s.logs)


async def test_no_folder_means_local_parse(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "feishu_prd_folder", "")
    assert prd.word_import_folder() == ""
    with pytest.raises(prd.PrdError, match="FEISHU_PRD_FOLDER"):
        await prd.import_word("x.docx", b"x")
    monkeypatch.setattr(s, "feishu_app_id", "a")
    monkeypatch.setattr(s, "feishu_app_secret", "b")
    monkeypatch.setattr(s, "feishu_prd_folder", "fldRaw")
    assert prd.word_import_folder() == "fldRaw"
