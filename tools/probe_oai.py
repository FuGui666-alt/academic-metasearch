"""OAI-PMH 端点探测 —— 判断某个站点/机构是否有标准元数据采集接口。

OAI-PMH 是图书馆行业的标准协议（VuFind / paper-search-mcp 都用它）。
如果学校图书馆开放了这个端点，就能**合规地**批量采集馆藏元数据，
比爬 ASP 页面文明得多。

用法：
    python tools/probe_oai.py https://某校图书馆域名/
    python tools/probe_oai.py https://某校图书馆域名 --deep

探测逻辑：
  1. 先请求 /oai 或 /oai/request 等常见路径，带 verb=Identify
  2. 判断返回是否包含 <OAI-PMH> 根元素
  3. 若命中，解析 repositoryName / adminEmail / earliestDatestamp
  4. --deep 时再试探 ListMetadataFormats / ListSets

注意：只做一次性探测，不抓取、不高频请求。探测失败很正常——
国内多数高校图书馆并未开放 OAI-PMH，这是常态，不是异常。
"""
from __future__ import annotations

import sys
import time
from urllib.parse import urljoin, urlparse

import httpx

# Windows 控制台默认 GBK，强制 UTF-8 避免中文与符号报错
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # 非交互终端可能不提供可配置的 stdout，不影响探测功能。
        pass

UA = "OAI-Probe/1.0 (academic metasearch; one-shot identify)"

# 常见 OAI-PMH 端点路径
CANDIDATE_PATHS = [
    "/oai",
    "/oai/request",
    "/oai2",
    "/oai-pmh",
    "/api/oai",
    "/cgi-bin/oai",
    "/OAI-PMH",
    "/repository/oai",
]


def probe(base: str, timeout: float = 8.0) -> list[dict]:
    """对 base 站点尝试若干常见 OAI 路径，返回命中的端点信息。"""
    parsed = urlparse(base if "://" in base else f"https://{base}")
    root = f"{parsed.scheme}://{parsed.netloc}"

    hits: list[dict] = []
    with httpx.Client(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(timeout, connect=5.0),
        follow_redirects=True,
    ) as client:
        for path in CANDIDATE_PATHS:
            url = urljoin(root, path)
            try:
                r = client.get(url, params={"verb": "Identify"})
            except httpx.HTTPError as e:
                print(f"  · {path:<16} 连接失败: {type(e).__name__}")
                continue

            body = (r.text or "")[:4000]
            is_oai = "<OAI-PMH" in body
            status = "✓ OAI-PMH" if is_oai else "✗"
            print(f"  · {path:<16} HTTP {r.status_code:<3} {status}")

            if is_oai:
                hits.append({
                    "url": url,
                    **parse_identify(r.text),
                })
            time.sleep(0.3)  # 礼貌间隔

    return hits


def parse_identify(xml_text: str) -> dict:
    """从 Identify 响应里抠出关键信息。"""
    import re

    def grab(tag: str) -> str:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml_text, re.DOTALL)
        return m.group(1).strip() if m else ""

    return {
        "repositoryName": grab("repositoryName"),
        "baseURL": grab("baseURL"),
        "protocolVersion": grab("protocolVersion"),
        "adminEmail": grab("adminEmail"),
        "earliestDatestamp": grab("earliestDatestamp"),
        "deletedRecord": grab("deletedRecord"),
        "granularity": grab("granularity"),
    }


def deep_check(endpoint: str, timeout: float = 8.0) -> None:
    """进一步查该端点的元数据格式与集合列表。"""
    print("\n[深探] 元数据格式与集合：")
    with httpx.Client(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(timeout, connect=5.0),
        follow_redirects=True,
    ) as client:
        for verb in ("ListMetadataFormats", "ListSets"):
            try:
                r = client.get(endpoint, params={"verb": verb})
                ok = "<OAI-PMH" in (r.text or "")
                print(f"  {verb:<22} HTTP {r.status_code} {'✓' if ok else '✗'}")
                if ok and verb == "ListMetadataFormats":
                    import re
                    fmts = re.findall(r"<metadataPrefix>(.*?)</metadataPrefix>", r.text)
                    print(f"    可用格式: {', '.join(fmts) if fmts else '未知'}")
            except httpx.HTTPError as e:
                print(f"  {verb:<22} 失败: {type(e).__name__}")
            time.sleep(0.3)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    target = sys.argv[1]
    deep = "--deep" in sys.argv

    print(f"探测目标: {target}")
    print("说明: OAI-PMH 是图书馆标准元数据协议。国内高校多数未开放，探测失败是常态。\n")
    print("[阶段] 试探常见 OAI-PMH 路径：")

    hits = probe(target)

    if not hits:
        print("\n结论: 未发现公开的 OAI-PMH 端点。")
        print("后续可做：")
        print("  1. 直接向图书馆/信息中心咨询是否有机构仓储或元数据接口")
        print("  2. 查看该馆是否加入 CALIS / CASHL / 本地联盟（联盟通常走统一采集）")
        print("  3. 退回「只做跳转链接」方案（本项目已实现）")
        return 0

    print(f"\n结论: 发现 {len(hits)} 个 OAI-PMH 端点。")
    for h in hits:
        print(f"\n  端点: {h['url']}")
        for k, v in h.items():
            if k != "url" and v:
                print(f"    {k}: {v}")

    if deep:
        deep_check(hits[0]["url"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
