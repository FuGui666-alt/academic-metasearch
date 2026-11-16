"""Semantic Scholar connector —— 摘要/引用网络质量高。

注意：无 key 时官方限流严格（常见 429）。本 connector 遇到 429 会直接抛
RateLimitedError，由 aggregator 标记该源失败，不影响其他源。

配置 key 的方式（任选其一）：
  1. 环境变量 S2_API_KEY=xxx
  2. 本项目根目录 .env 写入 S2_API_KEY=xxx
"""
from __future__ import annotations

import os

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,authors,year,abstract,externalIds,venue,citationCount,openAccessPdf,fieldsOfStudy"


class RateLimitedError(RuntimeError):
    """上游限流，交给 aggregator 标为失败即可。"""


@register
class SemanticScholarConnector(BaseConnector):
    name = "semanticscholar"
    label = "Semantic Scholar"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "query": query,
            "limit": min(limit, 20),
            "fields": FIELDS,
        }
        headers = {}
        key = os.environ.get("S2_API_KEY", "").strip()
        if key:
            headers["x-api-key"] = key

        r = await self.client.get(API, params=params, headers=headers)
        if r.status_code == 429:
            # 尊重上游限流：最多重试一次，读取 Retry-After，默认 1s，上限 5s。
            # 不做无限重试——避免把限流放大成上游压力。
            retry_after = r.headers.get("Retry-After", "1")
            try:
                delay = min(float(retry_after), 5.0)
            except ValueError:
                delay = 1.0
            import asyncio
            await asyncio.sleep(delay)
            r = await self.client.get(API, params=params, headers=headers)
            if r.status_code == 429:
                raise RateLimitedError("Semantic Scholar 限流（未配置 S2_API_KEY 时常见）")
        r.raise_for_status()
        items = (r.json() or {}).get("data") or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            title = (w.get("title") or "").strip()
            if not title:
                continue

            authors = [a.get("name", "") for a in (w.get("authors") or [])]
            authors = [a for a in authors if a]

            ext = w.get("externalIds") or {}
            doi = ext.get("DOI")

            oa_pdf = (w.get("openAccessPdf") or {}).get("url")
            oa_status = OaStatus.OPEN if oa_pdf else OaStatus.UNKNOWN

            pid = w.get("paperId") or ""

            out.append(
                PaperRecord(
                    source=self.name,
                    id=pid,
                    title=title,
                    authors=authors,
                    year=w.get("year"),
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=w.get("venue") or None,
                    abstract=w.get("abstract"),
                    keywords=(w.get("fieldsOfStudy") or [])[:8],
                    type=EntityType.PAPER.value,
                    oa_status=oa_status.value,
                    fulltext_url=oa_pdf,
                    landing_url=(f"https://www.semanticscholar.org/paper/{pid}" if pid else None),
                    citation_count=w.get("citationCount"),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out
