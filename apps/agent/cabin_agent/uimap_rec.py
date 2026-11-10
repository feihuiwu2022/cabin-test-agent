"""UI 地图录制的台架端：在车机上按用例步骤操作一遍，录下点击路径。

台架不持有模型凭证：每一步把带编号的截图发给云端，云端的多模态模型只回编号，台架按编号的控件框执行。
录完连同截图上传云端，状态为“待确认”；测试工程师在云端回看、确认后才并入 UI 地图、用于生成测试代码。
"""
from __future__ import annotations

import asyncio
import json

from cabin_common import uimap as um
from cabin_common.uimap import Decider, RecStep, Recording, UiPage

from . import assets
from .client import CloudClient
from .device.base import DeviceAdapter
from .runner import vision_local

SETTLE_S = 0.8


class CloudDecider:
    """经云端选编号。"""

    def __init__(self, client: CloudClient, rec_id: str = ""):
        self.client, self.rec_id = client, rec_id

    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        meta = {"rec_id": self.rec_id, "prompt": prompt}
        return await self.client.call_retry("POST", "/api/v1/uimap/decide", data={"meta": json.dumps(meta)},
                                            files=[("image", ("marked.png", image, "image/png"))]) or {}


def _ocr_boxes(png: bytes) -> list[tuple[str, list[int], float]]:
    """控件树为空（如 Unity / WebView 页面）时用本地 OCR 文字框编号；没装 OCR 时为空。"""
    if vision_local.ocr_unavailable():
        return []
    return vision_local.get_ocr().read(png)


async def record(dev: DeviceAdapter, steps: list[str], package: str, decider: Decider, *, case_id: str = "",
                 max_steps: int = um.MAX_STEPS, sw_version: str = "", model: str = "", rig_id: str = "",
                 session: str = "", settle_s: float = SETTLE_S) -> Recording:
    """录制结果里的截图按哈希存在本机图像缓存，upload() 时一并上传。"""
    size = await dev.screen_size()
    rec = Recording(rec_id=um.new_rec_id(case_id), case_id=case_id, rig_id=rig_id, steps_text=list(steps),
                    package=package, sw_version=sw_version, model=model, screen=list(size))
    if isinstance(decider, CloudDecider):
        decider.rec_id = rec.rec_id
    await dev.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
    await asyncio.sleep(settle_s)
    pending: RecStep | None = None
    try:
        for n in range(1, max_steps + 1):
            png, els, screen = await dev.screenshot("png"), await dev.dump_ui(), await dev.current_screen()
            sig = um.page_sig(screen, els)
            marks = um.marks_from(els, size) or um.marks_from(els, size, _ocr_boxes(png))
            if pending is not None and pending.control and pending.control.checkable:
                tapped = next((e for e in els if e.key and e.key == pending.control.key), None)
                pending.after_checked = tapped.checked if tapped else None
            if sig not in rec.pages:
                title = next((e.text for e in els if e.text), "")
                rec.pages[sig] = UiPage(sig=sig, screen=screen, title=title, shot_sha=assets.store_image(png),
                                        controls=marks)
            marked = um.render_marks(png, marks)
            ans = await decider.decide(um.record_prompt(steps, rec.trace, marks, screen), marked, session)
            act = str(ans.get("action") or "fail")
            st = RecStep(n=n, page=sig, action=act if act in ("tap", "back", "done", "fail") else "fail",
                         case_step=um.as_int(ans.get("step")), reason=str(ans.get("reason") or "")[:120],
                         marked_sha=assets.store_image(marked))
            if st.action == "tap":
                k = um.as_int(ans.get("mark"))
                if not k or not 1 <= k <= len(marks):
                    st.action, st.reason = "fail", f"模型给了不存在的编号 {ans.get('mark')}"
                else:
                    c = marks[k - 1]
                    if not c.text and not c.key:  # 无文字无 id 的控件：存图标裁图，回放时用模板定位
                        c = c.model_copy(update={"icon_sha": assets.store_image(vision_local.crop(png, c.bounds))})
                    st.mark, st.control = k, c
            rec.trace.append(st)
            pending = st
            if st.action in ("done", "fail"):
                rec.outcome, rec.note = st.action, st.reason
                break
            if st.action == "back":
                await dev.key("back")
            else:
                l, t, r, b = st.control.bounds
                await dev.tap((l + r) // 2, (t + b) // 2)
            await asyncio.sleep(settle_s)
        else:
            rec.outcome, rec.note = "max_steps", f"达到 {max_steps} 步上限仍未完成"
    except Exception as e:  # 设备断开 / 云端不可用：保留已录部分
        rec.outcome, rec.note = "error", f"{type(e).__name__}: {e}"
    return rec


async def upload(client: CloudClient, rec: Recording) -> dict:
    """录制 + 引用的截图上传云端，云端校验哈希后保存为“待确认”。"""
    files = [("images", (f"{sha}.png", assets.read_image(sha), "image/png")) for sha in sorted(rec.image_shas())]
    return await client.call_retry("POST", "/api/v1/uimap/recordings", data={"meta": rec.model_dump_json()},
                                   files=files) or {}
