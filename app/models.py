"""统一数据模型：所有 connector 的输出都必须映射成 PaperRecord。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class EntityType(str, Enum):
    PAPER = "paper"
    DATASET = "dataset"
    DATAPAPER = "datapaper"


class OaStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass
class PaperRecord:
    """一条学术实体记录（论文 / 数据集 / 数据论文）。"""

    source: str                                  # 来源 connector 名
    title: str
    id: str = ""                                 # 来源侧 id
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    venue: str | None = None
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    type: str = EntityType.PAPER.value
    oa_status: str = OaStatus.UNKNOWN.value
    fulltext_url: str | None = None              # 仅开放获取来源填写
    landing_url: str | None = None               # 详情页
    citation_count: int | None = None
    score: float = 0.0                           # 归一化相关度 0~1
    rank: int = 0                                # 来源侧原始排名（1 起）
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MergedRecord:
    """合并后的结果：多源命中同一实体。"""

    record: PaperRecord
    sources: list[str] = field(default_factory=list)
    all_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.record.to_dict()
        d["sources"] = self.sources
        d["all_ids"] = self.all_ids
        return d
