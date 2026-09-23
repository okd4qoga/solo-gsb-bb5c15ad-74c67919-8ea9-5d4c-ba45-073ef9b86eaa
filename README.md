# 科研样品分装追溯 API

基于 **FastAPI + SQLite** 的科研样品谱系(lineage)追溯服务。支持样品的**取样、分装、合并、销毁/报废**四类操作,在数据库事务中完成余量(销毁按可用量)校验与环路检测,并通过唯一操作号实现**幂等提交**;可按任意样品回溯完整来源与去向,并对余量账本做一致性校验。销毁操作允许一次提交多个样品的销毁量与原因,服务在单事务内按当前可用量(余量减未释放预留)逐项校验,任一不足则整体回滚并返回逐项冲突,成功后写入不可变销毁审计记录与出库流水并递增样品版本,同时接入批量提交与 dry-run 预校验。此外提供四个扩展模块:**预留模块**——客户端可用一个预留号为多个样品分别锁定数量,服务在同一事务中校验各样品可用余量(当前余量减去未释放预留的未核销部分),全部足够才一次创建,支持按预留号幂等重放、整单释放与可用量查询;**核销领用模块**——对现有预留单分次领用,每次提交核销号、预留号与若干样品领用量,服务在同一事务中校验预留未释放、逐项未核销量与样品余量,任一失败整单不变,成功后扣减样品余量、递增版本并写入不可变核销明细,同一核销号同内容幂等重放;核销后只将未核销部分继续计入锁定量,释放预留时仅解除剩余锁定,核销量纳入余量账本校验;**盘点模块**——按样品编号集合建批保存余量/版本快照,提交实盘结果时逐项乐观锁校验数据库余量未被其他操作改变并计算系统量与实盘量差异,确认时在单事务内再次校验版本、写入盘点调整流水并更新样品余量(任一项冲突则整批不改库存),按盘点批次号支持同请求幂等重放与不同内容冲突;**位置交接模块**——一次提交多个样品的原位置与目标位置,服务在单个 `BEGIN IMMEDIATE` 事务内逐项校验样品仍处于请求所述原位置,任一不符(样品不存在或已被移动)则整批不变,全部通过后统一更新各样品位置并逐项记录交接人、交接时间与变更前后位置(审计表不可变),同时提供按样品查询交接历史的接口,交接按交接单号幂等重放。

## 一键启动

需要本机安装 Docker 与 Docker Compose(v2)。

```bash
docker compose up --build
```

启动后访问:

- API 服务地址:**http://localhost:8000**
- 健康检查:http://localhost:8000/health
- 交互式文档(Swagger UI):http://localhost:8000/docs
- OpenAPI 描述:http://localhost:8000/openapi.json

数据持久化在 Docker 卷 `sample-data`(容器内 `/data/samples.db`)。停止后数据保留,可用 `docker compose down -v` 连同数据卷一并清除。

