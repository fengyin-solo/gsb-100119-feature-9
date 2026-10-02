"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
处置闭环用到的排水设施台账、设施整改待办、路段监测清单、观测与处置记录也收在这里，
并提供事务上下文：事务内任意一次写入抛错都会回滚整批，不留下半成品。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 处置闭环专用表名
T_DRAINAGE = "drainage"  # 排水设施台账（复用既有模块表）
T_TODO = "drainage_rectify_todo"  # 设施整改待办
T_MONITOR = "road_section_monitor"  # 路段监测清单
T_OBSERVATION = "drainage_observation"  # 观测历史（监测/人工分别留档）
T_DISPOSAL = "drainage_disposal"  # 处置主记录（含中断与幂等键）

DISPOSAL_TABLES = (T_DRAINAGE, T_TODO, T_MONITOR, T_OBSERVATION, T_DISPOSAL)

# 可模拟某阶段写入失败的阶段名（与服务层 STAGE_* 对齐），仅用于演示/测试事务回滚
FAIL_STAGES = ("ledger", "todo", "monitor", "observation", "disposal")


class WriteFailure(RuntimeError):
    """事务内某一步落库失败；抛出后整个事务回滚，外部记中断原因码。"""

    def __init__(self, stage: str, reason: str = "DB_WRITE_FAILED") -> None:
        super().__init__(f"阶段 {stage} 写入失败（{reason}）")
        self.stage = stage
        self.reason = reason


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for table in DISPOSAL_TABLES:
            self._tables.setdefault(table, [])

    def reset(self) -> None:
        """恢复到种子数据，供测试隔离使用。"""
        self.__init__()  # type: ignore[misc]

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in DISPOSAL_TABLES and name != T_DRAINAGE:
                # 处置闭环内部表不进运营看板，避免与业务模块重复计数
                continue
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
        ]
        return {"cards": cards, "modules": modules}

    @contextmanager
    def transaction(
        self,
        *,
        touch: tuple[str, ...] = DISPOSAL_TABLES,
        fail_stage: str | None = None,
    ) -> Iterator[None]:
        """在内存仓库上模拟数据库事务。

        进入时对涉及的表做快照，块内任意异常都按快照整体回滚后再抛出；
        ``fail_stage`` 非空时在提交前注入一次写入失败，用于验证三表同事务回滚。
        """
        snapshot = {name: copy.deepcopy(self._tables.get(name, [])) for name in touch}
        try:
            yield
            if fail_stage:
                if fail_stage not in FAIL_STAGES:
                    raise ValueError(f"未知失败阶段：{fail_stage}")
                raise WriteFailure(fail_stage)
        except BaseException:
            for name, data in snapshot.items():
                self._tables[name] = copy.deepcopy(data)
            raise


store = Store()
