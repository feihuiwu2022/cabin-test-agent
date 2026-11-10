"""R0 验收：对用例库里已生成的 TaskPlan 逐条干跑（PF-RUN-11），不操作车机，输出校验报告。

用法：uv run python scripts/plan_dry_run.py [--caps adb,audio]
"""
import argparse

from cabin_cloud.caselib import get_caselib
from cabin_cloud.execsvc.service import rig_capabilities
from cabin_cloud.planloader import dry_run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", default=",".join(sorted(rig_capabilities())), help="台架能力标签，逗号分隔")
    caps = set(ap.parse_args().caps.split(","))
    cases = [c for c in get_caselib().library() if c.plan_path]
    n_ok = 0
    reasons: dict[str, int] = {}
    for c in cases:
        r = dry_run(c.case_id, caps)
        n_ok += r["ok"]
        if not r["ok"]:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        mark = "可执行" if r["ok"] else f"阻塞({r['reason']})"
        print(f"{c.case_id} {mark:18} {r.get('n_steps', 0)} 步 · {c.title}")
        for e in r["errors"]:
            print(f"    ✕ {e}")
    print(f"\n台架能力 {sorted(caps)}：共 {len(cases)} 条 · 可执行 {n_ok} · 阻塞 {reasons or 0}")
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
