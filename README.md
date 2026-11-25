# 本地生活智能助手 · hmdp-intelligent-assistant

一个「业务后端 + AI 服务」双栈的本地生活平台：Java 管业务，Python 管智能。

用户可以用自然语言完成 **找店 → 查券 → 抢券** 的完整链路，写操作带人工确认。

![Java](https://img.shields.io/badge/Java-8-orange)
![SpringBoot](https://img.shields.io/badge/SpringBoot-2.3.12-brightgreen)
![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple)

---

## 架构

```
                    ┌──────────────────────────────────┐
                    │        ai-service (Python)       │
   用户 ─── 自然语言 ─►│  LangGraph 状态机                 │
                    │  ┌────────────────────────────┐  │
                    │  │ route → agent ⇄ tools      │  │
                    │  │      ↘ rag                 │  │
                    │  │      ↘ confirm (interrupt) │  │
                    │  └────────────────────────────┘  │
                    └───────┬─────────────────┬────────┘
                            │ HTTP            │ 向量检索
                            ▼                 ▼
              ┌──────────────────────┐   ┌──────────────┐
              │  hmdp (Java)         │   │  ChromaDB    │
              │  Spring Boot + Redis │   │  知识库       │
              │  商户/优惠券/秒杀     │   └──────────────┘
              └───────┬──────────────┘
                      ▼
              ┌──────────────┐        ┌──────────────┐
              │    MySQL     │        │    Redis     │
              └──────────────┘        └──────────────┘
```

**边界设计**：AI 服务不直接访问数据库，所有业务规则（库存扣减、一人一单、分布式锁）都留在 Java 侧，Python 只负责「理解意图 + 编排决策」。

---

## 两个模块

| 模块 | 路径 | 技术栈 | 职责 |
|---|---|---|---|
| **业务后端** | `src/` `pom.xml` | Java 8 / Spring Boot 2.3 / MyBatis-Plus / MySQL / Redis / Redisson | 商户、优惠券、秒杀下单、探店笔记、缓存治理 |
| **AI 服务** | `ai-service/` | Python 3.13 / FastAPI / LangChain 1.5 / LangGraph 1.2 / ChromaDB | Agent 编排、RAG 知识问答、工具调用 |

详细说明见 👉 [ai-service/README.md](ai-service/README.md)

---

## 技术亮点

### 1. 不可逆操作走架构级人工确认

抢券是写操作，**`seckill_voucher` 工具没有注册进模型的工具箱**。模型只能调用三个只读工具，写操作必须走显式路径：

```
confirm 节点 interrupt()        ← 整张图挂起，状态落盘 checkpointer
        ↓  用户回复「确认」
do_seckill 节点                 ← 从断点恢复，此时才真正调用写接口
```

这不是「Prompt 里写一句要确认」那种软约束——**模型在权限上就拿不到写操作**。

### 2. 两阶段检索

向量召回 Top-20 → rerank 精排 Top-5。RAG 回答强制带 `[来源]` 引用，资料不足时明确拒答。

### 3. 压测驱动的缓存修复

压测商户详情接口时发现缓存**从未命中**（`EXISTS cache:shop:1` 恒为 0），定位到互斥锁的 key 与缓存 key 复用了同一个 `cache:shop:{id}`，`finally` 里的 `unlock()` 把刚写入的缓存删掉了。

| 场景 | 并发 | QPS | P50 | P99 |
|---|---|---|---|---|
| 修复前 | 100 | 121 | 762 ms | 3,243 ms |
| 修复后 | 100 | **5,759** | **14 ms** | **37 ms** |

另外并发送 100 次请求时，修复前有 **17% 返回业务失败**（锁标记 `"1"` 被当成缓存 JSON 反序列化），但 HTTP 状态码全是 200 —— **只看 HTTP 200 的压测没有意义**。

> 压测环境：单机 i5-1240P，Redis 与应用同机（因此缓存命中数据偏乐观），MySQL 在虚拟机。详细数据与方法见 [jmeter/](jmeter/)。

---

## 快速开始

### 一键脚本（推荐）

```powershell
.\scripts\start-all.ps1     # 按依赖顺序启动：MySQL → Redis → hmdp(8081) → ai-service(8000)
.\scripts\status.ps1        # 查看各服务端口状态
.\scripts\stop-all.ps1      # 停止（虚拟机不会被关）
```

脚本行为：

- 按依赖顺序启动，每一步**等端口真正就绪**才进入下一步（MySQL 150s、hmdp 180s、其余更短）
- 如果虚拟机没开，自动用 `vmrun` headless 启动并等待 MySQL 端口
- 每个服务后台运行，日志写到 `logs/<service>.out.log`，PID 记录在 `logs/<service>.pid`
- 已启动的服务自动跳过，可重复执行
- `stop` 会连 mvn fork 出的 java 子进程一起清理；PID 文件丢失时按端口兜底

> 脚本里的路径（Redis 目录、JDK 8、Maven、虚拟机 vmx）都集中在 `scripts/dev.ps1` 顶部的
> `$Config`，换电脑只改那一段即可。
>
> 如果虚拟机已经在别处开好了，可加 `-SkipVm` 跳过检测。

### 手动启动

#### 1. 启动依赖

```bash
# MySQL 8 + Redis 5（Redis 需要 requirepass 123456，与 application.yaml 对齐）
```

### 2. 启动业务后端（8081）

```bash
mvn spring-boot:run
# 或 IDEA 直接运行 HmDianPingApplication
```

### 3. 启动 AI 服务（8000）

```bash
cd ai-service
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

copy .env.example .env            # 然后填入 AI_API_KEY
python -m uvicorn app.main:app --reload --port 8000
```

### 4. 自检（不需要 key，不消耗 token）

```bash
cd ai-service
python scripts/verify_all.py       # 预期 7/7 通过
```

### 5. 试一下

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"帮我找人均50以内的火锅店","thread_id":"u1"}'
```

---

## 项目结构

```
.
├── src/                        Java 业务代码
│   └── main/java/com/hmdp/
│       ├── controller/         REST 接口
│       ├── service/impl/       业务实现（缓存、秒杀、Feed 流）
│       └── utils/              拦截器、Redis 锁、ID 生成
├── ai-service/                 Python AI 服务
│   ├── app/
│   │   ├── clients/hmdp.py     调 Java 后端的 HTTP 客户端
│   │   ├── tools/              LangChain @tool 封装
│   │   ├── rag/                切分 / 检索 / rerank / 生成
│   │   └── graph/              LangGraph 状态机
│   └── scripts/verify_all.py   一键自检
├── jmeter/                     压测脚本与报告
└── pom.xml
```

---

## 已知限制

- **Checkpointer 用的是 `InMemorySaver`**：进程重启会话即丢失，生产应换 `RedisSaver`（图的逻辑无需改动）
- **Rerank 是「向量分 + 关键词重合」的轻量实现**：生产应接专用模型（bge-reranker / gte-rerank）
- **知识库只有示例文档**：需要灌真实商户资料才能体现 RAG 效果
- **压测为单机环境**：绝对值不具参考性，组间比值有效

---

## 开发者备忘

- `ai-service/.env` 已在 `.gitignore` 中，**请勿提交 API Key**
- 依赖版本：`langchain 1.5.2` / `langgraph 1.2.24` / `chromadb 1.5.9`
  —— 注意 LangChain 1.x 相对 0.x 有破坏性变更（`AgentExecutor` 已移除，改用 `create_agent`），网上旧教程大量失效