### 本地直接运行(无 Docker)

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 同样打开 http://localhost:8000
```

可通过环境变量调整:`SAMPLE_DB_PATH`(数据库文件路径,默认 `./data/samples.db`)、`BALANCE_TOLERANCE`(余量校验容差,默认 `1e-6`)。

## 数据模型与规则

| 概念 | 说明 |
| --- | --- |
| 样品 `sample_id` | 业务方提供的唯一编号;带有物料、单位、初始量与当前余量 |
| 操作号 `op_no` | **全局唯一的幂等键**。同一操作号 + 同一请求体重放,返回首次提交的原始结果;同号但内容不同返回 `409 op_no_conflict` |
| 取样 sampling | 从外部获得全新**根样品**,记录来源边(`parent=null`)与入库流水 |
| 分装 aliquot | 一个父样品拆成若干**新**子样品;总取出量不得超过父样品余量 |
| 合并 merge | 多个来源(可部分取用)汇入输出;输出可为新样品或已有样品;支持守恒校验 |
| 销毁 dispose | 一次提交多个样品的**销毁量与原因**;逐项按可用量(余量−未释放预留的未核销部分)校验,只出库、不产生谱系边 |
| 销毁审计记录 | 销毁成功即逐项写入不可变表 `dispose_records`(销毁量、单位、原因、操作者、前后版本、销毁后余量),触发器禁止 UPDATE/DELETE |
| 谱系边 | 每次转移记录 `parent → child`、数量、单位与操作号,构成可追溯有向无环图 |
| 出入库流水 | 与谱系边对应的 in/out 账本,余量可随时按流水重算审计 |
| 预留单 | 一个预留号(`reservation_no`)为多个样品分别锁定数量;`active`=锁定中,`released`=已整单释放 |
| 已预留量 / 可用量 | 已预留量 = 该样品全部未释放预留的**未核销**锁定合计;**可用量 = 当前余量 − 已预留量** |
| 核销领用单 | 对现有预留单的分次领用:一个核销号(`consumption_no`,幂等键)对应一张未释放预留单,逐项给出领用量;成功后扣减余量、版本 +1,只把未核销部分继续锁定 |
| 核销审计明细 | 核销成功即逐项写入不可变表 `consumption_items`(领用量、单位、前后版本、核销后余量、累计已核销/剩余锁定),头表 `consumptions` 与明细表触发器均禁止 UPDATE/DELETE |
| 预留审计事件 | 预留、幂等重放、释放与查询的结果快照均写入 `reservation_events`,随数据库持久化 |
| 样品版本 `version` | 乐观锁版本号,余量每被取样/分装/合并/盘点调整改变一次即 +1;盘点据此判断快照后余量是否被其他操作改变 |
| 盘点批次 | 一个盘点批次号(`stocktake_no`)对应一次盘点,状态机 `open`(已建批快照)→ `submitted`(已提交实盘与差异)→ `confirmed`(已确认调账) |
| 盘点快照 / 差异 | 建批时在事务内逐项保存系统余量、单位与版本;提交实盘时计算 **差异 = 实盘量 − 系统量** |
| 盘点调整流水 | 确认时逐项写入 `stocktake_adjustments`(系统量、实盘量、调整量、操作者、原因、前后版本),是余量账本的一部分 |
| 位置交接单 | 一个交接单号(`transfer_no`)对应一次多位置交接;逐项给出样品的**原位置**与**目标位置**,交接人必填 |
| 位置交接审计 | 交接成功后逐项写入不可变表 `location_transfer_items`(样品、变更前/后位置、交接人、交接时间),头表与明细表触发器均禁止 UPDATE/DELETE |

**事务保证**

- 余量校验、环路检测与写入在单个 `BEGIN IMMEDIATE` 事务中完成;任一操作冲突,**整批回滚**。
- 禁止环路:合并前在谱系图上检测"输出样品能否沿现有边到达任一输入"(即新边是否成环),冲突时返回完整环路径,如 `A1 -> B1 -> A1`。
- 禁止自合并、单位不一致、覆盖已有样品、负余量(数据库 `CHECK remaining >= 0` 兜底)。
- 冲突统一为 HTTP `409`,响应体 `conflicts[]` 携带冲突类型、操作号、批内下标(`batch_index`,从 0 起)、样品号、位置、请求量/可用量与环路径。

**预留事务保证**

- 创建预留时在单个 `BEGIN IMMEDIATE` 事务内检查每个样品的可用量(当前余量减去未释放预留的**未核销**部分);**任一样品不足即整单失败**,不写入任何预留(返回所有不足/不存在的样品及各自可用量)。
- 同一预留号 + 同一请求体重放返回首次结果(`replayed: true`),不重复锁定;同号不同内容返回 `409 reservation_no_conflict`;对已释放预留号再次创建返回 `409 reservation_closed`。
- `DELETE` 整单释放后**仅解除剩余(未核销)锁定**,锁定量立即归还,可被新预留使用;重复释放幂等;预留明细与核销明细在释放后均保留作为审计痕迹。
- 预留不改动样品余量与出入库流水,仅通过"未释放预留的未核销合计"影响可用量;核销领用由专用接口完成(见下节)。

**核销领用事务保证**

- 核销请求一次给出**核销号、预留号与若干样品领用量**,服务在单个 `BEGIN IMMEDIATE` 事务内依次校验:① 预留单存在且**未释放**;② 每个样品属于该预留单,且领用量不超过该明细的**未核销量**(预留锁定量 − 累计已核销);③ 样品当前余量足够。任一明细冲突(预留已释放、样品不存在/不在单内、超未核销量或余量不足)即**整单回滚**,响应 `conflicts[]` 一次返回全部冲突明细(含各项 `requested`/`available`),余量、版本、核销明细与锁定量一律不变。
- 全部通过后才:逐项扣减样品 `samples.remaining` 并**版本 +1**,写不可变核销单头 `consumptions`(保存首次结果快照)与明细 `consumption_items`(领用量、单位、前后版本、核销后余量、累计已核销/剩余锁定),头表与明细表触发器均禁止 UPDATE/DELETE。
- 核销后该预留明细只把**未核销部分**继续计入锁定量(可用量查询、预留查询、销毁校验均为同一口径);释放预留时仅解除剩余未核销锁定,已核销实物不归还。
- 核销量作为**非谱系出账**纳入余量账本重算,核销后 `/api/v1/checks/balances` 仍保持一致,响应各样品附 `consumed` 核销合计。
- **幂等**:同一核销号 + 同一请求体重放返回首次结果(`replayed: true`,即使预留此后已释放也只回放原结果),不重复扣减、不重复写明细;同号不同内容返回 `409 consumption_no_conflict`;对不存在预留号核销返回 `404 reservation_not_found`。

**销毁/报废事务保证**

- 销毁请求一次给出多个样品明细(每项销毁量与原因必填),服务在单个 `BEGIN IMMEDIATE` 事务内**逐项校验可用量 = 当前余量 − 未释放预留的未核销部分**;任一样品不足(或不存在)即**整单回滚**,响应 `conflicts[]` 一次返回全部冲突明细(含各项 `requested`/`available`),不更新任何余量、不写任何审计记录。
- 校验全部通过后:逐项写入出库流水(`ledger_entries`,方向 `out`,经 `dispose_id` 关联审计记录)与**不可变销毁审计记录** `dispose_records`(样品、销毁量、单位、原因、操作者、销毁前后版本、销毁后余量;数据库触发器禁止 UPDATE/DELETE),样品余量扣减且**版本 +1**。
- 销毁不产生谱系边、不创建样品;销毁出账纳入余量账本重算,销毁后 `/api/v1/checks/balances` 仍保持一致。
- **幂等**:同一操作号 + 同一请求体重放返回首次结果(`replayed: true`),不重复出库;同号不同内容返回 `409 op_no_conflict`。
- dispose 可与其他类型一起走 `/api/v1/batch`(整批一个事务,后续操作可见前面销毁后的余量),并支持 `/api/v1/validate`、`/api/v1/batch/validate` dry-run(冲突带批内下标,不写主库)。

**盘点事务保证**

- **建批(open)**:按样品编号集合在单个 `BEGIN IMMEDIATE` 事务内逐项保存「系统余量 / 单位 / 版本」快照;任一样品不存在则整批不建。
- **提交实盘(submitted)**:实盘明细必须与批次快照逐项一致(不重不漏,实盘量允许为 0);逐项校验样品仍存在,且数据库余量自快照以来**未被其他操作改变**(版本 + 余量双重比对),全部通过才计算并保存差异(实盘量 − 系统量)。**本步骤不改任何库存**。
- **确认(confirmed)**:在一个事务内对每一项**再次校验**样品版本仍等于提交时版本(即提交→确认之间余量未被改变);全部通过才逐项写入盘点调整流水、把样品余量更新为实盘量(有差异才递增版本),并保留操作者与原因。**任一项冲突即整批回滚,库存一律不动,不产生任何调整流水**。
- 账实相符(差异为 0)也写一条零差异审计流水,但不递增样品版本。盘点调整计入余量账本重算,因此确认后 `/api/v1/checks/balances` 仍保持一致。
- **幂等**:同批次号 + 同建批请求体重放返回当前批次(`replayed: true`),同号不同内容返回 `409 stocktake_no_conflict`;已提交批次仅接受相同实盘请求体重放,否则 `409 stocktake_result_conflict`;已确认批次接受相同操作者/原因的重复确认回放,不同内容返回 `409 stocktake_already_confirmed`。
- **数据库迁移**:旧版本数据库(无 `samples.version` 列)在启动初始化时经 `schema_migrations` 登记的迁移自动补列/补表,迁移幂等,重复启动不重复执行。

**位置交接事务保证**

- 交接请求一次给出多个样品明细,每项声明**请求所述原位置 `from_location`** 与**目标位置 `to_location`**(目标位置必填;原位置传 `null` 表示样品未登记位置)。服务在单个 `BEGIN IMMEDIATE` 事务内逐项校验样品存在且当前位置与 `from_location` 完全一致;**任一项不符(样品不存在或已被其他交接/操作移动)即整批回滚**,响应 `conflicts[]` 一次返回全部不符明细(冲突体带 `requested_location` 请求原位置与 `available_location` 当前实际位置),**所有样品位置保持不变**。
- 全部校验通过后才统一更新各样品 `samples.location`,并在同一事务内写交接单头 `location_transfers` 与逐项审计明细 `location_transfer_items`(交接人、交接时间、变更前位置、变更后位置);两张表均由触发器禁止 UPDATE/DELETE,交接记录不可变。
- 交接只改位置,**不改动余量、不产生谱系边与出入库流水**,交接后 `/api/v1/checks/balances` 仍保持一致。
- **幂等**:同一交接单号 + 同一请求体重放返回首次结果(`replayed: true`,返回首次交接时间),不重复移动、不重复写审计;即使重放时样品已不在原位置,也只回放原结果而不会再次移动;同号不同内容返回 `409 transfer_no_conflict`。
- **历史查询**:`GET /api/v1/samples/{sample_id}/transfers` 返回样品当前位置与按交接时间倒序(最新在前)的交接历史;样品不存在返回 HTTP `404`。新表随容器启动时的数据库初始化自动创建,旧版本数据库无需手工迁移。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/operations` | 提交单个操作(按 `type` 自动判别) |
| POST | `/api/v1/operations/sampling` | 取样 |
| POST | `/api/v1/operations/aliquot` | 分装 |
| POST | `/api/v1/operations/merge` | 合并 |
| POST | `/api/v1/operations/dispose` | 销毁/报废:一次提交多个样品的销毁量与原因,单事务逐项校验可用量 |
| POST | `/api/v1/batch` | 批量操作(含 dispose),单事务提交;`status` 为 `committed`/`replayed`/`mixed` |
| POST | `/api/v1/validate` | 单操作 dry-run 预校验(不写库) |
| POST | `/api/v1/batch/validate` | 批量 dry-run 预校验,冲突带 `batch_index` 定位 |
| GET  | `/api/v1/samples` | 样品列表(分页) |
| GET  | `/api/v1/samples/{id}/trace?direction=both` | 回溯来源 `upstream` / 去向 `downstream` / `both` |
| GET  | `/api/v1/samples/{id}/balance` | 单个样品余量账本校验 |
| GET  | `/api/v1/checks/balances` | 全量余量一致性校验,返回全部冲突及定位 |
| POST | `/api/v1/reservations` | 创建预留:一个预留号锁定多个样品,全部足够才一次创建 |
| GET  | `/api/v1/reservations?sample_id=...` | 查询样品当前余量 / 已预留量 / 可用量(可重复传参,不传返回全部) |
| GET  | `/api/v1/reservations/{reservation_no}` | 按预留号查询预留单 |
| DELETE | `/api/v1/reservations/{reservation_no}` | 整单释放预留(按预留号幂等,仅解除剩余未核销锁定) |
| POST | `/api/v1/consumptions` | 核销领用:对现有预留单分次领用,单事务校验预留未释放/逐项未核销量/样品余量,任一失败整单不变 |
| GET  | `/api/v1/consumptions/{consumption_no}` | 按核销号取首次核销领用结果(幂等凭证) |
| POST | `/api/v1/stocktakes` | 新增盘点批次:单事务保存样品集合的余量/版本快照(按批次号幂等) |
| POST | `/api/v1/stocktakes/{no}/submit` | 提交实盘结果:逐项校验样品仍存在且余量未被改变,计算系统量与实盘量差异 |
| POST | `/api/v1/stocktakes/{no}/confirm` | 确认盘点:事务内二次校验版本,写调整流水并更新余量(任一冲突整批不改) |
| GET  | `/api/v1/stocktakes/{no}` | 查询盘点批次:快照、实盘/差异、当前版本漂移、确认状态与调整流水 |
| POST | `/api/v1/transfers` | 位置交接:一次提交多个样品的原位置/目标位置,单事务校验全部仍在原位置,任一不符整批不变 |
| GET  | `/api/v1/samples/{id}/transfers` | 按样品查询位置交接历史(交接人、时间、变更前后位置,最新在前) |
| GET  | `/api/v1/operations/{op_no}` | 按操作号取首次提交的原始结果(幂等凭证) |
| GET  | `/health` | 健康检查 |

