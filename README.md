# 冷链货物追责服务

面向冷链货物追责的后端服务：录入货物与**自有测温 / 交接事件**、**冷链车次、每票货物的装卸时段与车载测温读数**，在**合并读数时间线**上按发生时间重建保管链，自动识别越温区间并在交接时刻拆分责任。技术栈 **Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL**。

车载测温（本轮）：同一冷链车承载多票货物时，可登记车次、每票货物的装卸时段（左闭右开）与车载探头读数；车载读数仅作用于读数时刻仍在车上的货物，并按**各自温度上限**判断越温；同刻有货物自有测温时**优先采用自有值**（车载原始读数仍可查）；交接仍只由货物自有事件改变保管方。质控接口 `GET /trips/{trip_id}/simultaneous-excursions` 按带时区查询窗返回同一车次上**多票货物同时越温**的时段（数量门槛 `min_cargo≥2`，货物集合变化即分段）。

测温值更正（本轮）：测温录错后，可凭**自有测温事件ID 或车载读数ID**（配合 `target_type` 与 `target_id`）、**更正ID**、**期望版本** `expected_version` 与**新温度**提交更正。更正**只改温度**——测温时刻、货物 / 车次归属与原始编号均不变，**交接不可更正**。原始测温 / 读数永不修改，更正只追加（append-only）；原始记录查询的 `temperature` 仍是原值，另显示 `effective_temperature` 与 `version`（0=原始，n=第 n 次更正后的有效值），原始记录及历次更正均可查。货物时间线、越温报告、责任确认与车次同时越温查询一律**按有效值重建**；同刻自有测温优先与装卸时段左闭右开规则继续适用；车载读数更正**同时影响读数时刻在车的全部货物**。原事件 / 原读数按原内容重传仍幂等；更正ID同内容重传返回原结果、异内容冲突；期望版本过期、目标不存在或目标为交接均拒绝且不落部分结果。更正与相关事件、责任表态及装卸登记经行锁串行；历史表态保留，其当前有效性按重建后的责任片段判断。

装卸前预检（本轮）：登记车次装卸时段前，可对一批待装货物做**温控兼容性预检**——输入车次、各票货物及左闭右开装卸时段与**预期车次修订号**，预检按“既有装卸 ∪ 本批拟登记”输出各重叠时段的货物集合、温度上限差异、已有车载读数覆盖与潜在越温风险，**只读不写入装卸记录**，并返回绑定车次修订号、涉及货物温度上限与车次读数版本的**预检摘要**。确认登记须携带该摘要：服务端在持锁事务内重算摘要，发现车次、货物或读数版本变化即拒绝过期摘要；通过后沿用现有批量原子校验，任一时段冲突整批不落库。车次维护单调递增的**修订号**（装卸登记 / 车载读数写入 / 车载读数更正使其 +1），车次查询接口可见。

装卸时段更正（本轮）：装卸时段录错后，调度员可凭**更正编号**、**原车次 + 货物 + 首次登记装车时刻**（被更正窗口的稳定身份）、**期望版本**、**修正后的带时区装卸时刻**与**原因**更正已有登记。更正**只改这一条装卸窗口**——车次 / 货物归属与稳定身份永不改变；**原窗口和历次更正可追溯**（append-only），车次详情展示**当前窗口**。新时段仍**左闭右开**且起点严格早于终点，重叠校验按“同一货物在**任意车次**的当前时段不得重叠”进行并**排除该条旧窗口本身**，冲突整次拒绝、不留部分变更。更正后**车载读数适用性按当前窗口重算**（恰在装车时刻纳入、卸车时刻排除），晚到读数、同刻自有测温优先与更正后有效温度规则继续适用；越温报告、责任确认、同时越温查询及车次详情一律反映当前窗口，**表态历史保留**并按重建后的责任片段判断有效性。更正与相关读数写入、测温更正、事件和表态在单事务内加锁串行、保持事务一致；成功推进**车次修订号**，使旧装卸预检摘要失效。同更正编号同请求重传返回原结果、异请求冲突；版本过期、原窗口不存在或时段非法均拒绝且不留部分变更。既有登记接口语义保持不变。

责任证据封存（本轮）：调查人员可凭**案件编号**、**货物编号**、**已闭合越温区间标识**及其**当前快照标识**封存责任证据。封存于持货物行锁的单事务内在**一致的已提交视图**上保存该区间**责任片段**、**当时有效表态**，以及重建该区间所用的**自有事件**、**车载读数及其有效温度与版本**（含同刻被自有测温取代的原始读数）、**当前装卸窗口及版本**与货物温控参数，并对全部封存内容生成 **SHA-256 内容摘要**；封存内容此后**不可改**。区间消失 / 重开或快照已变时**拒绝且不留记录**；**同案件编号同请求重试即使现况已变仍返回原包**，异请求冲突。`GET` 封存包恒返回**原内容与摘要**；对照现况接口按 `unchanged` / `changed` / `reopened` / `disappeared` 明确区间状态，并逐类列出证据项与责任片段的**增 / 删 / 变更**；后续晚到记录和测温 / 装卸更正**绝不改写原包**。既有责任确认与更正接口语义保持不变。

## 一、一键启动（Docker Compose，含数据库初始化）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/库/密码默认均为 `coldchain`） |

