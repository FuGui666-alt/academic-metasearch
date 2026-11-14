# 学术元搜索 (Academic Metasearch)

一个在本地运行的学术元搜索引擎：在一个界面里检索，后端并发查询多个公开学术数据源，去重归并后统一返回。

> 本版本只接**公开学术 API**。不包含任何付费墙站点抓取。全文获取通过 DOI / 原文链接跳转到合法来源。

## 当前能力

| 数据源 | 覆盖 | 需要 key | 实测状态 |
|--------|------|----------|----------|
| OpenAlex | 论文、作者、机构（2.5 亿+ 实体） | 否 | ✅ 稳定 |
| Crossref | DOI 元数据、出版社、引文 | 否 | ✅ 稳定 |
| DataCite | **数据集 / 数据论文**（Zenodo、Dryad、figshare 等） | 否 | ✅ 稳定（偏慢） |
| arXiv | 预印本，全文开放 | 否 | ✅ 稳定 |
| Europe PMC | 生医文献，部分开放全文 | 否 | ✅ 稳定（偶慢） |
| DOAJ | 开放获取期刊 | 否 | ✅ 稳定 |
| OpenAIRE | 欧盟开放科学聚合 | 否 | ✅ 可用（15~30s，已放宽超时） |
| Semantic Scholar | 摘要/引用网络 | 建议 | ⚠️ 无 key 时限流 429 |
| Zenodo | 通用数据仓储 | 否 | ❌ 本机网络不可达 |
| OpenReview | ML/CS 会议论文（ICLR/NeurIPS 等） | 否 | ✅ 可用（只取带标题的，见下） |

功能：
- **流式返回（SSE）**：哪个源先回来就先展示，首个结果通常 1~2s 内出现
- **Unpaywall 开放获取标注**：自动判断每篇有无合法免费全文，并补全 PDF 链接
- 多源并发检索，单源失败/超时不影响整体（超时按源可单独配置）
- 两级去重：DOI 优先 → 归一标题+首作者+年份 → 标题模糊兜底（0.92 阈值）
- 流式场景下支持**增量去重**（后到的源能并入已有结果，来源标签自动累积）
- 多源共现加成排序，结果标注来源标签
- 类型过滤（论文/数据集）、开放获取过滤、按年份排序
- 数据源可逐个开关
- 每条结果生成「知网检索 / 学校入口 / DOI」跳转链接（只生成外链，不抓取）

### 开放获取标注（Unpaywall）

对每条有 DOI 且尚未判定为开放的结果，向 Unpaywall 查询：
- `hybrid` / `bronze` / `gold` → 有免费全文，直接给出 PDF 链接
- `green` → 仓库版本可免费获取（可能是预印本）
- `closed` → 标记「需订阅」，不给假链接

配置真实邮箱（Unpaywall 要求）：
```bash
# Windows PowerShell
$env:UNPAYWALL_EMAIL="your@email.com"
# 或写进 .env 由启动脚本读取
```
未配置时用占位邮箱，请求仍可工作但可能被限流。

### 关于知网与知乎（明确不接入）

- **知网（CNKI）**：无公开检索 API，订阅内容受版权保护，本项目**不爬取**。只做跳转：
  点「知网检索」在你自己浏览器（校园网内）完成正式检索与下载。
- **知乎**：是问答社区，不是学术文献源，无公开 API，接入会污染学术检索的相关度排序。
  如确需，应做成独立的「扩展阅读」模块而非常规结果。

### OpenReview 的已知限制

`/notes/search` 是全文搜索，返回结果混合了论文正文与**审稿意见**（后者无 title 字段）。
取主论文的 `/notes?id=` 接口需要人机验证（403），本项目**不做绕过**。
因此只保留带 title 的条目（实测 25 条原始结果中约 4 条是论文），属于补充源而非主力源。

## 快速开始

```bash
pip install -r requirements.txt
python run.py
```

浏览器打开 <http://127.0.0.1:8000>

## 项目结构