## 使用示例

以下示例可在服务启动后直接执行(`http://localhost:8000`)。

### 1. 取样 100 mL

```bash
curl -X POST http://localhost:8000/api/v1/operations/sampling \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "sampling",
    "op_no": "OP-20260922-001",
    "sample_id": "S0",
    "material": "血清",
    "amount": 100,
    "unit": "mL",
    "location": "冷库A-1"
  }'
```

返回新建样品、谱系边与最新余量。再次提交**完全相同**的请求体,返回内容一致且 `replayed: true`,不会重复入库。

### 2. 批量分装(单事务)

```bash
curl -X POST http://localhost:8000/api/v1/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "ALI-0922",
    "operations": [
      {"type": "aliquot", "op_no": "OP-002", "parent_id": "S0",
       "outputs": [{"sample_id": "A1", "amount": 60},
                   {"sample_id": "A2", "amount": 30}],
       "location": "操作台2"}
    ]
  }'
# S0 余量:100 -> 10;新建 A1=60、A2=30
```

### 3. 余量不足冲突(返回操作与定位)

```bash
curl -X POST http://localhost:8000/api/v1/operations/aliquot \
  -H 'Content-Type: application/json' \
  -d '{"type": "aliquot", "op_no": "OP-003", "parent_id": "S0",
       "outputs": [{"sample_id": "A3", "amount": 20}]}'
```

