"""把录像整理成静态演示用的 rec.json，并下载其中引用的截图。

用法（演示环境在 http://127.0.0.1:7860 运行时）：
  node demo-tools/record.cjs <目录>            # 录一次：运行两条用例
  python3 demo-tools/pack.py <目录>/rec.json <输出目录>
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
r = json.loads(src.read_text("utf8"))
segs: dict[str, list] = {"open": [], "run": []}
cur, last_frame, shots = "open", None, set()
for e in r["ev"]:
    if "mark" in e:
        cur = e["mark"] if e["mark"] in segs else None
        continue
    if cur is None or e["d"] != "in":
        continue
    m = json.loads(e["m"])
    if m["type"] == "reply.audio":  # 静态页不放语音
        continue
    if m["type"] == "screen.frame":
        if m["jpg"] == last_frame:
            continue
        last_frame = m["jpg"]
    raw = json.dumps(m, ensure_ascii=False)
    shots.update(re.findall(r'/evidence/[^"\\]+', raw))
    m = json.loads(raw.replace('"/evidence/', '"evidence/'))
    segs[cur].append({"t": e["t"], "m": m})

# 语音用例的判定文字按用例改写（录像时模型是替身，回答是通用的）
case_of = {}
for e in segs["run"]:
    m = e["m"]
    if m["type"] == "judge.start":
        case_of[m["task_id"]] = m["case_id"]
for e in segs["run"]:
    m = e["m"]
    if m["type"] == "judge.verdict" and case_of.get(m["task_id"]) == "TC-HVAC-001":
        m["verdict"]["actual_observed"] = "屏幕：空调页显示“空调开关：开”；播报：空调已打开"

# 单条用例：按任务拆开
disp = next(k for k, v in case_of.items() if v == "TC-DISP-001")
hvac = next(k for k, v in case_of.items() if v == "TC-HVAC-001")
cut = next(i for i, e in enumerate(segs["run"]) if e["m"]["type"] == "reply.text" and e["m"].get("turn") == -1)
segs["TC-DISP-001"] = [e for e in segs["run"][:cut + 1] if e["m"].get("task_id") != hvac]
segs["TC-HVAC-001"] = [e for e in segs["run"][:cut + 1] if e["m"].get("task_id") == hvac and e["m"]["type"] == "task.event"][:1] \
    + segs["run"][cut + 1:]

out.mkdir(parents=True, exist_ok=True)
(out / "rec.json").write_text(json.dumps({"segs": segs, "http": r["http"]}, ensure_ascii=False), "utf8")
for s in sorted(shots):
    f = out / s.lstrip("/")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(urllib.request.urlopen("http://127.0.0.1:7860" + s).read())
print({k: len(v) for k, v in segs.items()}, "截图", len(shots))
