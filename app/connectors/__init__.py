"""导入所有 connector，触发 @register 注册。

新增数据源时在这里加一行 import 即可，主流程无需改动。
"""
from .base import REGISTRY, BaseConnector, build_clients, register  # noqa: F401

# 已有数据源
from .openalex import OpenAlexConnector  # noqa: F401
from .crossref import CrossrefConnector  # noqa: F401
from .datacite import DataCiteConnector  # noqa: F401
from .arxiv import ArxivConnector  # noqa: F401
from .europepmc import EuropePmcConnector  # noqa: F401

# 新增数据源
from .doaj import DoajConnector  # noqa: F401
from .openaire import OpenAireConnector  # noqa: F401
from .semanticscholar import SemanticScholarConnector  # noqa: F401
from .zenodo import ZenodoConnector  # noqa: F401
from .openreview import OpenReviewConnector  # noqa: F401

__all__ = ["REGISTRY", "BaseConnector", "build_clients", "register"]
