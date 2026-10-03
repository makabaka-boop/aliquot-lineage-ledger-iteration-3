# Sample Split Service

可追溯、不超分的样本分装库存服务。Python 3.12 / FastAPI 纯后端，SQLite 文件持久化，Docker Compose 一键运行。

## 快速开始

```bash
docker compose up --build      # 服务监听 http://localhost:8000
```

本地开发（Python 3.12+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # DATABASE_PATH 环境变量可指定 SQLite 文件，默认 data/lab.db
pytest                                 # 运行全部测试（含双连接并发竞争）
```

## 数据模型与不变量

- `tubes`：**当前状态**（余额 `balance_ul`、修订号 `revision`），以及冻结的初始量 `initial_ul`——登记或分装建管时写入一次，触发器拒绝之后再改。
- `splits` / `split_children` / `lineage_edges` / `consumptions`：**历史事实**，只插入；数据库触发器拒绝任何 UPDATE/DELETE，历史不可改写。
- `idempotency_keys` / `consumption_keys`：请求键 → 规范化正文哈希 + 原始响应，与分装/耗用同事务写入；两个流程的键命名空间各自独立。
- `quarantine_records`：管级隔离记录（原因、操作标识、目标管、解除信息），只插入；触发器允许且仅允许一次“解除”更新（`released_at` 由 NULL 置位），禁止删除、改写事实或重新打开。
- 有效隔离：目标管被“自身仍生效（未解除）的记录”与“任一祖先仍生效的记录”隔离——母管被隔离时既有后代一并停止分装/耗用；解除一条记录只翻转该行，不解除同管其他记录或祖先记录。隔离与解除不触碰余额、谱系边、耗用凭证或初始量，守恒结果不变。
- 不变量：任意时刻 `Σ 所有管的余额 + Σ 已耗用量 == Σ 登记的初始量`（分装只在母管与子管之间搬运微升量，耗用则把体积永久记到凭证上）。
- 旧库升级：首次打开没有 `initial_ul` 的库时，根管按“现余额 + 直接分装出量”恢复一次初始量并冻结（旧库没有耗用，该和即登记量），子管取其谱系边创建量；不会把现余额冒充初始量。隔离表在旧库打开时无损补建，旧分装/谱系/凭证行逐行不变。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tubes` | 登记初始样本管 `{id, balance_ul}`（正整数微升，上限 2^63-1），修订号从 0 开始，初始量冻结为登记量；回执即本次创建提交的状态（余额=登记量、修订号=0），不回读库，不受并发分装影响 |
| GET | `/tubes` | 列出全部管的当前余额、修订号与初始量 |
| GET | `/tubes/{id}` | 单管当前余额、修订号、初始量、母管 id，以及隔离状态 `quarantine`（见下）；同一读事务快照内读取 |
| POST | `/splits` | 分装（见下） |
| POST | `/consumptions` | 耗用（见下） |
| POST | `/quarantines` | 隔离一支管（见下） |
| POST | `/quarantine-records/{id}/release` | 解除一条隔离记录（见下） |
| GET | `/tubes/{id}/ancestry` | 从根到该管的祖先链，每一跳附原始分装记录与该管的直接隔离记录，末尾给出按根→叶排序的生效隔离原因；整条链在单个读事务快照中读取，各级余额保证来自同一时刻，分装记录中的 `expected_revision` 标明它消费的母管修订号 |
| GET | `/tubes/{id}/splits` | 该管作为母管的全部历史分装记录 |
| GET | `/tubes/{id}/conservation` | 按根管做的守恒复核（见下） |
| GET | `/healthz` | 健康检查 |

### POST /splits

```json
{
  "parent_id": "master-001",
  "expected_revision": 0,
  "request_key": "req-42",
  "children": [{"id": "aliquot-a", "amount_ul": 300}, {"id": "aliquot-b", "amount_ul": 150}]
}
```

- `children` 1–20 支，id 在请求内唯一且不得与任何现存管重复，`amount_ul` 为正整数微升。
- 子管总量不得超过母管当前余额（可以恰好分完，母管余额归 0）。
- **单事务**：母管扣减、子管建立、谱系边、修订号 +1、幂等记录在同一事务提交，失败整体回滚。

### POST /consumptions

