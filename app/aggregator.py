"""聚合器：并发扇出 + 流式产出。

两种用法：
  - search()         一次性等所有源返回（原有行为，供内部/测试使用）
  - search_stream()  流式：哪个源先回来就先推哪一部分，前端渐进渲染

流式协议（SSE 事件）：
  event: source   每个源完成时推送一次，含该源的原始结果数
  event: partial  每次有新结果并入时推送（含本轮新增的合并结果）
  event: done     全部结束，含最终统计
  event: error    整体异常

去重合并策略（流式下）：
  已合并结果内部维护一份去重索引。新源返回后与已有结果做合并：
  命中已有键 → 合并来源标签、补齐字段；未命中 → 作为新结果追加。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .cache import SEARCH_CACHE, TTLCache
from .connectors import REGISTRY, build_clients
from .merger import _combine, _should_fuzzy_merge, rank_results, merge
from .models import MergedRecord, PaperRecord


@dataclass
class SourceResult:
    name: str
    label: str
    ok: bool
    count: int = 0
    elapsed_ms: int = 0
    error: str | None = None


@dataclass
class SearchOutcome:
    query: str
    results: list[MergedRecord] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)
    elapsed_ms: int = 0


class IncrementalMerger:
    """流式场景下的增量去重合并器。

    维护已合并结果列表，支持把一个新源的结果并入。
    """

    def __init__(self):
        self._items: list[MergedRecord] = []
        # 归一化标题 → 该结果在 _items 中的位置，供模糊匹配加速
        self._by_title: dict[str, int] = {}

    def add_batch(self, records: list[PaperRecord]) -> int:
        """并入一批新结果，返回新增的合并条目数。"""
        # 先做批内去重（复用 merge 的精确键逻辑）
        batch = merge(records)
        added = 0

        for incoming in batch:
            idx = self._find_match(incoming.record)
            if idx is not None:
                self._merge_into(self._items[idx], incoming)
            else:
                self._items.append(incoming)
                self._index(incoming.record)
                added += 1

        return added

    def _index(self, rec: PaperRecord) -> None:
        from .normalize import normalize_title
        t = normalize_title(rec.title)
        if t:
            self._by_title[t] = len(self._items) - 1

    def _find_match(self, rec: PaperRecord) -> int | None:
        """在已有结果里找可合并的条目，返回下标或 None。"""
        from .normalize import dedup_key, normalize_title

        key = dedup_key(rec)
        for i, item in enumerate(self._items):
            if dedup_key(item.record) == key:
                return i

        # 精确键没命中 → 走模糊匹配
        for i, item in enumerate(self._items):
            if _should_fuzzy_merge(item.record, rec):
                return i
        return None

    def _merge_into(self, existing: MergedRecord, incoming: MergedRecord) -> None:
        """把 incoming 并入 existing（保留更完整的元数据）。"""
        combined = _combine([existing.record, incoming.record])
        sources = list(existing.sources)
        for s in incoming.sources:
            if s not in sources:
                sources.append(s)
        ids = dict(existing.all_ids)
        ids.update(incoming.all_ids)

        existing.record = combined.record
        existing.sources = sources
        existing.all_ids = ids

        # 更新标题索引
        from .normalize import normalize_title
        t = normalize_title(existing.record.title)
        if t:
            self._by_title[t] = self._items.index(existing)

    @property
    def items(self) -> list[MergedRecord]:
        return self._items

    def ranked(self) -> list[MergedRecord]:
        return rank_results(self._items)


class Aggregator:
    """元搜索聚合器。"""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    # ---------- 一次性返回（原有行为） ----------
    async def search(
        self,
        query: str,
        limit_per_source: int = 20,
        sources: list[str] | None = None,
    ) -> SearchOutcome:
        names, unknown = self._resolve_sources(sources)
        t0 = time.perf_counter()
        outcome = SearchOutcome(query=query)
        for n in unknown:
            outcome.sources.append(
                SourceResult(name=n, label=n, ok=False, error="未知数据源")
            )

        # 缓存命中：直接复用上次的全量结果（深拷贝，避免污染缓存）
        cache_key = TTLCache.make_key(
            query, limit_per_source, ",".join(names), False
        )
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            results, src_stats, elapsed_ms = cached
            outcome.results = results
            outcome.sources.extend(src_stats)
            outcome.elapsed_ms = elapsed_ms
            return outcome

        all_records: list[PaperRecord] = []
        if names:
            client_timeout = max(
                [getattr(REGISTRY[n], "timeout", self.timeout) or self.timeout for n in names]
            )
            async with build_clients(client_timeout) as client:
                tasks = [
                    self._run_one(client, REGISTRY[n], query, limit_per_source)
                    for n in names
                ]
                for res in await asyncio.gather(*tasks):
                    outcome.sources.append(res[0])
                    all_records.extend(res[1])

        outcome.results = rank_results(merge(all_records))
        outcome.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        SEARCH_CACHE.set(
            cache_key, (outcome.results, list(outcome.sources), outcome.elapsed_ms)
        )
        return outcome

    # ---------- 流式：先返回的先推送 ----------
    async def search_stream(
        self,
        query: str,
        limit_per_source: int = 20,
        sources: list[str] | None = None,
        enrich_oa: bool = True,
    ) -> AsyncIterator[dict]:
        """逐个产出事件字典，由上层转成 SSE。"""
        from .enrich import OaEnricher, apply_enrichment

        names, unknown = self._resolve_sources(sources)
        t0 = time.perf_counter()

        # 缓存命中：直接把上次的最终结果当 done 事件推出（含各源统计），
        # elapsed_ms 沿用原始检索耗时，保持状态栏数字有意义。
        cache_key = TTLCache.make_key(
            query, limit_per_source, ",".join(names), bool(enrich_oa)
        )
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            results, src_stats, elapsed_ms = cached
            yield {
                "event": "done",
                "data": {
                    "query": query,
                    "total": len(results),
                    "enriched": 0,
                    "elapsed_ms": elapsed_ms,
                    "cached": True,
                    "results": [m.to_dict() for m in results],
                    "sources": [
                        {"name": s.name, "label": s.label, "ok": s.ok,
                         "count": s.count, "elapsed_ms": s.elapsed_ms,
                         "error": s.error}
                        for s in src_stats
                    ],
                },
            }
            # 缓存结果已带 OA 标注，补发一个 oa 事件让状态栏显示篇数，
            # 与未命中路径的交互保持一致。
            if enrich_oa and results:
                yield {
                    "event": "oa",
                    "data": {
                        "annotated": 0,
                        "open_count": sum(
                            1 for m in results
                            if m.record.oa_status == "open"
                        ),
                        "results": [m.to_dict() for m in results],
                    },
                }
            return

        for n in unknown:
            yield {
                "event": "source",
                "data": {"name": n, "label": n, "ok": False, "count": 0,
                         "elapsed_ms": 0, "error": "未知数据源"},
            }

        merger = IncrementalMerger()
        source_stats: list[SourceResult] = []

        if names:
            client_timeout = max(
                [getattr(REGISTRY[n], "timeout", self.timeout) or self.timeout for n in names]
            )
            async with build_clients(client_timeout) as client:
                tasks = [
                    asyncio.create_task(
                        self._run_one(client, REGISTRY[n], query, limit_per_source),
                        name=n,
                    )
                    for n in names
                ]

                for coro in asyncio.as_completed(tasks):
                    stat, records = await coro
                    source_stats.append(stat)
                    yield {"event": "source", "data": {
                        "name": stat.name, "label": stat.label, "ok": stat.ok,
                        "count": stat.count, "elapsed_ms": stat.elapsed_ms,
                        "error": stat.error,
                    }}

                    if records:
                        added = merger.add_batch(records)
                        if added or True:
                            # 每次有源回来都推一次当前快照（前端可增量替换）
                            snapshot = merger.ranked()
                            yield {
                                "event": "partial",
                                "data": {
                                    "total": len(snapshot),
                                    "added": added,
                                    "results": [m.to_dict() for m in snapshot],
                                },
                            }

        # 先推最终结果：OA 增强（Unpaywall 逐条查询，预算最坏 8s）不再
        # 阻塞 done 事件。此前 OpenAIRE 一个源 20s + OA 增强 8s，用户要等
        # 26s 才看到"完成"；现在卡片立即可见，开放获取标注随后增量到达。
        final = merger.ranked()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        SEARCH_CACHE.set(
            cache_key, (final, list(source_stats), elapsed_ms)
        )
        yield {
            "event": "done",
            "data": {
                "query": query,
                "total": len(final),
                "enriched": 0,
                "elapsed_ms": elapsed_ms,
                "cached": False,
                "results": [m.to_dict() for m in final],
                "sources": [
                    {"name": s.name, "label": s.label, "ok": s.ok,
                     "count": s.count, "elapsed_ms": s.elapsed_ms, "error": s.error}
                    for s in source_stats
                ],
            },
        }

        # OA 增强：done 之后增量推送。事件携带增强后的全量结果，
        # 前端据此重渲染卡片（补上「开放获取」标签与全文链接）。
        enriched = 0
        if enrich_oa and merger.items:
            snapshot = merger.ranked()
            enricher = OaEnricher()
            info = await enricher.enrich([m.record for m in snapshot])
            if info:
                enriched = apply_enrichment(snapshot, info)
            # 增强后的结果覆盖缓存，使后续命中缓存的查询也带 OA 标注
            SEARCH_CACHE.set(
                cache_key, (snapshot, list(source_stats), elapsed_ms)
            )
            yield {
                "event": "oa",
                "data": {
                    "annotated": enriched,
                    "open_count": sum(
                        1 for m in snapshot
                        if m.record.oa_status == "open"
                    ),
                    "results": [m.to_dict() for m in snapshot],
                },
            }

    # ---------- 内部 ----------
    def _resolve_sources(self, sources: list[str] | None):
        names = sources or [n for n, c in REGISTRY.items() if c.enabled]
        unknown = [n for n in names if n not in REGISTRY]
        return [n for n in names if n in REGISTRY], unknown

    async def _run_one(
        self,
        client: httpx.AsyncClient,
        cls,
        query: str,
        limit: int,
    ) -> tuple[SourceResult, list[PaperRecord]]:
        t0 = time.perf_counter()
        eff_timeout = getattr(cls, "timeout", self.timeout) or self.timeout
        try:
            conn = cls(client)
            records = await asyncio.wait_for(conn.search(query, limit), timeout=eff_timeout)
            elapsed = int((time.perf_counter() - t0) * 1000)
            return (
                SourceResult(name=cls.name, label=cls.label, ok=True,
                             count=len(records), elapsed_ms=elapsed),
                records,
            )
        except asyncio.TimeoutError:
            return (
                SourceResult(name=cls.name, label=cls.label, ok=False,
                             elapsed_ms=int((time.perf_counter() - t0) * 1000),
                             error=f"超时（>{eff_timeout:.0f}s）"),
                [],
            )
        except Exception as e:
            return (
                SourceResult(name=cls.name, label=cls.label, ok=False,
                             elapsed_ms=int((time.perf_counter() - t0) * 1000),
                             error=f"{type(e).__name__}: {e}"),
                [],
            )
