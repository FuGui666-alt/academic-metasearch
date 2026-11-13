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
import re

import httpx

from .normalize import normalize_doi

API = "https://api.openalex.org/works"
MAILTO = "noreply@example.com"

# 批量取详情时每次最多塞多少个 id
BATCH_SIZE = 50


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
    """引文网络查询客户端。每次实例化一个，内部复用连接。"""

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

        # arXiv ID 要最先判：它长得像 DOI 但其实不是，normalize_doi 会原样放行
        arxiv_id = ident.removeprefix("arXiv:").removeprefix("arxiv:").strip()
        if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", arxiv_id):
            doi = f"10.48550/arxiv.{arxiv_id.split('v')[0]}"
        else:
            doi = normalize_doi(ident)
            # 只接受看起来像 DOI 的（含 / 且有注册前缀），其余视为无效
            if not doi or "/" not in doi:
                return None
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0)) as client:
            try:
                data = await self._get(
                    client, f"{API}/https://doi.org/{doi}", {"select": "id"}
                )
                return (data.get("id") or "").rsplit("/", 1)[-1] or None
            except Exception:
                return None

    async def citing(self, work_id: str, limit: int = 25) -> list[dict]:
        """谁引用了本文（向后追溯，找最新进展）。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0)) as client:
            data = await self._get(
                client,
                API,
                {
                    "filter": f"cites:{work_id}",
                    "per-page": min(limit, 50),
                    "sort": "cited_by_count:desc",
                    "select": ("id,doi,title,publication_year,cited_by_count,"
                               "authorships,primary_location,open_access,type"),
                },
            )
            return [_light(w) for w in (data.get("results") or []) if w.get("title")]

    async def referenced(self, work_id: str, limit: int = 25) -> list[dict]:
        """本文引用了谁（向前追溯，找奠基文献）。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0)) as client:
            # 1) 拿目标 work 的引用 id 列表
            meta = await self._get(
                client,
                f"{API}/{work_id}",
                {"select": "id,title,referenced_works,referenced_works_count"},
            )
            ref_ids = meta.get("referenced_works") or []
            if not ref_ids:
                return []

            ids = [r.rsplit("/", 1)[-1] for r in ref_ids][: max(limit, 25)]

            # 2) 分批取详情
            batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
            tasks = [
                self._get(
                    client,
                    API,
                    {
                        "filter": "openalex:" + "|".join(b),
                        "per-page": len(b),
                        "select": ("id,doi,title,publication_year,cited_by_count,"
                                   "authorships,primary_location,open_access,type"),
                    },
                )
                for b in batches
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            out: list[dict] = []
            for res in results:
                if isinstance(res, Exception):
                    continue
                out.extend(
                    _light(w) for w in (res.get("results") or []) if w.get("title")
                )

            # 按被引降序：被引多的一般是奠基性文献
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