```json
{
  "error": "conflict",
  "conflicts": [{
    "code": "insufficient_balance",
    "message": "父样品 S0 余量 10.0 mL 不足,本次分装需要 20.0 mL",
    "op_no": "OP-003",
    "batch_index": null,
    "sample_id": "S0",
    "location": null,
    "requested": 20.0,
    "available": 10.0,
    "cycle_path": null
  }]
}
```

### 4. 合并,并触发环路检测

```bash
# A1 取 40、A2 取 20 合并成 B1(60 mL)
curl -X POST http://localhost:8000/api/v1/operations/merge \
  -H 'Content-Type: application/json' \
  -d '{"type": "merge", "op_no": "OP-004",
       "inputs": [{"sample_id": "A1", "amount": 40},
                  {"sample_id": "A2", "amount": 20}],
       "output_id": "B1", "material": "混合血清"}'

# 试图把下游 B1 合回其上游 A1 -> 成环,409
curl -X POST http://localhost:8000/api/v1/operations/merge \
  -H 'Content-Type: application/json' \
  -d '{"type": "merge", "op_no": "OP-005",
       "inputs": [{"sample_id": "B1", "amount": 5}],
       "output_id": "A1"}'
# conflict code = cycle_detected, cycle_path = ["A1", "B1", "A1"]
```

合并时还可传 `expected_output_amount` 做输入/输出守恒校验;输出为已存在样品时则把总量并入该样品(不得成环)。

### 5. 回溯完整来源 / 去向

