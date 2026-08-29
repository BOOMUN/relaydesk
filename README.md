# RelayDesk

RelayDesk 是一个面向公司自用、为未来多租户 SaaS 预留边界的 WhatsApp 客服平台框架。第一版提供共享收件箱、联系人、知识库、客服分析、人工接管、WhatsApp Web/Meta 通道，以及 LangChain + LangGraph 客服流程。

## 当前闭环

- Evolution API（WhatsApp Web）扫码、LID 回复路由、安全 Webhook、消息去重、送达/已读回执、主动状态对账与失败安全重试
- Meta WhatsApp Cloud API 作为可切换的正式通道
- 统一 Actions 框架：输入 Schema、调用权限、风险等级、超时/重试、幂等键、人工确认、执行尝试与失败审计
- 转人工/恢复 AI、分配、联系人资料/标签/自定义字段、会话状态/优先级以及文本/模板/按钮/列表发送均通过 Action 执行
- LangGraph 持久业务表单：逐项收集订单号、出发日期、目的地及敏感操作资料，支持暂停、恢复、修改答案、超时和真实转人工
- 可配置线索资格流程：问题/选项评分、等级、标签、优先级及团队/客服分配均通过 Actions 落库
- 管理员审批的 REST Action 连接器：HTTPS 固定源站、域名/路径/方法白名单、Fernet 密钥加密、私网/重定向/DNS 重绑定防护
- 受控网络搜索后备：仅在已发布 Agent 规则允许且知识库无可靠答案时调用固定 Provider，结果按允许域名过滤并随回答显示来源
- Meta 模板、语言和审核状态同步；系统只允许发送已由 Meta 审核为 `APPROVED` 的模板
- Open、Pending、Expired、Solved、Blocked 会话状态
- AI、团队、具体客服三类处理责任边界
- LangGraph 标准工具调用：模型通过 `tool_calls` 在商品查价、知识问答、订单查询、VIP 与人工接管工具中选择，预构建 `ToolNode` 统一执行；客服范围内首次检索无结果会先执行一次 query rewrite 重试，仍无可验证证据才转人工
- 人工请求采用确定性同义句识别；固定通知客户后暂停 AI，并把会话放入高优先级“待处理”队列（客服 24 小时在线，不附加营业时间）
- LangChain 混合检索：PostgreSQL/pgvector HNSW 向量候选 + BM25 + 国家/租借类型硬过滤 + 最终重排；SQLite 仍可作为回归基线
- 多语种 `paraphrase-multilingual-MiniLM-L12-v2`（384 维）Embedding；结构化商品库继续保存价格、库存、国家等权威字段
- 联系人标签、知识文档 CRUD、基础统计与审计日志
- 网站知识采集：网址输入、同域 HTML/PDF 抓取、robots.txt、自动分类、待审核发布与持久化分块
- 网站知识每日北京时间 03:10 增量同步、Redis 任务队列、独立 Worker、失败延迟重试与内容修订审核
- 独立“商品价格”后台：多个网址分组、优惠价/原价、套餐规格、变价历史和每天 03:10 自动同步
- SongWiFi 专用价格适配器（WiFi 蛋、eSIM、旅行设备、eShop 商城商品及规格库存）及通用 Schema.org Product 适配器
- WhatsApp 纯文字价目表：按目的地/商品筛选，每项附套餐下单/商品链接，每段最多 20 项；只有明确要求完整价目表时发送全部
- 多轮查价追问：把等待中的目的地/商品条件写进消息元数据，客户下一句只回复“韩国”等短答案也能继续上一轮，服务重启后仍有效
- 30 分钟持久会话上下文：客户每次发言都会续期，同一会话内持续关联目的地、人数、天数和前文；转人工立即结束 AI 上下文
- 客户静默 30 分钟后先发送「AI 智能結束當前對話」，Provider 接受发送后自动归入“已解决”；失败会保留会话并延迟重试
- Vue 共享收件箱，支持桌面与移动屏幕
- RelayDesk 自有客服队列：我的、未分配、未读、等待中、状态与高级筛选
- 团队/客服转派、人工接手、内部备注、快捷回复、活动时间线
- 联系人标签、自定义字段；客服界面支持简体/繁体显示
- AI 按客户当前消息自动识别简体、繁体或英文并使用同一语言回复；不依赖联系人手动语言选择
- 英文聊天框右键即时翻译为繁体中文，以蓝色翻译框暂时显示且不写入数据库
- 轻量 SSE 实时通知，30 秒轮询作为断线兜底

