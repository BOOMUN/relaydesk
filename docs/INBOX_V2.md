# 客服工作台 V2 说明

这一版把“能查看消息”升级为适合多人协作的共享收件箱。联系人、团队、会话、消息和接管状态均由 AgentDesk 独立管理；系统没有 WATI Adapter 或 WATI 运行依赖。

## 功能闭环

| 客服操作 | API | 数据结果 | 是否调用 AI |
| --- | --- | --- | --- |
| 查看我的、未分配、未读队列 | `GET /api/conversations` | 按租户、负责人、未读数等条件查询 | 否 |
| 接手或转派会话 | `PATCH /api/conversations/{id}` | 更新团队、负责人并写入审计日志 | 否 |
| 回复客户 | `POST /api/conversations/{id}/messages` | 提交发送 Action，经审计后保存消息并调用 ChannelProvider | 否 |
| 添加内部备注 | 同上，`internal=true` | 保存 `direction=internal` 的消息，不调用 WhatsApp | 否 |
| 使用快捷回复 | `GET /api/quick-replies` | 客服选择固定话术后再发送 | 否 |
| 编辑标签和字段 | `PATCH /api/contacts/{id}` | 更新客户结构化资料 | 否 |
| 查看活动记录 | `GET /api/conversations/{id}/activity` | 查询会话相关审计日志 | 否 |
| 临时翻译英文消息 | `POST /api/conversations/{id}/messages/{message_id}/translate` | 只返回繁体中文翻译，不修改消息或数据库 | 是 |
| 接收客户消息并自动回答 | WhatsApp Webhook | 保存消息，然后运行 Agent 工作流 | 是 |

这里最重要的设计原则是：客服工作台的筛选、转派、备注和权限属于确定性的业务状态，不应交给 LangGraph。只有“理解客户消息并决定查询知识、调用工具或转人工”才进入 LangGraph。

## LangChain 与 LangGraph 如何配合

LangChain 是能力组件层：

- `ChatOpenAI` 连接 OpenAI 兼容模型网关；
- Prompt 规定分类和回答规则；
- Structured Output 把模型分类结果约束为固定字段；
- Embedding 与 VectorStore 用于知识检索；
- Tool 封装订单系统等受控业务查询。

LangGraph 是流程编排层：

```text
客户消息
   |
classify
   |-- greeting --> fixed greeting -> answer
   |-- knowledge -> retrieve -> answer
   |-- order -----> order tool -> answer
   `-- handoff ----------------> 固定转接通知
                                  |
                          暂停 AI + 标记待处理
```

可以把 LangChain 理解为“工具箱”，把 LangGraph 理解为“带状态和分支的流程图”。数据库中的会话状态仍是最终事实来源，不能只存在图的内存状态里。

自动化采用“失败时收紧权限”的策略：本地分类规则无法可靠识别意图时直接进入 `handoff`；知识检索结果低于 `AGENTDESK_RAG_MIN_SIMILARITY` 时也不生成答案。相似度阈值不是通用常数，替换正式 Embedding 和公司知识库后，需要用真实问题评测集重新校准。

客户明确提出“转人工、找客服、真人处理”等相似意思时，先由确定性规则锁定 `handoff`，避免外部模型误分类。通知固定为繁体“這邊給你轉接人工客服，請稍後”，之后会话进入高优先级 `pending` 队列且 AI 自动回复关闭。客服为 24 小时服务，所以该通知不包含营业时间。

## 30 分钟关联会话与自动解决

客户发出第一条新消息时，系统创建数据库持久化的上下文 session；之后每条消息把结束时间向后延长 30 分钟。LangGraph 只读取当前 session 的历史，自动结束或转人工后新消息会创建新 session，不会把上一段已结束问题带入新对话。

静默到期由独立调度器处理。调度器先发送繁体通知“AI 智能結束當前對話”，发送被 WhatsApp Provider 接受后才设置 `status=solved`。发送失败不会误标已解决，而是记录失败尝试并延迟重试。为了避免上线瞬间联系历史客户，只有功能启用后带 session ID 的新消息才参与自动结束。

## 内部备注为什么单独建模

内部备注仍放在 `messages` 表中，便于按时间顺序显示，但使用三个标识隔离：

- `direction=internal`；
- `delivery_status=internal`；
- `metadata_json.internal=true`。

发送服务检测到 `internal=true` 后不会调用 Evolution API 或 Meta API。会话列表的最后一条客户可见消息也会跳过内部备注，防止客服把团队文字误认为已经回复给客户。

## 实时更新

浏览器通过 `GET /api/events` 建立 Server-Sent Events 连接。服务端检查当前租户的会话、消息和联系人最新更新时间，发生变化时发送 `inbox.updated` 事件，前端再读取最新数据。

SSE 比 WebSocket 更适合当前阶段，因为数据只需要从服务器单向通知浏览器，协议简单，也容易经过反向代理。当前实现是本地开发版；真实并发增加后应改为 PostgreSQL/Redis 事件通知，避免每个浏览器连接轮询数据库。

## 五个客服席位与账号策略

`AGENTDESK_MAX_AGENT_SEATS=5` 是当前工作区容量配置。网页不提供公开注册，账号由管理员统一发放。当前版本只展示席位使用量；下一阶段再增加管理员创建、停用、重置密码和团队成员管理，并在后端强制检查席位上限。

## 繁体界面与临时翻译

客服工作台固定使用 `zh-TW` 繁体中文，不再提供简体界面切换。客户资料中的 `language` 仍独立保存，它描述客户内容而不是客服界面，因此不会因移除界面切换而丢失。

AI 自动回复同样固定使用 `zh-TW`。客户资料的语言标签仍可用于理解和检索，但不会切换 AI 到简体输出；所有 AI 消息在调用 WhatsApp Provider 前还有一次统一的繁体字符转换。人工客服手动输入的内容保持原文，系统不会擅自改写。

客服在英文聊天框按右键选择“翻译成繁体中文”时，服务器通过当前 OpenAI 兼容模型执行严格的英译繁任务，再用 OpenCC 统一字符。翻译结果仅保存在当前 Vue 页面内，显示在原消息下方的蓝色框；切换会话或重新加载页面即消失。该 API 按租户和消息 ID 校验权限，但不更新 `messages.metadata_json`，也不新增消息，所以不会把翻译误当成真实 WhatsApp 对话或长期客户数据。

## 上线前仍需完成

当前 SQLite、进程内 SSE 和同步 Agent 调用适合学习与本地验证，不适合直接承载公司流量。上线前至少需要：

1. PostgreSQL、Alembic 数据库迁移和 pgvector；
2. Webhook 入队、后台任务、重试与幂等状态机；
3. 管理员账号发放、角色权限和五席位后端强制限制；
4. 工作时间、SLA、自动分配和冲突控制；
5. HTTPS、密钥管理、备份恢复、审计保留与敏感数据脱敏；
6. 用已关联的真实号码完成收发、掉线恢复和消息状态端到端验证。
