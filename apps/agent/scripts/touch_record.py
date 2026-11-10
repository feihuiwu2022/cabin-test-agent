"""UI 地图录制（台架端）：让 CabinPilot 在车机上按用例步骤操作一遍，录下点击路径，上传云端待确认。

用法（apps/agent/ 下）：
  uv run python scripts/touch_record.py --steps "打开设置;点击空调;打开空调开关" --package com.android.car.settings \
      [--case TC-SET-001] [--serial emulator-5554 | --simcar] [--sw 版本] [--max-steps 12]
每一步的编号选择由云端多模态模型给出，台架不持有模型凭证；需要已注册的台架令牌（CABIN_DATA_DIR/rig_token）。
录完上传云端，在云端回看页确认后，才并入 UI 地图、用于生成测试代码：
  {CABIN_CLOUD_URL}/api/uimap/recordings/<录制编号>
"""
import argparse
import asyncio

from cabin_agent import secret, uimap_rec
from cabin_agent.client import CloudClient
from cabin_agent.config import get_settings
from cabin_common.uimap import MAX_STEPS


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", required=True, help="用例步骤，分号分隔")
    ap.add_argument("--package", required=True, help="被测应用包名（录制起点）")
    ap.add_argument("--case", default="")
    ap.add_argument("--serial", default="")
    ap.add_argument("--simcar", action="store_true")
    ap.add_argument("--sw", default="", help="软件版本（缺省取车机 OTA 版本）")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    a = ap.parse_args()
    s = get_settings()
    if not s.cloud_url:
        ap.error("需要 CABIN_CLOUD_URL（选编号与上传都走云端）")
    token_file = s.data_dir / "rig_token"
    if not token_file.is_file():
        ap.error(f"台架还没注册（{token_file} 不存在），先启动一次 cabin-agent 完成注册")
    steps = [x.strip() for x in a.steps.replace("；", ";").split(";") if x.strip()]
    client = CloudClient(s.cloud_url, secret.load(token_file))
    if a.simcar:
        from cabin_agent.device.simcar import SimCarAdapter
        dev = SimCarAdapter(bugs="")
    else:
        from cabin_agent.device.adb import AndroidAdbAdapter
        dev = AndroidAdbAdapter(a.serial or s.adb_serial)
    try:
        await dev.start()
        sw = a.sw or (await dev.health()).ota
        r = await uimap_rec.record(dev, steps, a.package, uimap_rec.CloudDecider(client), case_id=a.case,
                                   max_steps=a.max_steps, sw_version=sw, model=s.rig_model, rig_id=s.rig_id)
        print(f"录制 {r.rec_id}：{r.outcome} {r.note} · {len(r.trace)} 步 · {len(r.pages)} 个页面")
        for st in r.trace:
            print(f"  {st.n}. {st.action} {st.control.name() if st.control else ''} ← 用例步骤 {st.case_step or '—'}：{st.reason}")
        up = await uimap_rec.upload(client, r)
        print(f"已上传，待确认：{s.cloud_url.rstrip('/')}{up.get('review_url', '')}")
    finally:
        await dev.stop()
        await client.close()
    return 0 if r.outcome == "done" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
