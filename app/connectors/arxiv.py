"""arXiv connector —— 预印本，全文开放，ATOM XML 接口。"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "http://export.arxiv.org/api/query"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


@register
class ArxivConnector(BaseConnector):
    name = "arxiv"
    label = "arXiv"
    entity_type = EntityType.PAPER.value
    base_url = API

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(limit, 30),
            "sortBy": "relevance",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()

        root = ET.fromstring(r.text)
        entries = root.findall("a:entry", NS)

        out: list[PaperRecord] = []
        for i, e in enumerate(entries, start=1):
            title = _txt(e.find("a:title", NS)).replace("\n", " ").strip()
            if not title:
                continue
            title = " ".join(title.split())

            authors = [
                _txt(a.find("a:name", NS)).strip()
                for a in e.findall("a:author", NS)
            ]
            authors = [a for a in authors if a]

            published = _txt(e.find("a:published", NS))  # 2024-01-02T...
            year = int(published[:4]) if published[:4].isdigit() else None

            link = ""
            pdf_url = None
            for ln in e.findall("a:link", NS):
                if ln.get("title") == "pdf" or ln.get("type") == "application/pdf":
                    pdf_url = ln.get("href")
                elif ln.get("rel") == "alternate":
                    link = ln.get("href")

            summary = _txt(e.find("a:summary", NS)).strip() or None

            doi = None
            de = e.find("arxiv:doi", NS)
            if de is not None and de.text:
                doi = f"https://doi.org/{de.text.strip()}"

            journal = e.find("arxiv:journal_ref", NS)

            out.append(
                PaperRecord(
                    source=self.name,
                    id=_txt(e.find("a:id", NS)).rsplit("/", 1)[-1],
                    title=title,
                    authors=authors,
                    year=year,
                    doi=doi,
                    venue=(_txt(journal).strip() if journal is not None else "arXiv"),
                    abstract=summary,
                    keywords=[],
                    type=EntityType.PAPER.value,
                    oa_status=OaStatus.OPEN.value,
                    fulltext_url=pdf_url,
                    landing_url=link,
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out


def _txt(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text
