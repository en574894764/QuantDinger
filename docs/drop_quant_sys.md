# 移除 quant_sys 外部依赖 — 全量迁移计划

## 目标

将 `~/workspace/quant_sys/` 的全部能力收敛进 QuantDinger 内部，使 QuantDinger 成为量化系统的唯一运行时项目。

迁移后：
- **QuantDinger** = 数据管道 + 风控 + 策略 + 信号 + MCP Server + 调度（全部）
- **quant_sys** = 历史归档（代码保留但不再运行）
- **copilot** = MCP Server 继续通过 HTTP 桥接到 QuantDinger（或内嵌）

---

## 依赖全景

### 当前架构

```
quant_sys (外部项目)
  ├── Python 代码 (pipeline, data fetching, factor calc)
  ├── Data 文件 (Parquet, SQLite)      ← Docker volume mount
  ├── Config 文件 (YAML)               ← Docker volume mount
  └── PostgreSQL (investassist)        ← host.docker.internal:5432

copilot/mcp-servers/
  ├── quant-data      → HTTP → QuantDinger:5001
  ├── quant-bridge    → HTTP → QuantDinger:5001 + PYTHONPATH(dead) → quant_sys
  └── quant-research  → HTTP → QuantDinger:5001

QuantDinger
  ├── app/extensions/quant_sys/        (Flask 扩展，封装 quant_sys)
  │   ├── data/      → 依赖 /quant_sys_data (volume mount)
  │   ├── risk/      → 依赖 /quant_sys_data/system.db (volume mount)
  │   ├── strategy/  → 依赖 PG + /quant_sys_data
  │   ├── ideas/     → 依赖 SQLite
  │   ├── portfolio/ → 依赖 SQLite
  │   ├── dashboard/ → 依赖 PG
  │   └── execution/ → vnpy 执行引擎
  └── Docker volumes:
      ├── ~/workspace/quant_sys/data          → /quant_sys_data
      ├── ~/workspace/quant_sys/config        → /quant_sys_config
      └── ~/workspace/quant_sys/data/system.db → /data/system.db

Hermes cron (7 jobs)
  └── 全部 → curl → localhost:5001
```

### 依赖分类

| 类型 | 数量 | 说明 |
|------|------|------|
| 🔴 直接代码引用 | 1 | `quant-bridge/run.sh` PYTHONPATH（已确认死代码） |
| 🟡 HTTP 桥接 | 多个 | 3 个 MCP Server + web_ui + 7 cron，全走 `localhost:5001` |
| 🔴 数据挂载 | 3 | data/config/system.db Docker volumes |
| 🟡 PG 数据库 | 1 | `quantdinger-db` 容器（已在 docker-compose 内，不算外部） |

---

## Step 1：数据迁移

### 1.1 搬迁物理文件

| 来源 | 目标 | 
|------|------|
| `~/workspace/quant_sys/data/*.parquet` | `QuantDinger/backend_api_python/data/parquet/` |
| `~/workspace/quant_sys/data/system.db` | `QuantDinger/backend_api_python/data/system.db` |
| `~/workspace/quant_sys/config/*.yaml` | `QuantDinger/backend_api_python/config/quant_sys/` |

### 1.2 改代码路径

全局搜索替换：

| 旧路径 | 新路径 |
|--------|--------|
| `/quant_sys_data` | `QuantDinger 内部 data/ 目录` |
| `/quant_sys_config` | `QuantDinger 内部 config/quant_sys/` |
| `/data/system.db` | `QuantDinger 内部 data/system.db` |
| `QUANT_SYS_DATA_DIR` 默认值 | 更新 |
| `QUANT_SYS_SQLITE_PATH` 默认值 | 更新 |
| `QUANT_SYS_DATABASE_URL` | 保持不变（PG 已是 quantdinger-db） |

涉及文件清单：
- `data/pipeline_state.py` — `PIPELINE_STATE_PATH`
- `data/backfill.py` — `DEFAULT_DATA_DIR`, PG URL
- `data/cross_validate.py` — `DEFAULT_DATA_DIR`, PG URL
- `data/index_refresh.py` — 硬编码 `~/workspace/quant_sys/config`, PG URL
- `data/store/parquet.py` — `DEFAULT_DATA_DIR`
- `risk/signals.py` — `QUANT_SYS_SQLITE_PATH`
- `risk/audit.py` — `QUANT_SYS_SQLITE_PATH`
- `dashboard/api.py` — PG URL

### 1.3 改 Docker 挂载

`docker-compose.yml` 修改：

```yaml
# 旧
volumes:
  - ~/workspace/quant_sys/data:/quant_sys_data
  - ~/workspace/quant_sys/config:/quant_sys_config
  - ~/workspace/quant_sys/data/system.db:/data/system.db

# 新
volumes:
  - ./backend_api_python/data:/app/data          # 统一数据目录
  - ./backend_api_python/config:/app/config       # 统一配置目录
```

