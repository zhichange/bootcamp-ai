# Feature Design: Query Result Export (查询结果导出)

## 1. 背景与目标 (Background & Goals)

数据库查询工具目前支持 PostgreSQL / MySQL 连接管理、元数据浏览、SQL 执行与自然语言转 SQL。
用户执行查询后，结果仅展示在页面上，无法带走。数据分析、汇报、归档场景都需要把查询结果
保存为文件。

**目标**：用户可以将查询结果导出为 **至少两种格式**。本次实现支持三种：

| 格式 | MIME 类型 | 适用场景 |
|------|-----------|----------|
| CSV  | `text/csv` | Excel 打开、数据交换、ETL 输入 |
| JSON | `application/json` | 程序间数据传递、API 对接 |
| TSV  | `text/tab-separated-values` | 命令行工具（awk/cut/paste）、粘贴到表格 |

## 2. 设计思路 (Design Approach)

### 2.1 方案对比

| 方案 | 说明 | 优点 | 缺点 |
|------|------|------|------|
| A. 纯前端导出 | 把已加载到内存的查询结果用 Blob API 在浏览器端转成文件 | 零后端改动、不重复执行查询 | 不可复用（其他客户端无法使用）、无法处理超过前端内存的数据、格式逻辑散落在 UI 代码中 |
| B. 纯后端导出 | 新增导出 API，服务端重新执行 SQL 并把结果格式化后以文件流返回 | 逻辑集中、可测试、任何客户端（curl / REST Client / 第三方）都能用 | 需要重新执行一次查询 |
| **C. 后端为主 + 前端调用（已选）** | 后端提供导出端点，前端下拉选择格式后调用端点触发浏览器下载 | 兼具 B 的所有优点；前端 UI 简化为一次 HTTP 调用 | 导出时重复执行一次查询（可接受，见下） |

**选择方案 C 的理由**：

1. **架构一致性**：项目已形成 `adapters → services → api` 的清晰分层，导出作为一项
   服务能力放在 services 层、通过 api 层暴露，符合既有模式，也便于单元测试
   （项目有很强的测试文化，`tests/unit/` 已覆盖所有 API 端点）。
2. **重执行查询是可接受的**：导出端点完全复用现有的 `execute_query_with_service`
   管道——包括 SQL 校验（仅允许 SELECT）、自动 LIMIT 1000 保护、连接池复用与查询
   历史记录。重新执行保证导出的是**最新数据**，且 LIMIT 上限防止内存失控。
3. **开放即可复用**：导出能力变成 REST 端点后，`fixtures/test.rest`、curl、
   脚本都能直接下载文件，不只是页面按钮。

### 2.2 整体流程

```
┌──────────┐   POST /api/v1/dbs/{name}/query/export      ┌──────────────┐
│ Frontend │ ──────────────────────────────────────────▶ │  FastAPI     │
│ (Home)   │    body: { sql, format }                    │  queries.py  │
└──────────┘                                             └──────┬───────┘
     ▲                                                          │ 1. 查连接 (404 if missing)
     │  Response                                                │ 2. 复用 execute_query_with_service
     │  Content-Disposition: attachment;                        │    (validate → adapter → history)
     │  filename="todo_20260819_101500.csv"                     │ 3. export_service.format_result()
     │  Content-Type: text/csv; charset=utf-8                   │    (strategy pattern per format)
     └──────────────────────────────────────────────────────────┘ 4. Response(文件流)
```

### 2.3 后端设计

**新增 `app/services/export_service.py`（策略模式）**：

- `ExportFormat(str, Enum)`：`CSV / JSON / TSV`，每个成员携带 `mime_type` 与
  `content_type`（含 charset）属性。
- `format_result(result: QueryResult, fmt: ExportFormat) -> str`：按列顺序序列化
  `columns + rows` 为目标格式的字符串。
  - **CSV**：标准库 `csv.writer`，自动处理引号/逗号/换行的转义（RFC 4180），
    `None` 写为空字符串。
  - **JSON**：`json.dumps(..., indent=2, default=str)`，输出「行对象数组」，
    `default=str` 兜底 datetime/Decimal 等不可 JSON 序列化类型。
  - **TSV**：制表符分隔，值内的 `\t` / `\n` / `\r` 替换为空格，避免破坏列结构。
- `build_filename(database_name, fmt) -> str`：`{db}_{YYYYMMDD_HHMMSS}.{ext}`。

**新增端点（`app/api/v1/queries.py`）**：

```
POST /api/v1/dbs/{name}/query/export
Content-Type: application/json

{ "sql": "SELECT ...", "format": "csv" }   # format ∈ csv|json|tsv, 默认 csv
```

- 复用 `QueryInput`（sql 校验），新增 `ExportInput = QueryInput + format`。
- 执行成功 → `Response(content=..., media_type=..., headers={"Content-Disposition": ...})`，
  浏览器直接触发下载。
- 错误语义与现有 `/query` 端点完全一致：连接不存在 404、SQL 校验失败 400、
  执行失败 500、format 非法 422。

### 2.4 前端设计

`Home.tsx` 的结果卡片头部：

- 两个独立按钮（EXPORT CSV / EXPORT JSON）改为 **Dropdown 按钮「EXPORT」**，
  菜单项：CSV / JSON / TSV。
- 点击后 `apiClient.post(url, { sql, format }, { responseType: "blob" })`，
  用 `URL.createObjectURL` 触发下载（沿用现有下载方式）。
- 保留大数据量（>10000 行）确认弹窗与空结果提示。

## 3. 兼容性与安全

- 不修改任何现有端点 / 组件签名，纯增量变更。
- 导出走与 `/query` 相同的 `sql_validator`（仅 SELECT）与自动 LIMIT，无注入面扩大。
- 返回值仍是纯文本格式，设置 `charset=utf-8` 避免中文乱码。

## 4. 测试策略

1. **单元测试（新增 `tests/unit/test_export.py`）**：
   - 各格式序列化正确性：列头、多行数据、NULL、特殊字符（逗号/引号/换行/制表符）、
   中文、datetime。
   - 文件名生成格式。
   - API 层：200 + Content-Type + Content-Disposition、默认格式、404、400（非法 SQL）、
   422（非法 format）。
2. **手工验证**：`fixtures/test.rest` 追加导出请求；页面点击 EXPORT 下拉实际下载。

## 5. 后续扩展（不在本次范围）

- XLSX（需引入 openpyxl）、NDJSON、Markdown 表格——策略模式下只需新增枚举成员 +
  一个序列化函数。
- 流式导出（`StreamingResponse`）以支持远超 LIMIT 的数据量。
