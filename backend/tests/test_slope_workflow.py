"""边坡防护：结论版本状态机、事务联动、版本锁与风险投影的回归测试。"""
from __future__ import annotations

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as client:
        yield client


def _review(client: TestClient, slope_id: int, rating: str, version: int, **extra):
    values = {"rating": rating, "expected_version": version, **extra}
    return client.post(f"/api/slope/{slope_id}/reviews", json={"values": values})


def _publish(client: TestClient, slope_id: int, version: int):
    return client.post(f"/api/slope/{slope_id}/publish", json={"values": {"expected_version": version}})


def test_state_machine_rejects_skip_and_reverse(client: TestClient) -> None:
    # 待复核的边坡3不能直接发布（跳过现场复核）
    resp = _publish(client, 3, 1)
    assert resp.json()["ok"] is False
    assert "先完成现场复核" in resp.json()["message"]

    # 先推进到待发布
    resp = _review(client, 3, "欠稳定", 1)
    assert resp.json()["ok"] is True
    assert resp.json()["entry"]["phase"] == "待发布"

    # 待发布阶段不能再次现场复核（倒序）
    resp = _review(client, 3, "稳定", 2)
    assert resp.json()["ok"] is False
    assert "评级发布与踏勘任务" in resp.json()["message"]

    # 待发布阶段不能直接生成踏勘任务（跳级）
    resp = client.post("/api/slope/3/survey-tasks", json={"values": {"expected_version": 2, "points": []}})
    assert resp.json()["ok"] is False
    assert "评级发布" in resp.json()["message"]


def test_publish_transactionally_syncs_three_ledgers(client: TestClient) -> None:
    _review(client, 3, "不稳定", 1)
    resp = _publish(client, 3, 2)
    assert resp.json()["ok"] is True
    slope = resp.json()["entry"]
    sid = slope["id"]

    pavement = [row for row in client.get("/api/pavement").json()["items"] if row.get("_边坡来源") == sid]
    projects = [row for row in client.get("/api/project").json()["items"] if row.get("_边坡来源") == sid]
    assert len(pavement) == 1 and pavement[0]["病害状态"] == "待修复"
    assert len(projects) == 1 and projects[0]["工程状态"] == "待开工"

    # 新一轮复核降级为稳定后发布：三处联动同步收口，且不新增行
    _finish_all_tasks(client, sid)
    assert _review(client, sid, "稳定", 2).json()["entry"]["phase"] == "待发布"
    _publish(client, sid, 3)
    pavement = [row for row in client.get("/api/pavement").json()["items"] if row.get("_边坡来源") == sid]
    projects = [row for row in client.get("/api/project").json()["items"] if row.get("_边坡来源") == sid]
    assert pavement[0]["病害状态"] == "已修复" and pavement[0]["pending"] is False
    assert projects[0]["工程状态"] == "已竣工" and projects[0]["pending"] is False


def _finish_all_tasks(client: TestClient, slope_id: int) -> None:
    tasks = client.get(f"/api/slope/survey-tasks?slope={slope_id}&open_only=true").json()["items"]
    for task in tasks:
        client.post(f"/api/slope/survey-tasks/{task['id']}/complete")


def test_deviation_click_generates_task_and_inside_segment_rejected(client: TestClient) -> None:
    # 边坡1 是 K12+000~K12+700，既有分段只到 K12+600，K12+650 为偏离空档
    _finish_all_tasks(client, 1)  # 清掉种子任务回到待复核
    _review(client, 1, "不稳定", 3)
    _publish(client, 1, 4)
    resp = client.post(
        "/api/slope/1/survey-tasks",
        json={"values": {"expected_version": 4, "points": [{"stake": "K12+650"}]}},
    )
    assert resp.json()["ok"] is True

    # 踏勘中补点：同桩号已有待办则去重，不再生成
    dup = client.post(
        "/api/slope/1/survey-tasks",
        json={"values": {"expected_version": 4, "points": [{"stake": "K12+650"}]}},
    )
    assert dup.json()["ok"] is False
    assert "未重复生成" in dup.json()["message"]

    # 点在既有分段内：拒绝，不算偏离坡段
    inside = client.post(
        "/api/slope/1/survey-tasks",
        json={"values": {"expected_version": 4, "points": [{"stake": "K12+300"}]}},
    )
    assert inside.json()["ok"] is False
    assert "不属于偏离坡段" in inside.json()["message"]

    tasks = client.get("/api/slope/survey-tasks?slope=1&open_only=true").json()["items"]
    assert len(tasks) == 1 and tasks[0]["偏移桩号"] == "K12+650"


def test_failed_generation_rolls_back_transaction(client: TestClient) -> None:
    _review(client, 3, "不稳定", 1)
    _publish(client, 3, 2)
    # 先给合法偏离点，再给一个落在分段内的非法点：整体回滚，不留半成品
    resp = client.post(
        "/api/slope/3/survey-tasks",
        json={"values": {"expected_version": 2, "points": [{"stake": "K3+050"}, {"stake": "K3+100"}]}},
    )
    assert resp.json()["ok"] is False
    tasks = client.get("/api/slope/survey-tasks?slope=3&open_only=true").json()["items"]
    assert tasks == []


