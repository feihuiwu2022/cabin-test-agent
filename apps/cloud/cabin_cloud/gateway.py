"""cabin-gateway：会话管理、WebSocket 协议、意图路由、人工门入口。"""
from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect

from . import casewf
from .auth import resolve, token_of
from .assistant.loop import AgentLoop
from .assistant.tools import ToolCtx
from .bus import bus
from .config import get_settings
from .execsvc.service import STAGES, get_service
from .modelgw import get_gateway
from cabin_common.schema import REJECT_REASONS
from .store import get_store
from .voice.tts import speakable

RE_STOP = re.compile(r"^(停|停下|好了|别说了|闭嘴|stop)[。.!！]*$", re.I)
RE_YES = re.compile(r"^(确认|确定|好的?|可以|开始|执行吧?|没问题|是的?|对|ok|嗯)(执行|开始)?[。.!！,，]*$", re.I)
RE_NO = re.compile(r"^(取消|不要|算了|否|不用了?|不行|先别)[。.!！,，]*$")
RE_TEST_INTENT = re.compile(r"(测一下|测试一下|跑一下|执行.{0,6}(测试|用例)|台架|提单|建单|用例|monkey|回归)")
RE_REVIEW_OK = re.compile(r"(确认|是)(是)?缺陷|确认建单")
RE_REVIEW_LINK = re.compile(r"关联(已有|原)?单")
RE_REVIEW_REJECT = re.compile(r"驳回|不是缺陷|误报")
NEEDS_LOGIN = {"confirm.response", "review.action", "case.review.submit", "caselib.sync", "plan.trial"}  # 要留痕的操作
CASE_OPS = {"case.enter", "case.review.submit", "caselib.sync", "plan.trial", "review.action"}  # 用例相关操作只在云端
CLOUD_NO_RUN = "云端只做用例生成和审核。执行测试请到台架的本地页面。"
_states: dict[str, dict] = {}  # 会话状态按会话编号保留：刷新页面重连后，后台任务和审核进度还在
PUNCT = re.compile(r"[\s，。,.!！?？、:：；;\"'“”]")