## Actions 与 WhatsApp 通道

LangGraph 只输出结构化的 Action 请求，不持有 WhatsApp、数据库或任意第三方 REST 凭证。可信应用层补入租户、会话和操作者上下文，由 Action 执行器完成 Schema 校验、权限检查、风险确认、幂等去重、超时/重试和审计，再调用业务 handler。AI、人工客服、失败重试和会话自动结束使用同一条执行路径。

通道统一实现 `ChannelProvider`。Meta WhatsApp Cloud API 是正式生产通道，Evolution/WhatsApp Web 用于兼容或测试，`demo` 仅供本地回归；切换通道不修改 AI Graph。Provider 统一负责入站解析、Webhook 验签、发送状态、失败原因和 WhatsApp Message ID，入站事件在持久化时使用唯一键去重。

联系人、标签、自定义字段、团队、客服、分配关系、会话、消息、内部备注、AI/人工接管状态、Agent/知识库版本及 Action 审计均由 RelayDesk 独立管理。项目没有 WATI Adapter，也不依赖 WATI 运行。

## 业务自动化与外部能力

订单与线索表单状态保存在 `automation_form_sessions`，每次状态转换和答案修改追加到 `automation_form_events`。表单定义在启动时按已发布 Agent 版本做快照，因此管理员之后修改问题不会改变进行中的客户流程。`conversation-scheduler` 负责将超时表单转为真实人工接管状态。

退款、取消订单、修改地址和敏感资料只会创建 `order.sensitive.request`。客服必须先记录限时身份核验，再人工批准；批准结果是 `approved_for_manual_execution`，系统不会直接执行退款或修改订单。核验原文不入库，只保存 SHA-256 摘要、审计提示、核验人和有效期。

REST 连接器由管理员建立并批准，任何配置变更都会使其回到草稿状态。执行时再次校验审批状态、HTTP 方法、路径、DNS 与实际连接地址，拒绝代理环境变量、跳转、私网地址、超大响应和非允许内容类型。网络搜索使用固定 Brave Search API，不接受模型指定任意搜索 URL。

## 本地运行

Python 依赖已经安装在项目 `.venv`，前端生产文件位于 `frontend/dist`。启动服务：

```powershell
.\run.ps1
```

`run.ps1` 会同时确保知识库 Worker、商品价格 Worker、每日同步调度器与会话结束调度器在后台运行。知识采集与价格同步使用独立 Redis 队列，互不阻塞。定时任务使用 Evolution
环境中的 Redis（仅映射到本机 `127.0.0.1:6380`，知识任务使用 DB 7，WhatsApp
缓存使用 DB 6）。因此应先运行一次 `scripts/start-evolution.ps1`。

首次使用 `fastembed` 时会下载约 220 MB 的多语种模型；请在迁移/预热阶段完成下载，不要让首个真实客户请求承担模型初始化延迟。

浏览器访问 `http://127.0.0.1:8000`。局域网设备使用运行机器的 IPv4 地址和端口 `8000`。

首次启动前，请在 `.env` 中设置管理员账号：

```text
AGENTDESK_ADMIN_EMAIL=your-admin@example.com
AGENTDESK_ADMIN_PASSWORD=replace-with-a-strong-password
```

首次用于真实环境前必须修改账号密码，并设置 `AGENTDESK_SEED_DEMO_DATA=false`。

## WhatsApp Web 本地通道