```bash
# 向上:B1 的全部来源根样品与逐跳路径
curl "http://localhost:8000/api/v1/samples/B1/trace?direction=upstream"
# {"roots": ["S0"], "nodes": [{"sample_id": "B1", "depth": 0},
#                              {"sample_id": "A1", "depth": 1},
#                              {"sample_id": "A2", "depth": 1},
#                              {"sample_id": "S0",  "depth": 2}], ...}

# 向下:最终流到哪些无下游的叶子样品
curl "http://localhost:8000/api/v1/samples/S0/trace?direction=downstream"
```

响应同时包含 `nodes`(样品及跳数)、`edges`(带操作号的谱系边)、`operations`(相关操作与时间/位置)、`roots`、`leaves`。遍历用递归 CTE 并带路径防环保护。

### 6. 余量账本校验

```bash
curl http://localhost:8000/api/v1/checks/balances
```

对每个样品比较**冗余余量**与按出入库流水重算的结果,`consistent: false` 时在 `conflicts` 中给出样品定位(可经 `?tolerance=` 调整容差)。

### 7. 批量预校验(dry-run)

```bash
curl -X POST http://localhost:8000/api/v1/batch/validate \
  -H 'Content-Type: application/json' \
  -d '{"operations": [
    {"type": "aliquot", "op_no": "X1", "parent_id": "S0",
     "outputs": [{"sample_id": "K1", "amount": 2}]},
    {"type": "aliquot", "op_no": "X2", "parent_id": "S0",
     "outputs": [{"sample_id": "K2", "amount": 9}]}
  ]}'
# valid=false,conflicts[0].batch_index = 1(S0 在第一条后仅剩 8)
# 预校验基于内存快照执行,主库不会被写入
```

### 8. 预留:一个预留号锁定多个样品

```bash
curl -X POST http://localhost:8000/api/v1/reservations \
  -H 'Content-Type: application/json' \
  -d '{
    "reservation_no": "RSV-20260922-001",
    "note": "实验A备料",
    "items": [
      {"sample_id": "S0", "amount": 30},
      {"sample_id": "A1", "amount": 10}
    ]
  }'
```

服务在**同一事务**内校验每个样品的「当前余量 − 未释放预留」:全部足够才创建,任一不足则整单回滚(任何样品的已预留量都不变)。返回每个样品的当前余量、已预留量与可用量:

```json
{
  "reservation_no": "RSV-20260922-001",
  "status": "active",
  "items": [
    {"sample_id": "S0", "amount": 30.0, "unit": "mL",
     "remaining": 100.0, "reserved": 30.0, "available": 70.0},
    {"sample_id": "A1", "amount": 10.0, "unit": "mL",
     "remaining": 60.0, "reserved": 10.0, "available": 50.0}
  ],
  "note": "实验A备料",
  "replayed": false
}
```

同一预留号用**完全相同**的请求体重放,返回首次结果且 `replayed: true`,不会重复锁定;同号不同内容返回 `409 reservation_no_conflict`。样品可用量不足时返回 `409 insufficient_available`,冲突体带 `requested`(本次锁定量)与 `available`(当前可用量)。

### 9. 查询可用量

```bash
# 查询指定样品(参数可重复)
curl "http://localhost:8000/api/v1/reservations?sample_id=S0&sample_id=A1"
# 不传 sample_id 则返回全部样品
curl http://localhost:8000/api/v1/reservations
```

```json
{"samples": [
  {"sample_id": "S0", "unit": "mL",
   "remaining": 100.0, "reserved": 30.0, "available": 70.0}
]}
```

### 10. 整单释放

```bash
curl -X DELETE http://localhost:8000/api/v1/reservations/RSV-20260922-001
```

释放后该单全部锁定量归还各样品(响应给出释放后的余量 / 已预留 / 可用量);对同一预留号重复释放返回相同结果且 `replayed: true`。

预留、重放、释放与查询事件均持久化在 `reservation_events` 表(含结果快照),新表随容器启动时的数据库初始化自动创建,数据保存在 Docker 卷 `sample-data` 中。

### 11. 核销领用:对预留单分次领用

在第 8 节的预留单 `RSV-20260922-001`(S0 锁 30、A1 锁 10)基础上,提交**核销号、预留号与逐项领用量**做第一次领用:

```bash
curl -X POST http://localhost:8000/api/v1/consumptions \
  -H 'Content-Type: application/json' \
  -d '{
    "consumption_no": "CV-20260923-001",
    "reservation_no": "RSV-20260922-001",
    "operator": "吴九",
    "note": "实验组第一批领用",
    "items": [
      {"sample_id": "S0", "amount": 12},
      {"sample_id": "A1", "amount": 4}
    ]
  }'
```

服务在**单个事务**内校验预留未释放、逐项领用量不超过该明细未核销量且样品余量足够;任一失败整单不变。全部通过后扣减余量、版本 +1 并写不可变核销明细,响应逐项给出锁定量、本次前后的累计已核销与剩余锁定、核销后的余量/锁定/可用量及前后版本:

