# 代码审查与优化报告

审查范围：项目自有源码及配置，不含 `build/`、`dist/` 内第三方依赖和 `__pycache__`。修改前项目无测试目录，因此新增离线回归测试；没有为“通过测试”而删除边界用例。

## 目录与文件职责

```text
图书馆检索/
├─ app/
│  ├─ aggregator.py       并发检索、singleflight、缓存、流式增量合并
│  ├─ cache.py            进程内 TTL + LRU 缓存
│  ├─ citations.py        OpenAlex 引文双向查询与标识解析
│  ├─ enrich.py           Unpaywall OA 增强与回写
│  ├─ main.py             FastAPI 路由、静态页面、生命周期
│  ├─ merger.py           精确/模糊去重、字段合并、排序
│  ├─ models.py           PaperRecord/MergedRecord 数据模型
│  ├─ normalize.py        文本、标题、DOI 规范化与去重键
│  └─ connectors/
│     ├─ base.py          HTTP 客户端、UA、注册表、学校链接
│     ├─ arxiv.py         arXiv Atom 检索
│     ├─ crossref.py      Crossref 文献检索
│     ├─ datacite.py      DataCite 数据集检索
│     ├─ doaj.py          DOAJ 开放期刊检索
│     ├─ europepmc.py     Europe PMC 生医文献检索
│     ├─ openaire.py      OpenAIRE XML 检索
│     ├─ openalex.py      OpenAlex 检索与倒排摘要恢复
│     ├─ openreview.py    OpenReview 匿名全文检索
│     ├─ semanticscholar.py Semantic Scholar 检索与 API key
│     ├─ zenodo.py        Zenodo 论文/数据检索
│     └─ cnki_links.py    知网、DOI、学校图书馆外链
├─ web/
│  ├─ app.js              SSE 消费、筛选、排序、引用面板、链接渲染
│  ├─ index.html          页面结构
│  └─ style.css           页面样式
├─ tools/
│  ├─ mcp_e2e.py          MCP stdio 在线端到端脚本
│  └─ probe_oai.py        手动 OAI-PMH 端点探测工具
├─ tests/                 新增核心及 MCP 离线回归测试
├─ mcp_server.py          MCP stdio 服务与工具定义
├─ run.py                 Web 开发/本地启动入口
├─ requirements.txt       Python 运行依赖
├─ 启动.bat / stop.bat / 停止.bat  启停脚本
├─ 打包.bat / 学术元搜索.spec      PyInstaller 打包配置
└─ README.md / 使用手册.md          项目文档
```

## 高优先级（结果正确性、请求安全与资源安全）

1. **`app/connectors/europepmc.py:42`，OA 字符串判定错误。** `bool("N")` 为真，会把非 OA 标为开放；改为兼容布尔和 `Y/TRUE/1`，并移除“入 PMC 即 OA”的错误推断。
2. **`app/merger.py:139-143`，同名不同年误合并。** 原 `ta == tb` 提前返回绕过年份检查；现在完全相同标题仍须年份兼容，避免年度报告/同名综述被污染。
3. **`app/normalize.py:36-47`，DOI 未校验。** 原任意含斜杠 URL 都可进入 DOI 键；增加 DOI 语法校验、大小写前缀兼容，避免误去重及错误引文查询。
4. **`app/aggregator.py:226-242`，共享检索取消传播及对象污染。** 等待者取消会中断共享任务；使用 `asyncio.shield`，所有等待者独立深拷贝，任务清理校验任务身份。
5. **`mcp_server.py:34-45`，共享客户端未关闭。** MCP 此前不会执行 FastAPI lifespan；新增 MCP lifespan 初始化/关闭，HTTP 客户端现在跨检索、引文复用。
6. **`stop.bat`、`停止.bat`，按端口强杀任意进程。** 现在先核对进程命令行属于 `run.py` 或打包应用，再停止，降低误杀风险。
7. **`app/connectors/openaire.py:67-78`，XML 命名空间回退混入内外层 result。** 统一 local-name 后仅选 `results` 的直接子节点，避免字段串读。
8. **`app/cache.py:64-70`，缓存深拷贝失败中断主检索。** 缓存写入失败仅跳过缓存，不将加速层变成正确性依赖。

## 中优先级（可维护性、异常、性能与输入边界）

