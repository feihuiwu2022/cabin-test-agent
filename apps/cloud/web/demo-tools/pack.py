"""把用例生成 / 多台架看板的录像整理成静态演示用的 rec.json，并下载其中引用的截图。

用法（演示环境：云端 http://127.0.0.1:8800 + 三台 SimCar 台架 B1/B2/B3，B3 设 CABIN_SIMCAR_BUGS=no_exec）：
  node demo-tools/record_cases.cjs <目录>     # 示例 PRD → 生成 → 采纳建议并提交审核 → 测试代码 → 首次试跑
  node demo-tools/record_board.cjs <目录>     # 三台台架同时“立即运行”用例集
  python3 demo-tools/pack.py cases <目录>/cases.json <输出目录>
  python3 demo-tools/pack.py board <目录>/board.json <输出目录>
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

CLOUD = "http://127.0.0.1:8800"
kind, src, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
r = json.loads(src.read_text("utf8"))
shots: set[str] = set()


def fix(raw: str) -> str:
    shots.update(re.findall(r'/evidence/[^"\\]+', raw))
    return raw.replace('"/evidence/', '"evidence/')


if kind == "cases":
    segs: dict[str, list] = {"open": [], "import": [], "submit": []}
    cur = "open"
    for e in r["ev"]:
        if "mark" in e:
            cur = e["mark"] if e["mark"] in segs else None
            continue
        if cur and e["d"] == "in":
            segs[cur].append({"t": e["t"], "m": json.loads(fix(e["m"]))})
    data = {"segs": segs, "http": r["http"]}
    print({k: len(v) for k, v in segs.items()})
else:
    snaps, prev = [], None
    for s in r["snaps"]:
        j = fix(s["j"])
        body = json.loads(j)
        body.pop("now", None)
        key = json.dumps(body, sort_keys=True)
        if key != prev:
            snaps.append({"t": s["t"], "j": j})
            prev = key
    data = {"snaps": snaps, "http": {k: fix(v) for k, v in r["http"].items()}}
    print("snaps", len(snaps))

out.mkdir(parents=True, exist_ok=True)
(out / "rec.json").write_text(json.dumps(data, ensure_ascii=False), "utf8")
for s in sorted(shots):
    f = out / s.lstrip("/")
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        f.write_bytes(urllib.request.urlopen(CLOUD + s).read())
    except Exception as e:  # 证据已过期等：页面上该图显示不出来，不影响回放
        print("跳过", s, e)
print("截图", len(shots))
