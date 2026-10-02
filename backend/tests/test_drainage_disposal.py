"""排水设施处置闭环契约测试（只依赖标准库：本机未安装 pytest 时可直接 python3 运行）。

覆盖：
1. 四类空态/边界分支与正常排水互斥；
2. 监测与人工复测冲突以现场为准、历史按观测时点分别留档；
3. 台账/待办/清单同事务，任一阶段失败整批回滚；
4. 中断保留原因码、从失败分支续传、重复回传幂等。

运行：cd backend && python3 -m tests.test_drainage_disposal
"""
from __future__ import annotations

import sys
import traceback

from app.services.drainage_disposal import (
    BRANCH_FACILITY_MISSING,
    BRANCH_NO_WATER,
    BRANCH_NORMAL,
    BRANCH_PUMP_OFFLINE,
    BRANCH_RECHECK_TIMEOUT,
    STATUS_DONE,
    STATUS_INTERRUPTED,
    STATUS_RESET,
    DrainageDisposalService,
    classify,
)
from app.store import T_DISPOSAL, T_DRAINAGE, T_MONITOR, T_OBSERVATION, T_TODO, store

CASES: list[tuple[str, callable]] = []  # type: ignore[type-arg]


def case(name: str):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


def fresh() -> DrainageDisposalService:
    store.reset()
    return DrainageDisposalService()


def base(**overrides):
    values = {
        "设施编号": "DRAI-TEST",
        "设施类型": "雨水口",
        "所属路段": "测试路",
        "桩号位置": "K1+000",
        "观测时间": "2026-10-02T09:00:00",
        "监测积水深度": "0.30",
        "监测泵站状态": "在线",
        "监测时间": "2026-10-02T08:55:00",
        "人工积水深度": "0.30",
        "人工泵站状态": "在线",
        "人工复测时间": "2026-10-02T09:05:00",
    }
    values.update(overrides)
    return values


# ---- 1. 分支判定：空态/边界与正常排水不混用 -----------------------------
@case("正常排水：两侧一致且有积水")
def _():
    svc = fresh()
    res = svc.submit(base())
    assert res["ok"] and not res.get("中断"), res
    rec = res["record"]
    assert rec["分支编码"] == BRANCH_NORMAL and rec["状态"] == STATUS_DONE
    assert rec["处置结论"] == "正常排水"
    assert rec["可复位"] is False


@case("无积水：进入可复位分支，不与正常排水混用")
def _():
    svc = fresh()
    res = svc.submit(base(监测积水深度="0", 人工积水深度="0", 监测泵站状态="", 人工泵站状态=""))
    assert res["ok"], res
    rec = res["record"]
    assert rec["分支编码"] == BRANCH_NO_WATER and rec["可复位"] is True
    assert "正常排水" not in rec["处置结论"]
    todo = [t for t in svc.list_todos() if t["关联单号"] == rec["处置单号"]][0]
    assert todo["待办类型"] == "复位核查" and todo["可复位"] is True


@case("泵站离线：采信人工离线状态")
def _():
    svc = fresh()
    res = svc.submit(base(人工泵站状态="离线", 人工积水深度="0.40"))
    assert res["ok"], res
    assert res["record"]["分支编码"] == BRANCH_PUMP_OFFLINE
    led = [r for r in store.rows(T_DRAINAGE) if r.get("设施编号") == "DRAI-TEST"][0]
    assert "泵站离线" in led["设施状态"] and led["abnormal"] is True


@case("复测超时：人工复测未回且超时，挂超时分支")
def _():
    svc = fresh()
    values = base(复测超时="是", 人工积水深度="", 人工泵站状态="", 人工复测时间="")
    assert classify(values)["branch"] == BRANCH_RECHECK_TIMEOUT
    res = svc.submit(values)
    assert res["record"]["分支编码"] == BRANCH_RECHECK_TIMEOUT
    assert res["record"]["可复位"] is True


@case("设施编号缺失：编号缺失优先级最高")
def _():
    svc = fresh()
    res = svc.submit(base(设施编号=" "))
    assert res["ok"], res
    rec = res["record"]
    assert rec["分支编码"] == BRANCH_FACILITY_MISSING
    todo = [t for t in svc.list_todos() if t["关联单号"] == rec["处置单号"]][0]
    assert todo["待办类型"] == "补录编号"
    # 台账仍要留档但不能按正常编号混入，靠关联单号挂住
    led = [r for r in store.rows(T_DRAINAGE) if r.get("关联单号") == rec["处置单号"]]
    assert led and led[0]["设施编号"] == ""


@case("缺观测时间直接拒绝（幂等键不完整）")
def _():
    svc = fresh()
    res = svc.submit(base(观测时间=""))
    assert res["ok"] is False and "观测时间" in res["message"]