- `app/aggregator.py:81-164`：流式合并补 incoming 精确键索引，按年份分桶缩小模糊匹配候选；流式未知源统计进入 `done`，不再静默丢失。
- `app/merger.py:65-83`：合并主记录先深拷贝，不再回写 connector 输入对象。
- `app/cache.py:33-36`：缓存键改 JSON 编码，消除 `|` 分隔符、None/空串碰撞。
- `web/app.js:33-35,72-83`：数据源名称存 `data-source`，不再从提示文案反解；全关数据源发送显式无源标识，不再退回全部源。
- `app/main.py:101,149`、`mcp_server.py:51-54`：区分“未传 sources”和“明确空列表”。
- `app/citations.py`：抽公共字段投影与标题判空；复用共享客户端；`limit` 不再先多取 25 条；非法 URL 不发请求。批量引用部分失败仍以降级列表呈现，建议后续加 `partial/errors` 指标。
- `app/enrich.py:43-49,159-168`：校验并发和超时参数；补 landing URL 和 closed 状态；网络/解析异常分类，不吞掉编程错误。
- `app/connectors/base.py:55-59,86-92`：学校链接仅允许 HTTP(S)，同名 connector 不再静默覆盖；客户端在 lifespan 初始化，关闭加锁。
- `app/connectors/cnki_links.py`：复用 DOI 规范化。
- `app/connectors/zenodo.py:29`：`hits: null` 容错。
- `app/connectors/crossref.py`、`doaj.py`、`zenodo.py`：HTML 实体反转，避免 `&amp;` 等残留。
- `app/connectors/arxiv.py:39`：删除无效空白替换。
- `app/connectors/datacite.py:63-69`：CC 许可按 URI/前缀判断，不再任意包含 `cc` 即开放。
- `tools/mcp_e2e.py`：工作目录从脚本位置推导，消除机器路径耦合。
- `tools/probe_oai.py`：网络请求只捕获 `httpx.HTTPError`。
- `requirements.txt`：补上 MCP 运行依赖并限制主版本，避免全新环境导入失败。

## 低优先级（剩余优化建议）

以下条目已全部处理或明确保留，无遗留未决项：

- **Semantic Scholar 429 退避**（已实施）：尊重 `Retry-After` 头，最多重试一次，上限 5 秒，见 [semanticscholar.py](app/connectors/semanticscholar.py:45)。
- **PaperRecord 运行时校验**（已实施）：`__post_init__` 归一化 year/score/citation_count/authors/keywords 类型，见 [models.py](app/models.py:50)。
- **Unpaywall/引文可观测性**（已实施）：增加 `logging` 计数与类型记录，不改 API 结构，见 [enrich.py](app/enrich.py:110) 和 [citations.py](app/citations.py:143)。
- **连接器单条容错**（已实施）：crossref/doaj/datacite 增加 `isinstance(w, dict)` 防御，见各文件 search 循环入口。
- **OpenAlex 倒排索引类型校验**（已实施）：`_inv_to_text` 防御非 str 词和非 int 位置，见 [openalex.py](app/connectors/openalex.py:89)。
- **OpenAIRE 作者去重 O(n²)**（已实施）：改用 set，见 [openaire.py](app/connectors/openaire.py:94)。
- **Europe PMC 作者列表**（已实施）：优先使用结构化 `authorList`，见 [europepmc.py](app/connectors/europepmc.py:34)。
- **Zenodo fulltext 不拿详情页当全文**（已实施）：从 `files[0].links` 取下载地址，见 [zenodo.py](app/connectors/zenodo.py:76)。
- **公共请求助手抽取**：保留为后续重构建议，本次不扩大范围（涉及 10 个 connector 的统一抽象）。
- **全量 merge() O(n²)**：保留说明，流式路径已做年份预筛；全量路径的 blocking 优化属算法重构建议。
- **OpenReview 评论/正文过滤**：保留现状——匿名 search 响应无可靠类型字段，强行过滤会误删真实论文。
- **深拷贝成本**：保留——正确性优先于性能，不建议改共享引用。
- **METADATA_EMAIL**：部署配置项，实际部署时通过环境变量注入。
- **stop.bat 与 停止.bat 重复**：保留双入口以兼容用户习惯。

## 前端与安全结论

`web/index.html` 页面结构完整，`style.css` 未见独立高风险项。`web/app.js` 对外部字段进行 HTML 转义，链接限定 HTTP(S)，未发现可复现 XSS；本次未把正常 `innerHTML` 使用误报为漏洞。Web API 当前无认证，设计适用于本机使用；若暴露公网，应增加认证、CSRF/来源控制、速率限制，并限制异常信息。

## 验证

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q`：**12 passed**，含真实 FastAPI TestClient 路由及 MCP stdio 子进程握手/工具调用。
- `python -m ruff check --select F,I app run.py mcp_server.py tools tests`：通过。
- `python -m compileall -q app run.py mcp_server.py tools tests`：通过。
- `node --check web/app.js`：通过。
- `git diff --check`：通过。
- 未运行上游公开 API 的全量在线检索；真实可用性受网络和上游限流影响，离线测试不冒充上游 SLA 验证。
