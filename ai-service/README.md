# hmdp 智能助手 · AI Service（Python）

`hm-dianping`（Java 业务后端）旁边的 **AI 服务**：LangGraph Agent + RAG，
通过 HTTP 调用 Java 接口，**不直接碰数据库**。

```
┌──────────────────────┐   HTTP    ┌───────────────────────────┐
│  hmdp (Java 8081)    │◄──────────│  ai-service (Python 8000) │
│  商户 / 优惠券 / 秒杀 │           │  LangGraph Agent + RAG    │
└──────────────────────┘           └───────────────────────────┘
                                              │
                                        Redis (会话 / 验证码)
                                        Chroma (向量库)
```

---

## 一、目录结构

```
ai-service/
├── app/
│   ├── main.py               FastAPI 入口
│   ├── config.py             配置（pydantic-settings，读 .env）
│   ├── llm.py                模型工厂（ChatModel / Embeddings）
│   ├── clients/hmdp.py       调 Java 后端的 HTTP 客户端（含自动登录）
│   ├── tools/hmdp_tools.py   把接口包装成 LangChain @tool
│   ├── rag/
│   │   ├── indexer.py        文档 → 切分 → 向量化 → Chroma
│   │   ├── retriever.py      两阶段检索：向量粗排 Top-K → rerank 精排 Top-N
│   │   └── chain.py          RAG Prompt + 带引用生成
│   ├── graph/
│   │   ├── state.py          State 定义（TypedDict + reducer）
│   │   ├── nodes.py          各节点：route / agent / tools / rag / confirm
│   │   └── build.py          组装状态机并编译
│   └── api/routes.py         对外接口
├── data/docs/                知识库原文（.md/.txt/.pdf）
├── scripts/verify_all.py     一条命令自检（不消耗 token）
├── requirements.txt
└── .env.example
```

---

## 二、启动

```powershell
cd D:\develpo\hm-dianping\hm-dianping\ai-service

# 1) 配置 key（复制一份再改，.env 不要提交到 git）
Copy-Item .env.example .env
notepad .env

# 2) 启动服务
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 看接口文档。

**启动顺序**：`Redis → MySQL(VM) → hmdp:8081 → ai-service:8000`

---

## 三、先跑自检

不需要大模型 key，也不需要 Java 服务起来：

```powershell
.\.venv\Scripts\python.exe scripts\verify_all.py
```

预期输出 `7/7 通过`。它验证的是**图逻辑**，尤其是抢券的 interrupt 挂起/恢复/取消。
（用假后端，不会真的发 HTTP、不花 token。）

---

## 四、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（会报告 key 是否配好） |
| GET | `/api/tools` | 查看注册了哪些工具 |
| POST | `/api/chat` | 多轮对话（内部走 LangGraph） |
| POST | `/api/chat/resume` | **恢复被 interrupt 挂起的图** |
| POST | `/api/chat/stream` | 按节点粒度 SSE 流式推送 |
| POST | `/api/rag/index` | 重建知识库索引 |
| GET | `/api/rag/search?q=&top_k=20&top_n=5` | 调试检索效果 |

### 普通对话

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" `
  -d '{"message":"帮我找人均50以内的火锅店","thread_id":"u1"}'
```

### 抢券（会中断，等确认）

```powershell
# 第 1 步：触发抢券 → 返回 status=interrupt
curl.exe -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" `
  -d '{"message":"帮我抢7号券","thread_id":"u1","voucher_id":7}'

# 第 2 步：确认 → 图从断点继续，真正调用写接口
curl.exe -X POST http://127.0.0.1:8000/api/chat/resume -H "Content-Type: application/json" `
  -d '{"thread_id":"u1","answer":"确认"}'
```

### 知识库问答

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/rag/index"
curl.exe -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" `
  -d '{"message":"秒杀券一个人能买几张？","thread_id":"u2","intent":"rag"}'
```

---

## 五、四个核心设计（面试重点）

### 1. 权限分离：模型拿不到写操作工具

| 工具 | 给模型 | 说明 |
|---|---|---|
| `query_shop` / `get_shop_detail` / `list_vouchers` | ✅ | 只读查询，ReAct 循环里随便调 |
| `seckill_voucher` | ❌ | **不进模型工具箱**，只能走 `confirm` 节点 |

这样「模型自作主张抢券」在**架构上不可能发生**，而不是靠 Prompt 祈祷它别乱来。

### 2. `interrupt()` 人工确认

`confirm` 节点调用 `interrupt(payload)` → 整张图**暂停**，状态落进 checkpointer；
用户回复后 `Command(resume=...)` 从断点继续。

⚠️ **坑**：恢复时该节点会**从头重新执行**，所以 `interrupt()` 之前的代码必须幂等（不能有副作用）。

### 3. Checkpointer 断点续跑

默认 `InMemorySaver`，进程重启就丢。换生产方案只改一行：

```python
# pip install langgraph-checkpoint-redis
from langgraph.checkpoint.redis import RedisSaver
graph = build_assistant_graph(llm=..., checkpointer=RedisSaver.from_conn_string(REDIS_URL))
```

图的逻辑一行都不用改 —— 这就是「编排层和存储层解耦」。

### 4. 两阶段检索

向量召回 Top-20 → rerank 精排 Top-5。rerank 目前有两种实现：
- `keyword`（默认，零成本）：向量分 + 关键词重合度加权
- `llm`（更准但费 token）：用大模型逐条打分

生产环境应换专用 rerank 模型（bge-reranker / 通义 gte-rerank），接入点在 `retriever._keyword_rerank`。

---

## 六、为什么不用 LangChain 的 `AgentExecutor`

LangChain 1.x 里 `AgentExecutor` 已被移除，新的方式是 `langchain.agents.create_agent`（内部也是 LangGraph）。
本项目**手搭 StateGraph** 的理由：

1. 循环、分支、重试在图上看得见，出问题能定位；
2. 能在任何一步插 `interrupt`（`AgentExecutor` 做不到）；
3. 每个节点可以单独写单测。

---

## 七、常见问题

| 现象 | 原因 |
|---|---|
| `/api/health` 返回 `ok:false` | `.env` 里 `AI_API_KEY` 没配 |
| `query_shop` 返回「调用失败：调用超时」 | hmdp:8081 没启动 |
| 抢券返回「无法获取验证码」 | Redis 没起来，或 `HMDP_PHONE` 对应的用户不存在 |
| `/api/rag/index` 报没有文档 | `data/docs/` 下要放 `.md/.txt/.pdf` |
| Chroma 首次启动很慢 | 在下载 ONNX 模型，等一次就好 |
| 401 `invalid_api_key` | key 错了，或 `AI_BASE_URL` 和 key 的归属平台不一致 |

---

## 八、下一步

- [ ] checkpointer 换 `RedisSaver`，会话跨重启保留
- [ ] rerank 接专用模型，用评测集量化准确率提升
- [ ] 把 `chat/stream` 从「节点粒度」升级成「token 粒度」
- [ ] 补 pytest 单测与 GitHub Actions CI
