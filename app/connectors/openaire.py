"""OpenAIRE connector —— 欧盟开放科学聚合器，覆盖论文/数据集/项目产出。

解析注意：
  - 返回是 XML，正文结构为 <results><result><metadata><entity><result>...
  - 外层 <result> 无命名空间前缀，字段节点部分带 oaf:/dri: 前缀。
  - 因此统一按「本地名（local name）」匹配，不依赖前缀，最稳。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import EntityType, OaStatus, PaperRecord
from .base import BaseConnector, register

API = "https://api.openaire.eu/search/publications"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(node, name: str, want_class: str | None = None) -> str:
    """按本地名找第一个匹配节点的文本。可选按 classname 过滤。"""
    for e in node.iter():
        if _local(e.tag) != name:
            continue
        if want_class is not None:
            cls = e.get("classname") or ""
            if want_class.lower() not in cls.lower():
                continue
        txt = (e.text or "").strip()
        if txt:
            return txt
    return ""


def _all(node, name: str) -> list[str]:
    out = []
    for e in node.iter():
        if _local(e.tag) == name:
            txt = (e.text or "").strip()
            if txt:
                out.append(txt)
    return out


@register
class OpenAireConnector(BaseConnector):
    name = "openaire"
    label = "OpenAIRE"
    entity_type = EntityType.PAPER.value
    base_url = API
    # OpenAIRE 实测响应常在 10~15s 以上，单独放宽超时，避免被误判失败
    timeout = 30.0

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        params = {
            "title": query,
            "size": min(limit, 25),
            "format": "xml",
        }
        r = await self.client.get(API, params=params)
        r.raise_for_status()

        root = ET.fromstring(r.text)
        # 兼容命名空间；只取 results 容器的直接子节点，避免把内层实体 result
        # 与外层条目混在一起、造成字段串读。
        items = []
        for container in root.iter():
            if _local(container.tag) != "results":
                continue
            items.extend(
                child for child in container if _local(child.tag) == "result"
            )
            if items:
                break

        out: list[PaperRecord] = []
        seen_ids: set[str] = set()
        for i, item in enumerate(items, start=1):
            title = _first(item, "title", want_class="main title")
            if not title:
                title = _first(item, "title")
            title = " ".join(title.split())
            if not title:
                continue

            oid = _first(item, "objIdentifier")
            if oid and oid in seen_ids:
                continue
            if oid:
                seen_ids.add(oid)

            creators = []
            seen_creators: set[str] = set()
            for c in _all(item, "creator"):
                if c not in seen_creators:
                    seen_creators.add(c)
                    creators.append(c)

            date = _first(item, "dateofacceptance")
            year = int(date[:4]) if date[:4].isdigit() else None

            doi = _first(item, "pid", want_class="Digital Object Identifier")
            if not doi:
                for pid in (e for e in item.iter() if _local(e.tag) == "pid"):
                    cls = (pid.get("classname") or "").lower()
                    if "doi" in cls:
                        doi = (pid.text or "").strip()
                        break

            desc = _first(item, "description") or None

            best_url = None
            for u in _all(item, "url"):
                if u.startswith("http"):
                    best_url = u
                    break

            publisher = None
            for j in (e for e in item.iter() if _local(e.tag) == "journal"):
                publisher = (j.text or "").strip() or None
                if publisher:
                    break

            out.append(
                PaperRecord(
                    source=self.name,
                    id=oid or "",
                    title=title,
                    authors=creators,
                    year=year,
                    doi=f"https://doi.org/{doi}" if doi else None,
                    venue=publisher,
                    abstract=desc,
                    keywords=[],
                    type=EntityType.PAPER.value,
                    oa_status=OaStatus.UNKNOWN.value,
                    fulltext_url=best_url,
                    landing_url=best_url,
                    score=self.normalize_score(i),
                    rank=i,
                )
            )
            if len(out) >= limit:
                break
        return out
