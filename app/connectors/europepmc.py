"""Europe PMC connector —— 生医文献，部分开放全文。"""
from __future__ import annotations

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@register
class EuropePmcConnector(BaseConnector):
    name = "europepmc"
    label = "Europe PMC"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "query": query,
            "format": "json",
            "pageSize": min(limit, 25),
            "resultType": "core",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        items = ((r.json() or {}).get("resultList") or {}).get("result") or []

        out: list[PaperRecord] = []
        for i, w in enumerate(items, start=1):
            title = (w.get("title") or "").strip()
            if not title:
                continue

            # 优先使用结构化 authorList，比按逗号拆 authorString 更可靠
            author_list = w.get("authorList") or {}
            if isinstance(author_list, dict):
                raw_authors = author_list.get("author") or []
                if isinstance(raw_authors, dict):
                    raw_authors = [raw_authors]
                authors = [
                    a.get("fullName", "").strip()
                    for a in raw_authors
                    if isinstance(a, dict) and a.get("fullName", "").strip()
                ]
            else:
                author_str = w.get("authorString") or ""
                authors = [a.strip() for a in author_str.split(",") if a.strip()]

            year = w.get("pubYear")
            year = int(year) if isinstance(year, (int, str)) and str(year).isdigit() else None

            doi = w.get("doi")
            pmcid = w.get("pmcid")
            # API 同时可能返回布尔值或 Y/N 字符串，bool("N") 会误判开放获取。
            is_oa = str(w.get("isOpenAccess", "")).strip().upper() in {"TRUE", "Y", "1"}

            fulltext = None
            if is_oa:
                if pmcid:
                    fulltext = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
                elif doi:
                    fulltext = f"https://doi.org/{doi}"

            out.append(
                PaperRecord(
                    source=self.name,
                    id=w.get("id") or w.get("pmid") or "",
                    title=title,
                    authors=authors,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=w.get("journalTitle") or None,
                    abstract=(w.get("abstractText") or None),
                    keywords=(w.get("keywordList") or {}).get("keyword", [])[:8]
                    if isinstance(w.get("keywordList"), dict) else [],
                    type=EntityType.PAPER.value,
                    oa_status=(OaStatus.OPEN if is_oa else OaStatus.UNKNOWN).value,
                    fulltext_url=fulltext,
                    landing_url=(f"https://europepmc.org/article/{w.get('source','MED')}/{w.get('id')}"
                                 if w.get("id") else None),
                    citation_count=w.get("citedByCount"),
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out
