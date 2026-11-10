"""模块路由管理（backend/modules.yaml）。

用法：
  uv run python scripts/modules.py list       列出每个模块的文件夹、用例表、已入库用例数，以及配置问题
  uv run python scripts/modules.py relocate   改了 folder 后，把已有测试代码 / PRD 存档挪到新文件夹（git mv）
"""
import argparse

from cabin_cloud import modules, planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings


def cmd_list() -> int:
    s = get_settings()
    counts: dict[str, int] = {}
    for c in get_caselib().library():
        k = modules.code_of_case(c.case_id)
        counts[k] = counts.get(k, 0) + 1
    print(f"配置文件：{s.modules_file}")
    print(f"默认表：{s.feishu_bitable_url or '（未配置，用例只落本地）'}\n")
    listed = modules.all_modules()
    extra = [modules.get(k) for k in counts if k not in {m.code for m in listed}]
    for m in listed + extra:
        table = m.bitable or ("默认表" if s.feishu_bitable_url else "本地")
        tag = "" if m.listed else "（未在 modules.yaml 列出）"
        print(f"{m.code:<6} {m.name:<6} 文件夹 {m.folder}/  表 {table}  用例 {counts.get(m.code, 0)}{tag}")
    for p in modules.problems():
        print(f"! {p}")
    return 0


def cmd_relocate() -> int:
    moved = planner.relocate()
    for old, new in moved:
        print(f"{old} → {new}")
    print(f"挪动 {len(moved)} 个文件" if moved else "文件夹都已和 modules.yaml 一致")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "relocate"])
    raise SystemExit({"list": cmd_list, "relocate": cmd_relocate}[ap.parse_args().cmd]())