```json
{
  "consumption_no": "CV-20260923-001",
  "reservation_no": "RSV-20260922-001",
  "status": "active",
  "items": [
    {"sample_id": "S0", "amount": 12.0, "unit": "mL",
     "locked_amount": 30.0, "consumed_before": 0.0, "consumed_total": 12.0,
     "remaining_locked": 18.0,
     "remaining": 88.0, "reserved": 18.0, "available": 70.0,
     "from_version": 2, "to_version": 3},
    {"sample_id": "A1", "amount": 4.0, "unit": "mL",
     "locked_amount": 10.0, "consumed_before": 0.0, "consumed_total": 4.0,
     "remaining_locked": 6.0,
     "remaining": 56.0, "reserved": 6.0, "available": 50.0,
     "from_version": 1, "to_version": 2}
  ],
  "operator": "吴九",
  "replayed": false
}
```

注意核销只把**未核销部分**继续计入锁定:S0 余量 100→88、锁定 30→18,可用量保持 70 不变。用同一核销号**完全相同**的请求体重放返回首次结果且 `replayed: true`,不会重复扣减;同号不同内容返回 `409 consumption_no_conflict`。

后续可继续分次核销(如再领 S0 18、A1 6 即把预留全部核销,锁定归零);任一次中某明细领用量超过剩余未核销量(或样品余量不足)时返回 `409`(`insufficient_unconsumed` 附 `requested` 本次领用量/`available` 未核销量,或 `insufficient_balance`),`conflicts[]` 一次列出全部问题明细,**整单回滚**。核销后:

- `GET /api/v1/reservations/RSV-20260922-001` 的明细附 `consumed`(累计已核销)与 `remaining_locked`(剩余锁定);
- `DELETE /api/v1/reservations/RSV-20260922-001` 只解除剩余未核销锁定(上例中若已领 S0 12,则仅归还 18,已领用的 12 不回库);
- `GET /api/v1/checks/balances` 仍一致,各样品的 `consumed` 字段给出核销出账合计;
- `GET /api/v1/consumptions/CV-20260923-001` 可取回首次核销结果,不存在的核销号返回 `404`。

### 12. 盘点:快照 → 提交实盘 → 确认调账

**第一步:按样品编号集合建批**(服务端在同一事务保存每个样品的系统余量与版本快照):

```bash
curl -X POST http://localhost:8000/api/v1/stocktakes \
  -H 'Content-Type: application/json' \
  -d '{
    "stocktake_no": "STK-20260923-001",
    "note": "9月月度盘点",
    "operator": "赵五",
    "items": [{"sample_id": "S0"}, {"sample_id": "A1"}]
  }'
```

同批次号 + 同请求体重放返回相同内容且 `replayed: true`;同号不同内容返回 `409 stocktake_no_conflict`。任一样品不存在则整批不建(`409 sample_not_found`)。

**第二步:提交实盘结果**(逐项给出实盘量,允许盘亏到 0):

```bash
curl -X POST http://localhost:8000/api/v1/stocktakes/STK-20260923-001/submit \
  -H 'Content-Type: application/json' \
  -d '{
    "operator": "钱六",
    "items": [
      {"sample_id": "S0", "counted_amount": 9.5},
      {"sample_id": "A1", "counted_amount": 58.0}
    ]
  }'
```

服务逐项校验样品仍存在、且数据库余量自快照以来未被其他操作改变,然后返回差异 `difference = 实盘量 − 提交时系统量`(如 S0 快照 10、实盘 9.5,差异 −0.5)。明细必须与批次逐项一致(不重不漏),否则 `409 stocktake_items_mismatch`;余量已被改变时返回 `409 stocktake_version_conflict`(附快照量 `requested` 与当前量 `available`),批次保持 `open`,需重新建批。

**第三步:确认盘点**(事务内再次校验版本后调账;操作者与原因必填):

```bash
curl -X POST http://localhost:8000/api/v1/stocktakes/STK-20260923-001/confirm \
  -H 'Content-Type: application/json' \
  -d '{"operator": "张库管", "reason": "月度盘点差异调整:蒸发损耗与读数偏差"}'
```

确认在单个事务内对每一项**再次**校验版本仍等于提交时版本;全部通过才逐项写入 `stocktake_adjustments`(系统量、实盘量、调整量、单位、操作者、原因、前后版本)并把样品余量更新为实盘量。若提交→确认之间任一样品又被其他操作改动,返回 `409 stocktake_version_conflict` 且**整批回滚,库存一项都不改**。响应含每样品的 `delta_applied` 与全部调整流水:

```json
{
  "stocktake_no": "STK-20260923-001",
  "status": "confirmed",
  "items": [
    {"sample_id": "S0", "unit": "mL", "snapshot_remaining": 10.0,
     "counted_amount": 9.5, "difference": -0.5, "snapshot_version": 0,
     "current_remaining": 9.5, "current_version": 1,
     "version_drift": false, "delta_applied": -0.5}
  ],
  "adjustments": [
    {"sample_id": "S0", "system_amount": 10.0, "counted_amount": 9.5,
     "delta": -0.5, "operator": "张库管",
     "reason": "月度盘点差异调整:蒸发损耗与读数偏差",
     "from_version": 0, "to_version": 1}
  ],
  "replayed": false
}
```

