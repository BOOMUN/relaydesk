# AgentDesk 检索性能调优与切换门槛

本地 PostgreSQL/pgvector 候选库已经使用以下默认值：

- 连接池：`pool_size=10`、`max_overflow=10`、`pool_timeout=30s`、`pool_recycle=1800s`、`pre_ping=true`、LIFO；连接建立时设置 `hnsw.ef_search=64` 和 `hnsw.iterative_scan=strict_order`。
- 向量检索：候选数 `RAG_VECTOR_CANDIDATE_LIMIT=64`，保留 BM25、国家/租借类型硬过滤、结构化产品加权和最终重排。
- Embedding：`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，固定 384 维，批量大小 128，4 个推理线程；应用 lifespan 会在接收请求前执行一次文档/查询预热。
- 索引：PostgreSQL `vector(384)` + HNSW cosine index，以及 `(tenant_id, embedding_model, document_id)` 过滤索引。价格、库存、国家等字段仍在结构化产品表中。

## 近生产规模压测

`scripts/scale_knowledge_index.py` 的 `noise` 模式保留真实标注文档，并加入确定性无关向量；国家标签轮换到噪声标题中，确保国家硬过滤也经过大候选集。重复数据模式仍可用 `--mode duplicate`，但不应用于质量门槛。
噪声模式会识别已有 `benchmark_noise` 行，重复执行不会继续放大数据；测试结束可用精确的 `--cleanup-marker` 删除夹具。

```powershell
# 生成 50 倍规模（原有 115 块 -> 5,750 块）
.\.venv\Scripts\python.exe scripts\scale_knowledge_index.py `
  --database-url postgresql+psycopg://agentdesk:***@127.0.0.1:55432/agentdesk_scale_candidate `
  --mode noise --factor 50 --marker scale-prod50-noise-candidate

# 同一套评测：质量、串行 p50/p95/p99、端到端响应，以及可选连接池压力
.\.venv\Scripts\python.exe scripts\benchmark_retrieval.py `
  --baseline-url postgresql+psycopg://agentdesk:***@127.0.0.1:55432/agentdesk_scale_baseline `
  --candidate-url postgresql+psycopg://agentdesk:***@127.0.0.1:55432/agentdesk_scale_candidate `
  --baseline-embedding-provider local_hash --candidate-embedding-provider fastembed `
  --repeats 3 --pool-concurrency 24 --pool-requests 96 `
  --output data/evaluations/pgvector-scale50-noise-pool.json
```

切换门槛是：意图、国家召回、商品召回、Top-1/Top-3 均不得下降；串行检索和端到端响应 p95 相对基线回归不超过 10%；如果启用连接池压力测试，候选必须无错误且 p95 回归同样不超过 10%。

## 数据库切换

先运行 `scripts/check_knowledge_index.py <database-url> --json`，确认 `VECTOR(384)`、单一多语种模型和维度均通过，再把 `AGENTDESK_DATABASE_URL` 指向候选库。当前本地切换目标是独立的 `agentdesk` 数据库，绝不能指向 Evolution API 自带的 PostgreSQL。若要回滚，只需将该变量改回 SQLite URL 并重启 API；服务器切换须在单独维护窗口执行。
