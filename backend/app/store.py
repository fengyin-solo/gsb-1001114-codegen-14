"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

跨模块联动（边坡结论要同时刷边坡台账、路面病害、工程清单）统一走
``transaction()``：进入事务时先做整库快照，业务代码在快照上修改，
正常退出整体生效，中途抛异常自动回滚。事务持进程级版本锁，FastAPI
线程池里的并发改评会被串行化，配合版本号校验保证只有一个结论生效。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 边坡稳定性剖面带使用的内部表，只服务于边坡业务，不进运营概览的模块清单
INTERNAL_MODULES = {"slope_segment", "slope_survey_task", "risk_projection"}
PROJECTION_MODULE = "risk_projection"


class ConcurrentUpdateError(RuntimeError):
    """版本锁冲突：提交依据的结论版本已经被更新的现场复核覆盖。"""


class Store:
    def __init__(self) -> None:
        # RLock：同一个事务里调用同样开启事务的内部方法时可重入
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def reset(self) -> None:
        """恢复到种子初始状态（测试用）。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self.replace_projection(self._seed_projection())

    @staticmethod
    def _seed_projection() -> list[dict[str, Any]]:
        # 延迟导入避免循环依赖
        from app.services.slope import SlopeService

        return SlopeService()._projection_items()

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨表写入的事务边界：快照上改，异常整体回滚。

        注意：事务内必须通过 ``find``/``rows`` 重新取行，事务外持有的行引用
        属于已提交的旧版本，不能直接改。
        """
        with self._lock:
            committed = self._tables
            working = copy.deepcopy(committed)
            # 切换到工作副本后，事务内的一切读写都落在副本上
            self._tables = working
            try:
                yield
            except BaseException:
                self._tables = committed
                raise
            # 正常退出时保留 working 作为新的已提交版本

    def replace_projection(self, items: list[dict[str, Any]]) -> None:
        """按权威结论全量重算风险投影。

        以 ``key`` 去重：同一边坡/踏勘任务无论剖面带分多少段、分段重复加载
        多少遍，都只贡献一个风险点，不重复计数。必须在事务内调用。
        """
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for item in items:
            key = str(item.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            row = {"id": len(rows) + 1, "pending": True, "abnormal": True}
            row.update(item)
            row["key"] = key
            rows.append(row)
        self._tables[PROJECTION_MODULE] = rows

    def risk_points(self) -> int:
        return len(self.rows(PROJECTION_MODULE))

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            {"label": "风险点数", "value": self.risk_points()},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