---

## Step 2：代码回灌

当前本地 `app/extensions/quant_sys/` 下大量文件为 0 字节（被意外清零），容器内有完整代码。一次性拉回。

### 2.1 从容器回灌所有 0 字节文件

```bash
# 找出所有 0 字节 .py 文件，从容器 cat 回本地
find app/extensions/quant_sys -name "*.py" -size 0 | while read f; do
  docker exec quantdinger-backend cat "/app/$f" > "$f"
done
```

涉及子模块：
- `strategy/` — 因子库、回测引擎、模型训练、生命周期
- `portfolio/` — 持仓、快照
- `ideas/` — Idea Pool CRUD
- `macro/` — 宏观数据路由
- `execution/` — vnpy 执行引擎
- `scheduler/` — 定时任务（周报、信号 cron）
- `indicator.py`, `market_routes.py` — 指标和市场路由

### 2.2 补 `app/__init__.py`

本地 `app/__init__.py` 缺少 quant_sys 子模块注册（容器里有）。同步容器版本的注册逻辑：

```python
if os.getenv("QUANT_SYS_ENABLED", "").lower() in ("1", "true", "yes"):
    from app.extensions.quant_sys import quant_bp, init_app as quant_init
    quant_init(app)
    
    # Sub-blueprints
    from app.extensions.quant_sys.risk import init_app as risk_init
    risk_init(app)
    from app.extensions.quant_sys.strategy import init_app as strategy_init
    strategy_init(app)
    from app.extensions.quant_sys.portfolio import init_app as portfolio_init
    portfolio_init(app)
    from app.extensions.quant_sys.ideas import init_app as ideas_init
    ideas_init(app)
    from app.extensions.quant_sys.dashboard import init_app as dashboard_init
    dashboard_init(app)
    from app.extensions.quant_sys.macro import init_app as macro_init
    macro_init(app)
```

---

## Step 3：清理外部引用

### 3.1 Copilot 侧

| 文件 | 动作 |
|------|------|
| `copilot/mcp-servers/quant-bridge/run.sh` | 删除 `QUANT_SYS` 和 `PYTHONPATH` 两行（死代码） |
| `copilot/scripts/diagnose_quant_mcp.py` | 删除 `QUANT_HOME` 引用或改成指向 QuantDinger |

### 3.2 Hermes Cron

**不改**。7 个 cron job 全走 `localhost:5001`，QuantDinger 自举后无需变化。

### 3.3 备份脚本

`~/workspace/quant_sys/scripts/backup_db.sh`：
- PG 备份：保持（连接 `investassist`，目标不变）
- SQLite 备份：路径从 `quant_sys/data/system.db` → `QuantDinger/data/system.db`

可选：把备份脚本迁入 QuantDinger 自己的 `scripts/` 目录。

---

## Step 4：MCP Server 内嵌（可选高阶）

当前 MCP Server 作为独立进程运行，通过 HTTP 桥接。搬进 QuantDinger 内部：

### 4.1 改动范围

| MCP Server | 改动 |
|------------|------|
| `quant-data` | 用 Python import 替代 `urllib.request` HTTP 调用 |
| `quant-bridge` | 同上 |
| `quant-research` | 同上，`screen_a_shares` 保持 akshare 直调 |

### 4.2 架构

```
QuantDinger 容器内
  ├── Flask app (主进程)
  └── MCP Server 子进程 (FastMCP, stdio)
      ├── 直接 import app.extensions.quant_sys.*
      └── 零 HTTP 跳转，零网络延迟
```

Hermes 通过 stdio 连接到容器内的 MCP Server（需要 Hermes 支持 remote MCP transport 或在容器内运行 Hermes）。

> 如果短期内 Hermes 在主机的 stdio 模式不便改动，可暂时保留 HTTP 桥接方式，待 Hermes gateway 支持 TCP/HTTP transport 后再迁。

---

## 执行顺序

```
Step 1 (数据) ──┐
                ├── 并行 ──→ Step 3 (清理) ──→ Step 4 (MCP 内嵌)
Step 2 (代码) ──┘
```

- Step 1 + Step 2 可并行（互不依赖）
- Step 3 依赖 Step 1 路径变更完成
- Step 4 可选，可在 Step 3 之后择机执行

---

## 验收标准

1. ✅ `docker restart quantdinger-backend` 成功启动
2. ✅ `/api/quant/risk/signals/pending` 返回正常 JSON（非 404，非 readonly error）
3. ✅ `/api/quant/data/pipeline/status` 返回正常
4. ✅ 7 个 Hermes cron job 全部 last_status = ok
5. ✅ `quant-bridge` MCP tool 调用正常
6. ✅ `~/workspace/quant_sys/` 目录可安全备份归档
