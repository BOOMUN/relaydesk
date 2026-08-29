# AgentDesk 架构说明

## 为什么不是一个简单机器人

WhatsApp 客服平台有两套不同的状态：

1. 业务状态记录会话是否待处理、等待客户、已解决、由谁负责，以及 24 小时服务窗口是否有效。数据库是这套状态的最终事实来源。
2. Agent 状态记录本次消息应该查询知识库、调用订单工具还是转交人工。LangGraph 只编排这套推理过程。

如果把团队分配、权限和会话状态全部放进 LangGraph，重启、并发和人工操作会让状态难以一致。因此当前设计让普通应用服务管理业务状态，让 LangGraph 管理 AI 决策。

## 请求路径

```text
WhatsApp Web / Meta Webhook
        |
        v
ChannelProvider 验签/解析 -> 事件唯一键去重 -> 联系人/会话持久化
                              |
                              v
                  LangGraph agent node
                           |
              检索/回答或结构化 Action proposal
                           |
              可信编排层补入租户/会话上下文
                           |
       Schema/权限/风险/确认/幂等/超时与重试检查
                           |
                    Action handler
                    /            \
          AgentDesk 数据库      ChannelProvider
                                  |
                          Meta / Evolution / Demo
                           |
                 执行尝试、结果与失败审计
```

## WhatsApp Provider 边界

客服业务层通过 Action 调用统一的 `ChannelProvider`，不感知具体通道。Provider 的统一消息格式覆盖文字、Meta 已批准模板、回复按钮和列表；`demo` 生成本地消息 ID，`evolution` 调用独立 Evolution API 容器，`meta` 调用官方 Graph API。这样 WhatsApp Web 协议升级或未来迁移到官方 API 时，不需要修改 LangGraph、会话模型和客服界面。

Provider 接口统一负责发送、Webhook 验签/解析、连接状态和可选的发送状态对账、模板同步能力。Webhook 必须通过 `phone_number_id` 或 Evolution `instance` 精确找到通道账号，验签成功后才持久化；Provider 事件 ID 与租户形成唯一键，重复投递不会生成第二条消息。每条消息同时保留逻辑消息 ID 和 WhatsApp Message ID。

Evolution API 使用 Baileys 管理关联设备会话。AgentDesk 通过 REST 创建实例、读取连接状态和发送消息；Evolution 通过带独立共享密钥的 Webhook 返回消息。容器 API 仅绑定本机回环地址，浏览器和局域网设备不能直接取得 Evolution API Key。

Provider 的发送回执可能早于外发消息事务提交。`message_delivery_receipts` 会先持久化最新回执，再与消息合并，避免 `DELIVERY_ACK` 或 `READ` 因并发时序丢失；状态合并不会把 `read` 降级回 `sent`。Evolution v2 的 `MessageUpdate.messageId` 是其内部数据库 ID，AgentDesk 必须使用 `keyId` 关联 WhatsApp 消息 ID，同时兼容 Baileys 的数字枚举状态 `0..5`。

每一次实际发送都会写入 `message_delivery_attempts`。重试会产生新的 Provider ID，但较早尝试迟到的送达/已读回执仍能更新同一条逻辑消息。系统只允许重试明确处于 `failed` 的消息；`pending` 或 `sent` 但缺少回执并不等于未送达，因此只能先主动对账，不能自动重发，以避免客户收到重复内容。

## Actions 执行边界

模型不能直接调用 WhatsApp API、数据库写操作或任意第三方 REST API。LangGraph 中的转人工等业务工具只返回 Action proposal；可信编排层补入不可由模型伪造的 `tenant_id`、`conversation_id` 和调用者身份，再交给统一执行器。

每个 Action 定义名称与用途、Pydantic 输入 Schema、权限范围、允许调用者/角色、风险等级、超时、最大尝试次数、幂等键策略以及是否需要人工确认。执行器先保存请求，再校验权限和确认状态；每次尝试单独记录开始、结果、Provider 错误码、是否可重试和失败原因。相同租户、Action 名称与幂等键只执行一次。高风险的模型请求进入 `pending_confirmation`，管理员批准后才执行；拒绝也会保留审计记录。

首批 Action 覆盖转人工及恢复 AI、团队/客服分配、联系人资料、标签、自定义字段、会话状态/优先级、文字发送、Meta 已批准模板发送、按钮/列表发送、Meta 模板同步、身份核验、敏感订单请求及管理员批准的 REST 调用。AI 回复、人工发送、失败重试和会话自动结束都复用这些 handlers，REST API 不另写旁路逻辑。

