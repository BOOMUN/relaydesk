# 网站知识采集说明

## “让 Agent 学习网址”实际做了什么

这里的学习是 RAG 索引，不是重新训练 GPT，也不会改变模型参数：

```text
输入公开网址
   |
公网与同域校验 -> robots.txt / sitemap -> HTML、PDF 采集
   |
正文提取 -> 语言识别 -> 自动分类 -> 内容哈希去重
   |
网页文档（待审核）-> 分块 -> Embedding -> 数据库
   |
管理员审核发布
   |
LangChain 检索相关分块 -> LangGraph 决定回答或转人工
```

每一层都可追踪和删除。管理员删除网站来源时，该来源采集的网页文档和分块一起删除，不影响手工知识。

SongWiFi 的 WiFi 下单页、旅行设备页和商城商品页是 Nuxt 客户端渲染路由，初始 HTML 没有可用正文。采集任务会把同域、已启用的结构化产品目录转换为对应的产品页知识文本，覆盖 4G/5G WiFi、旅行设备和商城商品。知识文本只保存产品身份、目的地、网络、别名、规格和说明；实时价格及库存仍由结构化产品目录查询，避免向量检索返回过期数字。由于这些文本来自系统已经核实并用于报价的结构化目录，产品知识页会自动发布；其他公开网页仍须人工审核。

## 数据库分层

- `knowledge_sources`：输入的根网址、域名、页面/深度限制、任务状态和进度。
- `knowledge_web_pages`：每个网页的原始 URL、内容类型、语言、哈希、字数和审核状态。
- `knowledge_documents`：清洗后的完整正文、标题和业务分类；继续兼容手工文档。
- `knowledge_chunks`：用于 RAG 的文本分块、固定维度 Embedding、模型名称和内容哈希。生产 PostgreSQL 使用 `vector(384)` + HNSW；SQLite JSON 仅用于回归。
- `knowledge_page_revisions`：网站变化后的待审核版本；发布前不会替换 AI 正在使用的正文。
- `knowledge_page_sync_states`：页面最后出现时间、连续缺失次数和疑似下线状态。
- `knowledge_sync_runs`：初次、手动、每日和重试任务的运行记录与差异数量。

SQLite 仍用 JSON 作为回归兼容层；生产 PostgreSQL 的 `knowledge_chunks.embedding` 固定为 `vector(384)`，并建立 HNSW cosine 索引。使用 `scripts/migrate_pgvector.py` 安装扩展、复制结构化表和全量重建向量；脚本会验证模型集合/维度后才允许人工切换流量。

## 自动分类

第一版使用可审计的关键词评分，支持：

- 产品
- 常见问题
- 政策条款
- 订单物流
- 售后服务
- 客服服务
- 公司信息
- 其他

分类只是草稿建议，管理员可以在发布前修改。接入正式 GPT 后可以增加结构化 LLM 分类器，但不建议在没有成本控制和评测集时对数百页面逐页调用模型。

## 采集边界

- 只接受解析到公网 IP 的 `http/https` 地址。
- 只允许 80/443 端口，并且每次跳转都重新检查域名和公网地址。
- 只跟随输入域名（包含 `www` 等价形式）内的链接。
- 默认最多 500 页、链接深度 5；单个 HTML 最大 5 MB，PDF 最大 15 MB/200 页。
- 读取 `robots.txt` 和 sitemap；`robots.txt` 禁止的路径不会采集。
- HTML 会移除脚本、表单、导航和页脚，再保存主要正文。
- 客户端渲染的 SongWiFi 产品路由使用结构化目录生成可审核正文，并保留官方产品 URL 作为来源。
- PDF 保存可提取的文字；扫描图片型 PDF 暂不做 OCR。
- 图片、视频、Office 文件只保留网页中的上下文，不下载二进制内容。

## 每日同步

- 独立调度器每天在 `Asia/Shanghai` 03:10 创建同步任务。
- 如果机器在 03:10 没有运行，当天稍后启动时会补跑一次；Redis 日期键防止重复执行。
- 独立 Worker 串行抓取，客服 API 不需要等待 500 页任务完成。
- 内容哈希相同的网页只更新“最后看到”状态，不重新生成知识向量。
- 新页面进入草稿；已发布页面发生变化时保存到修订表，AI 继续检索上一版。
- 完整且无页面错误的两次连续扫描都找不到某页后，才标记为“疑似下线”；系统不会自动删除旧知识。
- 整体任务失败后分别延迟 30 分钟、90 分钟重试。管理员也可以点击“立即同步”。

本地任务使用 Redis DB 7，WhatsApp/Evolution 缓存使用 DB 6，两者逻辑隔离。正式生产仍应给采集 Worker 配置固定出口网络、域名允许列表、硬超时、恶意文件扫描和进程监管，并把 SQLite 迁移到 PostgreSQL。

## 审核与发布

普通采集页面初始为 `draft` 且 `is_active=false`。因此页面虽然已经进入数据库和分块表，但客服 Agent 不会检索它。由结构化目录生成且不含实时价格/库存的产品知识页是例外，会自动发布。管理员可以：

1. 按待审核状态和分类筛选；
2. 打开单页修改正文或分类；
3. 单页发布，或在网站来源卡片中“发布全部”；
4. 发现网站内容不可靠时删除整个来源。

这条审核边界对真实客服很重要：网络内容可能过期，也可能包含针对 Agent 的提示注入文字。

## API

- `POST /api/knowledge/sources`：创建网址采集任务。
- `GET /api/knowledge/sources`：查看进度和草稿/发布数量。
- `POST /api/knowledge/sources/{id}/retry`：重新采集。
- `POST /api/knowledge/sources/{id}/sync`：立即创建增量同步任务（`retry` 路径保留兼容）。
- `POST /api/knowledge/sources/{id}/publish`：发布该来源全部草稿。
- `DELETE /api/knowledge/sources/{id}`：删除来源及其网页知识。
- `PATCH /api/knowledge/{id}`：修改单页正文、分类或审核状态。

实现依据包括 Python 的 [`urllib.robotparser`](https://docs.python.org/3/library/urllib.robotparser.html)、[HTTPX Client](https://www.python-httpx.org/advanced/clients/)、[Beautiful Soup 文本提取](https://beautiful-soup-4.readthedocs.io/en/latest/) 与 [pypdf](https://pypdf.readthedocs.io/)。
