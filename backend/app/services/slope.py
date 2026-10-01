"""边坡防护业务规则：结论版本状态机、剖面带分段、踏勘任务与跨模块风险投影。

结论生命周期由单一版本状态机驱动，只允许顺序推进：

    待复核 ──现场复核──▶ 待发布 ──评级发布──▶ 待生成踏勘 ──生成踏勘任务──▶ 踏勘中
      ▲                                                                    │
      └──────────────────── 全部踏勘完成（新一轮现场复核） ◀───────────────┘

评级发布为稳定/基本稳定且没有偏离坡段时，结论直接闭环回到「待复核」。
任何跳级、倒序的动作都会被拒绝。所有跨表写入都在 ``store.transaction()``
里完成：边坡台账、路面病害待办、工程清单、踏勘任务、风险投影要么一起生效，
要么整体回滚。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.store import ConcurrentUpdateError, store

MODULE = "slope"
SEGMENT_MODULE = "slope_segment"
TASK_MODULE = "slope_survey_task"

# 单一结论版本状态机的全部阶段
PHASE_INIT = "待复核"
PHASE_REVIEWED = "待发布"
PHASE_PENDING_TASK = "待生成踏勘"
PHASE_SURVEYING = "踏勘中"
PHASE_ORDER = [PHASE_INIT, PHASE_REVIEWED, PHASE_PENDING_TASK, PHASE_SURVEYING]

REQUIRED_FIELDS = ["边坡编号", "所属路段", "边坡类型", "起点桩号", "终点桩号"]
SEGMENT_REQUIRED = ["起点桩号", "终点桩号", "坡高"]

RATINGS = ["稳定", "基本稳定", "欠稳定", "不稳定"]
RISK_RATINGS = {"欠稳定", "不稳定"}
# 不同评级下的默认巡检间隔（天），分段单独建档时沿用
DEFAULT_INTERVALS = {"稳定": 30, "基本稳定": 15, "欠稳定": 7, "不稳定": 3}

_STAKE_RE = re.compile(r"K?\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def stake_to_meter(value: Any) -> float | None:
    """把 ``K12+320`` 这样的桩号解析成米；纯数字也接受，解析不了返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _STAKE_RE.search(text)
    if match:
        return int(match.group(1)) * 1000 + float(match.group(2))
    try:
        return float(text)
    except ValueError:
        return None


