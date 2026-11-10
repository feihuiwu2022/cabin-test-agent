"""配置飞书用例库多维表格：没有链接时由应用创建；开放企业内链接可编辑；写入配置。

用法：uv run python scripts/setup_bitable.py [--url https://xxx.feishu.cn/base/<token>?table=<id>] [--fresh]
                                            [--module VOICE]
--fresh  清掉数据表的默认空行和默认列（只用于应用自己新建的表）。
--module 给某个模块单独配表：链接写进 backend/modules.yaml 该模块的 bitable；不加则写 .env 的默认表。
"""
import argparse
import asyncio
import re

from cabin_cloud import modules
from cabin_cloud.config import ROOT_DIR, get_settings
from cabin_cloud.integ.bitable import BitableClient, BitableError, parse_url


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--name", default="CabinPilot 用例库")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--module", default="", help="模块缩写，如 VOICE")
    args = ap.parse_args()
    s = get_settings()
    bt = BitableClient()
    code = args.module.strip().upper()
    if code:
        m = modules.get(code)
        if not m.listed:
            print(f"modules.yaml 里没有模块 {code}，先在文件里加上这一行再执行")
            return 1
        url = args.url or m.bitable
        args.name = args.name if args.name != "CabinPilot 用例库" else f"CabinPilot 用例库 · {m.name}"
    else:
        url = args.url or s.feishu_bitable_url
    async with bt._client() as c:
        if not url:
            d = (await bt._call(c, "POST", "/bitable/v1/apps", json={"name": args.name}))["app"]
            url = f"{d['url']}?table={d['default_table_id']}"
            args.fresh = True
            print("已创建多维表格")
        token, table, _ = parse_url(url)
        base = f"/bitable/v1/apps/{token}/tables/{table}"
        if args.fresh:
            await bt._call(c, "PATCH", base, json={"name": "用例"})
            rows = (await bt._call(c, "GET", f"{base}/records", params={"page_size": 500})).get("items") or []
            if rows:
                await bt._call(c, "POST", f"{base}/records/batch_delete", json={"records": [r["record_id"] for r in rows]})
            for f in (await bt._call(c, "GET", f"{base}/fields", params={"page_size": 100})).get("items") or []:
                if not f.get("is_primary"):
                    await bt._call(c, "DELETE", f"{base}/fields/{f['field_id']}")
            print(f"已清理默认内容：{len(rows)} 行")
        # 应用创建的表归应用所有，开放企业内链接可编辑，测试工程师才能打开和审核
        try:
            await bt._call(c, "PATCH", f"/drive/v1/permissions/{token}/public", params={"type": "bitable"},
                           json={"link_share_entity": "tenant_editable"})
            print("已开放：企业内获得链接的人可编辑")
        except BitableError as e:  # 只影响人打开表格，不影响应用读写
            print(f"未能开放链接权限，表格暂时只有应用能访问：{str(e)[:160]}")
    if code:
        path = s.modules_file
        text = path.read_text("utf8")
        pat = re.compile(r"^(\s*-\s*\{[^}\n]*\bcode:\s*" + code + r"\b[^}\n]*?\bbitable:\s*)(\"[^\"]*\"|'[^']*'|[^,}\s]*)",
                         re.M)
        if not pat.search(text):
            print(f"没能在 {path.name} 里定位 {code} 的 bitable 字段，请手工填入：{url}")
            return 1
        path.write_text(pat.sub(lambda mm: f'{mm.group(1)}"{url}"', text, count=1), "utf8")
        modules.reload()
        print(f"已写入 {path.name}：{code} → {url}")
        return 0
    env = ROOT_DIR / ".env"
    text = env.read_text("utf8")
    line = f"FEISHU_BITABLE_URL={url}"
    text = re.sub(r"^FEISHU_BITABLE_URL=.*$", line, text, flags=re.M) if "FEISHU_BITABLE_URL=" in text else text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text, "utf8")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
