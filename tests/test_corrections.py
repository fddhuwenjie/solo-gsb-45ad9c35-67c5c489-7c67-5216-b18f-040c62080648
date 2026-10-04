from datetime import datetime, timedelta, timezone

import app.main as main

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

# 东西向门线（坐标顺序：西 -> 东），右=南、左=北
GATE_EW = {
    "type": "LineString",
    "coordinates": [[0.0025, 0.005], [0.0075, 0.005]],
}


def pt(device, sec, lat, lon):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def create_fence(client, geometry=PLAIN_FENCE, config=None):
    payload = {"name": "更正测试围栏", "geometry": geometry, "config": config or {}}
    r = client.post("/fences", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def correct(client, corrections):
    r = client.post("/points/corrections", json={"corrections": corrections})
    assert r.status_code == 200, r.text
    return r.json()


def get_events(client, **params):
    r = client.get("/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_correction_rewrites_events_and_status(client):
    create_fence(client)
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),    # 内
        pt("dev1", 10, 0.005, 0.005),   # 内
        pt("dev1", 20, 0.02, 0.02),     # 外 -> leave
    ])
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter", "leave"]

    # 把 t=20 的点更正到围栏内：leave 消失，设备保持 inside
    result = correct(client, [
        {"device_id": "dev1", "ts": iso(20), "lat": 0.005, "lon": 0.005},
    ])
    assert result == {"corrected": 1, "devices_updated": 1}

    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]
    st = client.get("/devices/dev1/status").json()["fences"][0]
    assert st["state"] == "inside"
    assert datetime.fromisoformat(st["since"]) == BASE


def test_correction_can_introduce_events(client):
    create_fence(client)
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),     # 外
        pt("dev1", 10, 0.02, 0.02),    # 外（将被更正到内部）
        pt("dev1", 20, 0.02, 0.02),    # 外
    ])
    assert get_events(client, device_id="dev1") == []

    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005}])
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave"]
    assert datetime.fromisoformat(events[0]["ts"]) == BASE + timedelta(seconds=10)
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=20)


def test_correction_matches_timestamp_across_formats(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02)])
    # 同一时刻的 "Z" 写法与入库时的 "+00:00" 写法定位到同一条记录
    correct(client, [
        {"device_id": "dev1", "ts": iso(0).replace("+00:00", "Z"),
         "lat": 0.005, "lon": 0.005},
    ])
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_multi_device_batch(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev2", 0, 0.02, 0.02)])

    result = correct(client, [
        {"device_id": "dev1", "ts": iso(0), "lat": 0.005, "lon": 0.005},
        {"device_id": "dev2", "ts": iso(0), "lat": 0.006, "lon": 0.006},
    ])
    assert result == {"corrected": 2, "devices_updated": 2}
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]
    assert [e["type"] for e in get_events(client, device_id="dev2")] == ["enter"]


def test_missing_record_rejects_whole_batch(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev1", 10, 0.02, 0.02)])

    r = client.post("/points/corrections", json={"corrections": [
        {"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005},   # 存在
        {"device_id": "dev1", "ts": iso(30), "lat": 0.005, "lon": 0.005},   # 时间戳不存在
        {"device_id": "ghost", "ts": iso(0), "lat": 0.005, "lon": 0.005},   # 设备不存在
    ]})
    assert r.status_code == 422

    # 整批拒绝：存在的目标也未被改写，事件仍为空
    assert get_events(client, device_id="dev1") == []
    # 原目标仍可被后续单独更正（证明上一批未部分生效）
    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005}])
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_duplicate_target_rejects_whole_batch(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev1", 10, 0.02, 0.02)])

    r = client.post("/points/corrections", json={"corrections": [
        {"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005},
        {"device_id": "dev1", "ts": iso(10), "lat": 0.006, "lon": 0.006},
    ]})
    assert r.status_code == 422
    assert get_events(client, device_id="dev1") == []


def test_invalid_coordinates_rejected(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02)])

    for bad in ({"lat": 90.1, "lon": 0.0}, {"lat": -90.1, "lon": 0.0},
                {"lat": 0.0, "lon": 180.1}, {"lat": 0.0, "lon": -180.1}):
        r = client.post("/points/corrections", json={"corrections": [
            {"device_id": "dev1", "ts": iso(0), **bad},
        ]})
        assert r.status_code == 422

    # 空批次同样拒绝
    r = client.post("/points/corrections", json={"corrections": []})
    assert r.status_code == 422

    # 边界值合法
    result = correct(client, [
        {"device_id": "dev1", "ts": iso(0), "lat": 90, "lon": 180},
    ])
    assert result["corrected"] == 1