```json
{
  "tube_id": "aliquot-a",
  "expected_revision": 0,
  "request_key": "req-77",
  "amount_ul": 50,
  "purpose": "QC 检测"
}
```

- 不可逆“耗用凭证”：实验室从某支（子）管领走样本用于检测后，这部分体积从可继续分装的余额中永久扣除，记为不可修改的耗用事实。
- `amount_ul` 为正整数微升，不得超过管的当前余额（可以恰好耗完，余额归 0）；`purpose` 为非空用途说明。
- **单事务**：先按管当前余额裁决，再扣减、修订号 +1、追加耗用事实、写幂等记录，同一事务提交；余额不足、越界或写入异常都整体回滚，不留半条凭证，也不占请求键。
- 幂等语义与分装一致（同键同正文重放返回首次回执，异正文 409，失败不占键），与分装的请求键命名空间各自独立；耗用与分装争用同一旧修订号时最多一笔成功。

### GET /tubes/{id}/conservation

按根管做的守恒复核：在**单个读事务快照**中列出该根全部后代（含自身）的当前余额与修订号、子树内全部耗用凭证、以及根管冻结的登记初始量，并验证 `现存余额之和 + 累计耗用 == 初始量`（`conserved` 字段）。`id` 必须是根管（无母管），否则 422 `NOT_A_ROOT`。

### 管级隔离与解除

冷链复核发现母管有污染疑点时，立即把该管（及既有后代）挡在分装、耗用之外，同时保留此前真实发生的样本账。

```json
POST /quarantines
{"tube_id": "master-001", "reason": "冷链复核：温度记录断点", "operator_id": "qc-li"}
```

```json
POST /quarantine-records/3/release
{"operator_id": "qc-wang", "note": "复核阴性，放行"}
```

- 隔离记录保存**原因、操作标识、目标管**与时间；自增 `quarantine_id` 即记录标识。同一管可被多次隔离（多条记录并存）。
- **有效隔离状态** = 自身仍生效的记录 + 沿谱系向上所有祖先仍生效的记录（递归 CTE 在单次查询内算出）。母管被隔离时，隔离之前已经分出的全部后代都被挡住；隔离之后不再可能从被挡管产生新边。
- **解除一条记录只解除它自己**：同管的另一条记录、以及任何祖先记录仍然生效；已解除记录不能重新打开（409 `QUARANTINE_ALREADY_RELEASED`），记录不可删除或改写。
- `GET /tubes/{id}` 的 `quarantine` 字段给出：`is_quarantined`、`direct`（仅本管仍生效记录，按 id 排序）、`effective`（本管+祖先仍生效记录，**按根→叶**排序，同级按记录 id）。`GET /tubes/{id}/ancestry` 在每个链节点给出 `direct_quarantine`，并在顶层给出同样根→叶排序的 `effective_quarantine`；这些只读历史分装边、耗用凭证与初始量，绝不改写。
- 隔离/解除本身是单条写事务；失败整体回滚，余额、引用关系、守恒结果一律不变。
- **分装与耗用的隔离裁决在各自既有的 SQLite 写事务内完成**（`BEGIN IMMEDIATE` 取锁之后、修订号裁决之前），不存在“先检查再提交”窗口：隔离先提交，则等待写锁的分装/耗用醒来后看到记录并被 423 拒绝；隔离/解除后提交，则等待者按提交后的状态裁决。
- 幂等：已成功请求的同键重放先于隔离裁决命中存储回执，**隔离期间仍返回原回执且不重复扣减**；全新请求受当前隔离约束。
- 未隔离时，所有既有接口的请求与响应行为保持不变（`GET /tubes/{id}` 与 `/ancestry` 仅新增字段）。

### 幂等与并发

- 相同 `request_key` + 完全相同正文（JSON 语义相同，键序无关）→ 返回首次的原始结果，不重复扣减。
- 相同 `request_key` + 正文不同 → **409 REQUEST_KEY_CONFLICT**。
- 两个终端同时按同一旧修订号分装：`BEGIN IMMEDIATE` 在事务入口即取写锁，后到者看到修订号已变 → **412 REVISION_CONFLICT**，最多一笔成功。
- 失败的请求不占用请求键，修正后可用原键重发。

### 错误码

