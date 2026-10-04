-- 冷链追责服务 数据库初始化脚本
-- 由 PostgreSQL 官方镜像在数据卷首次创建时自动执行（/docker-entrypoint-initdb.d）。
-- 应用启动时也会执行 CREATE TABLE IF NOT EXISTS 作为兜底。

CREATE TYPE event_type AS ENUM ('measurement', 'handover');
CREATE TYPE stance_kind AS ENUM ('confirm', 'dispute');
CREATE TYPE interval_source AS ENUM ('own', 'vehicle');
CREATE TYPE correction_target AS ENUM ('own_event', 'vehicle_reading');

CREATE TABLE IF NOT EXISTS cargo (
    cargo_id          VARCHAR(64)   PRIMARY KEY,
    started_at        TIMESTAMPTZ   NOT NULL,
    temp_upper_limit  NUMERIC(10,3) NOT NULL,
    initial_holder    VARCHAR(128)  NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS event (
    event_id     VARCHAR(64)   PRIMARY KEY,
    cargo_id     VARCHAR(64)   NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    event_type   event_type    NOT NULL,
    occurred_at  TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3),
    giver        VARCHAR(128),
    receiver     VARCHAR(128),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 事件形状约束：测温只有温度；交接只有交出方/接收方且双方不同
    CONSTRAINT chk_event_shape CHECK (
        (event_type = 'measurement'
            AND temperature IS NOT NULL
            AND giver IS NULL
            AND receiver IS NULL)
        OR
        (event_type = 'handover'
            AND temperature IS NULL
            AND giver IS NOT NULL
            AND receiver IS NOT NULL
            AND giver <> receiver)
    )
);

CREATE INDEX IF NOT EXISTS ix_event_cargo_time
    ON event (cargo_id, occurred_at);

-- 同一货物同一时刻至多一笔测温
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_measurement
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'measurement';

-- 同一货物同一时刻至多一笔交接
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_handover
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'handover';

