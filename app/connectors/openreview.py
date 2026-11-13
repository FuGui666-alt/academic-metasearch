"""OpenReview connector —— 机器学习/计算机领域会议论文。

重要限制（2026-11-13 实测）：
  - 只有 /notes/search 可匿名访问；它本质是全文搜索，返回混合了论文主体与
    审稿意见。
  - 取主论文 note 的接口（/notes?id= / /notes?forum=）需要人机验证，返回
    403 ChallengeRequiredError。**本项目不做任何绕过**。
  - 因此本 connector 的策略是：只用 search 结果，筛掉没有 title 的条目
    （实测 25 条中约 4 条是真正的论文主体）。召回有限但完全合规，
    属于「有就补上，没有也不影响主流程」的补充源。
"""
from __future__ import annotations

import re

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://api2.openreview.net/notes/search"

# 从可能含 HTML 的字段里提取 DOI 本体（不含 doi.org/ 前缀，
# 交给 PaperRecord.__post_init__ 统一归一化）
_DOI_IN_HTML = re.compile(r"doi\.org/(10\.\d{4,9}/[^\s\"'<>\\]+)")


def _val(content: dict, key: str):
    v = content.get(key)
    return v.get("value") if isinstance(v, dict) else v


@register
class OpenReviewConnector(BaseConnector):
    name = "openreview"
    label = "OpenReview"
    entity_type = EntityType.PAPER.value
    base_url = API
    timeout = 20.0

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        # 多要一些原始结果，因为大部分会被过滤掉
        params = {
            "term": query,
            "limit": min(max(limit * 8, 40), 100),
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()
        notes = (r.json() or {}).get("notes") or []

        out: list[PaperRecord] = []
        seen: set[str] = set()
        rank = 0

        for n in notes:
            c = n.get("content") or {}
            title = (_val(c, "title") or "").strip()
            # 没有 title 的是审稿意见/评论，跳过
            if not title:
                continue

            fid = n.get("forum") or n.get("id") or ""
            key = fid or title
            if key in seen:
                continue
            seen.add(key)

            rank += 1
            authors = _val(c, "authors") or []
            if isinstance(authors, str):
                authors = [authors]
            # 防御：元素可能是 dict（如 {"value": "..."}）或 None
            clean_authors: list[str] = []
            for a in authors if isinstance(authors, (list, tuple)) else []:
                if isinstance(a, dict):
                    a = a.get("value") or a.get("name") or ""
                if isinstance(a, str) and a.strip():
                    clean_authors.append(a.strip())
            authors = clean_authors

            venue = _val(c, "venue") or None
            abstract = (_val(c, "abstract") or "").strip() or None
            landing = (f"https://openreview.net/forum?id={fid}" if fid else None)

            doi = None
            html = _val(c, "html") or ""
            if isinstance(html, str) and "doi.org/" in html:
                # html 字段通常是纯 DOI URL，但也可能是 <a href="..."> 片段。
                # 只提取 doi.org/ 之后的 DOI 本体（10.xxxx/yyy），
                # 避免畸形值进入记录、污染去重键与跳转链接。
                m = _DOI_IN_HTML.search(html)
                if m:
                    doi = m.group(1)

            out.append(
                PaperRecord(
                    source=self.name,
                    id=fid,
                    title=title,
                    authors=authors,
                    year=_year_from_venue(venue),
                    doi=doi,
                    venue=venue,
                    abstract=abstract,
                    keywords=[],
                    type=EntityType.PAPER.value,
                    oa_status=OaStatus.OPEN.value,
                    fulltext_url=landing,
                    landing_url=landing,
                    score=self.normalize_score(rank),
                    rank=rank,
                )
            )
            if len(out) >= limit:
                break

        return out


def _year_from_venue(venue: str | None) -> int | None:
    """从 venue 字符串里抠年份，如 'ICLR 2024' → 2024。"""
    if not venue:
        return None
    import re
    m = re.search(r"(19|20)\d{2}", str(venue))
    return int(m.group(0)) if m else None