| HTTP | `error.code` | 场景 |
|---|---|---|
| 422 | `VALIDATION_ERROR` | 未知字段、非法量（0/负数/小数/字符串/超出 int64 范围）、子管数越界、请求内子管 id 重复、用途为空、隔离原因/操作标识为空等 |
| 422 | `INSUFFICIENT_BALANCE` | 子管总量超过母管余额，或耗用量超过管余额 |
| 422 | `NOT_A_ROOT` | 对非根管请求守恒复核 |
| 423 | `TUBE_QUARANTINED` | 目标管被自身或祖先的生效隔离记录挡住，不能分装/耗用；错误体附按根→叶排序的 `effective_quarantine` |
| 404 | `TUBE_NOT_FOUND` / `PARENT_NOT_FOUND` / `QUARANTINE_RECORD_NOT_FOUND` | 管或隔离记录不存在 |
| 409 | `TUBE_ALREADY_EXISTS` | 重复登记同一管 id |
| 409 | `CHILD_ID_EXISTS` | 子管 id 已被占用 |
| 409 | `REQUEST_KEY_CONFLICT` | 请求键被不同正文复用 |
| 409 | `QUARANTINE_ALREADY_RELEASED` | 隔离记录已被解除，不能重复解除 |
| 412 | `REVISION_CONFLICT` | `expected_revision` 与当前修订号不符 |

错误体统一为 `{"error": {"code", "message", ...}}`。

## 测试

`pytest` 覆盖：登记与校验（含超出 int64 范围的体积返回 422 且不落库）、分装规则（余额不足/修订冲突/子管冲突各自不同的错误码）、幂等重试（含 JSON 键序打乱的重放）、**两个独立连接经真实 uvicorn 服务竞争同一母管**（2 并发与 8 并发均恰一笔成功）、同键并发重放只执行一次、重启后状态/幂等/谱系保持、逐层分装后的总量守恒、历史表触发器拒绝改写。

耗用与守恒（`tests/test_consumption.py`、`tests/test_conservation.py`）：耗用扣减与凭证同事务、余额不足/越界/写入异常整体回滚且不占键、同键重放与异正文冲突、**耗用与分装竞争同一旧修订号最多一笔成功**、耗用历史与冻结初始量触发器拒绝改写、逐层分装后跨层级耗用的守恒复核、非根管复核拒绝、**旧库升级**（按“现余额 + 直接分装出量”恢复并冻结初始量，旧分装记录与谱系边逐行不变、旧请求键仍可重放）、迁移幂等（重启不重复恢复）、重启后守恒复核与耗用重放保持。

`tests/test_acceptance.py` 用语句闸门（`gated_server`，在真实 uvicorn + SQLite 文件上把指定 SQL 停在请求中途）确定性交错：登记 INSERT 提交后、响应生成前插入一笔分装，回执仍为修订号 0 与全额；谱系查询读到后代后依次分装后代与各级祖先，返回链仍是查询开始时的同一快照，守恒可复算、`expected_revision` 可对版，交错请求的重放返回首次结果。

管级隔离（`tests/test_quarantine.py`）：记录形态与单管/谱系查询中的直接+根→叶生效原因、**嵌套隔离**（隔母管挡住既有后代、隔后代不波及祖先与新生旁支）、**祖先解除**（解除本管记录后祖先记录仍生效、同管多条记录解除一条仍隔离）、隔离挡住分装/耗用且不改余额、边、凭证、初始量（含失败不占键、守恒不变）、隔离记录触发器（禁删禁改、仅允许一次解除、不可重开）、隔离前成功请求在隔离期间仍重放原回执而新请求 423、**重启恢复**（生效记录与解除状态跨进程保持）、旧库无损升级后可隔离、以及用语句闸门确定性验证**隔离/解除与耗用争用同一管的提交顺序**（先提交的隔离拒绝等待锁的耗用；解除先提交则等待锁的耗用成功）。

## 项目结构

```
app/
  main.py      # 路由、事务编排、异常映射（create_app 工厂）
  db.py        # 连接、建表、不可变触发器、旧库 initial_ul 迁移、隔离表
  schemas.py   # Pydantic 请求模型（extra=forbid，严格正整数）
  errors.py    # ApiError
tests/         # pytest 套件
Dockerfile     # python:3.12-slim
docker-compose.yml
```
