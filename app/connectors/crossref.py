"""Crossref connector —— DOI 元数据权威源，补出版物类型与出版社信息。"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, metadata_email, register

API = "https://api.crossref.org/works"


@register
class CrossrefConnector(BaseConnector):
    name = "crossref"
    label = "Crossref"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "query": query,
            "rows": min(limit, 50),
            "select": ("DOI,title,author,issued,container-title,"
                       "abstract,type,is-referenced-by-count,URL,subject"),
            "mailto": metadata_email(),
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        items = ((r.json() or {}).get("message") or {}).get("items") or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            if not isinstance(w, dict):
                continue
            titles = w.get("title") or []
            if not isinstance(titles, list):
                titles = [titles]
            title = (titles[0] if titles else "").strip()
            if not title:
                continue

            authors = []
            for a in (w.get("author") or []):
                parts = [a.get("given"), a.get("family")]
                nm = " ".join(p for p in parts if p) or a.get("name")
                if nm:
                    authors.append(nm.strip())

            year = None
            issued = (w.get("issued") or {}).get("date-parts") or []
            if issued and issued[0] and issued[0][0]:
                year = issued[0][0]

            ct = w.get("container-title") or []
            venue = ct[0] if ct else None

            doi = w.get("DOI")
            out.append(
                PaperRecord(
                    source=self.name,
                    id=doi or "",
                    title=title,
                    authors=authors,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=venue,
                    abstract=_strip_jats(w.get("abstract")),
                    keywords=(w.get("subject") or [])[:8],
                    type=EntityType.PAPER.value,
                    oa_status=OaStatus.UNKNOWN.value,
                    landing_url=w.get("URL"),
                    citation_count=w.get("is-referenced-by-count"),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out


def _strip_jats(abstract: str | None) -> str | None:
    """Crossref 摘要常带 JATS XML 标签，去掉。"""
    if not abstract:
        return None
    import re
    from html import unescape
    txt = unescape(re.sub(r"<[^>]+>", " ", abstract))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None
