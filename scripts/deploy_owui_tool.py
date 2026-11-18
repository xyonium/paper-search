#!/usr/bin/env python3
"""把本仓库的 tool.py 推到 OpenWebUI，消除"手工粘贴的第二份副本"。

背景：tool.py 在仓库里只有一份，但 OWUI 运行时用的是工具表里的内容副本。
手工粘贴会让线上版本悄悄落后（2026-11 实测：仓库 2.9.11，OWUI 里还是 2.9.9，
而 2.9.9 在容器 mcp 2.3.0 下直接 ImportError）。这个脚本用 OWUI API 同步，
仓库始终是唯一源；Valves 与 UserValves 不动。

用法：
    export OWUI_API_KEY=sk-...                     # 设置 → 账号 → API 密钥

    python3 scripts/deploy_owui_tool.py --check    # 只比对版本，有漂移退出码 1
    python3 scripts/deploy_owui_tool.py            # 有差异才推送
    python3 scripts/deploy_owui_tool.py --force    # 强制推送

参数（都可环境变量给）：
    --openwebui-url  OWUI_URL / OPENWEBUI_URL      默认 http://open-webui:8080
    --api-key        OWUI_API_KEY / OPENWEBUI_API_KEY
    --tool-id        OWUI_TOOL_ID                  默认 academic_paper_search
    --file                                         默认仓库根的 tool.py

退出码：0 成功/已最新；1 版本漂移（--check）；2 参数或请求错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("需要 requests：pip install requests")

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^version:\s*(\S+)", re.M)


def _version(content: str) -> str:
    m = VERSION_RE.search(content or "")
    return m.group(1) if m else "(none)"


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _get_tool(base: str, key: str, tool_id: str) -> dict:
    r = requests.get(f"{base}/api/v1/tools/id/{tool_id}", headers=_headers(key), timeout=30)
    if r.status_code == 404:
        sys.exit(f"OWUI 里没有 id={tool_id} 的工具；先在 工作区→工具 里创建一次")
    r.raise_for_status()
    return r.json()


def _update_tool(base: str, key: str, tool_id: str, content: str, name: str) -> None:
    r = requests.post(
        f"{base}/api/v1/tools/id/{tool_id}/update",
        headers=_headers(key),
        json={"content": content, "name": name},
        timeout=120,
    )
    r.raise_for_status()


def _get_valves(base: str, key: str, tool_id: str) -> dict:
    """valves 在工具对象里；不同 OWUI 版本字段位置略有差异，取到就用。"""
    tool = _get_tool(base, key, tool_id)
    v = tool.get("valves")
    return v if isinstance(v, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="把 tool.py 同步到 OpenWebUI 工具")
    ap.add_argument("--openwebui-url",
                    default=os.environ.get("OWUI_URL") or os.environ.get("OPENWEBUI_URL")
                    or "http://open-webui:8080")
    ap.add_argument("--api-key",
                    default=os.environ.get("OWUI_API_KEY") or os.environ.get("OPENWEBUI_API_KEY"))
    ap.add_argument("--tool-id",
                    default=os.environ.get("OWUI_TOOL_ID", "academic_paper_search"))
    ap.add_argument("--file", default=str(ROOT / "tool.py"))
    ap.add_argument("--check", action="store_true", help="只比对版本；漂移则退出码 1")
    ap.add_argument("--force", action="store_true", help="版本相同也推送")
    ap.add_argument("--dry-run", action="store_true", help="不联网，只打印将要做什么")
    args = ap.parse_args()

    content = Path(args.file).read_text(encoding="utf-8")
    local_ver = _version(content)
    base = args.openwebui_url.rstrip("/")

    if args.dry_run:
        print(f"[dry-run] 本地 {args.file}: {len(content)} 字节, version={local_ver}")
        print(f"[dry-run] 目标 {base}/api/v1/tools/id/{args.tool_id}")
        return 0

    if not args.api_key:
        sys.exit("缺少 API key：export OWUI_API_KEY=...（OWUI → 设置 → 账号 → API 密钥）")

    tool = _get_tool(base, args.api_key, args.tool_id)
    remote_ver = _version(tool.get("content", ""))
    name = tool.get("name") or args.tool_id
    print(f"本地: {local_ver} ({len(content)} 字节) | OWUI: {remote_ver}")

    if remote_ver == local_ver and not args.force:
        print("已经是最新，无需推送。")
        return 0
    if args.check:
        print(f"版本漂移：OWUI 是 {remote_ver}，仓库是 {local_ver} —— 需要部署。")
        return 1

    before_valves = tool.get("valves") if isinstance(tool.get("valves"), dict) else {}
    _update_tool(base, args.api_key, args.tool_id, content, name)
    after = _get_tool(base, args.api_key, args.tool_id)
    after_ver = _version(after.get("content", ""))
    after_valves = after.get("valves") if isinstance(after.get("valves"), dict) else {}

    print(f"已推送：OWUI 现在是 {after_ver}")
    if before_valves != after_valves:
        # 防御：万一 OWUI 版本把 content 更新当成整体覆盖，尽量把 valves 写回去
        print("⚠️  检测到 valves 变化，正在回写原 valves…")
        r = requests.post(
            f"{base}/api/v1/tools/id/{args.tool_id}/valves/update",
            headers=_headers(args.api_key),
            json={"valves": before_valves},
            timeout=60,
        )
        if not r.ok:
            print(f"⚠️  valves 回写失败（{r.status_code}）：请到 UI 里核对手动配置")
            return 2
        print("valves 已恢复。")

    if after_ver != local_ver:
        print(f"⚠️  写入后版本号是 {after_ver}，与本地 {local_ver} 不一致，请检查")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
