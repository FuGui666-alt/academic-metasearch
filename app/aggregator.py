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
import copy
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from .cache import SEARCH_CACHE, TTLCache
from .connectors import REGISTRY
from .connectors.base import get_shared_client
from .merger import _combine, _should_fuzzy_merge, merge, rank_results
from .models import MergedRecord, PaperRecord

# 中文查询下默认跳过的数据源：
#   - Europe PMC 按英文词干拆分匹配，中文查询会返回完全无关的条目
#     （验收实测搜「粮食 霉变 检测」返回 carbazole 聚合物论文）；
#   - OpenReview 的 /notes/search 是全文搜索，混入审稿意见，且以英文
#     会议论文为主，中文查询命中质量差（实测返回 "Metamorphic testing
#     as a test case selection strategy"）。
# 用户显式指定 sources 时完全遵从，不受此规则影响；跳过的源以失败态
# 出现在 SSE source 事件里，前端状态栏可见原因，不做静默排除。
SKIP_FOR_CJK = ("europepmc", "openreview")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(text: str) -> bool:
    """查询词是否含中日韩表意文字（中文查询判定）。"""
    return bool(_CJK_RE.search(text or ""))


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
        # dedup_key → 该结果在 _items 中的下标。精确键命中 O(1)，
        # 不再逐条重算已有结果的 dedup_key（原实现每条新记录要对
        # 全量已有结果各算一次键 + 一次模糊比对，O(n²)）。
        self._by_key: dict[str, int] = {}
        # 年份分桶用于模糊候选预筛，避免对整批结果做 O(n²) 相似度计算。
        self._by_year: dict[int | None, set[int]] = {}

    def add_batch(self, records: list[PaperRecord]) -> tuple[int, int]:
        """并入一批新结果，返回 (新增条目数, 并入已有条目的更新数)。

        流式侧据此决定是否推送快照：两者皆 0（本批全部被过滤）时
        跳过推送，省一次全量序列化。
        """
        # 先做批内去重（复用 merge 的精确键逻辑）
        batch = merge(records)
        added = 0
        updated = 0

        for incoming in batch:
            idx = self._find_match(incoming.record)
            if idx is not None:
                self._merge_into(self._items[idx], incoming, idx)
                updated += 1
            else:
                self._items.append(incoming)
                self._index(incoming.record)
                added += 1

        return added, updated

    def _index(self, rec: PaperRecord) -> None:
        from .normalize import dedup_key
        k = dedup_key(rec)
        if k:
            self._by_key[k] = len(self._items) - 1
        self._by_year.setdefault(rec.year, set()).add(len(self._items) - 1)

    def _find_match(self, rec: PaperRecord) -> int | None:
        """在已有结果里找可合并的条目，返回下标或 None。"""
        from .normalize import dedup_key

        key = dedup_key(rec)
        if key:
            hit = self._by_key.get(key)
            if hit is not None:
                return hit

        # 精确键没命中 → 走模糊匹配（长度预筛在 _should_fuzzy_merge 内）
        if rec.year is None:
            candidate_ids = (
                set().union(*self._by_year.values()) if self._by_year else set()
            )
        else:
            candidate_ids = self._by_year.get(rec.year, set()).copy()
            candidate_ids.update(self._by_year.get(None, set()))
            candidate_ids.update(self._by_year.get(rec.year - 1, set()))
            candidate_ids.update(self._by_year.get(rec.year + 1, set()))
        for i in candidate_ids:
            if _should_fuzzy_merge(self._items[i].record, rec):
                return i
        return None

    def _merge_into(self, existing: MergedRecord, incoming: MergedRecord, idx: int) -> None:
        """把 incoming 并入 existing（保留更完整的元数据）。"""
        from .normalize import dedup_key

        old_key = dedup_key(existing.record)
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

        # 合并可能改变 DOI/标题 → 精确键随之迁移
        new_key = dedup_key(existing.record)
        incoming_key = dedup_key(incoming.record)
        if new_key:
            if new_key != old_key:
                self._by_key.pop(old_key, None)
            self._by_key[new_key] = idx
        if incoming_key:
            # 模糊并入后，incoming 的精确键也要指向合并实体，防止后续重复。
            self._by_key[incoming_key] = idx
        self._by_year.setdefault(combined.record.year, set()).add(idx)

    @property
    def items(self) -> list[MergedRecord]:
        return self._items

    def ranked(self) -> list[MergedRecord]:
        return rank_results(self._items)


# 并发护栏（N9）：同时进行的全量扇出上限。多标签页/多用户并发时
# 限制对上游 API 的瞬时压力（礼貌池 / 429 风险），多余的查询排队等待。
_FANOUT_SEM = asyncio.Semaphore(4)


