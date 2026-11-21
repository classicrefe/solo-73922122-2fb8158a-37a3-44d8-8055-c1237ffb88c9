# 水质平台 · 报文契约发布

固件升级前确认新报文能被分析程序读取、程序升级后仍能读旧归档的契约发布网页。
按设备型号登记**历代固件写入格式**（只追加）与**现用程序读取格式**（按程序号替换），
各至多 8 份。每次发布前用 **z3** 对「全部历代写入格式 × 全部现用程序」做**全量**兼容判定
（逻辑证明，非抽样试样例）：任一写入格式允许的全部报文必须被每个现用程序接受，
否则拒绝发布，并给出**合法反例报文**与**拒收程序**。

兼容结论（含反例）按**规范化契约内容**（SHA-256）寻址持久保存：内容没变的组合直接复用，
新增或替换一侧时只重算受影响组合，服务重启后仍然有效。每次发布都会显示本次
**复用 / 重算**的组合数量。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已发布格式与兼容结论均保留。
- 演示入口：首页创建型号后进入管理页；点内置示例按钮即可演示「先被拒、升级后通过」，
  每次发布会显示本次复用/重算的组合数量。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号管理页。
2. 在「发布新格式」粘贴受限 JSON Schema：
   - 左栏**追加固件写入格式**（版本号自动递增，只追加）；
   - 右栏**发布/替换程序读取格式**（同一程序号覆盖旧版）。
3. 发布结果即时展示：成功，或拒绝 + 每个不兼容组合的反例报文与拒收程序；
   同时显示本次「复用 N 个 / 重新求解 M 个」组合数。
   页面下方「发布日志」保留最近 50 条成功/拒绝/无效记录（含拒绝依据与复用/重算数）。
4. 页面内置三个示例，点按钮即可填入编辑器：
   - **温度扩范围**：固件 v2 扩到 -40~100℃，直接发布会被旧程序（-10~50℃）拒收，
     反例如 `{"temp": -40, ...}`；先升级程序再发固件则通过。
   - **质控码缺省**：固件缺省 `qc`（或写 `null`），把 `qc` 列为 `required` 的程序会被拒，
     反例为不含 `qc` 的报文；程序改为可选并允许 `null` 后通过。
   - **顶层 anyOf**：程序同时接受多种报文形态。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## 校验原理

对每对（写入格式 W，读取格式 R），把报文编码为 z3 变量（每个字段：是否出现、
值类型 null/int/string、整数值、字符串枚举下标），求解 `W 合法 ∧ R 拒收`：

- **UNSAT** ⇒ 不存在反例，`L(W) ⊆ L(R)` 成立——这是对全部报文的证明，不是抽样；
- **SAT** ⇒ 从模型还原一个反例报文，并用独立的纯 Python 判定器复核
  （确实满足 W、确实被 R 拒收）后随拒绝结果返回。

## 兼容结论缓存（增量复用）

- **按规范化内容寻址**：契约文档先解析为受限 Schema，再规范化为与书写顺序无关的形式
  （属性顺序、`required` 顺序、枚举顺序、`anyOf` 分支顺序/重复均不影响结果），
  取 `SHA-256` 作为键。同一固件内容 × 同一程序内容无论出现在哪个型号、哪个版本、
  哪次发布，只要内容相同就命中同一结论。
- **只算受影响的组合**：发布固件时新增「新固件 × 全部现用程序」；替换程序时只新增
  「全部历代固件 × 新程序」。其余组合直接复用已有结论（兼容 = 无问题；不兼容 =
  已有合法反例），不再调用 z3。
- **随发布事务原子生效**：结论与格式行、发布日志在**同一个数据库事务**中提交，
  提交失败则一起回滚——不会出现格式未变而缓存已变，也不会留下半写入的缓存。
  发布被拒绝时已发布格式不变；本次在锁内基于最新已提交状态算出的结论（含反例）
  与拒绝日志同事务提交，因此相同内容的再次尝试可直接复用反例，而不是重新求解。
- **不会泄露过期结论**：键是内容哈希而非型号/版本号，结论只对产生它的那对内容成立；
  内容一旦改变即换键重算（测试覆盖“旧程序的反例不得用于新程序”）。协作出现在
  不同型号/并发事务中时，`ON CONFLICT DO NOTHING` 保证先提交的结论不被覆盖，
  且每个事务只提交自己在锁内看到的状态下算出的内容对结论。
- **重启后仍可用**：结论存于 `compatibility_verdicts` 表（Compose 中即 PostgreSQL
  命名卷 `pgdata`），服务重启后照常复用。
- **可观测**：API 响应的 `reused` / `recomputed` / `checked_pairs`、页面提示与发布日志、
  `GET /api/cache/stats` 均给出复用与重算数量。
- 规范化算法带版本号 `CANON_VERSION`，算法调整时旧键自然失效，不会误用旧结论。
  每条命中结论还会用独立 Python 判定器复核反例，复核不过即当未命中重算。

## 并发与事务

- 每次发布在事务内先对型号行加锁（PostgreSQL `SELECT ... FOR UPDATE`），
  再基于最新已提交状态做**全量交叉检查**后落库——并发升级被串行化，
  不会各自通过却留下不兼容组合；命中缓存的组合直接复用结论，
  缓存与格式变更在同一事务内提交，回滚时一并撤销；
- 检查失败不改已发布格式；拒绝/无效尝试另写发布日志留痕（拒绝时附反例），
  拒绝时新算出的结论与拒绝日志同事务提交；
- 固件格式只追加（无修改/删除入口），程序格式按程序号替换；各至多 8 份。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看当前固件/程序格式 |
| GET | `/api/models/{name}/log` | 发布日志（含拒绝依据） |
| POST | `/api/models/{name}/firmware` | 追加固件格式 `{"schema": {...}}` |
| POST | `/api/models/{name}/programs` | 发布/替换程序格式 `{"program_id": "...", "schema": {...}}` |
| GET | `/api/cache/stats` | 已持久化兼容结论数（总/兼容/不兼容） |

响应：成功 `200 {"ok": true, "reused": N, "recomputed": M, "checked_pairs": N+M, ...}`；
不兼容 `409 {"ok": false, "problems": [{firmware_version, program_id, counterexample}], "reused": ..., "recomputed": ...}`；
语法/上限错误 `422`。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                  # 语法子集 + 规范化 + z3 判定 + 缓存单元（61 项）
python3 tests/test_transactions.py           # 发布事务原子性：提交/写入失败、拒绝、并发、跨型号复用（24 项）
python3 tests/test_cache.py                  # 缓存专项：发布/重启/拒绝/并发（自带临时服务，39 项）
cd app && DATABASE_URL=sqlite:////tmp/contracts.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                    # 原有集成自测：示例/上限/并发/页面（44 项）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API）
  service.py      发布事务：加锁 → 增量兼容检查 → 结论/格式/日志同事务落库或回滚
  solver.py       z3 编码与全量兼容判定（含反例复核）、VerdictCache 复用/重算
  schema_spec.py  受限 JSON Schema 子集解析、确定性报文判定、规范化哈希
  db.py           SQLAlchemy 模型（含 compatibility_verdicts 结论缓存）
  examples.py     温度扩范围、质控码缺省、anyOf 示例
  templates/      页面模板
tests/
  test_units.py         单元自测（语法/规范化/z3/缓存）
  test_transactions.py  事务原子性白盒自测
  test_cache.py         缓存专项集成自测（发布/重启/拒绝/并发）
  selftest.py           原有集成自测
docker-compose.yml
```