def test_segment_pagination_never_double_counts(client: TestClient) -> None:
    before = client.get("/api/overview").json()["cards"][-1]["value"]
    # 大量测点分多页加载，风险点数不受影响
    pages = [client.get("/api/slope/segments", params={"page": p, "size": 2}).json() for p in (1, 2, 3, 4)]
    assert sum(page["total"] for page in pages) == len(pages) * 7  # total 是全量计数而非本页
    assert client.get("/api/overview").json()["cards"][-1]["value"] == before

    # 风险投影按边坡去重：一个边坡同时有风险结论和多个踏勘任务，也只算一个风险点
    tasks_before = len(client.get("/api/slope/survey-tasks?slope=1&open_only=true").json()["items"])
    summary = client.get("/api/slope/risk-summary").json()
    overview_value = client.get("/api/overview").json()["cards"][-1]["value"]
    assert summary["风险点数"] == overview_value
    assert tasks_before >= 1


def test_profile_carries_height_protection_interval_and_adjacent_defects(client: TestClient) -> None:
    profile = client.get("/api/slope/1/profile").json()
    codes = [seg["分段编号"] for seg in profile["segments"]]
    assert codes == ["SLOP-0001-A", "SLOP-0001-B", "SLOP-0001-C"]
    seg_b = profile["segments"][1]
    assert seg_b["坡高"] == 26 and seg_b["防护形式"] == "锚杆框架" and seg_b["巡检间隔天"] == 3
    assert seg_b["防护前提"] == "锚杆框架@v3"
    # 连云大道 K12+200~240 纵向裂缝与 B 段桩号相交
    assert [d["病害编号"] for d in seg_b["相邻病害"]] == ["PAVE-0001"]


def test_historical_segments_keep_original_protection_premise(client: TestClient) -> None:
    # 复核时更换防护形式：既有分段仍保留 @v3，新分段继承当前防护前提
    _finish_all_tasks(client, 1)
    _review(client, 1, "欠稳定", 3, protection="锚索格构")
    profile = client.get("/api/slope/1/profile").json()
    assert {seg["防护前提"] for seg in profile["segments"]} == {"锚杆框架@v3"}

    resp = client.post(
        "/api/slope/1/segments",
        json={"values": {"起点桩号": "K12+600", "终点桩号": "K12+700", "坡高": 20}},
    )
    assert resp.json()["ok"] is True
    new_seg = resp.json()["entry"]
    assert new_seg["防护形式"] == "锚索格构" and new_seg["防护前提版本"] == 4


def test_concurrent_reviews_version_lock_only_one_wins(client: TestClient) -> None:
    def submit() -> tuple[int, bool]:
        # 每个线程各自拿一个独立 client 模拟并发改评
        local = TestClient(app)
        resp = local.post("/api/slope/3/reviews", json={"values": {"rating": "不稳定", "expected_version": 1}})
        local.close()
        return resp.status_code, resp.json().get("ok", False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(), range(8)))

    winners = [r for r in results if r[1] is True]
    losers = [r for r in results if r[1] is not True]
    assert len(winners) == 1
    # 其余并发请求都因版本锁失败（409 冲突），不会有两个结论同时生效
    assert all(code == 409 for code, _ in losers)
    slope = client.get("/api/slope/3").json()
    assert slope["version"] == 2 and len(slope["复核记录"]) == 1


def test_conflict_resolved_by_latest_field_review(client: TestClient) -> None:
    # 两个用户依次复核：后者覆盖前者，发布以最近一次现场复核为准
    assert client.post("/api/slope/3/reviews", json={"values": {"rating": "欠稳定", "expected_version": 1}}).json()["ok"]
    # 前端拿着 v1 发布 -> 409，提示以最近复核为准
    stale = _publish(client, 3, 1)
    assert stale.status_code == 409
    # 用最新版本发布：评级是最近一次的“欠稳定”
    ok = _publish(client, 3, 2)
    assert ok.json()["ok"] is True
    pavement = [row for row in client.get("/api/pavement").json()["items"] if row.get("_边坡来源") == 3]
    assert pavement[0]["严重程度"] == "欠稳定"


def test_full_cycle_returns_to_review_phase(client: TestClient) -> None:
    assert client.get("/api/slope/1").json()["phase"] == "踏勘中"
    _finish_all_tasks(client, 1)
    assert client.get("/api/slope/1").json()["phase"] == "待复核"
    # 还有未完成任务时不能开始新一轮复核
    _review(client, 1, "不稳定", 3)
    _publish(client, 1, 4)
    client.post(
        "/api/slope/1/survey-tasks",
        json={"values": {"expected_version": 4, "points": [{"stake": "K12+650"}]}},
    )
    blocked = _review(client, 1, "稳定", 4)
    assert blocked.json()["ok"] is False
    assert "踏勘任务" in blocked.json()["message"]