# ---- 2. 冲突以现场复测为准，历史按观测时点留档 ---------------------------
@case("冲突采信人工：监测无积水 vs 人工有积水")
def _():
    svc = fresh()
    values = base(监测积水深度="0", 监测泵站状态="", 人工积水深度="0.32", 人工泵站状态="在线")
    verdict = classify(values)
    assert verdict["conflicted"] is True
    assert verdict["branch"] == BRANCH_NORMAL  # 以人工为准
    res = svc.submit(values)
    rec = res["record"]
    assert rec["监测复测冲突"] is True and rec["数据来源"] == "现场人工复测"
    item = [m for m in svc.list_monitor_items() if m["关联单号"] == rec["处置单号"]][0]
    assert item["积水深度"] == "0.32" and "已按现场复测采信" in item["监测复测冲突"]


@case("冲突采信人工：监测在线 vs 人工泵站离线")
def _():
    svc = fresh()
    values = base(监测泵站状态="在线", 人工泵站状态="离线", 监测积水深度="0.3", 人工积水深度="0.3")
    verdict = classify(values)
    assert verdict["conflicted"] is True and verdict["branch"] == BRANCH_PUMP_OFFLINE


@case("观测历史：监测与人工按各自时点分别留档，互不覆盖")
def _():
    svc = fresh()
    res = svc.submit(base())
    biz = res["record"]["处置单号"]
    obs = [o for o in svc.list_observations() if o["关联单号"] == biz]
    assert len(obs) == 2, obs
    by_source = {o["观测来源"]: o for o in obs}
    assert by_source["在线监测"]["观测时点"] == "2026-10-02T08:55:00"
    assert by_source["现场人工复测"]["观测时点"] == "2026-10-02T09:05:00"
    assert by_source["现场人工复测"]["是否采信"] is True
    assert by_source["在线监测"]["是否采信"] is False


# ---- 3. 三表同事务：任一写入失败整批回滚 ---------------------------------
@case("台账阶段失败：三表与主记录均无新增")
def _():
    svc = fresh()
    counts = lambda: (
        len(store.rows(T_DRAINAGE)), len(store.rows(T_TODO)),
        len(store.rows(T_MONITOR)), len(store.rows(T_OBSERVATION)),
        len(store.rows(T_DISPOSAL)),
    )
    before = counts()
    res = svc.submit(base(模拟落库失败阶段="ledger"))
    after = counts()
    assert res["ok"] is False and res["中断"] is True
    # 台账/待办/清单/观测全部回滚；仅中断主记录留痕（事务外）
    assert after[:4] == before[:4], (before, after)
    assert after[4] == before[4] + 1


@case("清单阶段失败：台账与待办也一起回滚，不允许只成两表")
def _():
    svc = fresh()
    drain_before = len(store.rows(T_DRAINAGE))
    res = svc.submit(base(设施编号="DRAI-ROLLBACK-1", 模拟落库失败阶段="monitor"))
    assert res["ok"] is False
    assert len(store.rows(T_DRAINAGE)) == drain_before
    assert not [t for t in store.rows(T_TODO) if t.get("设施编号") == "DRAI-ROLLBACK-1"]
    assert not [m for m in store.rows(T_MONITOR) if m.get("设施编号") == "DRAI-ROLLBACK-1"]
    rec = res["record"]
    assert rec["状态"] == STATUS_INTERRUPTED
    assert rec["原因码"] == "DB_MONITOR_FAILED" and rec["失败阶段"] == "monitor"


@case("待办阶段失败：原因码 DB_TODO_FAILED，失败分支可识别")
def _():
    svc = fresh()
    res = svc.submit(base(模拟落库失败阶段="todo"))
    rec = res["record"]
    assert rec["原因码"] == "DB_TODO_FAILED"
    assert rec["分支编码"] == BRANCH_NORMAL


# ---- 4. 中断续传：原因码留痕、从失败分支继续 -----------------------------
@case("客户端保存中断：保留 CLIENT_OFFLINE，不写业务表")
def _():
    svc = fresh()
    drain_before = len(store.rows(T_DRAINAGE))
    res = svc.submit(base(模拟中断="是"))
    assert res["中断"] is True
    rec = res["record"]
    assert rec["状态"] == STATUS_INTERRUPTED and rec["原因码"] == "CLIENT_OFFLINE"
    assert rec["分支编码"] == BRANCH_NORMAL  # 分支已判定，等待续传
    assert len(store.rows(T_DRAINAGE)) == drain_before
    assert store.rows(T_TODO) == [] and store.rows(T_MONITOR) == []


