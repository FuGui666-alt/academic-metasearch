"""DataCite connector —— 数据集 / 数据论文专用源。

这是「数据论文」场景区别于普通学术检索的关键：Zenodo、Dryad、figshare、
各大科学数据中心的数据集 DOI 都注册在 DataCite 下。
"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://api.datacite.org/dois"


@register
class DataCiteConnector(BaseConnector):
    name = "datacite"
    label = "DataCite（数据集）"
    entity_type = EntityType.DATASET.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "query": query,
            "page[size]": min(limit, 25),
            "resource-type-id": "dataset",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        items = ((r.json() or {}).get("data")) or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            if not isinstance(w, dict):
                continue
            attr = w.get("attributes") or {}
            if not isinstance(attr, dict):
                continue
            titles = attr.get("titles") or []
            if not isinstance(titles, list):
                titles = [titles]
            title = (titles[0].get("title") if titles and isinstance(titles[0], dict) else "") or ""
            title = title.strip()
            if not title:
                continue

            creators = attr.get("creators") or []
            authors = []
            for c in creators:
                nm = c.get("name") or " ".join(
                    p for p in [c.get("givenName"), c.get("familyName")] if p
                )
                if nm:
                    authors.append(nm.strip())

            year = attr.get("publicationYear")

            descs = attr.get("descriptions") or []
            abstract = None
            for d in descs:
                if d.get("descriptionType") == "Abstract":
                    abstract = (d.get("description") or "").strip() or None
                    break

            subjects = [s.get("subject") for s in (attr.get("subjects") or [])
                        if s.get("subject")]

            url = attr.get("url")
            doi = attr.get("doi")
            rights = (attr.get("rightsList") or [])
            oa = OaStatus.OPEN if any(
                "open" in (r.get("rightsUri") or "").lower()
                or "creativecommons.org/licenses/" in (r.get("rightsUri") or "").lower()
                or (r.get("rightsIdentifier") or "").lower().startswith(("cc-", "cc0"))
                for r in rights
            ) else OaStatus.UNKNOWN

            out.append(
                PaperRecord(
                    source=self.name,
                    id=doi or (w.get("id") or ""),
                    title=title,
                    authors=authors,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=(attr.get("publisher") or None),
                    abstract=abstract,
                    keywords=subjects[:8],
                    type=EntityType.DATASET.value,
                    oa_status=oa.value,
                    fulltext_url=url,
                    landing_url=(f"https://doi.org/{doi}" if doi else url),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out