class SlopeService:
    # ---------------------------------------------------------------- 列表

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("边坡编号", ""))
                or keyword in str(row.get("所属路段", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("phase") == status or row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------ 台账登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        start = stake_to_meter(values.get("起点桩号"))
        end = stake_to_meter(values.get("终点桩号"))
        if start is None or end is None or end <= start:
            return None, ["起止桩号（需形如 K12+000，且终点在起点之后）"]
        with store.transaction():
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry.update({
                "坡高": str(values.get("坡高") or "—"),
                "防护形式": str(values.get("防护形式") or "未防护"),
                "稳定性评级": "未评级",
                "最近巡检": "—",
                "version": 1,
                "phase": PHASE_INIT,
                "status": PHASE_INIT,
                "pending": True,
                "abnormal": False,
                "复核记录": [],
            })
            rows.append(entry)
            store.replace_projection(self._projection_items())
        return entry, []

    # -------------------------------------------------------- 状态机：复核

    def review(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        rating = str(values.get("rating") or values.get("稳定性评级") or "").strip()
        expected = self._as_int(values.get("expected_version"))
        at = str(values.get("at") or date.today().isoformat())
        note = str(values.get("note") or "").strip()
        if rating not in RATINGS:
            return None, f"稳定性评级需为：{'、'.join(RATINGS)}"
        try:
            with store.transaction():
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"边坡 {entry_id} 不存在或已归档"
                if expected is None:
                    return None, "提交缺少 expected_version，无法确认结论版本，请刷新后重试"
                error = self._check_version(entry, expected)
                if error:
                    raise ConcurrentUpdateError(error)
                phase = entry.get("phase")
                if phase not in (PHASE_INIT, PHASE_SURVEYING):
                    return None, f"当前阶段为「{phase}」，需先完成评级发布与踏勘任务，才能再次现场复核"
                if phase == PHASE_SURVEYING and self._open_tasks(entry_id):
                    return None, "仍有未完成的工程踏勘任务，全部踏勘完成后才能发起新一轮现场复核"
                # 现场复核是新一轮结论的起点：历史坡段仍按原防护前提保留，
                # 只有新分段才继承本次防护形式
                protection = str(values.get("protection") or values.get("防护形式") or entry.get("防护形式") or "").strip()
                record = {
                    "at": at,
                    "rating": rating,
                    "protection": protection,
                    "note": note,
                    "based_on_version": entry["version"],
                }
                entry.setdefault("复核记录", []).append(record)
                entry["version"] = int(entry["version"]) + 1
                entry["phase"] = PHASE_REVIEWED
                entry["status"] = PHASE_REVIEWED
                entry["稳定性评级"] = rating
                entry["防护形式"] = protection or entry.get("防护形式")
                entry["最近巡检"] = at
                entry["pending"] = True
                return entry, f"现场复核已记录（结论版本 v{entry['version']}），可进行评级发布"
        except ConcurrentUpdateError as exc:
            raise _Conflict(str(exc)) from exc

    # -------------------------------------------------------- 状态机：发布

    def publish(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        expected = self._as_int(values.get("expected_version"))
        at = str(values.get("at") or date.today().isoformat())
        try:
            with store.transaction():
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"边坡 {entry_id} 不存在或已归档"
                if expected is None:
                    return None, "提交缺少 expected_version，无法确认结论版本，请刷新后重试"
                error = self._check_version(entry, expected)
                if error:
                    raise ConcurrentUpdateError(error)
                if entry.get("phase") != PHASE_REVIEWED:
                    return None, f"当前阶段为「{entry.get('phase')}」，评级发布前必须先完成现场复核，不允许跳级"
                rating = str(entry.get("稳定性评级") or "")
                is_risk = rating in RISK_RATINGS
                # 1) 刷边坡台账
                entry["abnormal"] = is_risk
                # 2) 刷路面病害待办
                self._sync_pavement_todo(entry, rating, at, is_risk)
                # 3) 刷工程清单
                self._sync_project(entry, rating, at, is_risk)
                deviating = self._deviation_points(entry)
                if is_risk and deviating:
                    entry["phase"] = PHASE_PENDING_TASK
                    entry["status"] = PHASE_PENDING_TASK
                    message = (
                        f"结论 v{entry['version']}（{rating}）已同步边坡台账、路面病害待办与工程清单，"
                        f"发现 {len(deviating)} 处偏离坡段，待生成工程踏勘任务"
                    )
                else:
                    entry["phase"] = PHASE_INIT
                    entry["status"] = PHASE_INIT
                    tail = "无偏离坡段，结论闭环" if is_risk else "结论闭环，等待下一轮巡检复核"
                    message = f"结论 v{entry['version']}（{rating}）已同步边坡台账、路面病害清单与工程待办，{tail}"
                entry["pending"] = True
                store.replace_projection(self._projection_items())
                return entry, message
        except ConcurrentUpdateError as exc:
            raise _Conflict(str(exc)) from exc

    # ------------------------------------------------- 状态机：踏勘任务生成

    def generate_survey_tasks(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        expected = self._as_int(values.get("expected_version"))
        point_list = values.get("points") or []
        segment_ids = values.get("segment_ids") or []
        if not isinstance(point_list, list):
            point_list = []
        if not isinstance(segment_ids, list):
            segment_ids = []
        try:
            with store.transaction():
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"边坡 {entry_id} 不存在或已归档", {}
                if expected is None:
                    return None, "提交缺少 expected_version，无法确认结论版本，请刷新后重试", {}
                error = self._check_version(entry, expected)
                if error:
                    raise ConcurrentUpdateError(error)
                if entry.get("phase") not in (PHASE_PENDING_TASK, PHASE_SURVEYING):
                    return None, f"当前阶段为「{entry.get('phase')}」，只有评级发布后的坡段才能生成踏勘任务", {}
                # 踏勘中补点只追加任务，不改变阶段；首次生成任务才把状态机推进到踏勘中
                was_waiting = entry.get("phase") == PHASE_PENDING_TASK
                segments = self._segments(entry_id)
                start_m = stake_to_meter(entry.get("起点桩号"))
                end_m = stake_to_meter(entry.get("终点桩号"))

                # 先把全部测点校验、组装完，再统一落表：任一测点不合法整个事务回滚，
                # 不会出现“前几个任务已生成、后面测点被拒”的半成品
                planned: list[tuple[dict[str, Any], str | None, float | None, dict[str, Any]]] = []
                skipped: list[str] = []

                # 点选的偏离桩号：落在既有所有分段之外才算偏离，落在分段内拒绝生成
                for point in point_list:
                    stake = str((point or {}).get("stake") or (point or {}).get("桩号") or "").strip()
                    meter = stake_to_meter(stake)
                    if meter is None:
                        return None, f"桩号「{stake}」无法识别，请使用 K12+320 形式", {}
                    if start_m is not None and end_m is not None and not (start_m <= meter <= end_m):
                        return None, f"桩号 {stake} 不在边坡 {entry.get('边坡编号')} 的管养范围（{entry.get('起点桩号')}~{entry.get('终点桩号')}）内", {}
                    hit = next((s for s in segments if float(s["起点米"]) <= meter <= float(s["终点米"])), None)
                    if hit is not None:
                        return None, f"桩号 {stake} 落在既有分段 {hit['分段编号']} 内，不属于偏离坡段，踏勘任务不生成", {}
                    if self._has_open_point_task(entry_id, meter):
                        skipped.append(stake)
                        continue
                    planned.append((None, stake, meter, point or {}))

                for raw in segment_ids:
                    seg_id = self._as_int(raw)
                    seg = next((s for s in segments if int(s.get("id", 0)) == seg_id), None)
                    if seg is None:
                        return None, f"分段 {raw} 不属于边坡 {entry.get('边坡编号')}，踏勘任务不生成", {}
                    if self._has_open_segment_task(entry_id, seg_id):
                        skipped.append(str(seg["分段编号"]))
                        continue
                    planned.append((seg, None, None, {}))

                if not planned and skipped:
                    return None, f"所选 {len(skipped)} 个测点已存在未完成踏勘任务，未重复生成", {}

                created: list[dict[str, Any]] = []
                for segment, stake, meter, point in planned:
                    task = self._build_task(entry, segment=segment, stake=stake, meter=meter, point=point)
                    store.rows(TASK_MODULE).append(task)
                    created.append(task)

                if was_waiting:
                    entry["phase"] = PHASE_SURVEYING
                    entry["status"] = PHASE_SURVEYING
                entry["pending"] = True
                store.replace_projection(self._projection_items())
                detail = {"created": len(created), "skipped": skipped, "tasks": created}
                tail = f"；{len(skipped)} 个测点已有待办，已去重" if skipped else ""
                return entry, f"已生成 {len(created)} 个工程踏勘任务，边坡进入踏勘中{tail}", detail
        except ConcurrentUpdateError as exc:
            raise _Conflict(str(exc)) from exc

    def list_survey_tasks(self, *, entry_id: int | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        rows = store.rows(TASK_MODULE)
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("边坡", 0)) == entry_id]
        if open_only:
            rows = [row for row in rows if row.get("pending")]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def close_survey_task(self, task_id: int) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            task = store.find(TASK_MODULE, task_id)
            if task is None:
                return None, f"踏勘任务 {task_id} 不存在"
            if not task.get("pending"):
                return None, f"踏勘任务 {task.get('任务编号')} 已完成，不能重复提交"
            task["status"] = "已踏勘"
            task["pending"] = False
            task["abnormal"] = False
            task["完成日期"] = date.today().isoformat()
            entry = store.find(MODULE, int(task["边坡"]))
            message = f"踏勘任务 {task['任务编号']} 已完成"
            if entry is not None and entry.get("phase") == PHASE_SURVEYING:
                if not self._open_tasks(int(task["边坡"])):
                    # 踏勘任务全部完成，状态机才允许回到新一轮现场复核
                    entry["phase"] = PHASE_INIT
                    entry["status"] = PHASE_INIT
                    message += "，本坡段踏勘任务全部完成，可发起新一轮现场复核"
            store.replace_projection(self._projection_items())
            return task, message

    # --------------------------------------------------------- 剖面带分段

    def list_segments(
        self,
        *,
        road: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """分段（大量测点）分页加载：相邻病害实时拼装，任何一页都不产生写入。"""
        rows = [dict(row) for row in store.rows(SEGMENT_MODULE)]
        for row in rows:
            slope = store.find(MODULE, int(row.get("边坡", 0)))
            if slope is not None:
                row["边坡编号"] = slope.get("边坡编号")
                row["所属路段"] = slope.get("所属路段")
                row["稳定性评级"] = slope.get("稳定性评级")
                row["偏离"] = self._segment_deviation(slope, row)
            row["防护前提"] = f"{row.get('防护形式')}@v{row.get('防护前提版本', 1)}"
            row["相邻病害"] = self._adjacent_defects(row)
        if road:
            rows = [row for row in rows if road in str(row.get("所属路段", ""))]
        rows.sort(key=lambda row: (int(row.get("边坡", 0)), float(row.get("起点米") or 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def slope_profile(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        segments = []
        for row in self._segments(entry_id):
            item = dict(row)
            item["防护前提"] = f"{row.get('防护形式')}@v{row.get('防护前提版本', 1)}"
            item["偏离"] = self._segment_deviation(entry, row)
            item["相邻病害"] = self._adjacent_defects(row)
            segments.append(item)
        return {
            "slope": entry,
            "segments": segments,
            "deviation_points": self._deviation_points(entry),
            "tasks": self.list_survey_tasks(entry_id=entry_id),
        }

    def add_segment(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [field for field in SEGMENT_REQUIRED if str(values.get(field) or "").strip() == ""]
        if missing:
            return None, f"缺少分段必填字段：{'、'.join(missing)}"
        start = stake_to_meter(values.get("起点桩号"))
        end = stake_to_meter(values.get("终点桩号"))
        if start is None or end is None or end <= start:
            return None, "分段起止桩号无效，需形如 K12+200，且终点在起点之后"
        try:
            height = float(values.get("坡高"))
        except (TypeError, ValueError):
            return None, "坡高需为数值（米）"
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"边坡 {entry_id} 不存在或已归档"
            slope_start = stake_to_meter(entry.get("起点桩号"))
            slope_end = stake_to_meter(entry.get("终点桩号"))
            if slope_start is not None and slope_end is not None and (start < slope_start or end > slope_end):
                return None, "分段超出边坡管养桩号范围"
            for seg in self._segments(entry_id):
                if start < float(seg["终点米"]) and float(seg["起点米"]) < end:
                    return None, f"与既有分段 {seg['分段编号']} 桩号重叠"
            rating = str(entry.get("稳定性评级") or "")
            interval = self._as_int(values.get("巡检间隔天"))
            if interval is None:
                interval = DEFAULT_INTERVALS.get(rating, 30 if rating not in RISK_RATINGS else 7)
            protection = str(values.get("防护形式") or entry.get("防护形式") or "未防护")
            # 历史坡段按原防护前提保留：新分段继承当前防护形式与结论版本
            segment = {
                "id": store.next_id(SEGMENT_MODULE),
                "边坡": entry_id,
                "分段编号": str(values.get("分段编号") or f"{entry['边坡编号']}-S{len(self._segments(entry_id)) + 1}"),
                "起点桩号": values.get("起点桩号"),
                "终点桩号": values.get("终点桩号"),
                "起点米": start,
                "终点米": end,
                "坡高": height,
                "防护形式": protection,
                "巡检间隔天": interval,
                "防护前提版本": int(entry.get("version", 1)),
            }
            store.rows(SEGMENT_MODULE).append(segment)
            store.replace_projection(self._projection_items())
            return segment, f"分段 {segment['分段编号']} 已建档（防护前提 {protection}@v{segment['防护前提版本']}）"

    # ------------------------------------------------------------ 风险概览

    def risk_summary(self) -> dict[str, Any]:
        slopes = store.rows(MODULE)
        tasks = store.rows(TASK_MODULE)
        phase_counts = {phase: 0 for phase in PHASE_ORDER}
        rating_counts: dict[str, int] = {}
        for row in slopes:
            phase_counts[str(row.get("phase"))] = phase_counts.get(str(row.get("phase")), 0) + 1
            rating = str(row.get("稳定性评级") or "未评级")
            rating_counts[rating] = rating_counts.get(rating, 0) + 1
        # 概览风险点数直接读风险投影：结论与踏勘任务合并去重后的结果
        return {
            "风险点数": store.risk_points(),
            "待踏勘任务": sum(1 for row in tasks if row.get("pending")),
            "阶段分布": phase_counts,
            "评级分布": rating_counts,
        }

    # ------------------------------------------------------------ 内部工具

    def _segments(self, entry_id: int) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(SEGMENT_MODULE) if int(row.get("边坡", 0)) == entry_id]
        return sorted(rows, key=lambda row: float(row.get("起点米") or 0))

    def _open_tasks(self, entry_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(TASK_MODULE)
            if int(row.get("边坡", 0)) == entry_id and row.get("pending")
        ]

    def _has_open_point_task(self, entry_id: int, meter: float) -> bool:
        return any(
            row.get("pending") and row.get("分段") is None
            and abs(float(row.get("偏移米", -1)) - meter) < 1
            for row in self._open_tasks(entry_id)
        )

    def _has_open_segment_task(self, entry_id: int, segment_id: int) -> bool:
        return any(
            row.get("pending") and self._as_int(row.get("分段")) == segment_id
            for row in self._open_tasks(entry_id)
        )

    def _build_task(
        self,
        entry: dict[str, Any],
        *,
        segment: dict[str, Any] | None,
        stake: str | None,
        meter: float | None,
        point: dict[str, Any],
    ) -> dict[str, Any]:
        version = int(entry.get("version", 1))
        if segment is not None:
            task_stake = segment.get("起点桩号")
            task_meter = segment.get("起点米")
            source = "剖面带分段"
            desc = str(point.get("说明") or f"分段 {segment['分段编号']} 需现场踏勘确认稳定性")
        else:
            task_stake = stake
            task_meter = meter
            source = "剖面带点选"
            desc = str(point.get("说明") or f"桩号 {stake} 偏离既有坡段，需现场踏勘")
        task_id = store.next_id(TASK_MODULE)
        return {
            "id": task_id,
            "任务编号": f"SURV-{task_id:04d}",
            "边坡": int(entry["id"]),
            "边坡编号": entry.get("边坡编号"),
            "所属路段": entry.get("所属路段"),
            "分段": int(segment["id"]) if segment is not None else None,
            "来源": source,
            "偏移桩号": task_stake,
            "偏移米": task_meter,
            "status": "待踏勘",
            "pending": True,
            "abnormal": True,
            "要求踏勘日期": str(point.get("due") or point.get("要求踏勘日期") or ""),
            "说明": desc,
            "创建于": date.today().isoformat(),
            "结论版本": version,
        }

    def _adjacent_defects(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        """同一路段、桩号区间与分段相交的路面病害，作为剖面带的相邻病害。"""
        slope = store.find(MODULE, int(segment.get("边坡", 0)))
        if slope is None:
            return []
        road = slope.get("所属路段")
        seg_start = float(segment.get("起点米") or 0)
        seg_end = float(segment.get("终点米") or 0)
        defects: list[dict[str, Any]] = []
        for row in store.rows("pavement"):
            if row.get("所属路段") != road:
                continue
            start = row.get("起点米", stake_to_meter(str(row.get("起止桩号", "")).split("~")[0]))
            end = row.get("终点米", stake_to_meter(str(row.get("起止桩号", "")).split("~")[-1]))
            start = float(start) if start is not None else None
            end = float(end) if end is not None else None
            if start is None or end is None or start >= seg_end or end <= seg_start:
                continue
            defects.append({
                "病害编号": row.get("病害编号"),
                "病害类型": row.get("病害类型"),
                "严重程度": row.get("严重程度"),
                "起止桩号": row.get("起止桩号"),
                "病害状态": row.get("病害状态") or row.get("status"),
            })
        return defects

    def _segment_deviation(self, slope: dict[str, Any], segment: dict[str, Any]) -> bool:
        """分段桩号脱离边坡管养范围即视为偏离坡段。"""
        slope_start = stake_to_meter(slope.get("起点桩号"))
        slope_end = stake_to_meter(slope.get("终点桩号"))
        if slope_start is None or slope_end is None:
            return False
        return float(segment["起点米"]) < slope_start or float(segment["终点米"]) > slope_end

    def _deviation_points(self, slope: dict[str, Any]) -> list[dict[str, Any]]:
        """边坡范围内未被既有分段覆盖的桩号空档，按分段间隙给出偏离点。"""
        slope_start = stake_to_meter(slope.get("起点桩号"))
        slope_end = stake_to_meter(slope.get("终点桩号"))
        if slope_start is None or slope_end is None:
            return []
        points: list[dict[str, Any]] = []
        cursor = slope_start
        for seg in self._segments(int(slope["id"])):
            if float(seg["起点米"]) > cursor:
                middle = (cursor + float(seg["起点米"])) / 2
                points.append({"桩号": self._format_meter(middle), "米": middle})
            cursor = max(cursor, float(seg["终点米"]))
        if cursor < slope_end:
            middle = (cursor + slope_end) / 2
            points.append({"桩号": self._format_meter(middle), "米": middle})
        return points

    @staticmethod
    def _format_meter(meter: float) -> str:
        meter = round(meter)
        return f"K{meter // 1000}+{meter % 1000:03d}"

    def _sync_pavement_todo(
        self,
        slope: dict[str, Any],
        rating: str,
        at: str,
        is_risk: bool,
    ) -> None:
        """发布结论刷写到路面病害清单（带边坡来源标记，只动自己创建的行）。"""
        rows = store.rows("pavement")
        linked = next((row for row in rows if row.get("_边坡来源") == int(slope["id"])), None)
        if linked is None:
            if not is_risk:
                return
            linked = {
                "id": store.next_id("pavement"),
                "_边坡来源": int(slope["id"]),
                "病害编号": f"PAVE-S{int(slope['id']):04d}",
                "所属路段": slope.get("所属路段"),
                "病害类型": "边坡风险投影",
                "面积": "—",
            }
            rows.append(linked)
        linked.update({
            "严重程度": rating,
            "起止桩号": f"{slope.get('起点桩号')}~{slope.get('终点桩号')}",
            "发现日期": at,
        })
        if is_risk:
            linked["status"] = "待修复"
            linked["病害状态"] = "待修复"
            linked["pending"] = True
            linked["abnormal"] = True
            linked["_结论版本"] = int(slope["version"])
        else:
            linked["status"] = "已修复"
            linked["病害状态"] = "已修复"
            linked["pending"] = False
            linked["abnormal"] = False

    def _sync_project(
        self,
        slope: dict[str, Any],
        rating: str,
        at: str,
        is_risk: bool,
    ) -> None:
        """发布结论刷写到工程清单：风险结论开工程条目，降级后收口。"""
        rows = store.rows("project")
        linked = next((row for row in rows if row.get("_边坡来源") == int(slope["id"])), None)
        if linked is None:
            if not is_risk:
                return
            linked = {
                "id": store.next_id("project"),
                "_边坡来源": int(slope["id"]),
                "工程编号": f"PROJ-S{int(slope['id']):04d}",
                "工程类型": "边坡防治工程",
                "承建单位": "待定",
                "竣工日期": "—",
            }
            rows.append(linked)
        linked.update({
            "工程名称": f"{slope.get('边坡编号')} {slope.get('所属路段')}边坡加固（{rating}）",
            "施工路段": slope.get("所属路段"),
            "开工日期": at,
        })
        if is_risk:
            linked["status"] = "待开工"
            linked["工程状态"] = "待开工"
            linked["pending"] = True
            linked["abnormal"] = True
            linked["_结论版本"] = int(slope["version"])
        else:
            linked["status"] = "已竣工"
            linked["工程状态"] = "已竣工"
            linked["竣工日期"] = at
            linked["pending"] = False
            linked["abnormal"] = False

    def _projection_items(self) -> list[dict[str, Any]]:
        """汇总权威风险投影：异常边坡与其未完成踏勘任务按边坡合并，天然去重。"""
        items: list[dict[str, Any]] = []
        for slope in store.rows(MODULE):
            rating = str(slope.get("稳定性评级") or "")
            open_tasks = self._open_tasks(int(slope["id"]))
            if rating not in RISK_RATINGS and not open_tasks:
                continue
            reasons: list[str] = []
            if rating in RISK_RATINGS:
                reasons.append(f"最新现场复评：{rating}")
            if open_tasks:
                reasons.append(f"未完成工程踏勘任务 {len(open_tasks)} 个")
            items.append({
                "key": f"slope:{int(slope['id'])}",
                "来源类型": "边坡",
                "来源编号": slope.get("边坡编号"),
                "所属路段": slope.get("所属路段"),
                "桩号": f"{slope.get('起点桩号')}~{slope.get('终点桩号')}",
                "评级": rating,
                "阶段": slope.get("phase"),
                "结论版本": int(slope.get("version", 1)),
                "待踏勘任务": len(open_tasks),
                "风险描述": "；".join(reasons),
            })
        return items

    @staticmethod
    def _check_version(entry: dict[str, Any], expected: int | None) -> str | None:
        """版本锁：提交依据的版本过期就拒绝，保证并发改评只有一个结论生效。"""
        if expected != int(entry.get("version", 1)):
            latest = entry.get("复核记录") or []
            latest_at = latest[-1].get("at") if latest else "—"
            return (
                f"结论版本已过期（页面基于 v{expected}，当前为 v{entry.get('version')}，"
                f"最近一次现场复核 {latest_at}）；评级冲突以最近一次现场复核为准，请刷新后再提交"
            )
        return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None


class _Conflict(Exception):
    """服务层版本冲突信号，路由层统一转 409。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# 供路由与启动引导复用
Conflict = _Conflict


def bootstrap_projection() -> None:
    """启动时按种子数据重建一次风险投影，保证概览风险点数开箱即准。"""
    service = SlopeService()
    with store.transaction():
        store.replace_projection(service._projection_items())
