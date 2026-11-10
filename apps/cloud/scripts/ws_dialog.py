"""里程碑 M5/M8 自验证：通过 WebSocket 走完 演示闭环（文本输入代替语音）。

用法：uv run python scripts/ws_dialog.py [ws://127.0.0.1:8800/ws]
"""
import asyncio
import json
import os
import sys
import time

import httpx
import websockets

AUTH = {"Authorization": f"Bearer {os.environ['CABIN_E2E_TOKEN']}"} if os.getenv("CABIN_E2E_TOKEN") else {}  # 用例闭环需要登录
URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8800/ws"
SCRIPT = [
    ("今天上海天气怎么样？", "reply"),  # 与测试无关：应说明只负责测试
    ("在B2台架测一下显示设置的亮度页", "confirm"),
    ("确认", "defect"),
    ("确认是缺陷", "done"),
    ("刚才的任务结果怎么样？", "reply"),
]


async def main():
    httpx.post(URL.replace("ws://", "http://").replace("/ws", "/api/demo/reset"), trust_env=False)
    async with websockets.connect(URL, max_size=2 ** 24, additional_headers=AUTH) as ws:
        await ws.send(json.dumps({"type": "session.open", "tts": True}))
        stats = {"audio": 0}
        for text, wait_for in SCRIPT:
            print(f"\n>>> {text}")
            t0 = time.time()
            first = None
            await ws.send(json.dumps({"type": "user.text", "text": text}))
            got = set()
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 150))
                except asyncio.TimeoutError:
                    print("   !! 超时")
                    break
                t = m["type"]
                if t == "screen.frame":
                    continue
                if t == "reply.audio":
                    stats["audio"] += 1
                    continue
                if t == "reply.text":
                    if m["text"]:
                        first = first or time.time() - t0
                        print(f"   bot{'(主动)' if m['turn'] < 0 else ''}: {m['text']}" + (f"  [来源：{m['src']}]" if m.get("src") else ""))
                    elif m.get("src"):
                        print(f"   [来源：{m['src']}]")
                    if m.get("final") and m["turn"] >= 0:
                        got.add("reply")
                elif t == "session.state":
                    print(f"   [state] mode={m['mode']} authed={m['authed']} ")
                    if m["mode"] == "lab":
                        got.add("mode")
                elif t == "confirm.request":
                    print(f"   [confirm.request] {m['plan']['id']} steps={m['plan']['summary']}")
                    got.add("confirm")
                elif t == "case.card":
                    print(f"   [case] {m['case']['id']} 预期：{m['case']['expected'][:40]}…")
                elif t == "task.event":
                    print(f"   [task] {m['status']} {m.get('desc') or ''}")
                    if m["status"] in ("已完成", "执行中断", "环境异常"):
                        got.add("done")
                elif t == "task.step":
                    print(f"   [step] {m['step']['idx']} {m['step']['action']} {m['step']['label']}")
                elif t == "judge.verdict":
                    v = m["verdict"]
                    print(f"   [verdict] consistent={v['consistent']} conf={v['confidence']} {v.get('diff_summary')}")
                elif t == "defect.card":
                    d = m["defect"]
                    print(f"   [defect] {d['title']} · {d['dup']['label']} {d['dup']['issue_key']}")
                    got.add("defect")
                elif t == "feishu.result":
                    print(f"   [feishu] {m['tier']} {m['title']}")
                elif t == "stage":
                    print(f"   [stage {m['stage'] + 1:02d}] {m['name']}")
                elif t == "error":
                    print("   [error]", m["text"])
                if wait_for in got and (wait_for != "reply" or first):
                    # 等主动播报 / 音频跟上
                    await asyncio.sleep(1.5 if wait_for != "done" else 4)
                    break
            print(f"   (首句 {first:.1f}s)" if first else "")
        # 收尾消息
        try:
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 6))
                if m["type"] == "reply.text" and m["text"]:
                    print(f"   bot: {m['text']}")
                if m["type"] == "reply.audio":
                    stats["audio"] += 1
        except asyncio.TimeoutError:
            pass
        print(f"\nTTS 音频段数：{stats['audio']}")


asyncio.run(main())
