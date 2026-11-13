"""FastAPI 应用入口。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from .aggregator import Aggregator
from .connectors import REGISTRY

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "web"

app = FastAPI(title="学术元搜索", version="0.1.0")
aggregator = Aggregator(timeout=15.0)

@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/static/{filename}")
async def static_file(filename: str):
    """静态文件：自带禁缓存头，避免改了前端还要手动清浏览器缓存。

    只允许白名单文件，防目录穿越。
    """
    allowed = {"app.js", "style.css"}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="Not found")

    path = WEB / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    media = "text/javascript" if filename.endswith(".js") else "text/css"
    content = path.read_bytes()
    return Response(
        content=content,
        media_type=f"{media}; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/sources")
async def api_sources():
    return {
        "sources": [
            {
                "name": c.name,
                "label": c.label,
                "entity_type": c.entity_type,
                "enabled": c.enabled,
            }
            for c in REGISTRY.values()
        ]
    }


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1, description="检索词"),
    limit: int = Query(20, ge=1, le=50, description="每个数据源返回条数"),
    sources: str | None = Query(None, description="逗号分隔的数据源名，省略则全部"),
    oa: bool = Query(False, description="是否用 Unpaywall 标注开放获取（会增加耗时）"),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="检索词不能为空")

    src_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else None

    outcome = await aggregator.search(query, limit_per_source=limit, sources=src_list)

    enriched = 0
    if oa and outcome.results:
        from .enrich import OaEnricher, apply_enrichment
        info = await OaEnricher().enrich([m.record for m in outcome.results])
        if info:
            enriched = apply_enrichment(outcome.results, info)
            # OA 增强可能把状态改成 open，需要重新排序以保持一致
            from .merger import rank_results
            outcome.results = rank_results(outcome.results)

    return JSONResponse(
        {
            "query": outcome.query,
            "elapsed_ms": outcome.elapsed_ms,
            "total": len(outcome.results),
            "enriched": enriched,
            "sources": [
                {
                    "name": s.name,
                    "label": s.label,
                    "ok": s.ok,
                    "count": s.count,
                    "elapsed_ms": s.elapsed_ms,
                    "error": s.error,
                }
                for s in outcome.sources
            ],
            "results": [m.to_dict() for m in outcome.results],
        }
    )


@app.get("/api/search/stream")
async def api_search_stream(
    q: str = Query(..., min_length=1, description="检索词"),
    limit: int = Query(20, ge=1, le=50, description="每个数据源返回条数"),
    sources: str | None = Query(None, description="逗号分隔的数据源名，省略则全部"),
    oa: bool = Query(True, description="是否用 Unpaywall 标注开放获取全文"),
):
    """SSE 流式检索：哪个源先回来就先推结果，前端渐进渲染。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="检索词不能为空")

    src_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else None

    async def event_gen():
        try:
            async for ev in aggregator.search_stream(
                query, limit_per_source=limit, sources=src_list, enrich_oa=oa
            ):
                payload = json.dumps(ev["data"], ensure_ascii=False)
                yield f"event: {ev['event']}\ndata: {payload}\n\n"
        except Exception as e:  # 流中断时也给出结构化错误
            err = json.dumps(
                {"message": f"{type(e).__name__}: {e}"}, ensure_ascii=False
            )
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 防 nginx 缓冲
        },
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "connectors": len(REGISTRY)}


@app.get("/api/oa/{doi:path}")
async def api_oa_lookup(doi: str):
    """单独查某个 DOI 的开放获取情况（调试/详情页用）。"""
    from .enrich import OaEnricher
    from .normalize import normalize_doi

    d = normalize_doi(doi)
    if not d:
        raise HTTPException(status_code=400, detail="DOI 格式不正确")

    info = await OaEnricher(budget_seconds=6.0).enrich_lookup(d)
    if not info:
        raise HTTPException(status_code=404, detail="未找到该 DOI 的信息")
    return {"doi": d, **info}


@app.get("/api/citations")
async def api_citations(
    id: str = Query(..., description="OpenAlex work id、DOI 或 OpenAlex URL"),
    limit: int = Query(25, ge=1, le=50, description="每个方向的条数"),
):
    """引文网络：同时返回「本文引用了谁」与「谁引用了本文」。"""
    from .citations import CitationClient

    client = CitationClient()
    work_id = await client.resolve_work_id(id)
    if not work_id:
        raise HTTPException(
            status_code=404,
            detail="无法解析该标识符（支持 OpenAlex work id / DOI）",
        )

    data = await client.both(work_id, limit=limit)
    return JSONResponse({"input": id, **data})
