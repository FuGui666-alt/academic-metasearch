"""字段规范化与去重键计算。"""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s\u4e00-\u9fff]", re.UNICODE)
_WS = re.compile(r"\s+")
# 常见的标题尾部噪声（中英）
_TAIL_NOISE = re.compile(
    r"(\s*[\(（]?(?:研究综述|综述|评论|研究进展|研究现状|"
    r"a\s+review|review|survey|a\s+survey)[\)）]?\s*)$",
    re.IGNORECASE,
)
_CN_NUM = str.maketrans("０１２３４５６７８９", "0123456789")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_CN_NUM)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip().lower()


def normalize_title(title: str | None) -> str:
    """标题归一化：去标点、去尾缀噪声、小写、压空白。"""
    t = clean_text(title)
    if not t:
        return ""
    t = _TAIL_NOISE.sub("", t)
    return _WS.sub(" ", t).strip()


def normalize_doi(doi: str | None) -> str | None:
    """DOI 归一化：去前缀 URL、去空白、统一小写。"""
    if not doi:
        return None
    d = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/",
                   "doi:"):
        if d.lower().startswith(prefix):
            d = d[len(prefix):]
            break
    d = d.strip().lower()
    return d if _DOI.fullmatch(d) else None


def first_author_surname(authors: list[str]) -> str:
    """首作者姓氏：英文取末词，中文取前两字。

    防御性处理：上游数据源的 authors 元素偶尔是 dict/None，统一转成字符串。
    """
    if not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        first = first.get("value") or first.get("name") or ""
    if not isinstance(first, str):
        return ""
    name = first.strip()
    if not name:
        return ""
    if all("\u4e00" <= ch <= "\u9fff" for ch in name if ch.isalpha()) and name:
        return name[:2]
    parts = name.replace(",", " ").split()
    return parts[-1].lower() if parts else ""


def dedup_key(rec) -> str:
    """去重键：DOI 优先，其次「归一标题 + 首作者姓 + 年份」。"""
    doi = normalize_doi(rec.doi)
    if doi:
        return f"doi:{doi}"
    title = normalize_title(rec.title)
    if title:
        surname = first_author_surname(rec.authors)
        year = rec.year or ""
        return f"tsu:{title}|{surname}|{year}"
    return f"raw:{rec.source}:{rec.id}"