class Session:
    def __init__(self, ws: WebSocket, sid: str | None = None, user: str = "测试工程师", surface: str = "cloud",
                 rig: str = ""):
        self.ws = ws
        self.rig = rig  # 本地页面所在的台架
        self.surface = surface  # cloud：云端 Web，用例生成与审核；agent：台架本地页面，执行测试
        self.closed = False
        self.user = user  # 登录用户；空表示未登录，只能看
        self.id = sid or f"S-{uuid.uuid4().hex[:4]}"
        self.terminal = "web"
        self.mode = "lab"  # 只有测试这一个场景
        self.authed = surface == "agent" and bool(user)  # 台架页面的登录用户才能下达测试；云端工作台不调台架
        self.tts_on = True
        self.history: list[dict] = []
        self.state: dict = _states.setdefault(self.id, {})
        self.turn_id = 0
        self.turn_task: asyncio.Task | None = None
        self.tts_tasks: list[asyncio.Task] = []
        self.audio_seq = 0
        self._send_lock = asyncio.Lock()
        self.store = get_store()
        self.svc = get_service()
        self.agent = AgentLoop()
        self.s = get_settings()

    # ---------- 下行 ----------
    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            if self.closed:
                return
            try:
                await self.ws.send_json(msg)
            except (RuntimeError, WebSocketDisconnect):  # 页面已关闭：后台任务继续，消息不再发
                self.closed = True

    async def push_state(self) -> None:
        await self.send({"type": "session.state", "session": self.id, "mode": self.mode, "authed": self.authed,
                         "rig": self.rig or self.s.rig_id, "user": self.user,
                         "device": self.s.device, "model": get_gateway().chat_model,
                         "vision_model": get_gateway().vision_model, "version_label": self.s.version_label,
                         "strict_data": self.s.strict_data, "llm_base": self.s.llm_base_url})

    async def stage(self, n: int) -> None:
        await self.send({"type": "stage", "stage": n, "name": STAGES[n]})

    async def log(self, text: str, kind: str = "") -> None:
        await self.send({"type": "log", "text": text, "kind": kind})

    def _tts(self, turn: int, text: str) -> asyncio.Task:
        """按句并发合成，按序下发。"""
        self.audio_seq += 1
        seq = self.audio_seq
        prev = self.tts_tasks[-1] if self.tts_tasks else None

        async def job():
            audio = None
            if self.tts_on and speakable(text):
                try:
                    audio = await get_gateway().tts(speakable(text), session=self.id)
                except Exception as e:
                    await self.log(f"<b>tts</b> 合成失败：{e}", "err")
            if prev and not prev.done():
                try:
                    await prev
                except BaseException:
                    pass
            if audio and turn == self.turn_id or (audio and turn < 0):
                await self.send({"type": "reply.audio", "turn": turn, "seq": seq, "text": text,
                                 "mp3": base64.b64encode(audio).decode()})

        t = asyncio.create_task(job())
        self.tts_tasks = [x for x in self.tts_tasks if not x.done()] + [t]
        return t

    async def bot_sentence(self, turn: int, text: str) -> None:
        await self.send({"type": "reply.text", "turn": turn, "text": text, "final": False})
        self._tts(turn, text)

    async def say(self, text: str, src: str = "") -> None:
        """系统主动播报（任务进度、结果）。用独立 turn（负数），不被下一句用户话取消前的播报覆盖。"""
        self.store.append(self.id, "assistant.message", {"text": text, "src": src, "proactive": True})
        await self.send({"type": "reply.text", "turn": -1, "text": text, "final": True, "src": src})
        self._tts(-1, text)

    # ---------- 上行 ----------
    async def handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t in NEEDS_LOGIN and not self.user:
            await self.send({"type": "error", "text": "请先登录"})
            return
        if t in CASE_OPS and self.surface == "agent":
            await self.send({"type": "error", "text": self.case_link()})
            return
        if t == "session.open":
            self.terminal = msg.get("terminal", "web")
            self.tts_on = msg.get("tts", True)
            await self.push_state()
            if self.surface == "cloud" and self.user:
                await self.enter_case_workbench()
                await casewf.resume(self)
        elif t == "user.text":
            await self.user_text((msg.get("text") or "").strip(), msg.get("via", "text"), msg.get("attachment"))
        elif t == "barge_in":
            await self.barge_in(msg.get("reason", "voice"))
        elif t == "tts.set":
            self.tts_on = bool(msg.get("on"))
        elif t == "confirm.response":
            await self.confirm(msg.get("kind", "plan"), bool(msg.get("ok")), via="button")
        elif t == "review.action":
            await self.review(msg["defect_id"], msg["action"], msg.get("reason", ""), msg.get("severity", ""),
                              msg.get("summary", ""), msg.get("link_key", ""))
        elif t == "case.review.submit":
            await self.send({"type": "casereview.closed", "batch_id": msg.get("batch_id")})
            await casewf.submit_review(self, msg.get("batch_id", ""), msg.get("decisions") or [])
        elif t == "caselib.sync":
            await casewf.sync_now(self)
        elif t == "plan.trial":
            await casewf.trial(self, msg.get("case_id", ""))
        elif t == "case.enter":
            await self.enter_case_workbench()

    async def barge_in(self, reason: str) -> None:
        self.turn_id += 1  # 让在途音频作废
        for x in self.tts_tasks:
            x.cancel()
        self.tts_tasks.clear()
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.store.append(self.id, "barge_in", {"reason": reason})
        await self.send({"type": "barge_in.ack"})

    async def user_text(self, text: str, via: str, attachment: dict | None = None) -> None:
        if attachment and not text:
            text = f"把这份 PRD 转成测试用例：{attachment.get('name', '')}"
        if not text:
            return
        if RE_STOP.match(text):
            await self.barge_in("stop_word")
            return
        if self.s.demo:  # 公开演示：每个会话每小时的对话轮数有上限（总调用数另在模型网关限制）
            now = time.time()
            self._turn_times = [x for x in getattr(self, "_turn_times", []) if now - x < 3600] + [now]
            if len(self._turn_times) > self.s.demo_turns_per_hour:
                await self.send({"type": "error", "text": "演示版每小时最多对话 %d 轮，稍后再试" % self.s.demo_turns_per_hour})
                return
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.turn_id += 1
        turn = self.turn_id
        self.store.append(self.id, "turn.start", {"turn": turn, "authed": self.authed, "via": via})
        self.store.append(self.id, "user.message", {"text": text, "via": via})
        await self.send({"type": "user.echo", "turn": turn, "text": text, "via": via})
        self.turn_task = asyncio.create_task(self._turn(turn, text, attachment))

    async def _reply(self, turn: int, text: str, src: str = "") -> None:
        self.store.append(self.id, "assistant.message", {"text": text, "src": src})
        await self.send({"type": "reply.text", "turn": turn, "text": text, "final": True, "src": src, "whole": True})
        self._tts(turn, text)

    async def _turn(self, turn: int, text: str, attachment: dict | None = None) -> None:
        try:
            if await self.route(turn, text, attachment):
                return
            await self.agent_turn(turn, text)
        except asyncio.CancelledError:
            self.store.append(self.id, "turn.cancelled", {"turn": turn})
        except Exception as e:
            await self.log(f"<b>error</b> {type(e).__name__}: {e}", "err")
            await self._reply(turn, "抱歉，我这边出了点问题，请再说一遍。")
        finally:
            self.store.append(self.id, "turn.end", {"turn": turn})

    # ---------- 意图路由（确定性部分） ----------
    async def route(self, turn: int, text: str, attachment: dict | None = None) -> bool:
        plain = PUNCT.sub("", text)
        if self.mode == "lab" and self.state.get("pending_plan") and (RE_YES.match(plain) or RE_NO.match(plain)):
            await self.confirm("plan", bool(RE_YES.match(plain)), via="voice", turn=turn)
            return True
        if self.mode == "lab" and self.state.get("pending_cancel") and (RE_YES.match(plain) or RE_NO.match(plain)):
            await self.confirm("cancel", bool(RE_YES.match(plain)), via="voice", turn=turn)
            return True
        if self.mode == "lab" and not attachment and (choice := await casewf.resolve_choice(self, text)) is not None:
            await self._reply(turn, choice)
            return True
        flow = casewf.wants(text, bool(attachment))
        if flow and self.surface == "agent":
            await self._reply(turn, self.case_link())
            return True
        if flow and self.mode == "lab":
            await self.log(f"<b>意图路由</b> 用例闭环 · {flow}", "md")
            if flow == "import":
                reply = await casewf.start_import(self, text, attachment)
            elif flow == "submit":
                reply = await casewf.submit_by_suggestion(self)
            elif flow == "bitable":
                reply = await casewf.read_bitable(self, text)
            elif flow == "sync":
                r = await casewf.sync_now(self)
                reply = (f"已从多维表格读回 {r['rows']} 行：更新 {len(r['updated'])} 条，新增 {len(r['created'])} 条，"
                         f"不合格 {len(r['invalid'])} 行。" if r["ok"] else r["error"])
            else:
                reply = await casewf.pull_and_code(self, text, only_codegen=flow == "code")
            if reply:
                await self._reply(turn, reply)
            return True
        pend = self.svc.pending_reviews(self.id) if self.mode == "lab" and self.surface == "cloud" else []
        if pend:
            d = pend[0]
            if RE_REVIEW_OK.search(plain):
                await self.review(d.id, "confirm")
                return True
            if RE_REVIEW_LINK.search(plain):
                await self.review(d.id, "link")
                return True
            if RE_REVIEW_REJECT.search(plain):
                reason = next((r for r in REJECT_REASONS if r[:2] in plain), "判定误报")
                await self.review(d.id, "reject", reason)
                return True
        if not self.authed and RE_TEST_INTENT.search(plain):
            await self._reply(turn, CLOUD_NO_RUN if self.surface == "cloud" else "请先登录，再下达测试任务。")
            self.store.append(self.id, "hook.denied", {"hook": "login" if self.surface == "agent" else "case_workbench",
                                                       "text": text})
            return True
        return False

    def case_link(self) -> str:
        return f"用例生成和审核在云端：{self.s.public_base_url}/cases，缺陷审核在 {self.s.public_base_url}/board"

    async def enter_case_workbench(self) -> None:
        """云端用例工作台：登录后直接进入，只开放用例闭环（不调台架，执行类工具不挂载）。"""
        if not self.user:
            await self.send({"type": "error", "text": "请先登录"})
            return
        await self.push_state()

    # ---------- Agent 回合 ----------
    async def agent_turn(self, turn: int, text: str) -> None:
        if RE_TEST_INTENT.search(text):
            await self.stage(0)
            await self.log(f"<b>意图路由</b> 测试任务 · {text[:30]}", "md")

        async def emit(kind: str, data: dict) -> None:
            await self.send({"type": kind, **data})
            if kind == "case.card":
                await self.stage(1)
                await self.log(f"<b>read_case</b> {data['source']} → {data['case']['id']}（取预期结果）", "md")
            elif kind == "confirm.request" and data.get("kind") == "plan":
                await self.stage(2)
                await self.log(f"<b>plan_task</b> 生成任务计划（{len(data['plan']['summary'])} 步）· Schema 校验通过", "md")
                self.store.append(self.id, "confirm.request", {"kind": "plan", "task_id": data["plan"]["id"]})

        async def on_tool(name: str, args: dict) -> None:
            await self.log(f"<b>tool.call</b> {name} {args}", "md")

        ctx = ToolCtx(session=self.id, authed=self.authed, utterance=text, user=self.user, rig=self.rig, emit=emit, state=self.state)
        res = await self.agent.handle(ctx, self.history, lambda s: self.bot_sentence(turn, s), on_tool)
        self.history = (self.history + res["messages"])[-16:]
        while self.history and not (self.history[0]["role"] == "user" and isinstance(self.history[0]["content"], str)):
            self.history.pop(0)  # 保证从完整用户回合开始
        await self.send({"type": "reply.text", "turn": turn, "text": "", "final": True, "src": res["src"]})

    # ---------- 人工门：计划确认 / 撤销确认（confirm_gate） ----------
    async def confirm(self, kind: str, ok: bool, via: str, turn: int | None = None) -> None:
        turn = turn if turn is not None else self.turn_id
        self.store.append(self.id, "confirm.response", {"kind": kind, "ok": ok, "by": self.user, "via": via})
        if kind == "plan":
            plan = self.state.pop("pending_plan", None)
            if not plan:
                return
            await self.send({"type": "confirm.closed", "kind": "plan", "ok": ok, "task_id": plan.id})
            if not ok:
                await self._reply(turn, "好的，计划已取消。")
                return
            await self.log("<b>confirm_gate</b> 用户确认 · 任务入队")
            await self._reply(turn, "好的，开始执行。")
            try:
                await self.svc.submit(plan, self.id)
            except ValueError as e:
                await self._reply(turn, f"计划校验没通过：{e}")
        elif kind == "cancel":
            tid = self.state.pop("pending_cancel", None)
            await self.send({"type": "confirm.closed", "kind": "cancel", "ok": ok, "task_id": tid})
            if ok and tid and await self.svc.cancel(tid):
                await self._reply(turn, f"任务已撤销。")
            elif not ok:
                await self._reply(turn, "好的，不撤销。")

    # ---------- 人工门：缺陷审核（review_gate） ----------
    async def review(self, defect_id: str, action: str, reason: str = "", severity: str = "", summary: str = "",
                     link_key: str = "") -> None:
        try:
            await self.svc.review(defect_id, action, by=self.user, reason=reason, severity=severity, summary=summary,
                                  link_key=link_key)
        except (KeyError, ValueError) as e:
            await self.send({"type": "error", "text": str(e)})


async def serve_ws(ws: WebSocket) -> None:
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):  # 其他网页不能借浏览器里的登录状态连过来
        await ws.close(4403)
        return
    await ws.accept()
    user = resolve(token_of(ws.headers, ws.cookies))
    surface = "agent" if ws.query_params.get("surface") == "agent" else "cloud"
    sess = Session(ws, ws.query_params.get("session") or None, user=user.name if user else "", surface=surface,
                   rig=ws.query_params.get("rig", ""))
    bus.attach(sess)
    get_store().append(sess.id, "session.open", {"terminal": ws.query_params.get("terminal", "web"), "at": time.time()})
    try:
        await sess.push_state()
        while True:
            msg = await ws.receive_json()
            await sess.handle(msg)
    except Exception:
        pass
    finally:
        sess.closed = True
        bus.detach(sess)
        if sess.turn_task:
            sess.turn_task.cancel()
