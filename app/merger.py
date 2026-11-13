"""归并层：去重 + 多源合并 + 排序。

去重分两级：
  一级（精确键）：DOI 优先；无 DOI 时「归一标题 + 首作者 + 年份」。
  二级（模糊兜底）：一级没配上、但标题高度相似且年份相同/缺失的，也合并。
                    用 difflib 的 SequenceMatcher 计算相似度，阈值 0.92。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .models import MergedRecord, PaperRecord
from .normalize import dedup_key, normalize_doi, normalize_title

# 合并时优先采用哪个源的元数据（越靠前越优先）
SOURCE_PRIORITY = [
    "crossref", "openalex", "datacite", "europepmc", "arxiv",
    "semanticscholar", "doaj", "openaire", "zenodo",
]

# 模糊合并阈值：低于此相似度不合并
FUZZY_THRESHOLD = 0.92


def _priority(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _completeness(rec: PaperRecord) -> int:
    """元数据完整度打分，用于挑选主记录。"""
    score = 0
    score += 10 if rec.abstract else 0
    score += 5 if rec.doi else 0
    score += 3 if rec.authors else 0
    score += 2 if rec.venue else 0
    score += 2 if rec.year else 0
    score += 2 if rec.keywords else 0
    return score


def merge(records: list[PaperRecord]) -> list[MergedRecord]:
    """按去重键合并多源结果。

    规则：
      1. 一级：同一 DOI / 归一标题+首作者+年份 视为同一实体；
      2. 二级：一级未命中但标题高度相似（>=FUZZY_THRESHOLD）且年份兼容的，也合并；
      3. 主记录选「元数据最完整」的那条，平局时按源优先级；
      4. 缺失字段用其它源补齐；
      5. 记录所有命中来源与各源自有 id。
    """
    buckets: dict[str, list[PaperRecord]] = {}
    for rec in records:
        buckets.setdefault(dedup_key(rec), []).append(rec)

    merged: list[MergedRecord] = []
    for key, group in buckets.items():
        merged.append(_combine(group))

    return _fuzzy_pass(merged)


def _combine(group: list[PaperRecord]) -> MergedRecord:
    """把一组已确认同源的记录合成一条。"""
    if len(group) == 1:
        rec = group[0]
        return MergedRecord(
            record=rec, sources=[rec.source], all_ids={rec.source: rec.id}
        )

    group_sorted = sorted(
        group, key=lambda r: (-_completeness(r), _priority(r.source))
    )
    primary = group_sorted[0]

    for other in group_sorted[1:]:
        _fill(primary, other)

    sources: list[str] = []
    all_ids: dict[str, str] = {}
    for r in group:
        if r.source not in sources:
            sources.append(r.source)
        all_ids[r.source] = r.id

    return MergedRecord(record=primary, sources=sources, all_ids=all_ids)


def _fuzzy_pass(items: list[MergedRecord]) -> list[MergedRecord]:
    """二级模糊去重：标题高度相似且年份兼容的合并。

    只处理「至少一方没有 DOI」的情况——两边 DOI 都不同说明确实是两篇。
    """
    result: list[MergedRecord] = []
    used = [False] * len(items)

    for i, item in enumerate(items):
        if used[i]:
            continue
        group = [item.record]
        meta_sources = list(item.sources)
        meta_ids = dict(item.all_ids)

        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            other = items[j]
            if not _should_fuzzy_merge(item.record, other.record):
                continue
            used[j] = True
            group.append(other.record)
            for s in other.sources:
                if s not in meta_sources:
                    meta_sources.append(s)
            meta_ids.update(other.all_ids)

        if len(group) == 1:
            result.append(item)
        else:
            combined = _combine(group)
            # 保留模糊合并阶段累积到的完整来源/ID 集合
            for s in meta_sources:
                if s not in combined.sources:
                    combined.sources.append(s)
            combined.all_ids.update(meta_ids)
            result.append(combined)

    return result


def _should_fuzzy_merge(a: PaperRecord, b: PaperRecord) -> bool:
    """判断两条记录是否应因标题高度相似而合并。"""
    doi_a = normalize_doi(a.doi)
    doi_b = normalize_doi(b.doi)
    # 两边 DOI 都有且不同 → 确认是两篇，不合并
    if doi_a and doi_b and doi_a != doi_b:
        return False

    ta = normalize_title(a.title)
    tb = normalize_title(b.title)
    if not ta or not tb:
        return False
    if ta == tb:
        return True

    # 长度差太大直接跳过，省算力
    if min(len(ta), len(tb)) / max(len(ta), len(tb)) < 0.8:
        return False

    ratio = SequenceMatcher(None, ta, tb).ratio()
    if ratio < FUZZY_THRESHOLD:
        return False

    # 年份兼容：都缺失，或相差不超过 1 年（预印本与正式发表常差 1 年）
    if a.year and b.year and abs(a.year - b.year) > 1:
        return False
    return True


def _fill(target: PaperRecord, src: PaperRecord) -> None:
    """用 src 补齐 target 的空字段。"""
    if not target.abstract and src.abstract:
        target.abstract = src.abstract
    if not target.doi and src.doi:
        target.doi = src.doi
    if not target.authors and src.authors:
        target.authors = src.authors
    if not target.venue and src.venue:
        target.venue = src.venue
    if not target.year and src.year:
        target.year = src.year
    if not target.keywords and src.keywords:
        target.keywords = list(src.keywords)
    if not target.fulltext_url and src.fulltext_url:
        target.fulltext_url = src.fulltext_url
    if not target.landing_url and src.landing_url:
        target.landing_url = src.landing_url
    if target.citation_count is None and src.citation_count is not None:
        target.citation_count = src.citation_count
    # OA 状态：只要有一个源说是开放，就标为开放
    if src.oa_status == "open" and target.oa_status != "open":
        target.oa_status = "open"
    # DOI 统一规范化
    target.doi = normalize_doi(target.doi)


def rank_results(merged: list[MergedRecord], boost_per_extra_source: float = 0.08) -> list[MergedRecord]:
    """综合排序：平均相关度 + 多源共现加成。

    多源同时命中的实体更可信，给一点加成。最终分数裁剪到 0~1。
    同时为每条结果注入「去别处继续找」的外链（知网/学校入口/DOI），
    这只是链接生成，不做任何抓取。
    """
    from .connectors.cnki_links import build_links

    for m in merged:
        base = m.record.score or 0.0
        extra = max(len(m.sources) - 1, 0) * boost_per_extra_source
        m.record.extra["_final_score"] = round(min(base + extra, 1.0), 4)
        m.record.extra["links"] = build_links(m.record.title, m.record.doi)

    merged.sort(
        key=lambda m: (
            -m.record.extra["_final_score"],
            -(m.record.year or 0),
        )
    )
    return merged