@case("重连续传：从失败分支完成，三表补齐，原原因码清除")
def _():
    svc = fresh()
    interrupted = svc.submit(base(模拟落库失败阶段="todo"))["record"]
    rid = interrupted["id"]
    # 续传时不再注入失败
    res = svc.resume(rid, {"模拟落库失败阶段": ""})
    assert res["ok"] is True, res
    rec = res["record"]
    assert rec["状态"] == STATUS_DONE and rec["原因码"] == ""
    assert rec["分支编码"] == BRANCH_NORMAL
    assert len(store.rows(T_TODO)) == 1 and len(store.rows(T_MONITOR)) == 1
    assert len(store.rows(T_OBSERVATION)) == 2  # 失败回滚后观测不重复留档


@case("续传仍失败：保留新原因码，整批再次回滚，可再续传")
def _():
    svc = fresh()
    rid = svc.submit(base(模拟落库失败阶段="ledger"))["record"]["id"]
    res = svc.resume(rid, {"模拟落库失败阶段": "monitor"})
    assert res["ok"] is False
    rec = res["record"]
    assert rec["原因码"] == "DB_MONITOR_FAILED" and rec["续传次数"] == 1
    assert store.rows(T_TODO) == []
    again = svc.resume(rid, {"模拟落库失败阶段": ""})
    assert again["ok"] is True and again["record"]["状态"] == STATUS_DONE


# ---- 5. 幂等：设施编号 + 观测时间 ----------------------------------------
@case("重复回传：返回首次结论，不重复落库")
def _():
    svc = fresh()
    first = svc.submit(base())
    assert first.get("重复回传") is None
    biz = first["record"]["处置单号"]
    second = svc.submit(base(人工积水深度="0.90"))  # 改数据也不影响幂等
    assert second["ok"] is True and second.get("重复回传") is True
    assert second["record"]["处置单号"] == biz
    todos = [t for t in store.rows(T_TODO) if t["关联单号"] == biz]
    assert len(todos) == 1
    monitors = [m for m in store.rows(T_MONITOR) if m["关联单号"] == biz]
    assert len(monitors) == 1


@case("同设施不同观测时间：不视为重复，分别处置")
def _():
    svc = fresh()
    svc.submit(base(观测时间="2026-10-02T09:00:00"))
    res2 = svc.submit(base(观测时间="2026-10-02T11:00:00"))
    assert res2.get("重复回传") is None
    assert len(store.rows(T_DISPOSAL)) == 2


@case("中断单重复回传：幂等返回中断记录而不是再插一条")
def _():
    svc = fresh()
    first = svc.submit(base(模拟中断="是"))
    second = svc.submit(base())
    assert second.get("重复回传") is True
    assert second["record"]["id"] == first["record"]["id"]
    assert second["record"]["状态"] == STATUS_INTERRUPTED


# ---- 6. 复位 -------------------------------------------------------------
@case("可复位分支可复位：待办关闭、清单与台账同步，主记录标记已复位")
def _():
    svc = fresh()
    rec = svc.submit(base(监测积水深度="0", 人工积水深度="0", 监测泵站状态="", 人工泵站状态=""))["record"]
    res = svc.reset(rec["id"])
    assert res["ok"], res
    todo = [t for t in store.rows(T_TODO) if t["关联单号"] == rec["处置单号"]][0]
    item = [m for m in store.rows(T_MONITOR) if m["关联单号"] == rec["处置单号"]][0]
    assert todo["待办状态"] == "已取消(复位)" and item["清单状态"] == "已复位"
    assert res["record"]["状态"] == STATUS_RESET


@case("正常排水不可复位；未完成中断单不可复位")
def _():
    svc = fresh()
    normal = svc.submit(base())["record"]
    assert svc.reset(normal["id"])["ok"] is False
    interrupted = svc.submit(base(观测时间="2026-10-02T12:00:00", 模拟中断="是"))["record"]
    assert svc.reset(interrupted["id"])["ok"] is False
    # 续传完成后才允许复位（它是正常分支，仍不可复位；这里用无积水分支验证路径）
    rid = svc.submit(base(
        观测时间="2026-10-02T13:00:00",
        监测积水深度="0", 人工积水深度="0",
        监测泵站状态="", 人工泵站状态="",
        模拟落库失败阶段="todo",
    ))["record"]["id"]
    assert svc.reset(rid)["ok"] is False
    assert svc.resume(rid, {"模拟落库失败阶段": ""})["ok"] is True
    assert svc.reset(rid)["ok"] is True


@case("复位后同键可重新回传（旧记录不参与幂等）")
def _():
    svc = fresh()
    values = base(监测积水深度="0", 人工积水深度="0", 监测泵站状态="", 人工泵站状态="")
    first = svc.submit(values)["record"]
    svc.reset(first["id"])
    again = svc.submit(values)
    assert again.get("重复回传") is None
    assert again["record"]["id"] != first["id"]


def main() -> int:
    failures = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:  # noqa: BLE001 - 契约测试需要汇总全部失败
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
