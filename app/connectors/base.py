"""Connector 基类与注册表。

新增数据源只需：继承 BaseConnector，实现 search()，用 @register 注册。
"""
from __future__ import annotations

import abc
from typing import Any, ClassVar

import httpx

from ..models import PaperRecord

# 共享的 httpx 客户端配置：统一 UA + 合理超时
DEFAULT_HEADERS = {
    "User-Agent": (
        "AcademicMetasearch/0.1 (https://lib.example.edu.cn/; "
        "mailto:your-email@example.com)"
    ),
    "Accept": "application/json",
}


class BaseConnector(abc.ABC):
    name: ClassVar[str] = ""
    label: ClassVar[str] = ""          # 前端展示名
    entity_type: ClassVar[str] = "paper"
    base_url: ClassVar[str] = ""
    enabled: ClassVar[bool] = True
    timeout: ClassVar[float] = 15.0

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        """执行检索，返回归一化的 PaperRecord 列表（rank 从 1 起）。"""
        raise NotImplementedError

    def normalize_score(self, rank: int) -> float:
        """把来源侧排名映射成 0~1 的相关度。越靠前越接近 1。"""
        from math import exp
        return round(exp(-0.15 * (max(rank, 1) - 1)), 4)


REGISTRY: dict[str, type[BaseConnector]] = {}


def register(cls: type[BaseConnector]) -> type[BaseConnector]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    REGISTRY[cls.name] = cls
    return cls


def build_clients(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )
