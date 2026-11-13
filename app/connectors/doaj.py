"""DOAJ connector —— 开放获取期刊论文，全文一律可免费获取。"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://doaj.org/api/search/articles"


@register
class DoajConnector(BaseConnector):
    name = "doaj"
    label = "DOAJ（开放期刊）"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        url = f"{API}/{_escape(query)}"
        params = {
            "pageSize": min(limit, 25),
            "page": 1,
        }
        r = await self.client.get(url, params=params)
        r.raise_for_status()
        items = (r.json() or {}).get("results") or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            bj = w.get("bibjson") or {}
            title = (bj.get("title") or "").strip()
            if not title:
                continue

            authors = [a.get("name", "") for a in (bj.get("author") or [])]
            authors = [a for a in authors if a]

            year = bj.get("year")
            year = int(year) if str(year).isdigit() else None

            journal = bj.get("journal") or {}
            venue = journal.get("title") or None

            doi = None
            fulltext = None
            landing = w.get("id")
            for ident in (bj.get("identifier") or []):
                if ident.get("type") == "doi":
                    doi = ident.get("id")
            for link in (bj.get("link") or []):
                if link.get("type") == "fulltext":
                    fulltext = link.get("url")
                    break

            # DOAJ 的 keywords 是字符串列表（不是 dict 列表）
            keywords = [k for k in (bj.get("keywords") or [])
                        if isinstance(k, str) and k]

            out.append(
                PaperRecord(
                    source=self.name,
                    id=w.get("id") or "",
                    title=title,
                    authors=authors,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=venue,
                    abstract=_abstract(bj.get("abstract")),
                    keywords=keywords[:8],
                    type=EntityType.PAPER.value,
                    oa_status=OaStatus.OPEN.value,   # DOAJ 收录即开放获取
                    fulltext_url=fulltext,
                    landing_url=landing,
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out


def _escape(q: str) -> str:
    """DOAJ 的路径检索：把空格转成 +，其余做 URL 安全处理。"""
    from urllib.parse import quote
    return quote(q.strip(), safe="")


def _abstract(s: str | None) -> str | None:
    if not s:
        return None
    import re
    txt = re.sub(r"<[^>]+>", " ", s)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None