## LangChain 与 LangGraph 的分工

LangChain 提供模型接口、Prompt、结构化输出、Embedding、VectorStore 和 Tool。LangGraph 把这些能力组织成状态图，并以 `conversation_id` 作为线程标识。Agent 将价格、知识检索、订单、VIP 与转人工能力声明为带输入 Schema 的 LangChain Tool；模型通过 `tool_calls` 选择能力。检索类结果进入回答证据边界，业务写操作只产生 Action proposal，并由图外的可信执行器处理。无模型或模型调用失败时使用相同 Schema 的确定性后备选择，不另走一套业务实现。

当前使用 LangGraph 官方 Agentic RAG 的 query-rewrite 图模式：`retrieve -> rewrite_query -> retrieve`。LangGraph 负责节点、状态和循环控制，`rewrite_query` 使用模型的 Structured Output 返回独立检索问题；这是官方推荐的可控组合方式，而不是依赖一个不可审计的全局字符串拼接器。

确定性意图规则仍用于边界和回归测试；生产知识检索使用 FastEmbed 的多语种 `paraphrase-multilingual-MiniLM-L12-v2`（384 维）。PostgreSQL 通过 pgvector HNSW 取候选，随后保留 BM25、目的地/租借类型硬过滤和最终重排。`LocalHashEmbeddings` 只允许显式用于 SQLite 回归夹具；Embedding provider 出错时不会静默降级，避免混入 `local-hash-v1`。

分类和检索都采用证据边界：明确人工/高风险请求直接走 `handoff`；其余客服范围内的问题先进入检索。首次检索没有有效证据时，LangGraph 按官方 Agentic RAG 模式调用一次 `rewrite_query` 节点，再重试检索；重写后仍没有可验证资料，才走 `handoff`。向量相似度低于配置阈值时视为没有证据，避免从最接近但无关的文档中拼出答案。

明确的人工请求不依赖模型临场判断。确定性规则覆盖“转人工、找客服、联系客服、真人处理”以及常见英文同义句，并优先于模型分类。进入 `handoff` 后系统发送固定繁体转接通知、关闭 `ai_enabled`、提高优先级并把会话写成 `pending`；后续客户补充消息不会把这个人工待处理事项自动改回 `open`。当前客服为 24 小时服务，因此转接通知不拼接工作时间。

AI 外发语言由客户当前消息自动识别为 `zh-CN`、`zh-TW` 或 `en`，不依赖联系人记录中的手动语言。LangGraph 各回答节点按本轮语言生成内容，发送边界再次检查中英文混答，并用 OpenCC 统一所需简繁字形；无法可靠清理时改为真实转人工。非转人工 AI 消息会套用对应语言的客户可见标题、分隔线和人工接管提示；网址、币种、型号等非中文标识保持不变。人工客服手动发送的消息不受这条 AI 规则改写。

## P2 业务自动化边界

订单资料和线索资格使用独立 LangGraph 状态机，但权威状态不只存在内存 checkpoint。`automation_form_sessions` 保存当前步骤、答案、表单定义快照、超时时间、评分和等级；`automation_form_events` 追加保存启动、暂停、恢复、修改、完成、超时和转人工事件。客户可随时暂停、恢复、修改已填答案或要求人工，调度器会把超时会话切换为真实人工接管。

线索问题、选项分数、等级、标签及分配规则属于已发布 Agent 版本。完成评分后只通过统一 Actions 更新联系人字段/标签、会话优先级和团队/客服分配，LangGraph 不直接写这些业务表。

敏感订单流程先创建待确认 Action。`identity_verifications` 只保存证据 SHA-256 摘要和限时审计记录；人工批准后写入 `sensitive_operation_requests`，状态为 `approved_for_manual_execution`，不会自动调用退款、取消或资料修改 API。REST Action 同样使用 Action 执行器，只允许管理员批准的 HTTPS 源站、路径模式和 HTTP 方法；凭据使用 Fernet 加密。调用前后同时检查 DNS 和实际连接地址，并关闭代理继承及重定向，阻止私网、链路本地、路径穿越、DNS 重绑定和任意跳转。

网络搜索是检索失败后的最后证据来源，只有已发布 Agent 版本明确启用时才调用固定 Brave Search Provider。结构化商品意图不会绕过商品库转去网络搜索，结果还会按管理员允许域名过滤；没有可引用结果时继续追问或转人工，所有采用的搜索结果必须随消息保存并显示 URL 来源。

## 数据边界