本地通道使用固定版本的 Evolution API `v2.3.7`，并把 API 只绑定到 `127.0.0.1:8081`。首次运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-evolution.ps1
```

脚本会在 `infra/evolution/.env` 生成本机专用的 API Key、Webhook Secret 和数据库密码，然后启动 Evolution API、PostgreSQL 与 Redis。重启 RelayDesk 后，在“设置 -> WhatsApp Web”点击“获取二维码”，再用手机 WhatsApp 的“关联设备”扫描。

Evolution 容器查看与停止：

```powershell
docker compose --env-file infra\evolution\.env -f infra\evolution\compose.yml ps
docker compose --env-file infra\evolution\.env -f infra\evolution\compose.yml stop
```

WhatsApp Web 接入不是 Meta 官方 Business API，存在掉线、协议变化和号码限制风险。不要把 Evolution API Key、二维码或会话文件交给第三方。

## 从干净环境安装

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
alembic upgrade head
cd frontend
yarn install --ignore-optional --registry https://registry.npmjs.org
npm.cmd run build
cd ..
.\run.ps1
```

前端使用 `yarn.lock` 固定依赖。当前 Windows 本地构建显式声明了 Rollup 与 esbuild 的 x64 二进制包；切换部署操作系统时应恢复对应平台的可选依赖安装。

## 实时服务配置

复制 `.env.example` 为 `.env`，按实际账号填写：

- `AGENTDESK_OPENAI_API_KEY`
- `AGENTDESK_OPENAI_BASE_URL`
- `AGENTDESK_OPENAI_MODEL`
- `AGENTDESK_EMBEDDING_PROVIDER`（生产建议 `fastembed`，回归夹具才使用 `local_hash`）
- `AGENTDESK_EMBEDDING_MODEL`
- `AGENTDESK_EMBEDDING_DIMENSIONS`
- `AGENTDESK_EMBEDDING_BATCH_SIZE`
- `AGENTDESK_EMBEDDING_REBUILD_ON_MISMATCH`
- `AGENTDESK_OPENAI_EMBEDDING_MODEL`（仅 `provider=openai`）
- `AGENTDESK_RAG_VECTOR_CANDIDATE_LIMIT`
- `AGENTDESK_RAG_HNSW_EF_SEARCH`
- `AGENTDESK_RAG_MIN_SIMILARITY`
- `AGENTDESK_RAG_MIN_RETRIEVAL_SCORE`
- `AGENTDESK_RAG_MIN_LEXICAL_SCORE`
- `AGENTDESK_RAG_SEMANTIC_OVERRIDE_SCORE`
- `AGENTDESK_SECRETS_ENCRYPTION_KEY`（Fernet 密钥；生产必须由密钥管理系统注入）
- `AGENTDESK_WEB_SEARCH_PROVIDER`（`disabled` 或 `brave`）
- `AGENTDESK_WEB_SEARCH_API_KEY`
- `AGENTDESK_WEB_SEARCH_TIMEOUT_SECONDS`
- `AGENTDESK_WEB_SEARCH_MAX_RESULTS`
- `AGENTDESK_AI_CONTEXT_INACTIVITY_MINUTES`
- `AGENTDESK_AI_CONTEXT_MAX_MESSAGES`
- `AGENTDESK_AI_CONTEXT_MAX_CHARACTERS`
- `AGENTDESK_KNOWLEDGE_REDIS_URL`
- `AGENTDESK_KNOWLEDGE_SYNC_TIMEZONE`
- `AGENTDESK_KNOWLEDGE_SYNC_HOUR`
- `AGENTDESK_KNOWLEDGE_SYNC_MINUTE`
- `AGENTDESK_WHATSAPP_PROVIDER`
- `AGENTDESK_EVOLUTION_API_URL`
- `AGENTDESK_EVOLUTION_INSTANCE_NAME`
- `AGENTDESK_META_VERIFY_TOKEN`
- `AGENTDESK_META_APP_SECRET`
- `AGENTDESK_META_ACCESS_TOKEN`
- `AGENTDESK_META_PHONE_NUMBER_ID`
- `AGENTDESK_META_BUSINESS_ACCOUNT_ID`
- `AGENTDESK_META_GRAPH_VERSION`

