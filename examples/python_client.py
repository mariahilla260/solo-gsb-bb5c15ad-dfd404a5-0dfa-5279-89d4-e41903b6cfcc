"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))

        # ----- 责任确认（仅针对已闭合越温区间）-----
        resp = api.get(f"/cargo/{CARGO}/responsibilities")
        show("责任确认：已闭合区间列表", resp)
        intervals = resp.json().get("intervals", [])
        if not intervals:
            print("（没有已闭合越温区间，跳过责任确认演示）")
            return

        interval = intervals[0]
        iid = interval["interval_id"]       # 区间标识 = 开启测温事件ID
        snap = interval["snapshot_id"]      # 当前快照标识（区间起止 + 责任片段）
        print(f"区间标识={iid} 结束测温事件={interval['end_event_id']} "
              f"快照={snap} 初始汇总={interval['summary']}")

        # 起始保管方确认担责
        show(
            "北京冷库 确认",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 异议必须带理由；忘记理由 -> 422 dispute_reason_required
        show(
            "上海冷库 异议但未带理由（拒绝示例）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "stance_id": "py-stance-bad",
                },
            ),
        )

        # 合法异议
        show(
            "上海冷库 异议（带理由）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "reason": "温控参数由交出方远程设定，接收方未实际接管",
                    "stance_id": "py-stance-2",
                },
            ),
        )

        show(
            "区间责任确认视图（此时汇总 has_dispute）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # 同编号同内容重传 -> 200 replayed，返回原记录
        show(
            "表态幂等重传",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 改口：新编号，历次表态保留
        show(
            "上海冷库 改口为确认（新编号）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-3",
                },
            ),
        )
        show(
            "表态历史（历次表态，含 superseded/is_current 标记）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}/stances"),
        )
        show(
            "最终区间视图（汇总 all_confirmed）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # ==================== 车次 / 装卸时段 / 车载测温 ====================
        trip = "TRUCK-PY-1"
        cargo_a = "COLD-CAR-A"
        cargo_b = "COLD-CAR-B"
        for cid, limit in ((cargo_a, "4.0"), (cargo_b, "8.0")):
            api.post(
                "/cargo",
                json={
                    "cargo_id": cid,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": limit,
                    "initial_holder": "北京冷库",
                },
            )

        show(
            "登记车次",
            api.post("/trips", json={
                "trip_id": trip, "vehicle_no": "京B-冷888", "note": "京沪班"}),
        )

        # 批量登记装卸时段（原子：左闭右开、同货物不得跨车次重叠，否则整批拒绝）
        show(
            "批量登记装卸时段",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_a,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                    {"cargo_id": cargo_b,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                ]},
            ),
        )

        # 非法时段（起点不早于终点）-> 422，整批不留记录
        show(
            "非法装卸时段（整体拒绝）",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_a,
                     "loaded_at": "2026-09-30T19:00:00+08:00",
                     "unloaded_at": "2026-09-30T19:00:00+08:00"},
                ]},
            ),
        )

        # 车载 7℃：对上限 4℃ 的 A 越温，对上限 8℃ 的 B 不越温
        show(
            "车载读数 7℃（回执含受影响货物）",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr1", "trip_id": trip,
                      "read_at": "2026-09-30T10:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )
        # 同编号同内容重传 -> 200 replayed
        show(
            "车载读数幂等重传",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr1", "trip_id": trip,
                      "read_at": "2026-09-30T10:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )

        # 11:00：货物 A 有自有测温 3.1℃，车载 7℃ 被取代（原始仍可查）
        api.post(
            "/events",
            json={"event_id": "py-ca-own1", "cargo_id": cargo_a,
                  "occurred_at": "2026-09-30T11:00:00+08:00",
                  "temperature": "3.1"},
        )
        show(
            "同刻车载读数（对 A 被自有测温取代）",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr2", "trip_id": trip,
                      "read_at": "2026-09-30T11:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )

        # 交接仍只由货物自有事件改变保管方
        for cid, hid in ((cargo_a, "py-ca-h1"), (cargo_b, "py-cb-h1")):
            api.post(
                "/events",
                json={"event_id": hid, "cargo_id": cid,
                      "occurred_at": "2026-09-30T12:00:00+08:00",
                      "giver": "北京冷库", "receiver": "上海冷库"},
            )

        # 车载 3℃ 闭合 A 的区间
        show(
            "车载 3℃ 闭合区间",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr3", "trip_id": trip,
                      "read_at": "2026-09-30T14:00:00+08:00",
                      "temperature": "3.0"},
            ),
        )

        show("车次详情（读数 + 各货物适用性 / 采用 / 越温）", api.get(f"/trips/{trip}"))
        show("货物 A 合并读数时间线（vr2 adopted=false）",
             api.get(f"/cargo/{cargo_a}/timeline"))
        show("货物 A 追责报告（区间 veh:py-vr1，车载开启/闭合）",
             api.get(f"/cargo/{cargo_a}/report"))
        show("货物 B 追责报告（7℃ 不超其 8℃ 上限，无越温）",
             api.get(f"/cargo/{cargo_b}/report"))

        # 对车载读数开启的区间表态：区间标识 veh:<读数ID>
        ivs = api.get(f"/cargo/{cargo_a}/responsibilities").json().get("intervals", [])
        if ivs:
            viid, vsnap = ivs[0]["interval_id"], ivs[0]["snapshot_id"]
            print(f"车载区间标识={viid} 快照={vsnap}")
            show(
                "北京冷库对车载区间确认",
                api.post(
                    f"/cargo/{cargo_a}/responsibilities/{viid}/stances",
                    json={"snapshot_id": vsnap,
                          "responsible_party": "北京冷库",
                          "stance": "confirm",
                          "stance_id": "py-stance-ca-1"},
                ),
            )
            show("车载区间表态历史",
                 api.get(f"/cargo/{cargo_a}/responsibilities/{viid}/stances"))

        # ==================== 质控：同一车次同时越温查询 ====================
        # 再挂一票上限 4℃ 的货物 Q：A、Q 上限 4℃，B 上限 8℃。
        # 10:00 车载 7℃：A、Q 越温（B 不越）；11:00 A 有自有 3.1℃ 测温先闭合
        # （同刻车载 7℃ 对 A 被取代），Q 仍越温；14:00 车载 3℃ 闭合 Q。
        cargo_q = "COLD-CAR-Q"
        show(
            "创建质控演示货物 Q（上限 4℃）",
            api.post(
                "/cargo",
                json={
                    "cargo_id": cargo_q,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )
        show(
            "Q 挂到同一车次",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_q,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                ]},
            ),
        )

        show(
            "同时越温时段（默认门槛 2）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T00:00:00+08:00",
                    "end_at": "2026-10-01T00:00:00+08:00",
                },
            ),
        )
        show(
            "门槛 3（B 上限 8℃ 全程不越温 -> 空结果）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T00:00:00+08:00",
                    "end_at": "2026-10-01T00:00:00+08:00",
                    "min_cargo": 3,
                },
            ),
        )
        show(
            "查询窗裁剪（09:30-10:30）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T09:30:00+08:00",
                    "end_at": "2026-09-30T10:30:00+08:00",
                },
            ),
        )
        show(
            "非法查询窗（终点不晚于起点，422）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-10-01T00:00:00+08:00",
                    "end_at": "2026-09-30T00:00:00+08:00",
                },
            ),
        )

        # ==================== 测温值更正（只改温度） ====================
        # 10:00 的车载读数 py-vr1 原录 7.0：把 A（上限 4）开启了越温区间。
        # 若该值录错（实际 2.0），凭读数ID + 更正ID + 期望版本(首次为 0)提交：
        show(
            "车载读数更正 7.0 -> 2.0（同时影响读数时刻在车的全部货物）",
            api.post(
                "/corrections",
                json={
                    "correction_id": "py-corr-vr1",
                    "target_type": "vehicle_reading",
                    "target_id": "py-vr1",
                    "expected_version": 0,
                    "new_temperature": "2.0",
                },
            ),
        )
        # 同内容重传 -> 200 replayed；版本过期 / 异内容 -> 409
        show(
            "车载更正幂等重传",
            api.post(
                "/corrections",
                json={
                    "correction_id": "py-corr-vr1",
                    "target_type": "vehicle_reading",
                    "target_id": "py-vr1",
                    "expected_version": 0,
                    "new_temperature": "2.0",
                },
            ),
        )
        show(
            "车载读数原始记录及历次更正",
            api.get("/vehicle-readings/py-vr1/corrections"),
        )
        show(
            "车次详情（原始 7.0 不变，有效温度 2.0 / 版本 1）",
            api.get(f"/trips/{trip}"),
        )
        show(
            "更正后货物 A 追责报告（10:00 不再越温，区间按有效值重建）",
            api.get(f"/cargo/{cargo_a}/report"),
        )

        # 自有测温更正：CARGO 的 p-t1 原录 8.2 改为 3.8（< 上限 4）
        show(
            "自有测温更正 8.2 -> 3.8（expected_version=0）",
            api.post(
                "/corrections",
                json={
                    "correction_id": "py-corr-own-1",
                    "target_type": "own_event",
                    "target_id": "p-t1",
                    "expected_version": 0,
                    "new_temperature": "3.8",
                },
            ),
        )
        show(
            "自有事件列表（temperature=原始值，effective_temperature/version=有效值）",
            api.get(f"/cargo/{CARGO}/events"),
        )
        show(
            "自有测温历次更正",
            api.get("/events/p-t1/corrections"),
        )
        show(
            "交接不可更正（422 handover_not_correctable）",
            api.post(
                "/corrections",
                json={
                    "correction_id": "py-corr-hand",
                    "target_type": "own_event",
                    "target_id": "p-h1",
                    "expected_version": 0,
                    "new_temperature": "3.8",
                },
            ),
        )

        # ==================== 装卸前温控兼容性预检与确认 ====================
        # 新车次：先挂一票上限 4℃ 的货物并录入 7℃ 车载读数，
        # 再为“上限 8℃ 的货物同车”做装前预检 + 确认登记
        ptrip = "TRUCK-PY-2"
        pre_a, pre_b = "COLD-PRE-A", "COLD-PRE-B"
        for cid, limit in ((pre_a, "4.0"), (pre_b, "8.0")):
            api.post(
                "/cargo",
                json={
                    "cargo_id": cid,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": limit,
                    "initial_holder": "北京冷库",
                },
            )
        api.post("/trips", json={"trip_id": ptrip, "vehicle_no": "京B-冷999"})
        api.post(
            f"/trips/{ptrip}/manifest",
            json={"items": [
                {"cargo_id": pre_a,
                 "loaded_at": "2026-09-30T09:00:00+08:00",
                 "unloaded_at": "2026-09-30T18:00:00+08:00"},
            ]},
        )
        api.post(
            "/vehicle-readings",
            json={"reading_id": "py-vr-pre", "trip_id": ptrip,
                  "read_at": "2026-09-30T10:00:00+08:00",
                  "temperature": "7.0"},
        )

        # 预检需携带预期车次修订号（先查询车次详情获得）
        revision = api.get(f"/trips/{ptrip}").json()["revision"]
        print(f"\n车次 {ptrip} 当前修订号 = {revision}")
        proposed = [
            {"cargo_id": pre_b,
             "loaded_at": "2026-09-30T09:00:00+08:00",
             "unloaded_at": "2026-09-30T18:00:00+08:00"},
        ]
        resp = api.post(
            f"/trips/{ptrip}/manifest/precheck",
            json={"expected_trip_revision": revision, "items": proposed},
        )
        show("装前预检（各时段货物集合 / 上限差异 / 读数覆盖 / 潜在越温风险）", resp)
        digest = resp.json()["digest"]
        print(f"预检摘要 = {digest}")
        print("预检为只读：车次装卸记录不变 =",
              [l["cargo_id"] for l in api.get(f"/trips/{ptrip}").json()["loadings"]])

        # 预检后该车次新增一笔读数 -> 原摘要过期，确认被拒
        api.post(
            "/vehicle-readings",
            json={"reading_id": "py-vr-pre2", "trip_id": ptrip,
                  "read_at": "2026-09-30T11:00:00+08:00",
                  "temperature": "5.0"},
        )
        show(
            "携过期摘要确认（409 stale_precheck_digest，不落库）",
            api.post(
                f"/trips/{ptrip}/manifest/confirm",
                json={"digest": digest, "items": proposed},
            ),
        )

        # 重新预检并携新摘要确认 -> 201，装卸时段落库
        revision = api.get(f"/trips/{ptrip}").json()["revision"]
        digest = api.post(
            f"/trips/{ptrip}/manifest/precheck",
            json={"expected_trip_revision": revision, "items": proposed},
        ).json()["digest"]
        show(
            "重新预检后确认登记（201）",
            api.post(
                f"/trips/{ptrip}/manifest/confirm",
                json={"digest": digest, "items": proposed},
            ),
        )
        detail = api.get(f"/trips/{ptrip}").json()
        print("确认后：revision =", detail["revision"],
              "loadings =", [l["cargo_id"] for l in detail["loadings"]])

        # 摘要一次性：同一摘要重复确认 -> 409
        show(
            "同一摘要重复确认（409 stale_precheck_digest）",
            api.post(
                f"/trips/{ptrip}/manifest/confirm",
                json={"digest": digest, "items": proposed},
            ),
        )

        # ----- 装卸时段更正（只改该条窗口）-----
        print("\n########## 装卸时段更正 ##########")
        ltrip, lc = "TRUCK-LC-PY", "COLD-LC-PY"
        api.post("/cargo", json={
            "cargo_id": lc, "started_at": "2026-09-30T06:00:00+08:00",
            "temp_upper_limit": "4.0", "initial_holder": "北京冷库"})
        api.post("/trips", json={"trip_id": ltrip, "vehicle_no": "京B-冷LCPY"})
        # 原始窗口 09:00-18:00，录一笔 17:30 的车载越温读数（旧窗口内在车）
        api.post(f"/trips/{ltrip}/manifest", json={"items": [{
            "cargo_id": lc,
            "loaded_at": "2026-09-30T09:00:00+08:00",
            "unloaded_at": "2026-09-30T18:00:00+08:00"}]})
        api.post("/vehicle-readings", json={
            "reading_id": "vr-lc-py", "trip_id": ltrip,
            "read_at": "2026-09-30T17:30:00+08:00", "temperature": "7.0"})
        before = api.get(f"/trips/{ltrip}").json()
        print("更正前：window_version =", before["loadings"][0]["window_version"],
              "revision =", before["revision"],
              "17:30 在车 =",
              before["readings"][0]["applications"][0]["on_board"])

        # 稳定身份：车次 + 货物 + 首次登记装车时刻；expected_version 取 window_version
        corr_body = {
            "correction_id": "lc-py-1",
            "cargo_id": lc,
            "origin_loaded_at": "2026-09-30T09:00:00+08:00",
            "expected_version": 0,
            "new_loaded_at": "2026-09-30T08:00:00+08:00",
            "new_unloaded_at": "2026-09-30T17:00:00+08:00",
            "reason": "派车单装卸时刻各录晚 1 小时",
        }
        show("更正装卸窗口 09:00-18:00 -> 08:00-17:00（201）",
             api.post(f"/trips/{ltrip}/loading-corrections", json=corr_body))

        after = api.get(f"/trips/{ltrip}").json()
        print("更正后：当前窗口 =",
              (after["loadings"][0]["loaded_at"],
               after["loadings"][0]["unloaded_at"]),
              "window_version =", after["loadings"][0]["window_version"],
              "revision =", after["revision"])
        print("17:30 读数按当前窗口已卸货（卸车时刻排除）：on_board =",
              after["readings"][0]["applications"][0]["on_board"])

        # 原窗口与历次更正可追溯
        show("原窗口与历次更正（按版本升序）", api.get(
            f"/trips/{ltrip}/loading-corrections",
            params={"cargo_id": lc,
                    "origin_loaded_at": "2026-09-30T09:00:00+08:00"}))

        # 同更正编号同请求重传 -> 200 replayed；异请求 -> 409
        show("同请求重传（200 replayed）",
             api.post(f"/trips/{ltrip}/loading-corrections", json=corr_body))
        show("同编号异请求（409 correction_id_conflict）",
             api.post(f"/trips/{ltrip}/loading-corrections",
                      json={**corr_body, "expected_version": 1,
                            "reason": "另一个原因"}))
        # 版本过期 / 原窗口不存在 / 新时段非法
        show("版本过期（409 stale_window_version）",
             api.post(f"/trips/{ltrip}/loading-corrections",
                      json={**corr_body, "correction_id": "lc-py-stale"}))
        show("原窗口不存在（404 loading_window_not_found）",
             api.post(f"/trips/{ltrip}/loading-corrections", json={
                 **corr_body, "correction_id": "lc-py-missing",
                 "origin_loaded_at": "2026-09-30T10:30:00+08:00"}))
        show("新时段非法（422 invalid_loading_window）",
             api.post(f"/trips/{ltrip}/loading-corrections", json={
                 **corr_body, "correction_id": "lc-py-bad",
                 "expected_version": 1,
                 "new_loaded_at": "2026-09-30T17:00:00+08:00",
                 "new_unloaded_at": "2026-09-30T17:00:00+08:00"}))

        # ----- 责任证据封存 -----
        print("\n########## 责任证据封存 ##########")
        sc, st = "COLD-SEAL-PY", "TRUCK-SEAL-PY"
        api.post("/cargo", json={
            "cargo_id": sc, "started_at": "2026-10-03T08:00:00+08:00",
            "temp_upper_limit": "4.0", "initial_holder": "北京冷库"})
        api.post("/trips", json={"trip_id": st, "vehicle_no": "京B-封PY"})
        api.post(f"/trips/{st}/manifest", json={"items": [{
            "cargo_id": sc,
            "loaded_at": "2026-10-03T08:00:00+08:00",
            "unloaded_at": "2026-10-03T20:00:00+08:00"}]})
        # 09:00 车载 7℃ 开启 veh:svr-py-1；10:00 北京->上海交接拆分；12:00 车载 3℃ 闭合
        api.post("/vehicle-readings", json={
            "reading_id": "svr-py-1", "trip_id": st,
            "read_at": "2026-10-03T09:00:00+08:00", "temperature": "7.0"})
        api.post("/events", json={
            "event_id": "se-py-h1", "cargo_id": sc,
            "occurred_at": "2026-10-03T10:00:00+08:00",
            "giver": "北京冷库", "receiver": "上海冷库"})
        api.post("/vehicle-readings", json={
            "reading_id": "svr-py-2", "trip_id": st,
            "read_at": "2026-10-03T12:00:00+08:00", "temperature": "3.0"})

        iv = api.get(f"/cargo/{sc}/responsibilities").json()["intervals"][0]
        iid, snap = iv["interval_id"], iv["snapshot_id"]
        print("区间标识 =", iid, "快照 =", snap[:20], "…")

        seal_body = {
            "case_id": "CASE-SEAL-PY-1",
            "cargo_id": sc,
            "interval_id": iid,
            "snapshot_id": snap,
        }
        show("封存责任证据（201；含内容摘要与全部重建证据）",
             api.post("/evidence-seals", json=seal_body))
        created = api.post("/evidence-seals", json=seal_body).json()
        print("同请求重传：replayed =", created.get("replayed"))

        show("查询封存包（原内容与摘要，不可改）",
             api.get("/evidence-seals/CASE-SEAL-PY-1"))
        show("对照现况（无变化 -> unchanged）",
             api.get("/evidence-seals/CASE-SEAL-PY-1/diff"))

        # 区间内补 11:00 不超限车载读数 -> 提前闭合，快照变化
        api.post("/vehicle-readings", json={
            "reading_id": "svr-py-3", "trip_id": st,
            "read_at": "2026-10-03T11:00:00+08:00", "temperature": "3.0"})
        show("旧快照封存新案件（409 stale_snapshot，拒绝不留记录）",
             api.post("/evidence-seals", json={**seal_body, "case_id": "CASE-SEAL-PY-STALE"}))
        show("原案件同请求重试（200 replayed，即使现况已变）",
             api.post("/evidence-seals", json=seal_body))
        show("对照现况（changed：片段与读数增删变更）",
             api.get("/evidence-seals/CASE-SEAL-PY-1/diff"))
        show("同案件编号异请求（409 case_id_conflict）",
             api.post("/evidence-seals",
                      json={**seal_body, "snapshot_id": "sha256:" + "f" * 64}))

        # 测温更正开启读数为不超限 -> 区间消失；对照 disappeared，原包不改写
        api.post("/corrections", json={
            "correction_id": "scorr-py-1", "target_type": "vehicle_reading",
            "target_id": "svr-py-1", "expected_version": 0,
            "new_temperature": "2.0"})
        show("更正后对照现况（disappeared；原包不变）",
             api.get("/evidence-seals/CASE-SEAL-PY-1/diff"))
        show("未知案件（404 seal_not_found）", api.get("/evidence-seals/NO-SUCH-CASE"))

        # ----- 车次探头故障登记 / 撤销（独立演示车次，避免影响上面封存链路）-----
        print("\n########## 车次探头故障登记 / 撤销 ##########")
        fc = "COLD-FAULT-PY-1"
        ft = "TRUCK-FAULT-PY"
        api.post("/cargo", json={
            "cargo_id": fc, "started_at": "2026-10-04T08:00:00+08:00",
            "temp_upper_limit": "4.0", "initial_holder": "北京冷库"})
        api.post("/trips", json={"trip_id": ft, "vehicle_no": "京B-故01"})
        api.post(f"/trips/{ft}/manifest", json={"items": [{
            "cargo_id": fc,
            "loaded_at": "2026-10-04T08:00:00+08:00",
            "unloaded_at": "2026-10-04T20:00:00+08:00"}]})
        api.post("/vehicle-readings", json={
            "reading_id": "fvr-py-1", "trip_id": ft,
            "read_at": "2026-10-04T09:00:00+08:00", "temperature": "8.0"})
        api.post("/vehicle-readings", json={
            "reading_id": "fvr-py-2", "trip_id": ft,
            "read_at": "2026-10-04T11:00:00+08:00", "temperature": "2.0"})
        print("登记前越温区间：",
              [e["interval_id"] for e in api.get(f"/cargo/{fc}/report").json()["excursions"]])

        fault_body = {
            "fault_id": "pf-py-1",
            "fault_start": "2026-10-04T08:30:00+08:00",
            "fault_end": "2026-10-04T10:30:00+08:00",
            "reason": "探头 PF-9 校准漂移（计量院证实）",
        }
        show("登记探头故障（201；09:00 读数被隔离）",
             api.post(f"/trips/{ft}/probe-faults", json=fault_body))
        show("同请求重传（200 replayed）",
             api.post(f"/trips/{ft}/probe-faults", json=fault_body))
        show("异内容冲突（409 fault_id_conflict）",
             api.post(f"/trips/{ft}/probe-faults",
                      json={**fault_body, "reason": "另一个原因"}))
        show("缺时区（422 timezone_required）",
             api.post(f"/trips/{ft}/probe-faults", json={
                 "fault_id": "pf-py-bad",
                 "fault_start": "2026-10-04T13:00:00",
                 "fault_end": "2026-10-04T14:00:00",
                 "reason": "x"}))
        print("登记后越温区间（开启读数被隔离 -> 空）：",
              api.get(f"/cargo/{fc}/report").json()["excursions"])
        tl = api.get(f"/cargo/{fc}/timeline").json()["points"]
        print("时间线车载点：",
              [(p["reading_id"], p["adopted"], p["quarantined"],
                p["covering_fault_ids"]) for p in tl if p["source"] == "vehicle"])

        revoke_body = {
            "revocation_id": "rv-py-1", "fault_id": "pf-py-1",
            "expected_version": 0,
        }
        show("撤销故障（201；读数按当前有效温度恢复采用）",
             api.post(f"/trips/{ft}/probe-faults/revoke", json=revoke_body))
        show("撤销同请求重传（200 replayed）",
             api.post(f"/trips/{ft}/probe-faults/revoke", json=revoke_body))
        show("版本过期（409 stale_fault_version）",
             api.post(f"/trips/{ft}/probe-faults/revoke", json={
                 "revocation_id": "rv-py-2", "fault_id": "pf-py-1",
                 "expected_version": 0}))
        print("撤销后越温区间恢复：",
              [e["interval_id"] for e in api.get(f"/cargo/{fc}/report").json()["excursions"]])
        show("故障登记列表（含撤销留痕）",
             api.get(f"/trips/{ft}/probe-faults"))


if __name__ == "__main__":
    main()