-- 冷链车次（一个车次可承载多票货物）
-- revision：车次修订号（初始 0），该车次的装卸登记 / 车载读数写入 /
-- 车载读数更正在同一事务内使其 +1，供装卸前预检的乐观并发校验与摘要绑定
CREATE TABLE IF NOT EXISTS trip (
    trip_id      VARCHAR(64)   PRIMARY KEY,
    vehicle_no   VARCHAR(64)   NOT NULL,
    note         VARCHAR(256),
    revision     INTEGER       NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 一票货物在一个车次上的装卸窗口。
-- 稳定身份（更正永不改变）：(trip_id, cargo_id, origin_loaded_at)——车次、货物与
-- 首次登记装车时刻；loaded_at/unloaded_at 保存“当前有效窗口”（更正就地更新），
-- origin_* 保存首次登记的原始窗口（append-only 留痕），window_version 为更正
-- 版本（0=原始，乐观锁）。当前窗口仍为左闭右开 [loaded_at, unloaded_at)。
CREATE TABLE IF NOT EXISTS loading (
    trip_id             VARCHAR(64)   NOT NULL
                          REFERENCES trip(trip_id) ON DELETE RESTRICT,
    cargo_id            VARCHAR(64)   NOT NULL
                          REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    origin_loaded_at    TIMESTAMPTZ   NOT NULL,   -- 稳定身份：首次登记装车时刻
    loaded_at           TIMESTAMPTZ   NOT NULL,   -- 当前窗口起点（含）
    unloaded_at         TIMESTAMPTZ   NOT NULL,   -- 当前窗口终点（不含）
    origin_unloaded_at  TIMESTAMPTZ   NOT NULL,   -- 首次登记卸车时刻（留痕）
    window_version      INTEGER       NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 当前窗口与原始窗口都左闭右开且起点严格早于终点
    CONSTRAINT chk_loading_window CHECK (loaded_at < unloaded_at),
    CONSTRAINT chk_loading_origin_window CHECK (origin_loaded_at < origin_unloaded_at),
    PRIMARY KEY (trip_id, cargo_id, origin_loaded_at)
);

CREATE INDEX IF NOT EXISTS ix_loading_cargo
    ON loading (cargo_id, loaded_at, unloaded_at);

-- 车载测温原始读数（仅对读数时刻仍在车上的货物按其各自上限判断越温；
-- 是否被某货物采用——同刻有自有测温时被取代——在合并时间线时计算）
CREATE TABLE IF NOT EXISTS vehicle_reading (
    reading_id   VARCHAR(64)   PRIMARY KEY,
    trip_id      VARCHAR(64)   NOT NULL
                     REFERENCES trip(trip_id) ON DELETE RESTRICT,
    read_at      TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3) NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 同车次同一时刻至多一笔车载读数
CREATE UNIQUE INDEX IF NOT EXISTS ux_vehicle_reading_trip_time
    ON vehicle_reading (trip_id, read_at);

CREATE INDEX IF NOT EXISTS ix_vehicle_reading_trip
    ON vehicle_reading (trip_id, read_at);

-- 责任确认表态（只追加：改口产生新编号，历次表态永久保留）
CREATE TABLE IF NOT EXISTS responsibility_stance (
    stance_id           VARCHAR(64)   PRIMARY KEY,
    cargo_id            VARCHAR(64)   NOT NULL
                          REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    -- 稳定区间标识：自有开启=测温事件ID；车载开启=veh:<读数ID>
    interval_key        VARCHAR(68)   NOT NULL,
    -- 开启读数来源（与下方两组坐标互斥对应）
    opener_source       interval_source NOT NULL,
    start_event_id      VARCHAR(64),   -- opener_source='own' 时有值
    vehicle_reading_id  VARCHAR(64),   -- opener_source='vehicle' 时有值
    responsible_party   VARCHAR(128)  NOT NULL,
    kind                stance_kind   NOT NULL,
    reason              VARCHAR(512),
    -- 提交时校验通过的快照标识（sha256:<hex>）；区间/片段变化后与当前快照不再一致
    snapshot_id         VARCHAR(71)   NOT NULL,
    -- 表态时该责任方承担的片段坐标 JSON（[["起","止","责任方"],...]），
    -- 晚到事件/读数重建后逐片段判定表态是否仍有效
    attested_segments   VARCHAR(2000) NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 表态形状：确认不得带理由；异议必须带非空理由
    CONSTRAINT chk_stance_shape CHECK (
        (kind = 'confirm' AND reason IS NULL)
        OR
        (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)
    ),

    -- 开启坐标二选一：自有事件 或 车载读数
    CONSTRAINT chk_stance_opener CHECK (
        (opener_source = 'own'
            AND start_event_id IS NOT NULL
            AND vehicle_reading_id IS NULL)
        OR
        (opener_source = 'vehicle'
            AND vehicle_reading_id IS NOT NULL
            AND start_event_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_stance_cargo_interval
    ON responsibility_stance (cargo_id, interval_key);

-- 测温值更正（append-only：原始测温/读数永不修改，有效温度取最新更正）。
-- 只改温度，时间、归属与原编号不变；交接不可更正（应用侧拒绝，无目标行）。
CREATE TABLE IF NOT EXISTS temperature_correction (
    correction_id       VARCHAR(64)   PRIMARY KEY,
    target_type         correction_target NOT NULL,
    -- own_event 目标坐标
    cargo_id            VARCHAR(64),
    target_event_id     VARCHAR(64),
    -- vehicle_reading 目标坐标
    target_reading_id   VARCHAR(64),
    trip_id             VARCHAR(64),
    -- 乐观锁：提交时目标的当前更正版本（首次为 0）；更正后的版本（=期望值+1）
    expected_version    INTEGER       NOT NULL,
    version             INTEGER       NOT NULL,
    new_temperature     NUMERIC(10,3) NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT chk_correction_shape CHECK (
        new_temperature IS NOT NULL
        AND expected_version >= 0
        AND version >= 1
    ),

    -- 目标坐标二选一：自有测温事件（无读数/车次坐标）或车载读数（无货物/事件坐标）
    CONSTRAINT chk_correction_target CHECK (
        (target_type = 'own_event'
            AND target_event_id IS NOT NULL
            AND target_reading_id IS NULL
            AND trip_id IS NULL)
        OR
        (target_type = 'vehicle_reading'
            AND target_reading_id IS NOT NULL
            AND trip_id IS NOT NULL
            AND target_event_id IS NULL
            AND cargo_id IS NULL)
    )
);

-- 每个目标下版本号唯一（应用侧在货物行锁 / 车次行锁内按序号分配）
CREATE UNIQUE INDEX IF NOT EXISTS ux_correction_event_version
    ON temperature_correction (target_event_id, version);

CREATE UNIQUE INDEX IF NOT EXISTS ux_correction_reading_version
    ON temperature_correction (target_reading_id, version);

CREATE INDEX IF NOT EXISTS ix_correction_event
    ON temperature_correction (target_event_id, created_at, correction_id);

CREATE INDEX IF NOT EXISTS ix_correction_reading
    ON temperature_correction (target_reading_id, created_at, correction_id);

-- 装卸时段更正（append-only：原始窗口留痕，当前窗口在 loading 表就地更新）。
-- 只改一条装卸窗口的时间，车次/货物归属与稳定身份（origin_loaded_at）不变。
CREATE TABLE IF NOT EXISTS loading_correction (
    correction_id          VARCHAR(64)   PRIMARY KEY,
    -- 被更正窗口的稳定身份（车次 + 货物 + 首次登记装车时刻，不可变）
    trip_id                VARCHAR(64)   NOT NULL,
    cargo_id               VARCHAR(64)   NOT NULL,
    origin_loaded_at       TIMESTAMPTZ   NOT NULL,
    -- 首次登记时的原始窗口（历次更正一致，仅留痕）
    origin_unloaded_at     TIMESTAMPTZ   NOT NULL,
    -- 更正前一刻的窗口（本次更正所改的旧窗口）
    previous_loaded_at     TIMESTAMPTZ   NOT NULL,
    previous_unloaded_at   TIMESTAMPTZ   NOT NULL,
    -- 更正后的新窗口
    new_loaded_at          TIMESTAMPTZ   NOT NULL,
    new_unloaded_at        TIMESTAMPTZ   NOT NULL,
    -- 乐观锁：提交时该窗口的当前更正版本（首次为 0）；更正后的版本（=期望值+1）
    expected_version       INTEGER       NOT NULL,
    version                INTEGER       NOT NULL,
    reason                 VARCHAR(512)  NOT NULL,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT chk_loading_correction_shape CHECK (
        expected_version >= 0
        AND version >= 1
        AND new_loaded_at < new_unloaded_at
        AND origin_loaded_at < origin_unloaded_at
        AND char_length(reason) > 0
    )
);

-- 每个被更正窗口下版本号唯一（应用侧在货物行锁 + 车次行锁内按序号分配）
CREATE UNIQUE INDEX IF NOT EXISTS ux_loading_correction_window_version
    ON loading_correction (trip_id, cargo_id, origin_loaded_at, version);

CREATE INDEX IF NOT EXISTS ix_loading_correction_window
    ON loading_correction (trip_id, cargo_id, origin_loaded_at, created_at);

-- 责任证据封存（append-only：封存内容此后不可改）。
-- 调查人员凭案件编号（主键）、货物编号、已闭合区间稳定标识与当前快照标识封存；
-- content 为封存时刻的规范化 JSON 原文，content_digest 为其 SHA-256 摘要；
-- request_hash 为（货物/区间/快照）请求指纹：同案件编号同请求重试即使现况
-- 已变也返回原包，异请求冲突。区间消失/重开/快照已变拒绝且不留记录。
CREATE TABLE IF NOT EXISTS evidence_seal (
    case_id        VARCHAR(128) PRIMARY KEY,
    cargo_id       VARCHAR(64)  NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    interval_key   VARCHAR(68)  NOT NULL,
    snapshot_id    VARCHAR(71)  NOT NULL,
    request_hash   VARCHAR(71)  NOT NULL,
    content        TEXT         NOT NULL,
    content_digest VARCHAR(71)  NOT NULL,
    sealed_at      TIMESTAMPTZ  NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_evidence_seal_cargo_interval
    ON evidence_seal (cargo_id, interval_key);

-- 车次探头故障登记（append-only：撤销不删不改，另以撤销行标记）。
-- 质控人员凭故障编号、车次、带时区起止时刻与原因登记左闭右开故障时段；
-- 任一生效（未撤销）故障覆盖的车载读数不参与货物越温重建（晚到读数同样
-- 隔离）。version 为故障版本：0=已登记未撤销，撤销成功后为 1（乐观锁）。
-- 故障登记 / 撤销成功在同事务使车次修订号 +1（旧装卸预检摘要失效）。
CREATE TABLE IF NOT EXISTS probe_fault (
    fault_id     VARCHAR(64)   PRIMARY KEY,
    trip_id      VARCHAR(64)   NOT NULL
                   REFERENCES trip(trip_id) ON DELETE RESTRICT,
    fault_start  TIMESTAMPTZ   NOT NULL,   -- 故障时段起点（含）
    fault_end    TIMESTAMPTZ   NOT NULL,   -- 故障时段终点（不含）
    reason       VARCHAR(512)  NOT NULL,
    version      INTEGER       NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT chk_probe_fault_shape CHECK (
        fault_start < fault_end AND char_length(reason) > 0
    )
);

CREATE INDEX IF NOT EXISTS ix_probe_fault_trip_time
    ON probe_fault (trip_id, fault_start, fault_end);

-- 车次探头故障撤销（append-only）：凭撤销编号 + 期望版本撤销一条故障；
-- 每条故障至多一条撤销（fault_id 唯一）。撤销最后一条覆盖读数的生效故障
-- 后，该读数按当前有效温度恢复参与重建（同刻自有测温仍优先）。
CREATE TABLE IF NOT EXISTS probe_fault_revocation (
    revocation_id    VARCHAR(64) PRIMARY KEY,
    fault_id         VARCHAR(64) NOT NULL UNIQUE
                       REFERENCES probe_fault(fault_id) ON DELETE RESTRICT,
    trip_id          VARCHAR(64) NOT NULL,
    expected_version INTEGER     NOT NULL,
    version          INTEGER     NOT NULL,
    revoked_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_probe_fault_revocation_shape CHECK (
        expected_version >= 0 AND version = expected_version + 1
    )
);
