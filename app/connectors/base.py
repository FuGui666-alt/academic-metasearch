"""Connector 基类与注册表。

新增数据源只需：继承 BaseConnector，实现 search()，用 @register 注册。
"""
from __future__ import annotations

import abc
import os
from typing import Any, ClassVar

import httpx

from ..models import PaperRecord

# 共享的 httpx 客户端配置：统一 UA + 合理超时
DEFAULT_HEADERS = {
    "Accept": "application/json",
}


def metadata_email() -> str:
    """OpenAlex / Crossref 等礼貌池（polite pool）要求的联系邮箱。

    优先读 METADATA_EMAIL 环境变量；未配置时退回项目现有占位邮箱
    （与 Unpaywall 增强、引文网络所用一致）。
    待补充：运营者真实邮箱——设置 METADATA_EMAIL 环境变量后即生效，
    无需改代码。
    """
    return (
        os.environ.get("METADATA_EMAIL", "").strip()
        or "noreply@example.com"
    )


def user_agent() -> str:
    """构造 UA：mailto 部分动态取 metadata_email()，env 改动对新连接生效。"""
    return (
        f"AcademicMetasearch/0.1 (https://lib.example.edu.cn/; "
        f"mailto:{metadata_email()})"
    )


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
        headers={**DEFAULT_HEADERS, "User-Agent": user_agent()},
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )
