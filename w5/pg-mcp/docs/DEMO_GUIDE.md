# pg-mcp 功能演示操作手册

本手册演示三类评审问题的修复效果：**多数据库与安全控制**、**弹性与可观测性整合**、**模型/配置缺陷与测试覆盖**。

已有截图证据存放在 `docs/evidence/`（见文末清单）；以下步骤可自行复现。

## 准备

```bash
cd w5/pg-mcp
uv sync --extra dev        # 安装依赖（demo 不需要 PostgreSQL 和 OpenAI key）
```

> demo 全程使用 mock 的 LLM 与数据库池，无需任何外部服务。

## 演示 1：功能演示脚本（多数据库 / 安全 / 限流 / 退避 / 指标）

```bash
uv run python scripts/demo_features.py
```

输出分 5 节，对应证据截图 `evidence_s1.png` ~ `evidence_s5.png`：

| 节 | 演示内容 | 关注点 |
|---|---|---|
| 1 | 多数据库 | `DATABASES` JSON 配置出 `sales`/`archive` 两个附加库；指定 `database='sales'` 请求由 sales 执行器执行；`wrongdb` 被拒并列出可用库；多库不指定库名被拒 |
| 2 | 安全控制 | `SECURITY_BLOCKED_TABLES=salaries,audit_log` 拒绝直接查询与 JOIN；`SECURITY_BLOCKED_COLUMNS=password_hash` 拒绝敏感列；`SECURITY_ALLOW_EXPLAIN=false` 拒绝 EXPLAIN，改为 `true` 后放行 |
| 3 | 限流 | 并发槽 1/1 占满后，新请求返回 `rate_limit_exceeded` |
| 4 | 重试退避 | DELETE/DROP 被拒后按 `retry_delay * backoff_factor**n` 退避，实测 `0.20s → 0.40s` |
| 5 | 指标 | `pg_mcp_query_requests_total{database=...}`、`pg_mcp_sql_rejected_total`、`pg_mcp_llm_calls_total` 等 Prometheus 指标 |

### 附：让 /metrics 端点在浏览器可见

```bash
uv run python scripts/demo_features.py --metrics
# 浏览器打开 http://localhost:9090/metrics，Ctrl+F 搜索 pg_mcp_
```

## 演示 2：真实服务端多库启动（需要 PostgreSQL + OpenAI key）

1. 配置 `.env`（参考 `.env.example`）：

   ```
   DATABASE_NAME=blog_small
   DATABASES=[{"name":"blog_medium","host":"localhost","user":"postgres","password":"postgres"}]
   SECURITY_BLOCKED_TABLES=salaries
   SECURITY_ALLOW_EXPLAIN=true
   ```

2. 启动服务 `uv run python main.py`，日志会逐库打印
   `Creating connection pool for database 'xxx'` 与 `Created SQL executor for database 'xxx'`，
   以及 `SQL validator configured`（含 blocked_tables / allow_explain）。

3. 在 MCP 客户端（如 Claude Desktop）调用 query 工具：
   - `{"question": "...", "database": "blog_medium"}` → 正常执行
   - `{"question": "...", "database": "不存在的库"}` → `database_error: Database 'xxx' not found`
   - 让 LLM 生成含 `salaries` 的 SQL → `security_violation: Access to table 'salaries' is not allowed`

## 演示 3：测试与质量基线

```bash
uv run pytest tests/unit --cov=src --cov-report=term   # 305 passed, coverage 88.57%
uv run ruff check .                                     # All checks passed!
uv run mypy src                                         # no issues in 30 source files
```

对应截图 `evidence_s5.png`（右下角为覆盖率摘要）。

## 截图证据清单（docs/evidence/）

| 文件 | 内容 |
|---|---|
| `evidence_s1.png` | 多数据库配置 + 按库路由 + 错误库名拒绝 |
| `evidence_s2.png` | 封禁表/封禁列/EXPLAIN 策略（拒绝与放行对照） |
| `evidence_s3.png` | 限流：并发打满返回 rate_limit_exceeded |
| `evidence_s4.png` | 重试退避实测序列 0.20s → 0.40s |
| `evidence_s5.png` | Prometheus 指标输出 + pytest 覆盖率 88.57% |
| `metrics_endpoint.png` | 真实 `http://localhost:9090/metrics` 端点中的 pg_mcp_* 指标 |
| `s1.html`~`s5.html` / `evidence.html` | 截图对应的网页源文件 |
| `demo_output.txt` / `pytest_tail.txt` / `ruff.txt` / `mypy.txt` | 原始命令输出 |
