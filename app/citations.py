"""引文网络：向前追溯（本文引用了谁）与向后追溯（谁引用了本文）。

数据全部来自 OpenAlex 官方 API，免费无 key。

两个方向的实现差异（实测）：
  - 被引（谁引用了本文）：`GET /works?filter=cites:W123` 一次拿到列表
  - 引用（本文引用了谁）：先取目标 work 的 `referenced_works`（一堆 OpenAlex ID），
    再用 `filter=openalex:W1|W2|W3` 批量取详情。批量上限 50 个 ID/次。

注意：OpenAlex 的 work id 形如 W2582566895，DOI 需要先解析成 work id。
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .connectors.base import get_shared_client, metadata_email
from .normalize import normalize_doi

logger = logging.getLogger(__name__)

API = "https://api.openalex.org/works"
# 与 connectors / Unpaywall 增强同一来源：读 METADATA_EMAIL env，
# 未配置时回退项目占位邮箱（base.metadata_email）。

MAILTO = metadata_email()

# 批量取详情时每次最多塞多少个 id
BATCH_SIZE = 50
_WORK_FIELDS = (
    "id,doi,title,display_name,publication_year,cited_by_count,"
    "authorships,primary_location,open_access,type"
)


def _has_title(work: dict) -> bool:
    return bool(work.get("title") or work.get("display_name"))


def _light(w: dict) -> dict:
    """把 OpenAlex work 压成引文列表需要的轻量结构。"""
    loc = (w.get("primary_location") or {})
    src = (loc.get("source") or {})
    oa = (w.get("open_access") or {})
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (w.get("authorships") or [])
    ]
    return {
        "id": (w.get("id") or "").rsplit("/", 1)[-1],
        "title": (w.get("title") or w.get("display_name") or "").strip(),
        "doi": w.get("doi"),
        "year": w.get("publication_year"),
        "venue": src.get("display_name"),
        "authors": [a for a in authors if a][:6],
        "author_count": len([a for a in authors if a]),
        "cited_by": w.get("cited_by_count"),
        "is_oa": bool(oa.get("is_oa")),
        "oa_url": oa.get("oa_url"),
        "type": w.get("type"),
    }


class CitationClient:
    """引文网络查询客户端，复用应用级共享 HTTP 连接池。"""

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict):
        r = await client.get(url, params={**params, "mailto": MAILTO})
        r.raise_for_status()
        return r.json()

    async def resolve_work_id(self, identifier: str) -> str | None:
        """把 work id / DOI / OpenAlex URL / arXiv ID 统一解析成 W 开头的 work id。"""
        ident = (identifier or "").strip()
        if not ident:
            return None
        if ident.upper().startswith("W") and ident[1:].isdigit():
            return ident
        if "openalex.org" in ident:
            tail = ident.rsplit("/", 1)[-1]
            if tail.upper().startswith("W"):
                return tail

        # arXiv ID 要最先判，再转换成其对应的 DataCite DOI。
        arxiv_id = ident.removeprefix("arXiv:").removeprefix("arxiv:").strip()
        if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", arxiv_id):
            doi = f"10.48550/arxiv.{arxiv_id.split('v')[0]}"
        else:
            doi = normalize_doi(ident)
            # normalize_doi 已验证注册前缀与 DOI 语法，普通 URL 不能进入此分支。
            if not doi:
                return None
        try:
            data = await self._get(
                get_shared_client(), f"{API}/https://doi.org/{doi}", {"select": "id"}
            )
            return (data.get("id") or "").rsplit("/", 1)[-1] or None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def citing(self, work_id: str, limit: int = 25) -> list[dict]:
        """谁引用了本文（向后追溯，找最新进展）。"""
        data = await self._get(
            get_shared_client(),
            API,
            {
                "filter": f"cites:{work_id}",
                "per-page": min(limit, 50),
                "sort": "cited_by_count:desc",
                "select": _WORK_FIELDS,
            },
        )
        return [_light(w) for w in (data.get("results") or []) if _has_title(w)]

    async def referenced(self, work_id: str, limit: int = 25) -> list[dict]:
        """本文引用了谁（向前追溯，找奠基文献）。"""
        client = get_shared_client()
        meta = await self._get(
            client,
            f"{API}/{work_id}",
            {"select": "id,title,referenced_works,referenced_works_count"},
        )
        ref_ids = meta.get("referenced_works") or []
        if not ref_ids:
            return []

        ids = [r.rsplit("/", 1)[-1] for r in ref_ids][:limit]
        batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
        tasks = [
            self._get(
                client,
                API,
                {
                    "filter": "openalex:" + "|".join(batch),
                    "per-page": len(batch),
                    "select": _WORK_FIELDS,
                },
            )
            for batch in batches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: list[dict] = []
        batch_errors = 0
        for res in results:
            if isinstance(res, Exception):
                batch_errors += 1
                logger.debug("OpenAlex referenced 批查询失败: %s", type(res).__name__)
                continue
            out.extend(_light(w) for w in (res.get("results") or []) if _has_title(w))
        if batch_errors:
            logger.info(
                "OpenAlex referenced 部分批次失败: %d/%d", batch_errors, len(batches)
            )

        out.sort(key=lambda x: -(x.get("cited_by") or 0))
        return out[:limit]

    async def both(self, work_id: str, limit: int = 25) -> dict:
        """同时取两个方向，任一失败不影响另一个。"""
        c_task = asyncio.create_task(self.citing(work_id, limit))
        r_task = asyncio.create_task(self.referenced(work_id, limit))
        citing_res, ref_res = await asyncio.gather(
            c_task, r_task, return_exceptions=True
        )
        return {
            "work_id": work_id,
            "citing": [] if isinstance(citing_res, Exception) else citing_res,
            "referenced": [] if isinstance(ref_res, Exception) else ref_res,
            "errors": {
                k: str(v)
                for k, v in (("citing", citing_res), ("referenced", ref_res))
                if isinstance(v, Exception)
            },
        }
