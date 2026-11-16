"""查询结果缓存：避免同一检索词反复全量扇出到各数据源。

写论文时要反复微调检索词、来回对比结果，无缓存时每次都把 10 个源重打
一遍（验收实测：同一查询第一次 6.7s，第二次 17.8s）。命中缓存后毫秒级
返回，同时把对 OpenAlex / Crossref 等公共 API 的请求量降一个数量级
（限流礼仪也更好）。

设计：
  - key 含 query / limit / sources / enrich_oa，不同参数互不串味；
  - 命中时返回深拷贝，调用方对结果对象的任何修改都不会污染缓存；
  - TTL 默认 15 分钟、容量 200 条，超出按 LRU 淘汰；
  - 进程内实现，服务重启即失效——学术元数据变化慢，可接受；
  - 任何异常都不影响主流程（缓存是纯加速，不是正确性依赖）。
"""
from __future__ import annotations

import copy
import json
import time
from collections import OrderedDict


class TTLCache:
    """带 TTL 与 LRU 淘汰的进程内缓存。"""

    def __init__(self, maxsize: int = 200, ttl: float = 900.0):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(*parts) -> str:
        """把缓存键的各部分拼成稳定字符串（None 与空串可区分）。"""
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    def get(self, key: str):
        """命中返回深拷贝；未命中/已过期返回 None。"""
        hit = self._d.get(key)
        if hit is None:
            self.misses += 1
            return None
        ts, val = hit
        if time.monotonic() - ts >= self.ttl:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        try:
            return copy.deepcopy(val)
        except Exception:
            # 拷贝失败不该让检索挂掉：当未命中处理
            self.misses += 1
            self.hits -= 1
            return None

    def set(self, key: str, val) -> None:
        # 深拷贝入缓存：调用方拿到的是活对象，之后还会原地修改
        # （如 /api/search 的 OA 增强、rank_results 注入分数），若直接
        # 存引用，这些修改会污染缓存里 key 声称的语义（实测：oa=true
        # 请求的 Unpaywall 标注泄漏进 oa=false 的缓存条目）。
        try:
            self._d[key] = (time.monotonic(), copy.deepcopy(val))
        except Exception:
            # 缓存只是加速层，不可深拷贝的上游数据不能中断检索。
            return
        self._d.move_to_end(key)
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._d),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


# 检索结果缓存实例：TTL 15 分钟，最多 200 条
SEARCH_CACHE = TTLCache(maxsize=200, ttl=900.0)
