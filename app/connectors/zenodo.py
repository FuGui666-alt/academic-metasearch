"""Zenodo connector —— CERN 运营的通用数据/论文仓储，DOI 齐全。

说明：实测本机网络对 zenodo.org 不可达（连接失败）。本 connector 保留完整实现，
网络恢复后即可工作；不可达时由 aggregator 标记该源失败，不影响其他源。
"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://zenodo.org/api/records"


@register
class ZenodoConnector(BaseConnector):
    name = "zenodo"
    label = "Zenodo"
    entity_type = EntityType.DATASET.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "q": query,
            "size": min(limit, 25),
            "sort": "bestmatch",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        items = ((r.json() or {}).get("hits") or {}).get("hits") or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            md = w.get("metadata") or {}
            title = " ".join((md.get("title") or "").split())
            if not title:
                continue

            authors = []
            for c in (md.get("creators") or []):
                nm = c.get("name") or " ".join(
                    p for p in [c.get("givenName"), c.get("familyName")] if p
                )
                if nm:
                    authors.append(nm.strip())

            date = md.get("publication_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None

            doi = md.get("doi")
            is_ds = (md.get("resource_type") or {}).get("type", "") == "dataset"

            abstract = (md.get("description") or "")
            if abstract:
                import re
                from html import unescape
                abstract = unescape(re.sub(r"<[^>]+>", " ", abstract))
                abstract = re.sub(r"\s+", " ", abstract).strip() or None

            keywords = list(md.get("keywords") or [])[:8]

            links = w.get("links") or {}
            # fulltext_url 只放真实文件下载链接；详情页（html）只作 landing_url。
            files = w.get("files") or []
            fulltext = None
            if isinstance(files, list) and files:
                f0 = files[0]
                if isinstance(f0, dict):
                    fl = f0.get("links") or {}
                    fulltext = fl.get("self") or fl.get("download") or None

            out.append(
                PaperRecord(
                    source=self.name,
                    id=str(w.get("id") or ""),
                    title=title,
                    authors=authors,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue="Zenodo",
                    abstract=abstract,
                    keywords=keywords,
                    type=(EntityType.DATASET if is_ds else EntityType.PAPER).value,
                    oa_status=(OaStatus.OPEN if md.get("access_right") == "open"
                               else OaStatus.UNKNOWN).value,
                    fulltext_url=fulltext,
                    landing_url=(links.get("self_html") or links.get("html")),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out
