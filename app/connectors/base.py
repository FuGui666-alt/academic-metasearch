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


# 应用级共享客户端（N2）：整个服务生命周期复用连接池。
_SHARED_CLIENT: httpx.AsyncClient | None = None


def get_shared_client() -> httpx.AsyncClient:
    """取应用级共享 httpx 客户端，惰性创建。

    此前每次检索 async with build_clients(...) 新建客户端：一次检索
    对 10 个源各做一轮 TCP+TLS 握手，连接无法跨请求复用。共享后握手
    只发生一次；per-source 超时仍由 aggregator._run_one 的 wait_for
    按 connector 自身 timeout 强制，不受共享超时影响。
    """
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        timeout = max((c.timeout for c in REGISTRY.values()), default=15.0)
        _SHARED_CLIENT = build_clients(timeout)
    return _SHARED_CLIENT


async def close_shared_client() -> None:
    """服务关闭时释放共享连接池（FastAPI lifespan 调用）。"""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed:
        await _SHARED_CLIENT.aclose()
    _SHARED_CLIENT = None
