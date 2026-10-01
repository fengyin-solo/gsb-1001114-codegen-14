"""边坡防护业务规则。

风险结论由**单一版本状态机**驱动，每个边坡只有一份生效结论（``superseded=False`` 的复核
版本），阶段严格顺序推进：

    待复核 ──现场复核──▶ 已复核 ──评级发布──▶ 已发布 ──点选偏离坡段生成踏勘任务──▶ 踏勘中 ──踏勘完成──▶ 已踏勘

跳级、倒序一律拒绝；历史坡段（``version=0``、结论阶段=历史归档）按原防护前提保留，不参与
新一轮评级。

两条跨表投影都在同一个数据库事务内提交：
- 评级发布：现场评级（冲突时以最近一次现场复核为准）刷入边坡台账、路面病害待办、工程清单；
- 剖面结论：点选的偏离测点生成踏勘任务，并统一刷到边坡台账、路面病害清单、工程待办，
  概览风险点数按测点主键去重重算，分段加载不会重复计数。

并发改评用乐观版本锁：调用方携带 ``expected_version``，事务内二次校验，版本不符直接抛出
``VersionConflict``，保证一个结论生效。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "slope"
REVIEW_MODULE = "slope_review"
PROFILE_MODULE = "slope_profile_point"
SURVEY_MODULE = "slope_survey"

REQUIRED_FIELDS = ["边坡编号", "所属路段", "边坡类型"]

# 风险结论阶段：顺序即状态机允许的推进方向
STAGE_DRAFT = "待复核"
STAGE_REVIEWED = "已复核"
STAGE_PUBLISHED = "已发布"
STAGE_SURVEYING = "踏勘中"
STAGE_SURVEYED = "已踏勘"
STAGE_ARCHIVED = "历史归档"
STAGE_ORDER = [STAGE_DRAFT, STAGE_REVIEWED, STAGE_PUBLISHED, STAGE_SURVEYING, STAGE_SURVEYED]

# 稳定性评级：现场复核口径，发布时以现场评级为准（建议评级仅作参考）
RATINGS = ["稳定", "较稳定", "较差", "不稳定"]
RISK_RATINGS = {"较差", "不稳定"}
RATING_TO_STATUS = {"稳定": "稳定", "较稳定": "稳定", "较差": "局部变形", "不稳定": "失稳"}


class WorkflowError(Exception):
    """状态机拒绝推进：跳级、倒序、缺字段等业务冲突。"""


class VersionConflict(WorkflowError):
    """乐观版本锁冲突：结论已被他人更新，调用方需刷新后重试。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class SlopeService:
    # ---------------------------------------------------------------- 台账
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
            rows = [row for row in rows if keyword in str(row.get("边坡编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            entry = {"id": store.next_id(MODULE), "version": 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry.update({
                "坡高": values.get("坡高") or "—",
                "防护形式": values.get("防护形式") or "—",
                "稳定性评级": "—",
                "最近巡检": _today(),
                "边坡状态": "待复核",
                "起点桩号": values.get("起点桩号") or "—",
                "终点桩号": values.get("终点桩号") or "—",
                "status": "待复核",
                "pending": True,
                "abnormal": False,
                "结论阶段": STAGE_DRAFT,
            })
            store.insert(MODULE, entry)
            review = {
                "id": store.next_id(REVIEW_MODULE),
                "slope_id": entry["id"],
                "version": 1,
                "stage": STAGE_DRAFT,
                "superseded": False,
                "复核人": "",
                "复核日期": _today(),
                "现场评级": "",
                "建议评级": "",
                "结论说明": "新登记边坡，待现场复核",
                "published_at": None,
            }
            store.insert(REVIEW_MODULE, review)
        return entry, []

    # ------------------------------------------------------------ 内部工具
    def _load_slope(self, slope_id: int) -> dict[str, Any]:
        slope = store.find(MODULE, slope_id)
        if slope is None:
            raise WorkflowError(f"边坡 {slope_id} 不存在或已归档")
        return slope

    def _guard_archived(self, slope: dict[str, Any]) -> None:
        if str(slope.get("结论阶段") or "") == STAGE_ARCHIVED or int(slope.get("version", 0)) == 0:
            raise WorkflowError("历史坡段按原防护前提保留，不再参与评级流转")

    def _check_version(self, slope: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is None:
            raise WorkflowError("缺少版本号 expected_version，无法确认结论是否被他人更新")
        current = int(slope.get("version", 0))
        if int(expected_version) != current:
            raise VersionConflict(
                f"结论版本已过期（你基于 v{expected_version}，当前为 v{current}），"
                "请刷新后以最近一次现场复核为准"
            )

    def _active_review(self, slope_id: int) -> dict[str, Any] | None:
        for row in reversed(store.rows(REVIEW_MODULE)):
            if int(row.get("slope_id", 0)) == slope_id and not row.get("superseded"):
                return row
        return None

    def _require_stage(self, slope: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
        review = self._active_review(int(slope["id"]))
        if review is None:
            raise WorkflowError("缺少风险结论版本，无法推进状态机")
        stage = str(review.get("stage"))
        if stage not in allowed:
            raise WorkflowError(
                f"不允许从「{stage}」执行该操作，现场复核 → 评级发布 → 踏勘任务只能顺序推进"
            )
        return review

    def _bump(self, slope: dict[str, Any]) -> int:
        # 走 store.update 而不是直接赋值：版本号变更必须能被事务回滚
        new_version = int(slope.get("version", 0)) + 1
        store.update(slope, {"version": new_version})
        return new_version

    # ------------------------------------------------------- ① 现场复核
    def submit_review(
        self,
        slope_id: int,
        values: dict[str, Any],
        expected_version: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        reviewer = str(values.get("复核人") or "").strip()
        rating = str(values.get("现场评级") or "").strip()
        note = str(values.get("结论说明") or "").strip()
        if not reviewer:
            raise WorkflowError("现场复核必须填写复核人")
        if rating not in RATINGS:
            raise WorkflowError(f"现场评级须为：{'、'.join(RATINGS)}")

        with store.transaction():
            slope = self._load_slope(slope_id)
            self._guard_archived(slope)
            self._check_version(slope, expected_version)
            current = self._active_review(slope_id)
            assert current is not None
            stage = str(current.get("stage"))

            # 待复核：就地完成首版现场复核；已复核后改评：旧版作废、出新版（最近一次现场复核为准）
            if stage == STAGE_DRAFT:
                review = current
                new_version = self._bump(slope)
                store.update(review, {
                    "stage": STAGE_REVIEWED,
                    "version": new_version,
                    "复核人": reviewer,
                    "复核日期": str(values.get("复核日期") or _today()),
                    "现场评级": rating,
                    "建议评级": str(values.get("建议评级") or rating),
                    "结论说明": note or "现场复核完成，待评级发布",
                })
            elif stage == STAGE_REVIEWED:
                store.update(current, {"superseded": True})
                new_version = self._bump(slope)
                review = {
                    "id": store.next_id(REVIEW_MODULE),
                    "slope_id": slope_id,
                    "version": new_version,
                    "stage": STAGE_REVIEWED,
                    "superseded": False,
                    "复核人": reviewer,
                    "复核日期": str(values.get("复核日期") or _today()),
                    "现场评级": rating,
                    "建议评级": str(values.get("建议评级") or rating),
                    "结论说明": note or f"改评覆盖旧结论，最近一次现场评级为「{rating}」",
                    "published_at": None,
                }
                store.insert(REVIEW_MODULE, review)
            else:
                raise WorkflowError(
                    f"当前阶段「{stage}」不能再提交现场复核，结论发布后流程不可倒序"
                )

            store.update(slope, {
                "结论阶段": STAGE_REVIEWED,
                "status": "待发布",
                "pending": True,
                "abnormal": rating in RISK_RATINGS,
            })
        return slope, review

    # ------------------------------------------------- ② 评级发布（投影）
    def publish_rating(
        self, slope_id: int, expected_version: int | None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with store.transaction():
            slope = self._load_slope(slope_id)
            self._guard_archived(slope)
            self._check_version(slope, expected_version)
            review = self._require_stage(slope, [STAGE_REVIEWED])

            # 评级冲突口径：现场评级 > 建议评级
            rating = str(review.get("现场评级") or review.get("建议评级") or "").strip()
            if rating not in RATINGS:
                raise WorkflowError("复核结论缺少有效评级，无法发布")

            new_version = self._bump(slope)
            store.update(review, {
                "stage": STAGE_PUBLISHED,
                "version": new_version,
                "published_at": _now(),
            })

            risky = rating in RISK_RATINGS
            slope_status = RATING_TO_STATUS[rating]
            # (1) 刷边坡台账
            store.update(slope, {
                "稳定性评级": rating,
                "边坡状态": slope_status,
                "status": slope_status,
                "最近巡检": review["复核日期"],
                "结论阶段": STAGE_PUBLISHED,
                "abnormal": risky,
                "pending": risky,
            })

            # (2) 刷路面病害待办：一个边坡一份结论投影，重复发布按来源键幂等更新
            pavement_row = self._find_projected("pavement", f"slope-conclusion:{slope_id}")
            pavement_values = {
                "病害编号": f"PAVE-{slope['边坡编号']}",
                "所属路段": slope.get("所属路段", ""),
                "病害类型": "边坡风险投影",
                "严重程度": rating,
                "起止桩号": f"{slope.get('起点桩号', '—')}~{slope.get('终点桩号', '—')}",
                "面积": "—",
                "发现日期": review["复核日期"],
                "病害状态": "待修复" if risky else "已核销",
                "status": "待修复" if risky else "已核销",
                "pending": risky,
                "abnormal": risky,
                "来源": f"slope-conclusion:{slope_id}",
                "结论版本": new_version,
            }
            if pavement_row is None:
                pavement_row = {"id": store.next_id("pavement"), **pavement_values}
                store.insert("pavement", pavement_row)
            else:
                store.update(pavement_row, pavement_values)

            # (3) 刷工程清单：风险待办，低风险核销
            project_row = self._find_projected("project", f"slope-conclusion:{slope_id}")
            project_values = {
                "工程编号": f"PROJ-{slope['边坡编号']}",
                "工程名称": f"{slope['边坡编号']}边坡风险处置工程",
                "工程类型": "边坡加固工程",
                "施工路段": slope.get("所属路段", ""),
                "承建单位": "—",
                "开工日期": "—",
                "竣工日期": "—",
                "工程状态": "待开工" if risky else "无需处置",
                "status": "待开工" if risky else "已竣工",
                "pending": risky,
                "abnormal": risky,
                "来源": f"slope-conclusion:{slope_id}",
                "结论版本": new_version,
            }
            if project_row is None:
                project_row = {"id": store.next_id("project"), **project_values}
                store.insert("project", project_row)
            else:
                store.update(project_row, project_values)

        return slope, review, {"pavement": pavement_row, "project": project_row}

    # ---------------------------------------- ③ 点选偏离坡段 → 踏勘任务
    def profile_points(
        self,
        *,
        slope_id: int | None = None,
        road: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """剖面测点分页。

        返回 ``(本页测点, 全量条数, 风险点数)``。风险点数按 ``point_key`` 去重后在服务端
        全量统计——前端分段加载时直接展示该值，不能把各页数量累加。
        """
        rows = store.rows(PROFILE_MODULE)
        if slope_id is not None:
            rows = [row for row in rows if int(row.get("slope_id", 0)) == slope_id]
        if road:
            rows = [row for row in rows if road in str(row.get("所属路段", ""))]
        rows = sorted(rows, key=lambda row: (int(row.get("slope_id", 0)), int(row.get("里程米", 0))))
        total = len(rows)
        risk_points = len({row["point_key"] for row in rows if row.get("is_risk")})
        start = max(page - 1, 0) * size
        return rows[start:start + size], total, risk_points

    def create_survey_tasks(
        self,
        slope_id: int,
        point_keys: list[str],
        expected_version: int | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        keys = [str(key).strip() for key in point_keys if str(key or "").strip()]
        if not keys:
            raise WorkflowError("请至少点选一个偏离坡段测点")
        keys = list(dict.fromkeys(keys))  # 同一次请求内去重

        with store.transaction():
            slope = self._load_slope(slope_id)
            self._guard_archived(slope)
            self._check_version(slope, expected_version)
            review = self._require_stage(slope, [STAGE_PUBLISHED, STAGE_SURVEYING])

            points: list[dict[str, Any]] = []
            for key in keys:
                point = self._find_point(key)
                if point is None or int(point.get("slope_id", 0)) != slope_id:
                    raise WorkflowError(f"测点 {key} 不属于边坡 {slope['边坡编号']}，任务未生成")
                if not point.get("deviation"):
                    raise WorkflowError(
                        f"测点 {key}（{point.get('里程桩号')}）不是偏离坡段，不能生成工程踏勘任务"
                    )
                points.append(point)

            first_transition = str(review.get("stage")) == STAGE_PUBLISHED
            if first_transition:
                new_version = self._bump(slope)
                store.update(review, {"stage": STAGE_SURVEYING, "version": new_version})
            else:
                new_version = int(slope["version"])

            # (a) 生成踏勘任务：按 point_key 幂等，重复点选不产生第二条、也不重复计数
            tasks: list[dict[str, Any]] = []
            task_seq = [store.next_id(SURVEY_MODULE)]
            for point in points:
                existing = self._find_survey(str(point["point_key"]))
                if existing is not None:
                    tasks.append(existing)
                    continue
                task = {
                    "id": task_seq[0],
                    "slope_id": slope_id,
                    "point_key": point["point_key"],
                    "任务编号": f"SURV-{int(task_seq[0]):04d}",
                    "任务状态": "待踏勘",
                    "status": "待踏勘",
                    "里程桩号": point.get("里程桩号"),
                    "踏勘事由": f"偏离坡段：{point.get('相邻病害') or '剖面参数偏离预警阈值'}",
                    "创建时间": _now(),
                    "来源版本": new_version,
                    "pending": True,
                    "abnormal": True,
                }
                store.insert(SURVEY_MODULE, task)
                tasks.append(task)
                task_seq[0] += 1
                store.update(point, {"投影状态": "已投影"})

            # (b) 剖面结论统一刷三表
            store.update(slope, {
                "结论阶段": STAGE_SURVEYING,
                "边坡状态": "踏勘中",
                "status": "踏勘中",
                "pending": True,
                "abnormal": True,
            })
            projection = self._project_profile_conclusion(slope, review, points, new_version)

            # (c) 风险点数去重重算（point_key 唯一），随概览接口读取
            risk_points = len({
                row["point_key"]
                for row in store.rows(PROFILE_MODULE)
                if row.get("is_risk")
            })
            projection["risk_points"] = risk_points

        return slope, tasks, projection

    def complete_survey(
        self, slope_id: int, expected_version: int | None, conclusion: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with store.transaction():
            slope = self._load_slope(slope_id)
            self._guard_archived(slope)
            self._check_version(slope, expected_version)
            review = self._require_stage(slope, [STAGE_SURVEYING])

            pending_tasks = [
                row for row in store.rows(SURVEY_MODULE)
                if int(row.get("slope_id", 0)) == slope_id and row.get("任务状态") == "待踏勘"
            ]
            if pending_tasks:
                raise WorkflowError(f"还有 {len(pending_tasks)} 个踏勘任务未完成，不能结束踏勘阶段")

            new_version = self._bump(slope)
            review_updates = {"stage": STAGE_SURVEYED, "version": new_version}
            if conclusion:
                review_updates["结论说明"] = str(conclusion)
            store.update(review, review_updates)
            rating = str(slope.get("稳定性评级") or "")
            final_status = RATING_TO_STATUS.get(rating, "稳定")
            store.update(slope, {
                "结论阶段": STAGE_SURVEYED,
                "边坡状态": final_status,
                "status": final_status,
                "pending": False,
                "abnormal": rating in RISK_RATINGS,
            })
        return slope, review

    # --------------------------------------------------------- 投影辅助
    def _find_projected(self, module: str, source: str) -> dict[str, Any] | None:
        for row in store.rows(module):
            if str(row.get("来源") or "") == source:
                return row
        return None

    def _find_point(self, point_key: str) -> dict[str, Any] | None:
        for row in store.rows(PROFILE_MODULE):
            if str(row.get("point_key")) == point_key:
                return row
        return None

    def _find_survey(self, point_key: str) -> dict[str, Any] | None:
        for row in store.rows(SURVEY_MODULE):
            if str(row.get("point_key")) == point_key and row.get("任务状态") != "已取消":
                return row
        return None

    def _project_profile_conclusion(
        self,
        slope: dict[str, Any],
        review: dict[str, Any],
        points: list[dict[str, Any]],
        version: int,
    ) -> dict[str, Any]:
        """剖面结论刷入路面病害清单与工程待办；全部登记在当前事务上。"""
        # 路面病害清单：每个偏离测点一条，按 point_key 幂等
        disease_rows: list[dict[str, Any]] = []
        for point in points:
            source = f"slope-point:{point['point_key']}"
            values = {
                "病害编号": f"PAVE-{point['point_key']}",
                "所属路段": point.get("所属路段", ""),
                "病害类型": "边坡偏离投影",
                "严重程度": str(review.get("现场评级") or "较差"),
                "起止桩号": point.get("里程桩号"),
                "面积": "—",
                "发现日期": _today(),
                "病害状态": "待修复",
                "status": "待修复",
                "pending": True,
                "abnormal": True,
                "来源": source,
                "结论版本": version,
            }
            row = self._find_projected("pavement", source)
            if row is None:
                row = {"id": store.next_id("pavement"), **values}
                store.insert("pavement", row)
            else:
                store.update(row, values)
            disease_rows.append(row)

        # 工程待办：同一边坡合并为一条踏勘工程，反复点选只更新不新增
        source = f"slope-conclusion:{int(slope['id'])}"
        task_count = len([
            row for row in store.rows(SURVEY_MODULE)
            if int(row.get("slope_id", 0)) == int(slope["id"]) and row.get("任务状态") == "待踏勘"
        ])
        project_values = {
            "工程编号": f"PROJ-{slope['边坡编号']}",
            "工程名称": f"{slope['边坡编号']}边坡踏勘处置工程（{task_count}个待踏勘点）",
            "工程类型": "边坡踏勘工程",
            "施工路段": slope.get("所属路段", ""),
            "承建单位": "—",
            "开工日期": "—",
            "竣工日期": "—",
            "工程状态": "待开工",
            "status": "待开工",
            "pending": True,
            "abnormal": True,
            "来源": source,
            "结论版本": version,
        }
        project_row = self._find_projected("project", source)
        if project_row is None:
            project_row = {"id": store.next_id("project"), **project_values}
            store.insert("project", project_row)
        else:
            store.update(project_row, project_values)

        return {"pavement": disease_rows, "project": project_row}

    # ----------------------------------------------------------- 查询面
    def list_surveys(
        self, *, slope_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = store.rows(SURVEY_MODULE)
        if slope_id is not None:
            rows = [row for row in rows if int(row.get("slope_id", 0)) == slope_id]
        if status:
            rows = [row for row in rows if row.get("任务状态") == status]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def workflow_detail(self, slope_id: int) -> dict[str, Any]:
        slope = self._load_slope(slope_id)
        reviews = [
            dict(row) for row in store.rows(REVIEW_MODULE)
            if int(row.get("slope_id", 0)) == slope_id
        ]
        reviews.sort(key=lambda row: int(row.get("version", 0)), reverse=True)
        points = [
            row for row in store.rows(PROFILE_MODULE)
            if int(row.get("slope_id", 0)) == slope_id
        ]
        points.sort(key=lambda row: int(row.get("里程米", 0)))
        tasks = self.list_surveys(slope_id=slope_id)
        return {
            "slope": slope,
            "stage_order": STAGE_ORDER,
            "reviews": reviews,
            "active_review": reviews[0] if reviews and not reviews[0].get("superseded") else next(
                (row for row in reviews if not row.get("superseded")), None
            ),
            "profile_points": points,
            "risk_points": len({row["point_key"] for row in points if row.get("is_risk")}),
            "surveys": tasks,
        }

    def risk_summary(self) -> dict[str, Any]:
        """边坡风险总览：所有数字都在服务端按主键去重统计。"""
        points = store.rows(PROFILE_MODULE)
        risk_keys = {row["point_key"] for row in points if row.get("is_risk")}
        projected_keys = {row["point_key"] for row in points if row.get("投影状态") == "已投影"}
        slopes = store.rows(MODULE)
        return {
            "risk_points": len(risk_keys),
            "projected_points": len(projected_keys),
            "pending_surveys": sum(
                1 for row in store.rows(SURVEY_MODULE) if row.get("任务状态") == "待踏勘"
            ),
            "pending_reviews": sum(
                1 for row in slopes if row.get("结论阶段") in (STAGE_DRAFT, STAGE_REVIEWED)
            ),
            "abnormal_slopes": sum(1 for row in slopes if row.get("abnormal")),
        }
