"""缺陷闭环：指纹与查重、合并、按车型建票、附件、用户映射、配置与凭证。Jira 用模拟服务。"""
import json
import time

import httpx
import pytest

from cabin_cloud import auth, modules, stats, vault
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc import rigapi, service as svc_mod
from cabin_cloud.execsvc.defects import to_candidates
from cabin_cloud.integ.jira import ATTACH_LIMIT, get_jira, pick_attachments
from cabin_cloud.main import app
from cabin_cloud.store import get_store
from cabin_common.results import AssertResult, PlanResult
from cabin_common.schema import DefectCandidate
from test_rigagent import LocalJudgeStub, URL, agent, case_with_plan, cloud, submit  # noqa: F401
from test_runner import FakeJudge


class FakeJira(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls, self.issues, self.users, self.reject_reporter = [], [], {}, False

    async def handle_async_request(self, req):
        path, body = req.url.path, b"".join([x async for x in req.stream])
        self.calls.append((req.method, path, req.headers.get("authorization", ""), body))
        if path.endswith("/rest/api/2/search"):
            return self._json({"issues": self.issues})
        if path.endswith("/rest/api/2/user/search"):
            who = req.url.params.get("username")
            return self._json([{"name": self.users[who]}] if who in self.users else [])
        if path.endswith("/rest/api/2/myself"):
            return self._json({"displayName": "cabin-bot"})
        if path.endswith("/rest/api/2/issue") and req.method == "POST":
            d = json.loads(body)
            if self.reject_reporter and "reporter" in d["fields"]:
                return self._json({"errors": {"reporter": "no"}}, 400)
            return self._json({"key": f"{d['fields']['project']['key']}-1"}, 201)
        if "/attachments" in path:
            return self._json([{}])
        if path.startswith("/rest/api/2/issue/"):
            return self._json({"fields": {"status": {"name": "Closed"}}})
        return self._json({})

    def _json(self, v, code=200):
        return httpx.Response(code, json=v)

    def sent(self, tail):
        return [c for c in self.calls if c[1].endswith(tail)]


@pytest.fixture
def jira(monkeypatch, cloud):  # noqa: F811
    s = get_settings()
    monkeypatch.setattr(s, "secret_key", "测试密钥")
    monkeypatch.setattr(s, "admins", ("管理员",))
    j = get_jira()
    fake = FakeJira()
    monkeypatch.setattr(j, "transport", fake)
    cloud.x("DELETE FROM kv")
    cloud.x("DELETE FROM defects")
    cloud.x("DELETE FROM review_memory")
    cloud.x("DELETE FROM user_profile")
    cloud.set_kv("jira", {"url": "http://jira.test", "user": "bot", "secret": vault.seal("pw"), "token_auth": False,
                          "projects": {"A01-CN": {"project": "A01", "component": "座舱"}}})
    yield fake
    cloud.x("DELETE FROM kv")


def result(*asserts) -> PlanResult:
    return PlanResult(run_id="r", case_id="TC-J-001", asserts=list(asserts))


def vision():
    return AssertResult(assertion_id="a1", type="vision", result="fail", confidence=0.9, detail={"verdict": {"diff_summary": "截断"}})


def test_fingerprint_has_model_case_assert_and_type():
    a = to_candidates(result(vision()), "T", "B2", "OTA", "pkg", "", "A01-CN")[0]
    b = to_candidates(result(vision()), "T", "B3", "OTA", "pkg", "", "B01-EU")[0]
    assert a.fingerprint == "A01-CN|TC-J-001|a1|J-01|" and a.fingerprint != b.fingerprint
    assert "OTA" not in a.fingerprint  # 版本在查重时比较，这样才分得出老问题延续


def test_crash_type_is_part_of_fingerprint():
    def crash(frames):
        return AssertResult(assertion_id="a2", type="rule", result="fail",
                            detail={"rule": "J-02", "kind": "崩溃", "package": "p", "frames": frames})

    fa = to_candidates(result(crash("a.b")), "T", "B2", "O", "p", "", "M")[0].fingerprint
    fb = to_candidates(result(crash("c.d")), "T", "B2", "O", "p", "", "M")[0].fingerprint
    assert fa != fb


def defect(**kw) -> DefectCandidate:
    base = dict(id=f"D-{time.time_ns()}", task_id="T-none", rule="J-01", kind="显示异常", title="t", summary="s", case_id="TC-J-001",
                ota="OTA-2", model="A01-CN", fingerprint="A01-CN|TC-J-001|a1|J-01|", finder="张工", rig="B2", session="S")
    return DefectCandidate(**{**base, **kw})


def save(d):
    get_store().save_defect(d.model_dump())
    return d


@pytest.mark.parametrize("versions,status,category", [(["OTA-2"], "Open", "duplicate"), (["OTA-1"], "Open", "continuation"),
                                                      (["OTA-1"], "Closed", "regression")])
async def test_dedupe_categories(jira, versions, status, category):
    jira.issues = [{"key": "A01-9", "fields": {"summary": "s", "status": {"name": status}, "versions": [{"name": v} for v in versions]}}]
    assert (await get_jira().find_dup(defect())).category == category
    q = jira.sent("/search")[-1]
    assert q[0] == "GET"


async def test_confirm_creates_in_model_project_with_reporter(jira, cloud):  # noqa: F811
    cloud.save_user_profile("u1", "张工", "zhang@x.com")
    jira.users["zhang@x.com"] = "zhangsan"
    d = save(defect())
    r = await svc_mod.get_service().review(d.id, "confirm", by="李工")
    body = json.loads(jira.sent("/rest/api/2/issue")[0][3])["fields"]
    assert r.status == "confirmed" and r.jira.key == "A01-1" and body["project"]["key"] == "A01"
    assert body["reporter"] == {"name": "zhangsan"} and body["components"] == [{"name": "座舱"}]
    assert "张工" in body["description"] and "李工" in body["description"] and any(x.startswith("fp-") for x in body["labels"])
    assert cloud.user_profile("张工")["jira_user"] == "zhangsan"  # 查到后记下来


async def test_manual_jira_user_wins(jira, cloud):  # noqa: F811
    cloud.save_user_profile("u1", "张工", "zhang@x.com")
    cloud.set_jira_user("张工", "zs01")
    await svc_mod.get_service().review(save(defect()).id, "confirm", by="李工")
    assert json.loads(jira.sent("/rest/api/2/issue")[0][3])["fields"]["reporter"] == {"name": "zs01"}
    assert not jira.sent("/user/search")


async def test_no_reporter_permission_still_files(jira, cloud):  # noqa: F811
    cloud.save_user_profile("u1", "张工", "z@x.com")
    cloud.set_jira_user("张工", "zs01")
    jira.reject_reporter = True
    r = await svc_mod.get_service().review(save(defect()).id, "confirm", by="李工")
    assert r.status == "confirmed" and len(jira.sent("/rest/api/2/issue")) == 2


async def test_duplicate_found_at_review_links_instead(jira):
    d = save(defect())
    jira.issues = [{"key": "A01-7", "fields": {"summary": "s", "status": {"name": "Open"}, "versions": [{"name": "OTA-2"}]}}]
    r = await svc_mod.get_service().review(d.id, "confirm", by="李工")
    assert r.status == "linked" and r.jira.key == "A01-7" and not jira.sent("/rest/api/2/issue")


async def test_second_reviewer_is_told_who_did_it(jira):
    d = save(defect())
    svc = svc_mod.get_service()
    await svc.review(d.id, "reject", by="李工", reason="判定误报")
    with pytest.raises(ValueError, match="李工"):
        await svc.review(d.id, "confirm", by="王工")


async def test_failed_create_stays_pending_and_keeps_evidence(jira, monkeypatch):
    root = get_settings().evidence_dir / "R-keep"
    root.mkdir(exist_ok=True)
    (root / "video.mp4").write_bytes(b"v")
    d = save(defect(run_id="R-keep"))

    async def boom(f, files):
        raise httpx.ConnectError("断开")

    monkeypatch.setattr(get_jira(), "_rest_create", boom)
    with pytest.raises(ValueError, match="建票失败"):
        await svc_mod.get_service().review(d.id, "confirm", by="李工")
    assert get_store().get_defect(d.id)["status"] == "pending" and (root / "video.mp4").exists()


async def test_evidence_uploaded_then_deleted(jira):
    root = get_settings().evidence_dir / "R-up"
    root.mkdir(exist_ok=True)
    for n in ("video.mp4", "log.tar.zip", "step-07.png", "step-01.png", "step-07.ui.json"):
        (root / n).write_bytes(b"x")
    d = save(defect(run_id="R-up", actual_shot="/evidence/R-up/step-07.png"))
    r = await svc_mod.get_service().review(d.id, "confirm", by="李工")
    assert sorted(r.jira.attached) == ["log.tar.zip", "step-07.png", "video.mp4"] and not root.exists()


async def test_reject_deletes_evidence(jira):
    root = get_settings().evidence_dir / "R-rj"
    root.mkdir(exist_ok=True)
    d = save(defect(run_id="R-rj"))
    await svc_mod.get_service().review(d.id, "reject", by="李工", reason="判定误报")
    assert not root.exists()


def test_attachments_drop_video_first(tmp_path):
    log, vid, shot = tmp_path / "l.tar.zip", tmp_path / "v.mp4", tmp_path / "s.png"
    log.write_bytes(b"x" * 20)
    vid.write_bytes(b"x" * 20)
    shot.write_bytes(b"x" * 5)
    picked, skipped = pick_attachments([vid, shot, log], limit=30)
    assert [p.name for p in picked] == ["l.tar.zip", "s.png"] and [p.name for p in skipped] == ["v.mp4"]
    assert ATTACH_LIMIT == 30 * 1024 * 1024


async def test_oversize_noted_in_ticket(jira, monkeypatch):
    root = get_settings().evidence_dir / "R-big"
    root.mkdir(exist_ok=True)
    (root / "video.mp4").write_bytes(b"x" * 40)
    monkeypatch.setattr("cabin_cloud.integ.jira.pick_attachments", lambda fs: ([], fs))
    r = await svc_mod.get_service().review(save(defect(run_id="R-big")).id, "confirm", by="李工")
    assert r.jira.skipped == ["video.mp4"] and "超过附件上限" in json.loads(jira.sent("/rest/api/2/issue")[0][3])["fields"]["description"]


# ---------- 持久化与归属 ----------
async def test_review_works_after_restart(jira):
    d = save(defect())
    svc = svc_mod.get_service()
    svc.tasks.clear()  # 内存里的任务没了
    assert [x.id for x in svc.pending_reviews()] == [d.id]
    assert (await svc.review(d.id, "reject", by="李工", reason="判定误报")).status == "rejected"


def test_expire_old_reviews(jira, cloud):  # noqa: F811
    old = save(defect(created_at=time.time() - 8 * 86400))
    new = save(defect())
    assert svc_mod.get_service().expire_reviews() == 1
    assert cloud.get_defect(old.id)["status"] == "expired" and cloud.get_defect(new.id)["status"] == "pending"


async def test_scheduled_run_goes_to_module_members(agent, jira, monkeypatch, tmp_path):  # noqa: F811
    f = tmp_path / "m.yaml"
    f.write_text("modules:\n  - {code: RIG, name: 台架, members: [甲, 乙]}\n", "utf8")
    monkeypatch.setattr(get_settings(), "modules_file", f)
    modules.reload()
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    await case_with_plan()
    st = await submit()  # 这里的任务没有登录用户
    await agent.claim_once()
    await agent.drain()
    await st.job
    d = svc_mod.get_service().pending_reviews()[-1]
    assert d.finder == "" and d.assignees == ["甲", "乙"] and d.module == "台架" and d.model == "A01-CN"
    modules.reload()


async def test_same_problem_from_two_rigs_merges(agent, jira, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    await case_with_plan()
    st = await submit(tid="T-j-a")
    await agent.claim_once()
    await agent.drain()
    await st.job
    first = svc_mod.get_service().pending_reviews()[-1]
    st2 = await submit(tid="T-j-b")
    await agent.claim_once()
    await agent.drain()
    await st2.job
    assert len(svc_mod.get_service().pending_reviews()) == 1
    assert st2.defects == [] and first.id == svc_mod.get_service().pending_reviews()[0].id


# ---------- 配置接口 ----------
def client(name):
    token = auth.issue_session(auth.User(id=name, name=name))
    get_store().save_user_profile(name, name, "")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Authorization": f"Bearer {token}"})


async def test_secret_is_encrypted_and_never_returned(jira, cloud):  # noqa: F811
    async with client("管理员") as c:
        body = {"url": "http://j.test/", "user": "bot", "secret": "明文密码", "projects": {"B01-EU": {"project": "B01", "component": ""}}}
        assert (await c.put("/api/settings/jira", json=body)).status_code == 200
        r = (await c.get("/api/settings/jira")).json()
    assert "明文密码" not in json.dumps(cloud.get_kv("jira"), ensure_ascii=False) and r["has_secret"] is True
    assert "明文密码" not in json.dumps(r, ensure_ascii=False) and "secret" not in r
    assert get_jira().cfg["secret"] == "明文密码" and get_jira().project_of("B01-EU")["project"] == "B01"


async def test_blank_secret_keeps_old(jira):
    async with client("管理员") as c:
        await c.put("/api/settings/jira", json={"url": "http://j.test", "user": "bot", "secret": "旧"})
        await c.put("/api/settings/jira", json={"url": "http://j.test", "user": "bot2", "secret": ""})
    assert get_jira().cfg["secret"] == "旧" and get_jira().cfg["user"] == "bot2"


async def test_only_admin_changes_jira(jira):
    async with client("普通人") as c:
        assert (await c.get("/api/settings/jira")).status_code == 403
        assert (await c.put("/api/settings/jira", json={"url": "http://x"})).status_code == 403


async def test_secret_needs_key(jira, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "")
    async with client("管理员") as c:
        r = await c.put("/api/settings/jira", json={"url": "http://x", "secret": "s"})
    assert r.status_code == 409 and "CABIN_SECRET_KEY" in r.text


async def test_token_auth_uses_bearer(jira, cloud):  # noqa: F811
    cloud.set_kv("jira", {**cloud.get_kv("jira"), "token_auth": True})
    assert await get_jira().myself() == "cabin-bot"
    assert jira.calls[-1][2].startswith("Bearer ")


async def test_user_fills_own_jira_name(jira, cloud):  # noqa: F811
    async with client("王工") as c:
        await c.put("/api/settings/me", json={"jira_user": "wang01"})
        assert (await c.get("/api/settings/me")).json()["jira_user"] == "wang01"


async def test_sync_status_counts_open_and_closed(jira, cloud):  # noqa: F811
    d = save(defect(status="confirmed", jira={"tier": "rest", "key": "A01-3"}))
    await svc_mod.get_service().sync_jira()
    assert cloud.get_defect(d.id)["jira_status"] == "Closed"
    assert stats.overview is not None


async def test_board_marks_mine(jira):
    save(defect(assignees=["王工"]))
    save(defect(assignees=["李工"], fingerprint="x"))
    async with client("王工") as c:
        rows = (await c.get("/api/board")).json()["defects"]
    assert sorted(r["mine"] for r in rows) == [False, True] and rows[0]["model"] == "A01-CN"