首次启动时，`db/init.sql` 由 PostgreSQL 官方镜像自动执行（建枚举类型、表、CHECK 约束、部分唯一索引）。应用启动时也会执行 `CREATE TABLE IF NOT EXISTS` 作为兜底。API 容器通过 `depends_on: service_healthy` 等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库数据卷（下次重新初始化）
```

## 二、本地开发（不走 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可连接的 PostgreSQL，并先执行 db/init.sql
export DATABASE_URL="postgresql+psycopg2://coldchain:coldchain@localhost:5432/coldchain"
uvicorn app.main:app --reload --port 8000
```

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain` | SQLAlchemy 数据库连接串（容器内主机名为 `db`） |
| `POSTGRES_USER` | `coldchain` | 数据库用户（compose） |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码（compose） |
| `POSTGRES_DB` | `coldchain` | 数据库名（compose） |
| `API_PORT` | `8000` | 宿主机映射的 API 端口 |
| `DB_PORT` | `5432` | 宿主机映射的数据库端口 |

也可在项目根目录放 `.env` 文件供 compose 读取。时间字段统一按 UTC 存储；请求时间建议带时区偏移（如 `+08:00`），无时区时间按 UTC 解释。

## 四、HTTP API

基址：`http://localhost:8000`。所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/cargo` | 创建货物 |
| `GET` | `/cargo` | 货物列表 |
| `GET` | `/cargo/{cargo_id}` | 货物详情 |
| `POST` | `/events` | 写入货物**自有**测温 / 交接事件 |
| `GET` | `/cargo/{cargo_id}/events` | 按发生时间列出**自有事件**（不含车载读数） |
| `GET` | `/cargo/{cargo_id}/report` | **追责报告**：越温区间（含读数来源）+ 责任片段 |
| `GET` | `/cargo/{cargo_id}/timeline` | **合并读数时间线**：自有事件 + 适用的车载读数（含被取代点） |
| `GET` | `/cargo/{cargo_id}/responsibilities` | **责任确认**：全部已闭合区间的确认状态 |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}` | 单个已闭合区间的责任确认视图 |
| `POST` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 提交责任方表态（确认 / 异议） |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 区间历次表态历史（含已改口 / 已失效） |
| `POST` | `/trips` | 登记车次 |
| `GET` | `/trips` | 车次列表（含车次修订号） |
| `GET` | `/trips/{trip_id}` | 车次详情：修订号、装卸时段、车载原始读数及对各货物的适用性 |
| `POST` | `/trips/{trip_id}/manifest` | 批量登记车次上各票货物的装卸时段（原子） |
| `POST` | `/trips/{trip_id}/manifest/precheck` | **装卸前温控兼容性预检**（只读，输出各重叠时段分析与预检摘要） |
| `POST` | `/trips/{trip_id}/manifest/confirm` | **携预检摘要确认登记**装卸时段（摘要过期拒绝；通过后批量原子校验） |
| `POST` | `/trips/{trip_id}/loading-corrections` | **装卸时段更正**（只改该条窗口；原车次+货物+原装车时刻定位，乐观版本、原因） |
| `GET` | `/trips/{trip_id}/loading-corrections` | 一条装卸窗口的**原始/当前窗口与历次更正**（按版本升序） |
| `POST` | `/evidence-seals` | **责任证据封存**（案件编号 + 货物 + 已闭合区间标识 + 当前快照标识，生成内容摘要） |
| `GET` | `/evidence-seals/{case_id}` | 查询**封存包原内容与摘要**（不可改） |
| `GET` | `/evidence-seals/{case_id}/diff` | 封存包**对照当前现况**：证据项 / 责任片段的增删变更与区间状态 |
| `POST` | `/vehicle-readings` | 写入车载测温读数（允许晚到；幂等 / 冲突） |
| `GET` | `/trips/{trip_id}/simultaneous-excursions` | **质控：同一车次多票货物同时越温时段** |
| `POST` | `/corrections` | **提交测温值更正**（只改温度；自有测温 / 车载读数） |
| `GET` | `/events/{event_id}/corrections` | 自有事件的原始测温与**历次更正**（交接为空历史） |
| `GET` | `/vehicle-readings/{reading_id}/corrections` | 车载读数的原始温度与**历次更正** |
| `GET` | `/health` | 健康检查 |

### 1. 创建货物 `POST /cargo`

```json
{
  "cargo_id": "COLD-001",
  "started_at": "2026-09-30T08:00:00+08:00",
  "temp_upper_limit": "4.0",
  "initial_holder": "北京冷库"
}
```

字段：货物编号、起始时间、温度上限（℃）、起始保管方。编号重复返回 `409 cargo_exists`。

### 2. 写入事件 `POST /events`

两种事件由请求字段区分（互斥，不能同时出现）：

测温（含 `temperature`）：

```json
{ "event_id": "evt-1", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T10:00:00+08:00", "temperature": "8.2" }
```

交接（含 `giver` 与 `receiver`）：

```json
{ "event_id": "evt-2", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T12:00:00+08:00",
  "giver": "北京冷库", "receiver": "上海冷库" }
```

每个事件都带 **事件ID、货物编号、发生时间**。

### 2.1 登记车次 `POST /trips`

```json
{ "trip_id": "TRUCK-1", "vehicle_no": "京B-冷888", "note": "京沪冷链班" }
```

车次编号重复返回 `409 trip_exists`，车次不存在返回 `404 trip_not_found`。
每个车次维护**修订号** `revision`（初始 0）：该车次的装卸登记、车载读数写入、
车载读数更正成功落库时使其 +1（失败回滚不递增）；车次列表 / 详情均返回当前
修订号，供装卸前预检的“预期车次修订号”使用。

### 2.2 登记装卸时段 `POST /trips/{trip_id}/manifest`

一个车次承载多票货物时，**批量**登记每票货物的装卸时段：

```json
{
  "items": [
    { "cargo_id": "COLD-001",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" },
    { "cargo_id": "COLD-002",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" }
  ]
}
```

* 时段**左闭右开** `[loaded_at, unloaded_at)`，且起点必须**严格早于**终点，否则
  `422 invalid_loading_window`；
* 同一货物的装卸时段不得与任意车次的既有时段重叠（首尾相接可以）——重叠即“同货物同时
  属于两个车次”，返回 `409 loading_overlap`；同批内重叠同样拒绝；
* 货物或车次不存在分别返回 `404 cargo_not_found` / `404 trip_not_found`；
* 本接口为**批量原子**操作：任一条非法整批拒绝，**不留任何部分记录**。
* 登记成功后每条窗口响应带**稳定身份** `origin_loaded_at`（首次登记装车时刻，
  之后的装卸时段更正以此定位该窗口）与 `window_version`（初始 `0`）；
  `loaded_at` / `unloaded_at` 始终表示**当前有效窗口**（见 2.10 装卸时段更正）。

### 2.3 写入车载测温读数 `POST /vehicle-readings`

```json
{
  "reading_id": "vr-1",
  "trip_id": "TRUCK-1",
  "read_at": "2026-09-30T10:00:00+08:00",
  "temperature": "7.0"
}
```

读数是车次探头的**原始读数**，写入**允许晚到**（乱序补录）。回执给出读数时刻仍在车上、
按各自温度上限受影响的货物：

```json
{ "reading": { "reading_id": "vr-1", "trip_id": "TRUCK-1",
               "read_at": "2026-09-30T02:00:00Z", "temperature": "7.000", "…": "…" },
  "affected_cargo_ids": ["COLD-001"] }
```

* 读数仅作用于读数时刻仍在**同一车次**车上的货物
  （`loaded_at ≤ read_at < unloaded_at`，左闭右开），与该货物无关的车次读数不入其时间线；
* 同车次同一时刻至多一笔读数：冲突 `409 same_timestamp_conflict`；
* 编号幂等：`reading_id` **同内容**重传返回 `200 {"replayed": true, …}`（仍给出受影响货物），
  **异内容**返回 `409 reading_id_conflict`，原记录不变；车次不存在 `404 trip_not_found`；
* 读数时刻车上无货物时读数**仍正常入库**（原始可查），仅 `affected_cargo_ids` 为空。

### 2.4 车次详情 `GET /trips/{trip_id}`

返回车次信息（含当前**修订号** `revision`）、全部装卸时段，以及车载原始读数；每笔读数除原始 `temperature` 外另给
`effective_temperature` / `version`（更正后的当前有效温度与版本），并对车次上**每票
已登记货物**给出：

| 字段 | 含义 |
| --- | --- |
| `on_board` | 读数时刻该货物是否仍在车上（左闭右开边界） |
| `adopted` | 是否被该货物越温时间线**采用**（在车上且同刻无自有测温） |
| `over_limit` | 按**该货物自身温度上限**与读数的**当前有效温度**判断是否越温；不在车上时为 `null` |
| `reason` | `adopted` 已采用 / `not_on_board` 不在车上 / `supplanted_by_own` 同刻有自有测温被取代 |

### 2.5 货物读数时间线 `GET /cargo/{cargo_id}/timeline`

返回该货物的**合并读数时间线**（按时间）：货物自有测温 / 交接，以及对其适用的车载原始读数。
每个点带 `source`（`own` / `vehicle`）、`event_id` / `reading_id` / `trip_id`、
`adopted`（车载点同刻被自有测温取代时为 `false`，原始读数仍列出）、
`changes_holder`（仅自有交接为 `true`——车载读数不改变保管方）、
`temperature`（测温点为**当前有效温度**，已含更正）与 `version`（有效温度的更正版本，
0=原始值）。

### 2.6 质控：同时越温时段 `GET /trips/{trip_id}/simultaneous-excursions`

同一车次承载多票货物时，查找**同时越温**的时段。查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `trip_id`（路径） | 是 | 车次编号；未知车次返回 `404 trip_not_found` |
| `start_at` | 是 | 查询起点（**含**），ISO 8601 且**必须带时区**（如 `2026-09-30T08:00:00+08:00`） |
| `end_at` | 是 | 查询终点（**不含**，左闭右开），同样必须带时区；不严格晚于起点返回 `422 invalid_query_window` |
| `min_cargo` | 否 | 数量门槛（正整数，**不小于 2**，缺省 `2`）；非法返回 `422 invalid_min_cargo` |

```bash
curl -G "http://localhost:8000/trips/TRUCK-1/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00" \
  --data-urlencode "min_cargo=2"
```

处理规则：

* 对每票货物按**现有合并时间线**重建越温区间（同刻自有测温优先，车载读数仅对
  在车货物按各自上限生效），再与该货物在**本车次**的装载时段（左闭右开
  `[loaded_at, unloaded_at)`）及查询窗相交；货物从上一车次带着未闭合区间装车时，
  自本车次装车时刻起计入；
* **未闭合区间**先被卸货时刻截断，再裁剪至查询终点；
* 同一次查询基于**一致的已提交数据视图**（PostgreSQL 下以 REPEATABLE READ
  快照事务执行）；晚到读数 / 事件**提交后**的下一次查询即反映新结果；
* 扫描线叠加：**货物集合变化即分段**，相邻且集合相同的片段合并，仅保留越温
  货物数达到门槛的**正长度**片段；片段按时间升序、互不重叠，货物编号按序排列。

```json
{
  "trip_id": "TRUCK-1",
  "start_at": "2026-09-29T16:00:00Z",
  "end_at": "2026-09-30T16:00:00Z",
  "min_cargo": 2,
  "segments": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T03:00:00Z",
      "cargo_count": 2,
      "cargo_ids": ["COLD-CAR-A", "COLD-CAR-Q"]
    }
  ]
}
```

无任何达门槛片段时返回 `200` 且 `segments` 为空数组。响应时间统一为 UTC（`Z`）。

### 2.7 测温值更正 `POST /corrections`

测温录错后提交**只改温度**的更正；测温时刻、货物 / 车次归属与原始编号不变，**交接不可更正**。目标由 `target_type` + `target_id` 指定：

* `target_type="own_event"`、`target_id=`自有**测温事件ID**；
* `target_type="vehicle_reading"`、`target_id=`**车载读数ID**。

```json
{
  "correction_id": "corr-1",
  "target_type": "own_event",
  "target_id": "evt-temp-1",
  "expected_version": 0,
  "new_temperature": "3.8"
}
```

* `correction_id`：客户端指定的更正编号；
* `expected_version`：提交时目标的**当前更正版本**（首次更正为 `0`，之后取上一次更正回执 / 历史查询中的 `version`）——乐观锁，不一致即拒绝；
* `new_temperature`：更正后的新温度（℃）。时间不在更正范围内，故无时间字段。

回执（`201`）给出更正记录、更正后的**有效温度与版本**，以及受影响货物：

```json
{
  "correction": {
    "correction_id": "corr-1",
    "target_type": "own_event",
    "target_id": "evt-temp-1",
    "cargo_id": "COLD-001",
    "trip_id": null,
    "expected_version": 0,
    "version": 1,
    "new_temperature": "3.800",
    "created_at": "2026-10-02T03:00:00Z"
  },
  "effective_temperature": "3.800",
  "version": 1,
  "affected_cargo_ids": ["COLD-001"]
}
```

处理规则：

* 更正**只改温度**：原测温 / 原读数行永不修改，更正只追加；每个目标维护单调版本，
  更正后的版本 = `expected_version + 1`；当前有效温度取该目标**最新一条**更正；
* **自有测温更正**：`affected_cargo_ids` 恒为该测温所属货物；
* **车载读数更正**：`correction.trip_id` 给出车次，`affected_cargo_ids` 为**读数时刻
  仍在车上的全部货物**（左闭右开 `loaded_at ≤ read_at < unloaded_at`，可能为空），
  各货物仍按**各自温度上限**重建越温；
* **同刻自有测温优先**继续适用：该时刻存在自有测温的货物，车载读数（即使已更正）
  仍标记被取代、不参与其重建；**装卸时段左闭右开**继续适用；
* 货物时间线、越温报告（含读数来源与 `veh:<读数ID>` 区间标识）、责任确认与
  车次同时越温查询全部**按有效温度重建**；区间标识仍由开启读数的原编号决定
  （自有=事件ID、车载=`veh:<读数ID>`），不因更正换号；
* **历史表态保留**：更正重建后，区间消失 / 重新开放 / 片段变化使旧表态失效，
  片段未变化的表态继续有效（表态历史中的 `is_current` 按重建结果重新判定）；
* **幂等与冲突**：
  - 原事件 / 原读数按**原内容**重传仍幂等（`temperature` 按原始录入值比对，不看更正值）；
  - `correction_id` **同内容**（目标类型、目标编号、期望版本、新温度全相同）重传
    返回 `200 {"replayed": true, …原结果…}`；**异内容** `409 correction_id_conflict`；
  - `expected_version` 与当前版本不一致 → `409 stale_version`；
  - 目标不存在：自有事件 `404 event_not_found`、车载读数 `404 reading_not_found`；
  - 目标是交接 → `422 handover_not_correctable`；`target_type` 非法 / 期望版本为负
    → `422 bad_correction_shape`；
  - 以上失败**一律不写任何部分结果**。
* **串行**：更正与相关写入在单事务内加锁串行——自有更正锁所属货物行；车载更正
  **先按 cargo_id 升序锁定读数时刻在车的全部货物行、再锁定车次行**（与车载读数
  写入同一锁顺序），故更正与这些货物的事件写入、责任表态、车次装卸登记 / 读数
  写入严格串行，版本校验所见必为持锁期间的最新有效温度。

### 2.8 原始记录与历次更正查询

* `GET /events/{event_id}/corrections`：返回该自有事件的原始测温（`original_temperature`）、
  当前 `effective_temperature` / `version` 与历次更正（按版本升序，append-only）；
  目标为**交接**时也可查：`version=0`、温度字段为 `null`、`corrections` 为空；
  事件不存在 → `404 event_not_found`。
* `GET /vehicle-readings/{reading_id}/corrections`：车载读数的原始温度与历次更正，
  结构同上；读数不存在 → `404 reading_not_found`。

```json
{
  "target_type": "own_event",
  "target_id": "evt-temp-1",
  "target_exists": true,
  "original_temperature": "8.200",
  "effective_temperature": "3.800",
  "version": 1,
  "corrections": [
    { "correction_id": "corr-1", "target_type": "own_event",
      "target_id": "evt-temp-1", "cargo_id": "COLD-001", "trip_id": null,
      "expected_version": 0, "version": 1,
      "new_temperature": "3.800", "created_at": "…",
      "original_temperature": "8.200" }
  ]
}
```

此外，原始记录列表 / 详情也直接携带有效值：

* `GET /cargo/{cargo_id}/events` 中每条测温事件除原始 `temperature` 外，另给
  `effective_temperature` 与 `version`（交接二者为 `null` / `0`）；
* 车次详情 `GET /trips/{trip_id}` 的每笔车载读数除原始 `temperature` 外，另给
  `effective_temperature` 与 `version`；`over_limit` 等适用性按有效温度判定；
* 货物合并时间线 `GET /cargo/{cargo_id}/timeline` 的测温点 `temperature` 为
  **当前有效温度**（含更正），并带 `version`（被取代的车载点也携带，仅供查阅）。

### 2.9 装卸前温控兼容性预检与确认

登记装卸时段前，可先对一批待装货物做**预检**（只读，不写入任何装卸记录），
再凭预检摘要**确认登记**。两步均按车次维度进行。

**第一步：预检** `POST /trips/{trip_id}/manifest/precheck`

```json
{
  "expected_trip_revision": 2,
  "items": [
    { "cargo_id": "COLD-002",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" }
  ]
}
```

* `expected_trip_revision`：调用方预期的**车次修订号**（先经 `GET /trips/{trip_id}`
  查询获得）；与车次当前修订号不一致返回 `409 stale_trip_revision`；
* 时段仍为左闭右开且起点严格早于终点，非法返回 `422 invalid_loading_window`；
* 车次 / 货物不存在分别返回 `404 trip_not_found` / `404 cargo_not_found`。

预检把“车次既有装卸 ∪ 本批拟登记时段”做扫描线叠加，输出车上货物集合恒定
的各个时段（非空集合才成段，左闭右开），逐段给出：

| 字段 | 含义 |
| --- | --- |
| `cargo_ids` / `proposed_cargo_ids` | 该时段车上货物集合 / 其中来自本批拟登记的货物 |
| `temp_limit_min` / `temp_limit_max` / `temp_limit_spread` | 集合内各票货物温度上限的最小 / 最大 / 极差（同车温控要求差异） |
| `readings` / `reading_count` | 落在该时段内的车次**既有车载读数**（有效温度 `effective_temperature` 与更正版本 `version`） |
| `risks` | **潜在越温风险**：时段内既有读数有效温度高于某票货物自身上限的（货物 × 读数）组合；`supplanted_by_own=true` 表示该货物在读数时刻有自有测温（同刻自有优先，车载读数实际被取代，仅供参考） |

响应同时返回**预检摘要** `digest`（`sha256:` 前缀）：绑定本批时段内容、车次
修订号、涉及货物（本批 ∪ 车次既有装卸）温度上限与该车次全部既有读数的
（编号、时刻、更正版本、有效温度）。预检为只读：不写入装卸记录；整次预检
基于一致的已提交数据视图（PostgreSQL 下 REPEATABLE READ 快照）。

```json
{
  "trip_id": "TRUCK-1",
  "trip_revision": 2,
  "digest": "sha256:c9426353…616f7a",
  "segments": [
    {
      "start": "2026-09-30T01:00:00Z",
      "end": "2026-09-30T10:00:00Z",
      "cargo_ids": ["COLD-001", "COLD-002"],
      "proposed_cargo_ids": ["COLD-002"],
      "cargo_count": 2,
      "temp_limit_min": "4.000",
      "temp_limit_max": "8.000",
      "temp_limit_spread": "4.000",
      "readings": [
        { "reading_id": "vr-1", "read_at": "2026-09-30T02:00:00Z",
          "effective_temperature": "7.000", "version": 0 }
      ],
      "reading_count": 1,
      "risks": [
        { "cargo_id": "COLD-001", "reading_id": "vr-1",
          "read_at": "2026-09-30T02:00:00Z",
          "effective_temperature": "7.000", "temp_upper_limit": "4.000",
          "supplanted_by_own": false }
      ]
    }
  ]
}
```

**第二步：确认登记** `POST /trips/{trip_id}/manifest/confirm`

```json
{
  "digest": "sha256:c9426353…616f7a",
  "items": [
    { "cargo_id": "COLD-002",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" }
  ]
}
```

* 服务端在**持锁事务内**（与装卸登记同一锁顺序：先货物行升序、再车次行）
  按当前数据重算预检摘要：车次修订号、涉及货物温度上限或车载读数版本在
  预检后发生变化（含本车次新增装卸 / 新增读数 / 读数更正），摘要不一致即
  返回 `409 stale_precheck_digest`，**不落任何记录**；
* 摘要对批次内容敏感：挪用到其他货物 / 时段同样视为过期；
* 摘要通过后**沿用与 `POST /trips/{trip_id}/manifest` 相同的批量原子校验**：
  任一时段非法（`422 invalid_loading_window`）、同批或跨车次重叠
  （`409 loading_overlap`）整批拒绝、不留部分记录；
* 确认成功（`201`）与直接登记一样使车次修订号 +1——同一摘要因此**只能用一次**，
  重复确认返回 `409 stale_precheck_digest`；
* 未知车次 / 货物同样返回 `404 trip_not_found` / `404 cargo_not_found`。

直接登记接口 `POST /trips/{trip_id}/manifest` 不受影响，可继续单独使用；
预检 + 确认是在其之上为“先评估兼容性、再原子落库”提供的两步流程。

### 2.10 装卸时段更正 `POST /trips/{trip_id}/loading-corrections`

装卸时段录错后更正**已有登记中的一条装卸窗口**。更正**只改这一条窗口的时间**——
车次 / 货物归属与窗口的**稳定身份**（首次登记装车时刻 `origin_loaded_at`）永不改变，
也不影响同车次其它窗口或其它货物。车次详情与各装卸窗口响应中均给出当前窗口、
稳定身份 `origin_loaded_at` 与该窗口的更正版本 `window_version`（0=原始）。

```json
{
  "correction_id": "lc-1",
  "cargo_id": "COLD-001",
  "origin_loaded_at": "2026-09-30T09:00:00+08:00",
  "expected_version": 0,
  "new_loaded_at": "2026-09-30T08:00:00+08:00",
  "new_unloaded_at": "2026-09-30T17:00:00+08:00",
  "reason": "派车单装卸时刻各录晚 1 小时"
}
```

* `correction_id`：客户端指定的更正编号（幂等 / 冲突裁决用）；
* `cargo_id` + `origin_loaded_at`：与路径上的车次共同定位**原窗口**——
  `origin_loaded_at` 是该窗口**首次登记时的装车时刻**（稳定身份，不随更正改变，
  取自车次详情该窗口的 `origin_loaded_at`）；
* `expected_version`：提交时该窗口的当前更正版本（首次更正为 `0`，随后取车次
  详情中的 `window_version`）——乐观锁，不一致即拒绝；
* `new_loaded_at` / `new_unloaded_at`：**修正后的带时区**装卸时刻（必须显式
  携带时区偏移，否则 `422 timezone_required`）；
* `reason`：更正原因，必填且非空白。

回执（`201`）给出更正记录（原始窗口、更正前窗口、新窗口、版本与原因）、更正后
该窗口的**当前窗口**与版本；`affected_cargo_ids` 恒为该窗口所属货物（单元素）：

```json
{
  "correction": {
    "correction_id": "lc-1", "trip_id": "TRUCK-1", "cargo_id": "COLD-001",
    "origin_loaded_at": "2026-09-30T01:00:00Z",
    "origin_unloaded_at": "2026-09-30T10:00:00Z",
    "previous_loaded_at": "2026-09-30T01:00:00Z",
    "previous_unloaded_at": "2026-09-30T10:00:00Z",
    "new_loaded_at": "2026-09-30T00:00:00Z",
    "new_unloaded_at": "2026-09-30T09:00:00Z",
    "expected_version": 0, "version": 1,
    "reason": "派车单装卸时刻各录晚 1 小时",
    "created_at": "2026-10-03T03:00:00Z"
  },
  "loaded_at": "2026-09-30T00:00:00Z",
  "unloaded_at": "2026-09-30T09:00:00Z",
  "origin_loaded_at": "2026-09-30T01:00:00Z",
  "origin_unloaded_at": "2026-09-30T10:00:00Z",
  "version": 1,
  "affected_cargo_ids": ["COLD-001"]
}
```

处理规则：

* **只改当前窗口、身份不变**：原始装卸记录行就地保存当前窗口
  （`loaded_at` / `unloaded_at`）与 `window_version`，稳定身份列
  `origin_loaded_at` / `origin_unloaded_at` 永不改变；每次更正只向
  `loading_correction` **追加一行**（原始窗口、更正前窗口、新窗口、版本、原因）；
* **新窗口合法性**：仍为左闭右开 `[new_loaded_at, new_unloaded_at)` 且起点严格
  早于终点，否则 `422 invalid_loading_window`；原因必填非空白；
* **重叠校验排除自身**：按“同一货物在**任意车次**的当前时段不得重叠”校验，
  **排除被更正的这条旧窗口本身**（新窗口与自身相同、或仅作平移均不会与自身
  冲突），首尾相接允许；与其它窗口（含跨车次）重叠 → `409 loading_overlap`，
  整次拒绝、不留部分变更；
* **车载读数适用性按当前窗口重算**：`loaded_at ≤ read_at < unloaded_at`
  （恰在装车时刻纳入、恰在卸车时刻排除）——窗口平移 / 伸缩后，边界两侧的车载
  读数在“在车 / 已卸货”间重新判定；晚到读数规则、同刻自有测温优先、测温更正
  后的**有效温度**规则继续适用；
* **报告 / 责任 / 质控 / 车次详情反映当前窗口**：货物时间线、越温报告（含
  `veh:<读数ID>` 区间标识）、责任确认、车次同时越温查询与车次详情一律按更正后的
  当前窗口重建；**历史表态保留**，其当前有效性按重建后的责任片段逐片段判断
  （区间消失 / 重新开放 / 片段变化即失效，未变化片段继续有效）；
* **事务一致 + 修订号**：更正与相关读数写入、测温更正、货物事件、责任表态、
  车次装卸登记在单事务内按统一锁顺序（**先货物行、再车次行**）加锁串行；
  成功时同事务把**车次修订号 +1**，使此前的装卸**预检摘要失效**；任何校验失败
  整事务回滚（当前窗口不改、不追加更正、修订号不增、不留部分变更）；
* **幂等与冲突**：
  - 同 `correction_id` **同请求**（原身份、期望版本、新窗口、原因全相同）重传
    返回 `200 {"replayed": true, …原结果…}`；
  - 同 `correction_id` **异请求** → `409 correction_id_conflict`，原记录不变；
  - `expected_version` 与当前 `window_version` 不一致 → `409 stale_window_version`；
  - 原窗口不存在（车次存在但无此稳定身份窗口）→ `404 loading_window_not_found`；
    车次不存在 → `404 trip_not_found`；
  - 新时段非法 / 缺时区 / 原因为空 → `422`（`invalid_loading_window` /
    `timezone_required`）；以上失败一律不留部分结果；
  - 既有装卸登记接口 `POST /trips/{trip_id}/manifest`（及预检 / 确认）的语义
    保持不变。

### 2.11 装卸窗口与历次更正查询 `GET /trips/{trip_id}/loading-corrections`

按稳定身份查询一条装卸窗口的**原始 / 当前窗口与历次更正**。查询参数（均必填）：

| 参数 | 说明 |
| --- | --- |
| `cargo_id` | 货物编号 |
| `origin_loaded_at` | 窗口**首次登记装车时刻**（稳定身份），ISO 8601 且**必须带时区** |

```bash
curl -G "http://localhost:8000/trips/TRUCK-1/loading-corrections" \
  --data-urlencode "cargo_id=COLD-001" \
  --data-urlencode "origin_loaded_at=2026-09-30T09:00:00+08:00"
```

返回 `origin_loaded_at` / `origin_unloaded_at`（原始窗口）、`loaded_at` /
`unloaded_at`（当前窗口）、当前 `version` 与按版本升序的历次更正 `corrections`
（每条含原始窗口、更正前窗口、新窗口、版本与原因）。车次存在但无此窗口时返回
`200` 且 `window_exists=false`、`version=0`、空更正列表；车次不存在返回
`404 trip_not_found`；缺少 `origin_loaded_at` 返回 `422 missing_parameter`，
时刻非法 / 无时区返回 `422`。

### 2.12 责任证据封存 `POST /evidence-seals`

调查人员可凭**案件编号**、**货物编号**、**已闭合越温区间标识**及其**当前
快照标识**封存责任证据。封存于持货物行锁的单事务内、在**一致的已提交视图**
上保存：

* 区间**责任片段**（区间起止、开启 / 闭合读数来源坐标、当前快照标识）；
* **当时有效表态**：封存时各责任方最新一条仍对当前快照有效的表态及汇总状态；
* 重建该区间所用的**自有事件**（不晚于区间闭合时刻的全部测温 / 交接，测温附
  当前有效温度 `effective_temperature` 与更正版本 `version`）；
* 重建该区间所用的**车载读数**（区间内对该货物在车适用的读数，左闭右开；
  附当前有效温度、更正版本，同刻有自有测温被取代的原始读数同样封存并标记
  `adopted=false`）；
* **当前装卸窗口及版本**（与区间时间范围相交的窗口，含稳定身份
  `origin_loaded_at`、当前窗口与 `window_version`）；
* 货物温控参数（编号、起始时间、温度上限、起始保管方）。

封存对上述全部内容取规范化 JSON 的 SHA-256，生成**内容摘要**
`content_digest`（`sha256:` 前缀）；封存内容（规范化原文与摘要）此后
**不可改**（append-only，任何接口都不修改 / 不删除）。

```json
{
  "case_id": "CASE-2026-001",
  "cargo_id": "COLD-001",
  "interval_id": "evt-temp-1",
  "snapshot_id": "sha256:836e6b3a…e797e4"
}
```

* `case_id`：客户端指定的案件编号（封存包主键）；
* `interval_id`：已闭合区间稳定标识（自有开启=测温事件ID；车载开启=`veh:<读数ID>`，
  先经 `GET /cargo/{cargo_id}/responsibilities` 获取）；
* `snapshot_id`：该区间**当前快照标识**（同上接口返回）。

处理规则：

* **拒绝且不留记录**：
  - 货物不存在 → `404 cargo_not_found`；区间不存在 → `404 interval_not_found`；
  - 区间仍开放 → `422 interval_open`；
  - 提交的快照标识与当前快照不一致（区间边界 / 责任片段已因晚到记录或更正
    变化）→ `409 stale_snapshot`；
    以上失败整事务回滚，**不留任何封存记录**。
* **同案件编号重试（幂等）**：同 `case_id` + 同请求（货物 / 区间 / 快照
  三元组一致）重传，**即使此后现况已变**（区间消失 / 重开 / 快照变化）也
  返回**原封存包**：`200 {"replayed": true, "seal": …原包…}`；
* **异请求冲突**：同 `case_id` + 异请求（不同货物 / 区间 / 快照）→
  `409 case_id_conflict`，原封存不变；
* 封存与同货物的事件写入、车载读数写入、责任表态、测温更正、装卸登记 /
  更正共用货物行锁，封存所见即持锁期间的最新一致视图。

回执 `201`（结构见下）：`case_id` / `cargo_id` / `interval_id` /
`snapshot_id`（封存绑定的快照）/ `sealed_at` / `content_digest` 与 `content`
（封存内容原文）。

### 2.13 查询封存包 `GET /evidence-seals/{case_id}`

按案件编号返回**原封存内容与内容摘要**：封存内容此后不可改，响应恒为封存
时刻的原文（片段、表态、自有事件、车载读数有效温度 / 版本、装卸窗口及版本
均为封存时值）。案件不存在 → `404 seal_not_found`。

```json
{
  "case_id": "CASE-2026-001",
  "cargo_id": "COLD-001",
  "interval_id": "evt-temp-1",
  "snapshot_id": "sha256:836e6b3a…",
  "content_digest": "sha256:2f1a…",
  "sealed_at": "2026-10-03T03:30:00Z",
  "content": {
    "case_id": "CASE-2026-001",
    "sealed_at": "2026-10-03T03:30:00Z",
    "cargo": { "cargo_id": "COLD-001", "started_at": "…",
               "temp_upper_limit": "4.000", "initial_holder": "北京冷库" },
    "interval": {
      "interval_id": "evt-temp-1",
      "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "start_source": "own", "end_source": "vehicle",
      "start_event_id": "evt-temp-1", "end_event_id": null,
      "start_reading_id": null, "end_reading_id": "vr-2",
      "start_trip_id": null, "end_trip_id": "TRUCK-1",
      "snapshot_id": "sha256:836e6b3a…",
      "segments": [
        { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "…T04:00:00Z", "end": "…T08:00:00Z", "responsible_party": "上海冷库" }
      ]
    },
    "current_valid_stances": [
      { "stance_id": "stance-bj-1", "responsible_party": "北京冷库",
        "stance": "confirm", "reason": null,
        "created_at": "…", "snapshot_id": "sha256:836e6b3a…",
        "segments": [ { "start": "…", "end": "…", "responsible_party": "北京冷库" } ] }
    ],
    "summary": "pending",
    "own_events": [
      { "event_id": "evt-temp-1", "event_type": "measurement",
        "occurred_at": "…", "temperature": "8.200",
        "effective_temperature": "8.200", "version": 0,
        "giver": null, "receiver": null }
    ],
    "vehicle_readings": [
      { "reading_id": "vr-2", "trip_id": "TRUCK-1", "read_at": "…",
        "temperature": "3.100", "effective_temperature": "3.100",
        "version": 0, "adopted": true }
    ],
    "loading_windows": [
      { "trip_id": "TRUCK-1", "cargo_id": "COLD-001",
        "origin_loaded_at": "…", "loaded_at": "…", "unloaded_at": "…",
        "window_version": 0 }
    ],
    "content_digest": "sha256:2f1a…"
  }
}
```

### 2.14 封存包对照现况 `GET /evidence-seals/{case_id}/diff`

把封存包与当前数据对照（**只读**，不改写封存原包），给出明确状态：

| `status` | 含义 |
| --- | --- |
| `unchanged` | 区间仍存在、已闭合且快照标识与封存时相同（证据项无增删变更） |
| `changed` | 区间仍闭合但边界 / 责任片段已变（快照不同） |
| `reopened` | 同标识区间当前**已重新开放**（`current_interval.is_open=true`、`end`/`current_snapshot_id` 为 null） |
| `disappeared` | 同标识区间当前**已消失**（`current_interval=null`） |

响应逐类列出**证据项**的增 / 删 / 变更：

* `evidence_changes.own_events`（键 `event_id`）、
  `evidence_changes.vehicle_readings`（键 `reading_id`）、
  `evidence_changes.loading_windows`（键 `trip_id|origin_loaded_at`）；
  每项给出 `change`（`added` / `removed` / `changed`）、`fields`（变更字段，
  如测温更正后为 `["effective_temperature","version"]`、窗口更正后含
  `loaded_at` / `window_version`）与 `sealed` / `current` 新旧值；
* `segment_changes`：责任片段以**片段起点**为稳定键的增 / 删 / 变更；
  区间重开时当前末段 `end=null`，区间消失时封存片段全部为 `removed`；
* 区间消失时证据对照范围自货物起始时间起、上界不限（新的开启点 / 窗口更正
  可能把适用读数带到封存起点之前），以便逐项定位消失原因。

**后续晚到记录和测温 / 装卸更正绝不改写封存原包**：它们只反映在本对照结果与
当前重建接口中；任何时候 `GET /evidence-seals/{case_id}` 与封存成功回执
（及幂等重放）返回的内容、摘要完全一致。

### 3. 追责报告 `GET /cargo/{cargo_id}/report`

```json
{
  "cargo_id": "COLD-001",
  "temp_upper_limit": "4.000",
  "current_holder": "广州冷库",
  "excursions": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "segments": [
        { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T06:00:00Z", "responsible_party": "上海冷库" },
        { "start": "2026-09-30T06:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "interval_id": "evt-temp-1",
      "start_source": "own",
      "end_source": "vehicle",
      "start_reading_id": null,
      "end_reading_id": "vr-2",
      "start_trip_id": null,
      "end_trip_id": "TRUCK-1"
    }
  ]
}
```

未闭合区间：`end` 为 `null`、`is_open` 为 `true`，最后一个责任片段的 `end` 也是 `null`（未结束）。区间与片段均按时间升序返回。

区间的读数来源字段：

* `interval_id`：区间**稳定标识**——货物自有测温开启=测温事件ID（如 `evt-temp-1`）；
  **车载读数开启=`veh:<读数ID>`**（如 `veh:vr-1`），供责任查询与表态长期引用；
* `start_source` / `end_source`：开启 / 闭合读数来源 `own`（自有测温）或 `vehicle`（车载读数）；
  开放区间无闭合来源；
* `start_reading_id` / `end_reading_id`（车载时）、`start_trip_id` / `end_trip_id`（所属车次）；
  自有测温时读数ID字段为 `null`、事件坐标见责任确认视图的 `start_event_id` / `end_event_id`。

### 4. 责任确认 `GET /cargo/{cargo_id}/responsibilities[/{interval_id}]`

围绕**已闭合**越温区间开展责任确认（开放区间不参与）。区间以**稳定区间标识** `interval_id` 定位：货物自有测温开启时等于开启测温事件ID，车载读数开启时为 `veh:<读数ID>`。响应同时给出开启 / 闭合读数来源（自有 / 车载）与事件 / 读数坐标、结束测温事件ID（自有闭合时）、责任片段、**当前快照标识**、各责任方最新一条有效表态与汇总状态。

```json
{
  "cargo_id": "COLD-001",
  "intervals": [
    {
      "interval_id": "evt-temp-1",
      "start_event_id": "evt-temp-1",
      "end_event_id": "evt-temp-2",
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "snapshot_id": "sha256:836e6b3a…e797e4",
      "segments": [
        { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "…T04:00:00Z", "end": "…T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "latest_stances": [
        {
          "stance_id": "stance-bj-1",
          "responsible_party": "北京冷库",
          "stance": "confirm",
          "reason": null,
          "created_at": "2026-09-30T09:00:00Z",
          "segments": [ { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" } ]
        }
      ],
      "summary": "pending"
    }
  ]
}
```

* **快照标识 `snapshot_id`**：`sha256:` 前缀，对「区间起止 + 责任片段序列」的规范化 JSON 取 SHA-256；边界或责任片段一变，快照标识即变。
* **汇总状态 `summary`**：`all_confirmed` 全确认（当前片段中每个责任方最新有效表态均为确认）／`has_dispute` 有异议（最新有效表态中存在异议）／`pending` 待确认（尚有责任方未表态且无异议）。
* 列表接口只返回已闭合区间；直查单个区间时，区间不存在返回 `404 interval_not_found`，区间仍开放返回 `422 interval_open`。

### 5. 提交表态 `POST /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

请求体（货物编号与区间标识在路径上）：

```json
{
  "snapshot_id": "sha256:836e6b3a…e797e4",
  "responsible_party": "上海冷库",
  "stance": "dispute",
  "reason": "该时段温控由交出方远程设定，接收方未实际接管",
  "stance_id": "stance-sh-1"
}
```

* `stance`：`confirm` 确认担责；`dispute` 异议——**异议必须同时提供非空 `reason`**，确认不得带理由。
* `responsible_party` **必须出现在当前责任片段中**，否则 `422 party_not_in_segment`。
* `snapshot_id` 必须等于提交时的当前快照标识；晚到事件导致区间边界 / 责任片段变化后旧快照被拒：`409 stale_snapshot`。
* `stance_id` 为客户端指定的表态编号：
  - **同编号 + 同内容重传**：返回 `200 {"replayed": true, "stance": …原记录…}`（**即使该表态所依据的快照已失效也返回原记录**）；
  - **同编号 + 异内容**：`409 stance_id_conflict`，原记录不变；
  - **改口**（确认⇄异议）必须使用**新表态编号**，产生一条新记录；历次表态全部保留。
* 区间不存在 `404 interval_not_found`、区间仍开放 `422 interval_open`，同样拒绝且不落库。
* 表态提交与同货物的事件写入在事务中共用货物行锁（`SELECT ... FOR UPDATE`）**串行化**：校验用的快照一定来自持锁期间重建出的最新事件链。

### 6. 表态历史 `GET /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

返回区间的历次表态（按提交时间升序），即使区间当前已不存在或重新开放也可查。每条记录附：

* `is_current`：对**当前**快照是否仍有效——晚到事件重建后，所在责任片段边界 / 责任方发生变化的旧表态为 `false`，片段未变化的继续有效；区间消失 / 重新开放时全部为 `false`；
* `superseded`：是否已被同责任方更新的有效表态改口（新编号表态）。

头部还给出 `interval_exists`、`is_open`、`current_snapshot_id`。

## 五、业务规则

1. **时间合法性**：事件发生时间早于货物起始时间 → `422 before_start`，拒绝。
2. **越温区间**：
   - 测温 **高于** 温度上限（`temp > limit`，等于上限不算越温）开启区间；
   - 区间内首次 **不超温**（`temp <= limit`）的测温在其测温时刻闭合区间；
   - 区间内重复超限测温不另开区间；区间外的不超限测温无影响。
3. **责任拆分**：区间跨越交接时，按交接时刻切分——`[区间起, 交接时刻)` 归交出方，**交接时刻起归接收方**；不产生零长度片段。
4. **晚到记录**：事件允许乱序写入。每次写入都把候选事件并入既有事件，按发生时间整体重算受影响区间；因此交接、测温可事后补录。
5. **交接合法性（整体拒绝）**：任何一笔交接的交出方必须等于其发生时刻的保管方；晚到插入若导致此前已存在的交接不再满足该条件，本次写入**整体回滚拒绝，既有结果保持不变**（`422 giver_mismatch`）。
6. **同刻冲突**：同一货物同一时刻至多一笔测温、至多一笔交接（测温与交接可同刻发生，且测温先于交接处理），冲突 → `409 same_timestamp_conflict`。
7. **幂等**：同一 `event_id` 重传：内容完全一致（时间按同一时刻归一化比较）返回 `200 {"replayed": true}` 且不重复写入；内容不一致返回 `409 event_id_conflict`，原记录不变。
8. **并发**：同一货物的写入在事务中以 `SELECT ... FOR UPDATE` 锁定货物行串行化，并由部分唯一索引 / CHECK 约束在数据库层兜底。**责任表态提交与事件写入共用同一把货物行锁**，二者对同一货物严格串行。
9. **责任确认（仅已闭合区间）**：
   - 区间标识 = 开启区间的稳定标识：货物自有测温开启=测温事件ID；**车载读数开启=`veh:<reading_id>`**；快照标识由区间起止与责任片段序列的 SHA-256 决定（与读数来源无关，只取决于时间坐标与责任方）；
   - 表态为只追加记录：同编号同内容重传返回原记录（快照失效亦然），同编号异内容冲突，改口必须用新编号且历次表态保留；
   - 新表态以当前快照校验（过期拒绝），责任方必须在当前片段中；区间不存在 / 开放均拒绝且不落库；
   - 晚到事件 / 晚到车载读数重建后：区间标识变化（开启读数被自有测温取代等）、边界或责任片段变化的旧表态失效、未变化片段上的表态继续有效，全部历史仍可查。
10. **车次与装卸时段**：同一冷链车可承载多票货物；装卸时段左闭右开 `[loaded_at, unloaded_at)` 且起点严格早于终点；同一货物的时段不得跨车次（或同批）重叠，首尾相接允许；登记为批量原子操作，任一非法整批拒绝、不留部分记录。
11. **车载读数适用性**：车载读数只作用于读数时刻仍在**同一车次**车上的货物（`loaded_at ≤ t < unloaded_at`）；同一读数对不同货物按**各自温度上限**分别判断越温，互不影响；读数不改变任何货物的保管方，保管方只由自有交接改变。
12. **读数来源优先**：同一时刻货物有自有测温时采用**自有值**，该时刻车载读数对该货物标记为“已取代”、不参与越温重建，但**原始读数仍可查**（车次详情与货物时间线均保留）。
13. **车载读数晚到与幂等**：读数允许乱序写入，写入后对全部受影响货物的时间线按时刻整体重建；`reading_id` 同内容重传幂等、异内容冲突；同车次同刻至多一笔读数。
14. **串行一致性**：车载读数写入在单事务内**先按 cargo_id 升序锁定全部受影响货物行、再锁定车次行**，与这些货物的自有事件写入、责任表态严格串行（与装卸登记、同车次读数写入也经车次行锁串行），校验与重建所见必为持锁期间的最新时间线。
15. **同时越温质控查询**：对车次上每票货物按其合并时间线重建越温区间（规则 11–13 同样适用），先与该车次装卸时段相交（装车前 / 卸货后不计，左闭右开），再与左闭右开查询窗相交；未闭合区间先后被卸货时刻与查询终点截断。货物集合变化即分段，相邻同集合片段合并，仅保留达 `min_cargo`（≥2）门槛的正长度片段。查询只读、不加写锁：PostgreSQL 下以 REPEATABLE READ 快照事务保证同次查询视图一致，晚到读数 / 事件须提交后才会被后续查询看到。
16. **测温值更正**：
    - 更正**只改温度**：凭目标（自有测温事件ID / 车载读数ID + `target_type`）、更正ID、期望版本与新温度提交；测温时刻、货物 / 车次归属与原编号不变，交接不可更正；
    - 原始测温 / 读数永不修改，更正只追加；每目标版本单调递增（首次更正后版本 1，新有效温度 = 最新更正的 `new_temperature`；无更正时有效温度 = 原始温度、版本 0）；
    - 原始记录查询的 `temperature` 恒为原值，另显示 `effective_temperature` / `version`；原始记录及历次更正可查；货物时间线、越温报告、责任确认及车次同时越温查询全部按有效值重建，区间稳定标识仍用原编号（事件ID / `veh:<读数ID>`）；
    - 同刻自有测温优先、装卸时段左闭右开规则在更正后继续适用；车载读数更正同时影响读数时刻在车的全部货物（按各自上限），同刻有自有测温被取代的货物不受其取值影响；
    - 原事件 / 读数按原内容重传仍幂等；更正ID同内容（目标 + 期望版本 + 新温度）重传返回原结果、异内容 `correction_id_conflict`；期望版本过期 `stale_version`、目标不存在（`event_not_found` / `reading_not_found`）、目标为交接（`handover_not_correctable`）均拒绝且不落部分结果；
    - 更正与相关事件、表态及装卸登记串行：自有更正锁货物行，车载更正先按 cargo_id 升序锁在车货物行再锁车次行；历史表态保留，其当前有效性按重建后的责任片段逐片段判断（区间消失 / 开放 / 片段变化即失效，未变化片段继续有效）。
17. **装卸前预检与确认**：
    - 车次维护单调递增的**修订号**（初始 0）：该车次的装卸登记、车载读数写入、车载读数更正、**装卸时段更正**在持车次行锁的同一事务内使其 +1（失败回滚不递增）；车次列表 / 详情返回当前修订号；
    - 预检只读：输入本批货物与左闭右开装卸时段、预期车次修订号；修订号不一致 `409 stale_trip_revision`，未知车次 / 货物 `404`，非法时段 `422`；输出按“既有装卸 ∪ 本批拟登记”叠加出的各时段货物集合、温度上限差异、既有车载读数覆盖（有效温度与版本）与潜在越温风险（同刻自有测温的货物标记 `supplanted_by_own`），不写入任何装卸记录；
    - 预检摘要绑定本批时段、车次修订号、涉及货物温度上限与车次全部既有读数的（编号、时刻、更正版本、有效温度）；确认在持锁事务内重算摘要，车次、货物或读数版本任一变化即 `409 stale_precheck_digest` 且不落库；
    - 摘要通过后沿用直接登记的批量原子校验（同批 / 跨车次重叠整批拒绝）；确认成功同样使修订号 +1，同一摘要只能用一次。
18. **装卸时段更正**：
    - 装卸时段录错后，凭更正编号、原车次 + 货物 + **首次登记装车时刻**（稳定身份 `origin_loaded_at`）、期望版本 `expected_version`、修正后的**带时区**装卸时刻与原因更正已有登记；更正只改该条装卸窗口的当前时间，车次 / 货物归属与稳定身份永不改变，也不影响其它窗口；
    - 原始窗口与历次更正 append-only 可查（`GET /trips/{trip_id}/loading-corrections`）；`loading` 行就地保存当前窗口与 `window_version`（0=原始，乐观锁），车次详情展示**当前窗口**、稳定身份与版本；
    - 新窗口仍左闭右开且起点严格早于终点（否则 `invalid_loading_window`），三个时刻必须带时区（`timezone_required`），原因必填非空白；重叠校验按“同一货物任意车次当前时段不得重叠”进行并**排除该条旧窗口本身**（首尾相接允许），冲突 `loading_overlap` 整次拒绝；
    - 更正后车载读数适用性按当前窗口重算（`loaded_at ≤ t < unloaded_at`，装车时刻纳入、卸车时刻排除）；晚到读数、同刻自有测温优先与测温更正后有效温度规则继续适用；越温报告、责任确认、同时越温查询与车次详情反映当前窗口；表态历史保留并按重建后的责任片段判断有效性；
    - 更正与相关读数写入、测温更正、货物事件、责任表态、装卸登记在单事务内按统一锁顺序（先货物行、再车次行）串行；成功使车次修订号 +1（旧装卸预检摘要随之失效），失败整事务回滚、不留部分变更（当前窗口不改、不追加更正、修订号不增）；
    - 同更正编号同请求（身份 + 期望版本 + 新窗口 + 原因全同）重传返回原结果（`replayed`）、异请求 `correction_id_conflict`；期望版本过期 `stale_window_version`、原窗口不存在 `loading_window_not_found`、车次不存在 `trip_not_found`、时段非法 / 缺时区均拒绝且不落部分结果；既有登记（含预检 / 确认）接口语义不变。
19. **责任证据封存**：
    - 调查人员凭**案件编号**（客户端指定，封存包主键）、货物编号、已闭合区间稳定标识与**当前快照标识**封存；封存于持货物行锁的单事务内、在一致的已提交视图上保存区间责任片段、当时各责任方最新有效表态与汇总状态、重建该区间所用的自有事件（不晚于闭合时刻的全部事件，测温附有效温度与版本）、车载读数（区间内在车适用，附有效温度 / 版本 / `adopted`，被同刻自有测温取代的原始读数同样封存）、与区间相交的当前装卸窗口（稳定身份 + 当前窗口 + `window_version`）及货物温控参数，并对规范化封存内容取 SHA-256 生成 `content_digest`；
    - 封存内容 append-only、此后不可改（任何接口都不修改 / 不删除）；区间不存在 `interval_not_found` / 货物不存在 `cargo_not_found` / 区间开放 `interval_open` / 快照已变 `stale_snapshot` 一律拒绝，整事务回滚、不留封存记录；
    - 同案件编号 + 同请求（货物 / 区间 / 快照三元组一致）重试**即使现况已变**（区间消失 / 重开 / 快照变化）也返回原封存包（`200 replayed`）；同案件编号 + 异请求 → `case_id_conflict`，原封存不变；
    - `GET /evidence-seals/{case_id}` 恒返回原封存内容与摘要；封存包对照现况（只读）返回 `unchanged` / `changed`（仍闭合快照不同）/ `reopened`（明确重开，当前区间 `is_open=true`）/ `disappeared`（明确消失，当前区间为 null），并以稳定键列出自有事件（`event_id`）/ 车载读数（`reading_id`）/ 装卸窗口（`trip_id|origin_loaded_at`）与责任片段（片段起点）的 added / removed / changed（含变更字段与新旧值）；
    - 后续晚到记录与测温 / 装卸更正只反映在对照结果与当前重建接口中，**绝不改写封存原包**（原包查询、封存回执与幂等重放恒一致）；封存与同货物的事件 / 读数 / 表态 / 更正 / 装卸写入经货物行锁串行；既有责任确认、表态与全部更正接口语义不变。

错误响应统一格式：`{"error": {"code": "...", "message": "..."}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cargo_not_found` / `interval_not_found` / `trip_not_found` / `event_not_found` / `reading_not_found` / `loading_window_not_found` / `seal_not_found` | 货物、已闭合区间、车次、更正目标事件或车载读数、被更正装卸窗口、封存证据包（案件编号）不存在 |
| 409 | `cargo_exists` / `trip_exists` / `event_id_conflict` / `same_timestamp_conflict` / `integrity_conflict` / `stance_id_conflict` / `stale_snapshot` / `reading_id_conflict` / `loading_overlap` / `correction_id_conflict` / `stale_version` / `stale_trip_revision` / `stale_precheck_digest` / `stale_window_version` / `case_id_conflict` | 编号或同刻冲突、表态编号冲突、快照过期（表态 / 封存）、装卸跨车次重叠、更正编号异内容 / 异请求冲突、测温更正期望版本过期、装卸窗口版本过期、预期车次修订号过期、预检摘要过期、封存案件编号异请求冲突 |
| 422 | `before_start` / `giver_mismatch` / `bad_event_shape` / `interval_open` / `party_not_in_segment` / `bad_stance_shape` / `dispute_reason_required` / `invalid_loading_window` / `reserved_event_id` / `invalid_query_window` / `timezone_required` / `invalid_min_cargo` / `missing_parameter` / `handover_not_correctable` / `bad_correction_shape` / `bad_loading_correction_shape` | 业务校验失败（整体拒绝、不落库）；`handover_not_correctable`=交接不可更正，`bad_correction_shape`=测温更正目标类型非法 / 期望版本为负，`bad_loading_correction_shape`=装卸更正定位时刻非法，其余见各项 |
| 200 | 响应体 `replayed=true` | 事件 / 表态 / 车载读数 / 测温更正同 ID 同内容幂等重传 |

## 六、调用示例

```bash
# curl 脚本（演示完整链路 + 幂等 + 报告）
bash examples/curl_examples.sh

# Python 示例（httpx）
python examples/python_client.py
```

或直接打开 http://localhost:8000/docs 使用 Swagger UI 调用。

## 七、测试

```bash
pip install pytest httpx

pytest -q                                          # 172 个用例（内存库），并发用例自动跳过
DATABASE_URL="sqlite:////tmp/cc.db" \
  pytest tests/test_concurrency.py -q              # 7 个并发串行化用例（事件 / 表态 / 车载读数 / 车载测温更正 / 自有测温更正 / 装卸时段更正 / 责任证据封存，需文件型库）
```

测试覆盖：越温开闭、跨交接责任拆分、同刻测温+交接、未闭合区间、晚到重算、交出方整体拒绝、同刻冲突、幂等重传、并发写入串行化，责任确认的快照校验、确认/异议、汇总状态、表态幂等与改口、晚到事件后表态逐片段失效与历史可查；车次登记、装卸时段左闭右开与跨车次重叠整批拒绝、车载读数仅对在车货物按各自上限越温、同刻自有测温优先（车载原始可查）、交接不被车载读数改变、区间稳定标识 `veh:<读数ID>` 与车载区间表态/失效、车载读数晚到重建、车载写入与受影响货物事件写入的并发串行化；质控同时越温查询的集合变化分段、相邻同集合合并、门槛过滤、查询窗与装卸时段裁剪（左闭右开）、未闭合区间裁至查询终点、装车前带入的开放区间自装车时刻起算、同刻自有优先、晚到读数后查询反映新结果、空结果与参数 / 未知车次错误；测温值更正的只改温度（时间 / 归属 / 原编号不变）、版本递增与乐观锁、原始记录保留原值另显示有效温度与版本、历次更正可查、自有 / 车载更正后报告 / 时间线 / 责任确认 / 同时越温按有效值重建、车载更正同时影响在车全部货物、同刻自有优先与左闭右开继续适用、交接不可更正、更正与原记录幂等重传 / 异内容冲突 / 版本过期 / 目标缺失拒绝且不落部分结果、更正后历史表态按重建片段判定有效性、车载 / 自有更正并发串行化；装卸前预检的分段分析（货物集合 / 温度上限差异 / 读数覆盖与有效值 / 潜在越温风险与同刻自有取代标记、左闭右开边界）、预检只读不落库、预期车次修订号校验、未知车次 / 货物与非法时段报错、确认携摘要（通过后沿用批量原子校验、重叠整批拒绝）、摘要过期（装卸登记 / 新增读数 / 读数更正 / 挪用到其他批次 / 重复使用均拒绝且不落库）、车次修订号随装卸登记 / 读数写入 / 读数更正单调递增且失败不递增；以及本轮装卸时段更正的只改该条窗口（车次 / 货物归属与稳定身份不变、其它窗口不动）、原窗口与历次更正 append-only 可追溯、车次详情展示当前窗口 / 稳定身份 / 版本、新窗口左闭右开与必须带时区 / 原因为空拒绝、重叠校验排除自身且跨车次 / 同车次重叠整批拒绝（首尾相接允许）、版本乐观锁（过期 `stale_window_version`）、原窗口不存在 / 车次不存在拒绝、同编号同请求幂等 / 异请求冲突、失败不留部分变更且编号可复用、车载读数适用性按当前窗口重算（装车纳入 / 卸货排除）、越温报告 / 时间线 / 责任确认 / 同时越温反映当前窗口、表态历史保留并按重建片段失效、成功推进车次修订号使旧预检摘要过期、装卸时段更正与事件写入并发串行化；以及本轮责任证据封存的封存内容（区间责任片段与来源坐标、当时有效表态与汇总、自有事件原始 / 有效温度与版本、车载读数有效温度 / 版本 / adopted、当前装卸窗口稳定身份 / 当前窗口 / 版本、货物温控、SHA-256 内容摘要与规范化原文一致）、区间不存在 / 开放 / 快照已变拒绝且不留记录、同案件编号同请求在变化 / 重开 / 消失后仍幂等返回原包、异请求 `case_id_conflict` 且原包不变、封存包查询返回原内容与摘要、现况对照四态（unchanged / changed / reopened / disappeared）与证据项（自有事件 / 车载读数 / 装卸窗口）及责任片段的增删变更（测温更正改有效温度与版本、车载读数更正使区间消失、装卸窗口更正改窗口与版本并按当前窗口重算适用性、被同刻自有测温取代读数标记 adopted=false）、晚到记录与更正绝不改写原包、车载 `veh:` 区间封存、封存与事件写入并发串行化。

## 八、目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由与异常映射
│   ├── crud.py            # 自有事件写入事务、幂等、整链校验
│   ├── correction_crud.py # 测温值更正事务（货物行锁 / 在车货物行锁→车次行锁、版本与幂等）
│   ├── loading_correction_crud.py # 装卸时段更正事务（货物行锁→车次行锁、窗口版本/重叠排除自身/幂等）
│   ├── evidence_crud.py   # 责任证据封存事务（货物行锁、快照校验、案件编号幂等/冲突、原包查询与对照）
│   ├── sealing.py         # 封存证据归集、规范化 JSON / 内容摘要、现况对照（四态与证据项/片段增删变更，纯逻辑）
│   ├── vehicle_crud.py    # 车次 / 装卸时段 / 车载读数写入事务、装前预检与确认（货物行锁→车次行锁）
│   ├── stance_crud.py     # 责任表态写入事务（与事件 / 读数共用货物行锁）
│   ├── timeline.py        # 合并读数时间线（自有事件 + 适用车载读数，同刻自有优先，含更正有效值，按当前窗口判在车）
│   ├── overlap.py         # 质控同时越温：越温区间 × 装卸时段 × 查询窗求交 + 扫描线分段（纯函数）
│   ├── precheck.py        # 装前预检：在车窗口扫描线分段、上限差异 / 读数覆盖 / 风险分析与摘要指纹（纯函数）
│   ├── chain.py           # 保管链重建 / 越温责任拆分（纯函数，含 veh: 区间标识）
│   ├── responsibility.py  # 快照标识、表态有效性、确认汇总（纯逻辑）
│   ├── models.py          # SQLAlchemy 模型（货物 / 事件 / 车次 / 装卸[当前窗口+稳定身份+版本] / 车载读数 / 表态 / 测温更正 / 装卸更正 / 责任证据封存）
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # 引擎、Session（含 SQLite 兼容）
│   └── timeutil.py        # UTC 归一化
├── db/init.sql            # PostgreSQL 初始化（枚举、表、约束、索引）
├── examples/              # curl / Python 调用示例（含车次、车载测温、同时越温查询、测温更正、装前预检确认、装卸时段更正与责任证据封存完整链路）
├── tests/                 # pytest 单元 + API + 责任确认 + 车次车载 + 同时越温 + 测温更正 + 装前预检 + 装卸时段更正 + 责任证据封存 + 并发测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
