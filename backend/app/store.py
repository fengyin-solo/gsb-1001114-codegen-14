"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

事务与锁：
- 所有写操作都在 ``store.lock`` 保护下进行，FastAPI 的线程池下并发改评是串行化的；
- ``store.transaction()`` 提供“要么全部生效、要么全部回滚”的工作单元，任务生成与
  风险投影（台账/路面病害/工程清单多表写入）必须在同一个事务里提交；
- 业务层在事务内对版本号做二次校验（乐观锁），保证并发改评只有一个结论生效。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable

from app.seed import SEED_ROWS


class Transaction:
    """记录写操作的反向动作，异常时逆序回滚，正常退出时一次性提交。

    内存库里“提交”不需要额外动作——变更在事务过程中已经落到表上，锁在 ``__exit__``
    时才释放；回滚则按日志逆序撤销，保证多表投影不会留下半截数据。
    """

    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        self._undo: list[Callable[[], None]] = []
        self.active = True

    def append(self, table: list[dict[str, Any]], row: dict[str, Any]) -> None:
        def undo() -> None:
            table.remove(row)

        self._undo.append(undo)

    def update(self, row: dict[str, Any], snapshot: dict[str, Any]) -> None:
        changed = dict(row)

        def undo() -> None:
            row.clear()
            row.update(snapshot)

        self._undo.append(undo)
        del changed

    def delete(self, table: list[dict[str, Any]], index: int, row: dict[str, Any]) -> None:
        def undo() -> None:
            table.insert(index, row)

        self._undo.append(undo)

    def rollback(self) -> None:
        if not self.active:
            return
        for undo in reversed(self._undo):
            undo()
        self._undo.clear()
        self.active = False

    def commit(self) -> None:
        if not self.active:
            return
        self._undo.clear()
        self.active = False

    def release(self) -> None:
        self._lock.release()


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self.lock = threading.RLock()
        self._local = threading.local()

    # ---- 事务 ----------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """开启工作单元：嵌套时复用外层事务（与 RLock 的可重入语义一致）。"""
        tx = getattr(self._local, "tx", None)
        if tx is not None and tx.active:
            yield tx
            return
        self.lock.acquire()
        tx = Transaction(self.lock)
        self._local.tx = tx
        try:
            yield tx
            tx.commit()
        except Exception:
            tx.rollback()
            raise
        finally:
            self._local.tx = None
            tx.release()

    def _current_tx(self) -> Transaction | None:
        tx = getattr(self._local, "tx", None)
        if tx is not None and tx.active:
            return tx
        return None

    # ---- 读 ------------------------------------------------------------
    def module_names(self) -> list[str]:
        with self.lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        # 读不强制加锁：调用方在事务内持锁；裸读拿到的是 list 引用，GIL 下追加/删除安全。
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    # ---- 写（自动登记到当前事务，无事务时即时生效） ---------------------
    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            table = self.rows(module)
            table.append(row)
            tx = self._current_tx()
            if tx is not None:
                tx.append(table, row)
            return row

    def update(self, row: dict[str, Any], values: dict[str, Any]) -> None:
        with self.lock:
            snapshot = dict(row)
            row.update(values)
            tx = self._current_tx()
            if tx is not None:
                tx.update(row, snapshot)

    def delete(self, module: str, entry_id: int) -> bool:
        with self.lock:
            table = self.rows(module)
            for index, row in enumerate(table):
                if int(row.get("id", 0)) == entry_id:
                    table.pop(index)
                    tx = self._current_tx()
                    if tx is not None:
                        tx.delete(table, index, row)
                    return True
            return False

    def overview(self) -> dict[str, object]:
        with self.lock:
            modules: list[dict[str, object]] = []
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            # 概览风险点数：按剖面测点去重统计（同一测点只归属一个坡段，不会重复计数）。
            risk_points = sum(
                1 for row in self.rows("slope_profile_point") if row.get("is_risk")
            )
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
                {"label": "边坡风险点数", "value": risk_points},
            ]
            return {"cards": cards, "modules": modules, "risk_points": risk_points}


store = Store()