```
app/
  models.py          统一数据模型 PaperRecord / MergedRecord
  normalize.py       字段规范化、DOI 规范化、去重键计算
  merger.py          去重合并 + 排序（多源共现加成）
  aggregator.py      并发扇出、超时隔离、失败降级
  main.py            FastAPI 路由
  connectors/
    base.py          Connector 基类 + 注册表
    openalex.py      OpenAlex
    crossref.py      Crossref
    datacite.py      DataCite（数据集）
    arxiv.py         arXiv
    europepmc.py     Europe PMC
web/                 前端（原生 HTML/CSS/JS，无需构建）
run.py               启动入口
```

## 新增一个数据源

只需两步，不动主流程：

```python
# app/connectors/yoursource.py
from ..models import PaperRecord
from .base import BaseConnector, register

@register
class YourConnector(BaseConnector):
    name = "yoursource"
    label = "你的数据源"

    async def search(self, query: str, limit: int = 20) -> list[PaperRecord]:
        r = await self.client.get("https://api.example.com/search",
                                  params={"q": query, "n": limit})
        r.raise_for_status()
        return [PaperRecord(...) for i, item in enumerate(r.json()["items"], 1)]
```

然后在 `app/connectors/__init__.py` 里 import 一下即可。

## API

| 端点 | 说明 |
|------|------|
| `GET /api/search/stream?q=&limit=&sources=&oa=` | **流式检索（SSE）**，前端默认使用 |
| `GET /api/search?q=&limit=&sources=` | 一次性检索（等所有源返回后统一输出） |
| `GET /api/citations?id=&limit=` | **引文网络**（本文引用了谁 / 谁引用了本文） |
| `GET /api/oa/{doi}` | 查单个 DOI 的开放获取情况 |
| `GET /api/sources` | 列出所有数据源 |
| `GET /api/health` | 健康检查 |

### SSE 事件协议

| 事件 | 说明 |
|------|------|
| `source` | 某个源完成（含成功/失败、条数、耗时） |
| `partial` | 结果增量更新（含当前全部结果的快照） |
| `oa` | 开放获取标注完成（标注条数、免费全文篇数） |
| `done` | 全部结束（最终结果 + 各源统计） |
| `error` | 整体异常 |

## 引文网络

结果卡片上的「引用关系」按钮，追两个方向：

- **被引 · 谁引用了本文** → 找最新进展（关键词搜不到的新论文，因为未必用你搜的词）
- **引用 · 本文引用了谁** → 找奠基文献（按被引数排序，高被引的多半是经典）

数据全部来自 OpenAlex 官方 API，**免费无 key**。

支持的标识符（后端自动解析）：

| 输入 | 示例 |
|------|------|
| OpenAlex work id | `W2582566895` |
| DOI | `10.2136/vzj2016.10.0105` |
| arXiv ID | `2401.00001` 或 `arXiv:2401.00001` |
| OpenAlex URL | `https://openalex.org/W2582566895` |

```bash
curl "http://127.0.0.1:8000/api/citations?id=10.2136/vzj2016.10.0105&limit=25"
```

## 工具脚本

### `tools/probe_oai.py` —— OAI-PMH 端点探测

OAI-PMH 是图书馆行业标准元数据协议。若学校图书馆开放此端点，可**合规地**
批量采集馆藏元数据，无需抓取网页。

```bash
python tools/probe_oai.py https://某校图书馆域名/
python tools/probe_oai.py https://某图书馆域名 --deep
```

**实测参考**：国内高校多数未开放 OAI-PMH 端点（常见路径可能返回
`410 Gone`），这是常态。如需馆藏接入，建议直接向图书馆/信息中心
咨询是否有机构仓储接口。

## 后续可做

- [ ] 接 Unpaywall，标注「哪些论文有合法免费全文」
- [ ] Redis 缓存最近查询
- [ ] 检索历史与收藏（需加数据库 + 用户态）
- [ ] 本地建索引模式（定时同步，替代实时联邦查询）
- [ ] 学校馆藏对接（需先获得图书馆授权，见 README 下方说明）

## 合规边界

1. 仅索引公开元数据与摘要，不做订阅全文的本地分发。
2. 遵循各 API 限流礼仪（User-Agent 带联系方式、合理并发）。
3. 不要用爬虫替代官方 API（反爬、条款、稳定性三重问题）。
4. 学校已订购数据库（知网/IEEE 等）无公开 API，本软件只做跳转，不做抓取。
5. 若将来接入学校馆藏系统，**先与图书馆书面确认**。