def test_correction_refreshes_crossings(client):
    fence_id = create_fence(client)
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": GATE_EW,
        "forward_name": "南->北",
        "reverse_name": "北->南",
        "min_displacement_m": 0.0,
    })
    assert r.status_code == 201, r.text
    submit(client, [
        pt("dev1", 0, 0.004, 0.005),    # 门线南
        pt("dev1", 10, 0.006, 0.005),   # 门线北 -> forward 穿越
    ])
    crossings = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings) == 1 and crossings[0]["direction"] == "forward"

    # 把第二个点更正到门线南侧：穿越消失
    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.004, "lon": 0.006}])
    assert client.get("/crossings", params={"device_id": "dev1"}).json() == []

    # 再更正回北侧：穿越恢复
    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.006, "lon": 0.005}])
    crossings = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings) == 1 and crossings[0]["direction"] == "forward"


def test_correction_leaves_other_devices_untouched(client):
    create_fence(client)
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 10, 0.02, 0.02),
        pt("dev2", 0, 0.005, 0.005),
        pt("dev2", 10, 0.02, 0.02),
    ])
    before = get_events(client, device_id="dev2")

    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005}])

    # 未受影响设备不重算：事件（含 id）逐条相同
    assert get_events(client, device_id="dev2") == before
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_occupancy_report_reflects_correction(client):
    fence_id = create_fence(client)
    r = client.post(f"/fences/{fence_id}/capacity-rules", json={
        "effective_from": iso(0), "max_occupancy": 1,
    })
    assert r.status_code == 201, r.text
    submit(client, [
        pt("dev1", 10, 0.005, 0.005),   # 界内
        pt("dev2", 20, 0.02, 0.02),     # 界外（将被更正到界内）
    ])

    def counts():
        r = client.get(
            f"/fences/{fence_id}/occupancy",
            params={"start": iso(0), "end": iso(100)},
        )
        assert r.status_code == 200, r.text
        return [(s["count"], s["exceeded"]) for s in r.json()["segments"]]

    # dev1 进入后未离开：[0,10) 无人，[10,100) 1 人
    assert counts() == [(0, False), (1, False)]
    correct(client, [{"device_id": "dev2", "ts": iso(20), "lat": 0.005, "lon": 0.005}])
    assert counts() == [(0, False), (1, False), (2, True)]


def test_clearance_report_reflects_correction(client):
    fence_id = create_fence(client)
    r = client.post(f"/fences/{fence_id}/clearance-tasks", json={
        "name": "清场", "start_at": iso(0), "deadline": iso(100),
    })
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    submit(client, [
        pt("dev1", -20, 0.005, 0.005),  # 窗口开始前已进入 -> 起始名单
        pt("dev1", 50, 0.005, 0.005),   # 仍在内（将被更正到界外）
    ])

    def statuses():
        r = client.get(f"/fences/{fence_id}/clearance-tasks/{task_id}/report")
        assert r.status_code == 200, r.text
        return {d["device_id"]: d["status"] for d in r.json()["devices"]}

    assert statuses() == {"dev1": "remaining"}
    correct(client, [{"device_id": "dev1", "ts": iso(50), "lat": 0.02, "lon": 0.02}])
    assert statuses() == {"dev1": "cleared"}


def test_failed_recompute_rolls_back_everything(client, monkeypatch):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev1", 10, 0.02, 0.02)])

    def boom(db, device_id):
        raise RuntimeError("重算故障")

    monkeypatch.setattr(main, "_recompute_device", boom)
    r = client.post("/points/corrections", json={"corrections": [
        {"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005},
    ]})
    assert r.status_code == 500
    monkeypatch.undo()

    # 整体回滚：不留下新坐标配旧事件——事件仍为空，原目标可再次正常更正
    assert get_events(client, device_id="dev1") == []
    correct(client, [{"device_id": "dev1", "ts": iso(10), "lat": 0.005, "lon": 0.005}])
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_points_resubmission_is_not_a_correction(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02)])   # 界外

    # 同 (device_id, ts) 不同坐标重提交：仍按重复忽略，不改写已入库坐标
    again = submit(client, [pt("dev1", 0, 0.005, 0.005)])
    assert again == {"accepted": 0, "duplicates": 1, "devices_updated": 0}
    assert get_events(client, device_id="dev1") == []