**查询批次**(`GET /api/v1/stocktakes/STK-20260923-001`)在任意阶段返回快照、实盘量与差异、确认状态,以及每样品查询时刻的余量/版本和 `version_drift`(快照后是否已被其他操作改变);已确认时还返回调整流水。盘点调整纳入余量账本,`/api/v1/checks/balances` 的 `adjustments` 字段给出各样品调整合计。

### 13. 销毁/报废:多明细单事务提交

一次提交多个样品的销毁量与原因(每项 `reason` 必填),服务按**可用量(余量 − 未释放预留)**逐项校验:

```bash
curl -X POST http://localhost:8000/api/v1/operations/dispose \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "dispose",
    "op_no": "DSP-20260923-001",
    "operator": "孙七",
    "location": "危废暂存柜",
    "items": [
      {"sample_id": "S0", "amount": 5,  "reason": "交叉污染"},
      {"sample_id": "A2", "amount": 10, "reason": "过期报废"}
    ]
  }'
```

全部明细可用量足够才提交:逐项出库并写不可变销毁审计记录、余量扣减且版本 +1,响应给出每样品销毁后的余量/已预留/可用量与前后版本:

```json
{
  "op_no": "DSP-20260923-001",
  "type": "dispose",
  "samples_created": [],
  "edges": [],
  "remaining": {"S0": 5.0, "A2": 20.0},
  "disposed": [
    {"sample_id": "S0", "amount": 5.0, "unit": "mL", "reason": "交叉污染",
     "remaining": 5.0, "reserved": 0.0, "available": 5.0,
     "from_version": 2, "to_version": 3},
    {"sample_id": "A2", "amount": 10.0, "unit": "mL", "reason": "过期报废",
     "remaining": 20.0, "reserved": 0.0, "available": 20.0,
     "from_version": 1, "to_version": 2}
  ],
  "operator": "孙七",
  "replayed": false
}
```

任一样品可用量不足(或不存在)时返回 `409`,`conflicts[]` 中**一次列出全部问题明细**(可含多条 `insufficient_available` 与 `sample_not_found`),整单回滚、余量与审计均不变。同一操作号用相同请求体重放返回首次结果且 `replayed: true`;同号不同内容返回 `409 op_no_conflict`。销毁同样支持 `/api/v1/batch`(可与取样/分装/合并混批,任一冲突整批回滚)、`/api/v1/validate` 与 `/api/v1/batch/validate`(不写库,冲突带 `batch_index`)。销毁后 `/api/v1/checks/balances` 仍为一致(销毁量计入各样品 `total_out`)。

### 14. 位置交接:多样品原位置校验、整批原子移动

一次提交多个样品的原位置与目标位置(交接人必填)。服务在**单个事务**内逐项校验样品仍处于请求所述原位置:

```bash
curl -X POST http://localhost:8000/api/v1/transfers \
  -H 'Content-Type: application/json' \
  -d '{
    "transfer_no": "TRF-20260923-001",
    "operator": "周八",
    "note": "冷库A搬迁至冷库B",
    "items": [
      {"sample_id": "S0", "from_location": "冷库A-1", "to_location": "冷库B-1"},
      {"sample_id": "A1", "from_location": "冷库A-2", "to_location": "冷库B-2"}
    ]
  }'
```

全部样品都仍在声明的原位置时才提交:统一更新各样品位置并逐项记录交接人、交接时间与变更前后位置:

```json
{
  "transfer_no": "TRF-20260923-001",
  "operator": "周八",
  "note": "冷库A搬迁至冷库B",
  "items": [
    {"sample_id": "S0", "from_location": "冷库A-1", "to_location": "冷库B-1"},
    {"sample_id": "A1", "from_location": "冷库A-2", "to_location": "冷库B-2"}
  ],
  "handover_at": "2026-09-23T02:10:33.518Z",
  "replayed": false
}
```

若其中某样品已被其他交接移动(或不存在),返回 `409 location_mismatch`(或 `sample_not_found`),冲突体同时给出请求原位置 `requested_location` 与样品当前实际位置 `available_location`;**整批回滚,同批次内其他样品的位置也一律不动**,且不写任何交接记录:

```json
{
  "error": "conflict",
  "conflicts": [{
    "code": "location_mismatch",
    "message": "样品 A1 当前实际位置「冷库B-2」与请求所述原位置「冷库A-2」不符,整批拒绝交接",
    "op_no": "TRF-20260923-002",
    "sample_id": "A1",
    "requested_location": "冷库A-2",
    "available_location": "冷库B-2"
  }]
}
```

同一交接单号用**完全相同**的请求体重放返回首次结果且 `replayed: true`(即使样品此后又被移动,也只回放原结果、不会再次移动);同号不同内容返回 `409 transfer_no_conflict`。

**按样品查询交接历史**(最新在前,并给出样品当前位置):

```bash
curl http://localhost:8000/api/v1/samples/S0/transfers
```

