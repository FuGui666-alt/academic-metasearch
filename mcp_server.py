"""学术元搜索 MCP Server（stdio）—— 把检索能力接入 DSH / 任意 MCP 宿主。

设计：进程内直接 import app.aggregator 以库方式跑检索，
不需要启动 8000 端口的 HTTP 服务，也不与浏览器版冲突
（两边的 SEARCH_CACHE 各自独立，均为进程内存态）。

本文件是纯增量插件入口，不影响 启动.bat / exe / Web 界面任何链路。

在 DSH 中注册（MCP Connector，stdio）：
    command: python
    args:    <PROJECT_ROOT>/mcp_server.py
    cwd:     <PROJECT_ROOT>

环境变量与浏览器版共用同一套约定（.env.bat 由 DSH 侧不加载，
如需 METADATA_EMAIL/S2_API_KEY 请在 DSH 的 envJson 里配置）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 让 shim 无论从哪个 cwd 启动都能 import app 包
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP

from app.aggregator import Aggregator

mcp = FastMCP("academic-metasearch")

# 与 main.py /api/oa 相同的 DOI 白名单口径
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _clean_srcs(sources: str | None) -> list[str] | None:
    if not sources:
        return None
    return [s.strip() for s in sources.split(",") if s.strip()]


@mcp.tool()
async def metasearch(
    q: str,
    limit: int = 10,
    sources: str | None = None,
    oa: bool = False,
) -> dict:
    """多源并发检索学术文献/数据集并去重归并。

    一次查询并发打 10 个公开学术 API（OpenAlex/Crossref/arXiv/DOAJ/
    DataCite/EuropePMC/OpenAIRE/SemanticScholar/Zenodo/OpenReview），
    DOI 优先 → 归一标题+作者+年份 → 模糊兜底 三级去重，多源共现加成排序。
    oa=true 时用 Unpaywall 标注开放获取与免费 PDF 链接（更慢）。

    Args:
        q: 检索词（中英文均可，≤300 字符）
        limit: 每源返回条数 1~50（默认 10）
        sources: 逗号分隔的源名白名单（如 "openalex,arxiv"），省略则全部
        oa: 是否做 Unpaywall 开放获取标注
    """
    query = (q or "").strip()
    if not query:
        return {"error": "检索词不能为空"}
    query = query[:300]

    agg = Aggregator(timeout=15.0)
    outcome = await agg.search(
        query,
        limit_per_source=max(1, min(int(limit), 50)),
        sources=_clean_srcs(sources),
    )

    enriched = 0
    if oa and outcome.results:
        from app.enrich import OaEnricher, apply_enrichment
        from app.merger import rank_results

        info = await OaEnricher().enrich([m.record for m in outcome.results])
        if info:
            enriched = apply_enrichment(outcome.results, info)
            outcome.results = rank_results(outcome.results)

    return {
        "query": outcome.query,
        "total": len(outcome.results),
        "elapsed_ms": outcome.elapsed_ms,
        "enriched": enriched,
        "sources": [
            {
                "name": s.name, "ok": s.ok, "count": s.count,
                "elapsed_ms": s.elapsed_ms, "error": s.error,
            }
            for s in outcome.sources
        ],
        "results": [
            {
                "title": m.record.title,
                "authors": m.record.authors,
                "year": m.record.year,
                "doi": m.record.doi,
                "venue": m.record.venue,
                "abstract": m.record.abstract,
                "type": m.record.type,
                "oa_status": m.record.oa_status,
                "fulltext_url": m.record.fulltext_url,
                "landing_url": m.record.landing_url,
                "citation_count": m.record.citation_count,
                "score": m.record.extra.get("_final_score"),
                "sources": m.sources,
            }
            for m in outcome.results
        ],
    }


@mcp.tool()
async def citations(id: str, limit: int = 25) -> dict:
    """查一条文献的引文网络：谁引用了它（citing）+ 它引用了谁（referenced）。

    Args:
        id: OpenAlex work id（如 W2039635063）或 DOI（如 10.13031/2013.21723）
        limit: 每个方向返回条数 1~50（默认 25）
    """
    from app.citations import CitationClient

    client = CitationClient()
    work_id = await client.resolve_work_id((id or "").strip())
    if not work_id:
        return {"error": "无法解析该标识符（支持 OpenAlex work id / DOI）"}
    data = await client.both(work_id, limit=max(1, min(int(limit), 50)))
    return {"input": id, **data}


@mcp.tool()
async def oa_lookup(doi: str) -> dict:
    """查单个 DOI 的开放获取情况（是否免费全文、PDF 链接、许可证）。

    Args:
        doi: 规范 DOI，如 10.13031/2013.21723
    """
    from app.enrich import OaEnricher
    from app.normalize import normalize_doi

    d = normalize_doi((doi or "").strip())
    if not d or not DOI_RE.match(d):
        return {"error": "DOI 格式不正确（应为 10.xxxx/yyy）"}
    info = await OaEnricher(budget_seconds=6.0).enrich_lookup(d)
    if not info:
        return {"error": "未找到该 DOI 的信息"}
    return {"doi": d, **info}


@mcp.tool()
async def list_sources() -> dict:
    """列出全部可用数据源及其覆盖范围。"""
    from app.connectors import REGISTRY

    return {
        "sources": [
            {"name": c.name, "label": c.label, "entity_type": c.entity_type,
             "enabled": c.enabled}
            for c in REGISTRY.values()
        ]
    }


if __name__ == "__main__":
    mcp.run()
