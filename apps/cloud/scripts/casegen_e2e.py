"""一期 A 用例闭环端到端自验证（真实模型）：经 WebSocket 走完
导入 PRD → 需求点 / 待确认项 → 用例 + 自检 → 人工审核（含修改、驳回）→ 入库 + 通知 → 按车型拉取（A-15）→ 测试代码 + 静态校验。

用法（backend/ 下，服务已启动）：
  uv run python scripts/casegen_e2e.py                       # 示例 PRD §4.1
  uv run python scripts/casegen_e2e.py "把 D:\\docs\\x.docx 的 §3.2 转成测试用例"
  uv run python scripts/casegen_e2e.py "https://example.com/prd.md 生成用例"
环境变量 CABIN_E2E_URL 指定服务地址（默认 http://127.0.0.1:8800）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import websockets

from cabin_cloud.config import get_settings

AUTH = {"Authorization": f"Bearer {os.environ['CABIN_E2E_TOKEN']}"} if os.getenv("CABIN_E2E_TOKEN") else {}  # 用例闭环需要登录
BASE = os.getenv("CABIN_E2E_URL", f"http://127.0.0.1:{get_settings().port}")


async def main(cmd: str) -> int:
    t0 = time.time()
    ws_url = BASE.replace("http", "ws", 1) + "/ws"
    async with websockets.connect(ws_url, max_size=2 ** 24, additional_headers=AUTH) as ws:
        async def send(m: dict) -> None:
            await ws.send(json.dumps(m, ensure_ascii=False))

        async def until(pred, timeout: float = 300) -> dict:
            end = time.time() + timeout
            while time.time() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
                if m["type"] == "reply.text" and m.get("text") and m.get("final"):
                    print(f"  [{time.time() - t0:5.1f}s] 机器人：{m['text']}")
                if m["type"] == "error":
                    print(f"  [{time.time() - t0:5.1f}s] 错误：{m['text']}")
                if m["type"] == "reply.text" and "失败" in (m.get("text") or ""):
                    raise RuntimeError(m["text"])
                if pred(m):
                    return m
            raise TimeoutError

        await send({"type": "session.open", "terminal": "e2e", "tts": False})
        await send({"type": "tts.set", "on": False})
        await until(lambda m: m["type"] == "session.state")
        print(f"发送：{cmd}")
        await send({"type": "user.text", "text": cmd, "via": "text"})
        doc = await until(lambda m: m["type"] in ("casegen.doc", "reply.text") and (m["type"] == "casegen.doc" or m.get("whole")))
        if doc["type"] != "casegen.doc":
            print("未进入用例生成：", doc.get("text"))
            return 1
        print(f"  文档：《{doc['name']}》{doc['section'] and '§' + doc['section']} · {doc['n_blocks']} 段")
        reqs = await until(lambda m: m["type"] == "casegen.reqs", 240)
        for r in reqs["requirements"]:
            print(f"  需求 {r['req_id']} §{r['section']} {r['summary']}")
        for a in reqs["ambiguities"]:
            print(f"  待确认 {a['id']} §{a['section']} {a['question']}")
        rv = await until(lambda m: m["type"] == "casereview.open", 300)
        cases = rv["cases"]
        cov = rv["coverage"]
        print(f"  生成 {len(cases)} 条 · 覆盖 {cov['covered']}/{cov['total_reqs']} · 类型 {cov['by_type']} · 自检有意见 {len(cov['flagged'])}")
        for c in cases:
            flag = "；".join(f"{k['kind']}:{k['msg']}" for k in c["checks"])
            print(f"   {c['case_id']} [{c['type']}/{'+'.join(c['channel'])}] {c['title']} | "
                  f"{('“' + c['utterance'] + '” → ' + c['expect_screen'] + ' / ' + c['expect_tts']) if c['utterance'] else c['expected']}"
                  f"{' ⚠ ' + flag if flag else ''}")

        # 人工审核：自检建议驳回的驳回；第一条无意见的用例做一次修改（验证“修改后通过”进入测试代码）
        decisions, edited = [], False
        for c in cases:
            if c["suggestion"] == "驳回":
                decisions.append({"case_id": c["case_id"], "decision": "reject", "reason": "与已有用例重复"})
            elif not edited and c["precondition"]:
                edited = True
                decisions.append({"case_id": c["case_id"], "decision": "pass",
                                  "edits": {"precondition": c["precondition"] + "；车机已连接台架"}})
            else:
                decisions.append({"case_id": c["case_id"], "decision": "pass"})
        await send({"type": "case.review.submit", "batch_id": rv["batch"]["batch_id"], "decisions": decisions})
        lib = await until(lambda m: m["type"] == "caselib.result")
        print(f"  审核结果 {lib['counts']} · 入库 {len(lib['written'])} 条")
        fs = await until(lambda m: m["type"] == "feishu.result")
        print(f"  飞书（{fs['tier']}）：{fs['title']}")

        models = rv["batch"]["models"] or ["A01-CN"]
        await send({"type": "user.text", "text": f"拉取 {models[0]} 用例", "via": "text"})
        pull = await until(lambda m: m["type"] == "casepull.result")
        need = [r for r in pull["cases"] if r["plan_status"] != "ready"]
        print(f"  拉取 {models[0]}：{len(pull['cases'])} 条，待生成测试代码 {len(need)} 条")
        results = {}
        while len(results) < len(need):
            r = await until(lambda m: m["type"] == "plan.result", 300)
            results[r["case_id"]] = r
            print(f"   {r['case_id']} {r.get('generated_by')} · {'校验通过' if r['ok'] else '未通过 ' + '；'.join(r['errors'])}"
                  f" · {r.get('n_steps')} 步 · git {r.get('plan_version')}" + (f" · 告警 {len(r['warnings'])}" if r['warnings'] else ""))
        await until(lambda m: m["type"] == "reply.text" and "测试代码生成完成" in (m.get("text") or ""), 60)

    async with httpx.AsyncClient(base_url=BASE, timeout=30, trust_env=False) as c:
        a15 = (await c.get("/api/v1/cases", params={"model": models[0]})).json()["data"]["cases"]
        ready = [x for x in a15 if x["plan_status"] == "ready"]
        print(f"A-15 GET /api/v1/cases?model={models[0]} → {len(a15)} 条，ready {len(ready)}")
        if ready:
            r = await c.get(f"/api/v1/plans/{ready[0]['case_id']}")
            print(f"下载 TaskPlan {ready[0]['case_id']} @ {r.headers.get('x-plan-version')}：\n" + r.text[:900])
        edited_id = next((d["case_id"] for d in decisions if d.get("edits")), None)
        if edited_id:
            txt = (await c.get(f"/api/v1/plans/{edited_id}")).text
            print(f"修改后通过的 {edited_id} 测试代码含审核修改：{'车机已连接台架' in txt}")
    print(f"总耗时 {time.time() - t0:.0f}s")
    bad = [r for r in results.values() if not r["ok"]]
    return 1 if bad or not ready else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "把示例 PRD 的 §4.1 转成测试用例")))