`OPENAI_API_KEY` 与 `OPENAI_MODEL` 存在时启用外部聊天模型。生产切换到 PostgreSQL 前，先启动 `compose.pgvector.yml` 中的独立数据库，再执行 `scripts/migrate_pgvector.py`；该脚本会安装 `vector` 扩展、创建固定 `vector(384)` 列、复制结构化产品/报价表，并用同一多语种模型重建全部知识块，完成校验后才可切换 `AGENTDESK_DATABASE_URL`。不要把 RelayDesk 指向 Evolution API 自带的 PostgreSQL。`RAG_MIN_SIMILARITY` 是检索相似度下限，低于该值不会拿文档强行回答，而是转人工；正式知识库上线前应使用评测集重新校准。Sub2API 使用自定义 `OPENAI_BASE_URL`，例如以 `/v1` 结尾的兼容地址。公网业务必须使用 HTTPS，不能用明文 HTTP 传输 API Key 和客户消息。

RelayDesk 启动时会检查 PostgreSQL 的列类型、模型集合和向量维度；发现旧 `local-hash-v1` 或错误维度会拒绝提供服务，避免迁移未完成时混用索引。只读检查可运行 `scripts/check_knowledge_index.py <database-url>`。

### 向量迁移与对比

```powershell
# 1) 启动隔离的 pgvector（不会修改 Evolution 数据库）
docker compose -f compose.pgvector.yml up -d

# 2) 先保存迁移前基线（用于质量/延迟对比）
Copy-Item data/agentdesk.db data/baseline-before-migration.db

# 3) 复制现有结构化数据并重建全部多语种向量
$env:AGENTDESK_EMBEDDING_PROVIDER = "fastembed"
.\.venv\Scripts\python scripts\migrate_pgvector.py `
  --database-url postgresql+psycopg://agentdesk:agentdesk-local-only@127.0.0.1:55432/agentdesk `
  --source-database-url sqlite:///./data/agentdesk.db

# 4) 用现有评测集比较基线与候选库（不自动切生产流量）
#    请把迁移前备份作为 baseline；下面文件名只是示例。
.\.venv\Scripts\python scripts\benchmark_retrieval.py `
  --baseline-url sqlite:///./data/baseline-before-migration.db `
  --candidate-url postgresql+psycopg://agentdesk:agentdesk-local-only@127.0.0.1:55432/agentdesk `
  --output data/evaluations/pgvector-comparison.json
```

报告同时给出检索和端到端响应的 p50/p95/p99；默认 p95 回归阈值为 10%，超出时会标记 `hold_cutover_and_tune_latency_before_production`。只有当 Top-1/Top-3、国家召回、商品召回没有回归且 p95 延迟符合目标时，才由运维人员手动更新生产 `AGENTDESK_DATABASE_URL`。`scripts/rebuild_embeddings.py` 可用于已有单库的全量重建；它会验证最终模型集合和向量维度，拒绝静默混入 `local-hash-v1`。

Evolution 的敏感配置由 `scripts/setup-evolution.ps1` 自动生成。需要切回 Meta 时，将 `AGENTDESK_WHATSAPP_PROVIDER` 设置为 `meta`，再填写 Meta 配置项。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run build
```

客服工作台 V2 的接口、数据流和 LangChain/LangGraph 分工见
[docs/INBOX_V2.md](docs/INBOX_V2.md)。

网址学习、网页分类、审核发布和数据库分层见
[docs/KNOWLEDGE_CRAWLING.md](docs/KNOWLEDGE_CRAWLING.md)。

商品价格采集、变价历史、LangChain 价格 Tool 与 WhatsApp 分段规则见
[docs/PRODUCT_PRICING.md](docs/PRODUCT_PRICING.md)。

## 设计说明

参见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。正式生产前还需要 PostgreSQL/pgvector、数据库迁移、受进程管理器监管的 Worker、对象存储、密钥管理、HTTPS、备份恢复和合规审查。

性能调优、近生产规模压测与切换门槛见 [docs/PERFORMANCE_TUNING.md](docs/PERFORMANCE_TUNING.md)。