class Aggregator:
    """元搜索聚合器。"""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        # 进行中的检索任务（N2 singleflight）：同 key 并发请求共享同一个扇出
        self._inflight: dict[str, asyncio.Task] = {}

    # ---------- 一次性返回（原有行为） ----------
    async def search(
        self,
        query: str,
        limit_per_source: int = 20,
        sources: list[str] | None = None,
    ) -> SearchOutcome:
        names, unknown, skipped = self._resolve_sources(query, sources)
        outcome = SearchOutcome(query=query)
        for n in unknown:
            outcome.sources.append(
                SourceResult(name=n, label=n, ok=False, error="未知数据源")
            )
        for n in skipped:
            outcome.sources.append(
                SourceResult(
                    name=n, label=REGISTRY[n].label, ok=False, count=0,
                    elapsed_ms=0, error="中文查询已跳过（相关度低）",
                )
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

        # singleflight（N2）：并发相同查询共享同一个扇出任务。缓存只在
        # 任务完成时写入，进行中的任务此前不共享——双击/多标签页会各自
        # 全量打 10 个上游。
        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._search_uncached(
                    cache_key, query, limit_per_source, names, unknown, skipped
                ),
                name="search:" + cache_key,
            )
            self._inflight[cache_key] = task
        # 某个 HTTP 等待方断开时，不能取消其他请求正在共享的检索任务。
        res = await asyncio.shield(task)
        # 每个等待方拿到独立对象，避免 OA 增强和排序互相污染。
        outcome = copy.deepcopy(res)
        # 完成回调也应有移除自己的机会，避免任务表在 asyncio.run 结束前残留。
        if self._inflight.get(cache_key) is task and task.done():
            self._inflight.pop(cache_key, None)
        return outcome

    async def _search_uncached(
        self,
        cache_key: str,
        query: str,
        limit_per_source: int,
        names: list[str],
        unknown: list[str],
        skipped: list[str],
    ) -> SearchOutcome:
        """真正的全量扇出（singleflight 的飞行任务本体）。

        inflight 表项由本任务结束时自行清理：创建方中途取消不影响其他
        等待者，任务继续跑完并写缓存。
        """
        try:
            t0 = time.perf_counter()
            outcome = SearchOutcome(query=query)
            for n in unknown:
                outcome.sources.append(
                    SourceResult(name=n, label=n, ok=False, error="未知数据源")
                )
            for n in skipped:
                outcome.sources.append(
                    SourceResult(
                        name=n, label=REGISTRY[n].label, ok=False, count=0,
                        elapsed_ms=0, error="中文查询已跳过（相关度低）",
                    )
                )

            all_records: list[PaperRecord] = []
            if names:
                async with _FANOUT_SEM:
                    client = get_shared_client()
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
        finally:
            # 旧任务清理时同 key 可能已有新任务，只能移除自己。
            if self._inflight.get(cache_key) is asyncio.current_task():
                self._inflight.pop(cache_key, None)

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

        names, unknown, skipped = self._resolve_sources(query, sources)
        t0 = time.perf_counter()

        # 缓存命中：直接把上次的最终结果当 done 事件推出（含各源统计），
        # elapsed_ms 沿用原始检索耗时，保持状态栏数字有意义。
        cache_key = TTLCache.make_key(
            query, limit_per_source, ",".join(names), bool(enrich_oa)
        )
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            results, src_stats, elapsed_ms = cached
            src_stats = [
                SourceResult(name=n, label=n, ok=False, error="未知数据源")
                for n in unknown
            ] + src_stats
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

        # 中文查询跳过的源：以失败态推送，前端状态栏显示原因（不静默）
        for n in skipped:
            yield {
                "event": "source",
                "data": {
                    "name": n, "label": REGISTRY[n].label, "ok": False,
                    "count": 0, "elapsed_ms": 0,
                    "error": "中文查询已跳过（相关度低）",
                },
            }

        merger = IncrementalMerger()
        source_stats: list[SourceResult] = [
            SourceResult(name=n, label=n, ok=False, error="未知数据源")
            for n in unknown
        ]

        # 跳过的源也计入最终统计，保证 done 事件的状态栏与实际一致
        for n in skipped:
            source_stats.append(
                SourceResult(
                    name=n, label=REGISTRY[n].label, ok=False, count=0,
                    elapsed_ms=0, error="中文查询已跳过（相关度低）",
                )
            )

        if names:
            async with _FANOUT_SEM:
                client = get_shared_client()
                tasks = [
                    asyncio.create_task(
                        self._run_one(client, REGISTRY[n], query, limit_per_source),
                        name=n,
                    )
                    for n in names
                ]

                try:
                    for coro in asyncio.as_completed(tasks):
                        stat, records = await coro
                        source_stats.append(stat)
                        yield {"event": "source", "data": {
                            "name": stat.name, "label": stat.label, "ok": stat.ok,
                            "count": stat.count, "elapsed_ms": stat.elapsed_ms,
                            "error": stat.error,
                        }}

                        if records:
                            added, updated = merger.add_batch(records)
                            if added or updated:
                                # 有新增或已有条目被补充时推快照（前端可增量替换）
                                snapshot = merger.ranked()
                                yield {
                                    "event": "partial",
                                    "data": {
                                        "total": len(snapshot),
                                        "added": added,
                                        "results": [m.to_dict() for m in snapshot],
                                    },
                                }
                finally:
                    # 客户端断开 / 生成器被关闭时，as_completed 循环中途退出，
                    # 未消费完的任务会继续跑完并向已关闭的连接写数据，产生
                    # "Task exception was never retrieved" 噪音。这里统一
                    # 取消并回收，保证 SSE 断开后不再有遗留任务。
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

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
    def _resolve_sources(
        self, query: str, sources: list[str] | None
    ) -> tuple[list[str], list[str], list[str]]:
        """解析本次检索实际要查的数据源。

        返回 (known, unknown, skipped)：
          - unknown：不在 REGISTRY 里的名字（含用户拼错），标记为失败；
          - skipped：中文查询下按 SKIP_FOR_CJK 规则跳过的源，
            仅在用户未显式指定 sources 时生效。
        """
        names = sources or [n for n, c in REGISTRY.items() if c.enabled]
        unknown = [n for n in names if n not in REGISTRY]
        known = [n for n in names if n in REGISTRY]
        skipped: list[str] = []
        if sources is None and _has_cjk(query):
            skipped = [n for n in known if n in SKIP_FOR_CJK]
            known = [n for n in known if n not in SKIP_FOR_CJK]
        return known, unknown, skipped

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
        except TimeoutError:
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