```json
{
  "sample_id": "S0",
  "current_location": "冷库B-1",
  "history": [
    {"transfer_no": "TRF-20260923-001", "operator": "周八",
     "note": "冷库A搬迁至冷库B",
     "from_location": "冷库A-1", "to_location": "冷库B-1",
     "handover_at": "2026-09-23T02:10:33.518Z"}
  ]
}
```

样品不存在时该历史接口返回 HTTP `404`。交接只改位置、不动余量,交接后 `/api/v1/checks/balances` 仍保持一致;交接审计写入不可变表 `location_transfers` / `location_transfer_items`,新表随容器启动自动创建,数据保存在 Docker 卷 `sample-data` 中。

## 冲突码

| code | 含义 |
| --- | --- |
| `insufficient_balance` | 父样品/输入余量不足,附 `requested`/`available` |
| `cycle_detected` | 合并会形成谱系环路,附 `cycle_path` |
| `sample_not_found` | 引用了不存在的样品 |
| `sample_already_exists` | 取样/分装目标样品已存在 |
| `unit_mismatch` | 操作单位与样品单位不一致 |
| `self_merge` | 样品合并到自身 |
| `output_amount_mismatch` | 守恒校验:期望输出量与输入合计不符 |
| `material_required` | 新建合并产物缺少 `material` |
| `op_no_conflict` | 操作号已存在但请求内容不同(幂等键冲突) |
| `duplicate_op_no_in_batch` | 同一批次内操作号重复,附 `batch_index` |
| `balance_inconsistent` | 余量账本重算与冗余余量不一致 |
| `insufficient_available` | 预留/销毁可用量不足(当前余量−未释放预留的未核销部分),附 `requested`/`available` |
| `reservation_no_conflict` | 预留号已存在但请求内容不同 |
| `reservation_closed` | 预留号已释放,不能再次创建或核销领用 |
| `reservation_not_found` | 预留号不存在(查询/释放/核销时返回 HTTP 404) |
| `consumption_no_conflict` | 核销号已存在但请求内容不同(幂等键冲突) |
| `reservation_item_not_found` | 核销明细中的样品不属于该预留单 |
| `insufficient_unconsumed` | 核销量超过该预留明细的未核销量,附 `requested`(本次领用量)/`available`(剩余未核销量) |
| `consumption_not_found` | 核销号不存在(查询时返回 HTTP 404) |
| `stocktake_no_conflict` | 盘点批次号已存在但建批请求内容不同 |
| `stocktake_result_conflict` | 批次已提交过不同的实盘结果(幂等键冲突) |
| `stocktake_items_mismatch` | 实盘明细与批次快照不一致(缺少样品/出现批次外样品) |
| `stocktake_version_conflict` | 样品数据库余量自快照/提交后已被其他操作改变(乐观锁版本不一致) |
| `stocktake_not_submitted` | 批次尚未提交实盘结果,不能确认 |
| `stocktake_closed` | 批次已确认,不能再次提交实盘结果 |
| `stocktake_already_confirmed` | 批次已确认且操作者/原因与本次确认请求不同 |
| `stocktake_not_found` | 盘点批次不存在(查询/提交/确认时返回 HTTP 404) |
| `location_mismatch` | 样品当前实际位置与请求所述原位置不符,附 `requested_location`/`available_location` |
| `transfer_no_conflict` | 交接单号已存在但请求内容不同(幂等键冲突) |

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 项目结构

```
app/
  config.py        # 数据库路径、容差等配置
  db.py            # SQLite 连接 / 建表 / 增量迁移(WAL、外键、CHECK 约束、预留/核销/盘点/销毁/位置交接表)
  schemas.py       # Pydantic 请求与响应模型(含冲突结构、预留、核销领用、盘点、销毁与位置交接模型)
  service.py       # 核心:事务提交、余量/可用量/环路校验、销毁审计、幂等回放、dry-run、递归回溯、账本审计
  reservations.py  # 预留:单事务多样品锁定、幂等重放、整单释放、可用量查询、事件审计(锁定量只计未核销部分)
  consumptions.py  # 核销领用:单事务校验预留未释放/逐项未核销量/样品余量,扣减余量、递增版本、不可变明细,幂等重放
  stocktakes.py    # 盘点:快照建批、提交版本校验与差异、确认二次校验后整批原子调账
  transfers.py     # 位置交接:单事务多样品原位置校验、整批原子移动、不可变审计、幂等重放、按样品查历史
  main.py          # FastAPI 路由
tests/
  test_api.py          # 操作链路端到端测试(TestClient)
  test_disposals.py    # 销毁/报废:可用量校验、整单回滚、不可变审计、幂等、批量与 dry-run
  test_reservations.py # 预留模块端到端/并发/审计测试
  test_consumptions.py # 核销领用:分次核销、整单回滚、版本与锁定量口径、释放、账本一致、不可变审计、并发与旧库建表
  test_stocktakes.py   # 盘点工作流端到端/并发确认/旧库迁移测试
  test_transfers.py    # 位置交接:原位置校验整批回滚、幂等、历史查询、并发串行化、不可变审计、旧库建表
Dockerfile
docker-compose.yml
```
