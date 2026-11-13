"""OpenAlex connector —— 主力数据源，覆盖最广、免费无需 key。"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://api.openalex.org/works"


@register
class OpenAlexConnector(BaseConnector):
    name = "openalex"
    label = "OpenAlex"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "search": query,
            "per-page": min(limit, 50),
            "mailto": "your-email@example.com",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        data = r.json()

        out: list[PaperRecord] = []
        for i, w in enumerate(data.get("results", []), start=1):
            title = (w.get("title") or w.get("display_name") or "").strip()
            if not title:
                continue

            authors = [
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])
            ]
            authors = [a for a in authors if a]

            venue = None
            loc = w.get("primary_location") or {}
            src = loc.get("source") or {}
            if src.get("display_name"):
                venue = src["display_name"]

            oa = w.get("open_access") or {}
            oa_url = oa.get("oa_url")
            oa_status = (OaStatus.OPEN if oa.get("is_oa")
                         else OaStatus.CLOSED if oa.get("is_oa") is False
                         else OaStatus.UNKNOWN)

            keywords = [
                k.get("display_name", "")
                for k in (w.get("keywords") or [])
                if k.get("display_name")
            ]

            abstract = None
            if w.get("abstract_inverted_index"):
                abstract = _inv_to_text(w["abstract_inverted_index"])

            doi = w.get("doi")
            landing = w.get("id")

            out.append(
                PaperRecord(
                    source=self.name,
                    id=(w.get("id") or "").rsplit("/", 1)[-1],
                    title=title,
                    authors=authors,
                    year=w.get("publication_year"),
                    doi=doi,
                    venue=venue,
                    abstract=abstract,
                    keywords=keywords[:8],
                    type=EntityType.PAPER.value,
                    oa_status=oa_status.value,
                    fulltext_url=oa_url,
                    landing_url=landing,
                    citation_count=w.get("cited_by_count"),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out


def _inv_to_text(inv: dict[str, list[int]]) -> str | None:
    """OpenAlex 的倒排摘要还原成普通文本。"""
    if not inv:
        return None
    pairs: list[tuple[int, str]] = []
    for word, positions in inv.items():
        for p in positions:
            pairs.append((p, word))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    return " ".join(w for _, w in pairs)