每个核心业务表都有 `tenant_id`。即使当前只有一个公司，这也防止后续 SaaS 化时重新设计全部数据关系。所有面向客服的查询必须同时按当前用户的 `tenant_id` 过滤。

联系人、标签、自定义字段、团队、客服、分配关系、会话、消息、内部备注、AI/人工接管状态、Agent 配置、知识库版本及 Action 执行审计都以 AgentDesk 数据库为最终事实来源。系统不建设 WATI Adapter，也没有 WATI 运行依赖。

结构化业务数据不进入向量库。FAQ、政策和产品文档使用 RAG；商品价格、订单、库存、退款和客户账户通过受控 Tool/API 查询。价格问题由确定性规则优先路由到 `pricing`，即使外部模型误分类也不会落入自由文本 RAG。高风险写操作不能直接复用当前演示订单 Tool，必须增加身份验证、授权、幂等键和审批。

网站知识不是直接“训练 GPT”。系统先创建 `knowledge_sources`，Redis 调度器与独立 Worker 采集同域公开 HTML/PDF，再把每个页面保存为 `knowledge_documents + knowledge_web_pages`，同时将分块和向量保存在 `knowledge_chunks`。新页面默认是草稿；管理员发布后 `is_active=true`，才会进入 Agent 的 RAG 检索。每天北京时间 03:10 以内容哈希做增量比对；已发布页面的新内容进入 `knowledge_page_revisions`，发布前旧版本仍可被 Agent 检索。该审核门可以防止错误网页、导航噪声或恶意提示注入直接影响真实客服回复。

商品价格使用另一条数据链：`product_price_sources -> products -> product_price_offers -> product_price_history`。价格同步会自动应用最新结构化字段，并为新建、变价和下架写不可变历史；抓取失败时保留上一版，连续两次权威扫描缺失才停用商品。商品价格 Worker 使用独立 Redis 队列，避免大规模知识库采集延迟报价同步。后台和 AI 都读取同一组关系表，因此客服看到的价格与 WhatsApp 发出的价格一致。

多轮追问不能只依赖进程内 LangGraph checkpoint。价格 Tool 需要客户补充目的地或商品时，Agent 会把 `awaiting_input`、原始查询和本轮回复语言写入 AI 消息的 `metadata_json`。下一条客户消息先读取这份持久状态：若只是“韩国”等待补充短答，就和原查询组合并固定回到 `pricing`；若客户明确要求人工、订单或其他服务，则新意图优先。分类模型同时读取带角色的最近对话，但精确价格续问不依赖模型猜测，因此 API 重启后仍能接续。

现在每个新客户消息还会写入持久的 `ai_context_session_id` 和 `ai_context_close_due_at`。同一活动会话中的客户与 AI 消息共享 session ID，模型最多读取最近 40 条、12,000 字符的当前会话记录；分类阶段的 Structured Output 和检索失败后的 LangGraph `rewrite_query` 节点都会把含代词或省略信息的短句改写为独立查询，且重写最多重试一次。价格路线另行保存累计的结构化查询，所以“韩国”之后再问“一個人去七天要多少錢”仍能保留目的地，而不是把短句当成新问题。

独立 `conversation-scheduler` 每 15 秒检查一次新机制跟踪的会话。最后一条客户消息静默满 30 分钟后，系统先调用 WhatsApp Provider 发送「AI 智能結束當前對話」；只有 Provider 接受发送才把会话改为 `solved` 并写审计日志。失败时会话保持原状态，5 分钟后复用同一逻辑消息重试。转人工、人工暂停 AI、手动解决或封锁会立即写入上下文边界。旧消息没有 session ID，启用功能时不会被回溯批量发送通知。

## 生产演进

本地首版是模块化单体，便于学习和快速验证。进入真实流量前按顺序增加：

1. 通过 `scripts/migrate_pgvector.py` 将 SQLite 结构化数据迁移到 PostgreSQL；所有后续 Schema 变更使用 Alembic，并在发布前执行 `alembic upgrade head`。
2. 生产知识索引固定使用 pgvector `vector(384)` + HNSW；每次更换 Embedding 模型都必须全量重建并重新跑评测集。
3. Webhook 只入队，Agent 运行和 Provider API 发送交给工作进程。
4. LangGraph 内存 Checkpointer 替换为 PostgreSQL Checkpointer。
5. 接入对象存储处理图片、语音和 PDF，并在进入模型前扫描和脱敏。
6. 增加 SLA、工作时间、自动分配、广播和 WhatsApp Flows。
7. 部署 HTTPS、密钥管理、限流、监控、告警、备份与灾难恢复。
