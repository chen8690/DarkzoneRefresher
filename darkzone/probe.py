"""真机探针：截一张图 → 打印界面解析结果和所有可点坐标；可选点一个目标。

只用来「核对坐标」和「手动走一步」，不做刷新循环（刷新循环在 runner.py）。

用法::

    python -m darkzone.probe                      # 只看当前界面，不发任何点击
    python -m darkzone.probe --tap select         # 点「选择」进词条详情页
    python -m darkzone.probe --tap row1           # 点第 1 行装备的 ↻

安全约定：默认拒绝点「派遣」（要显式 `--allow-dispatch`），因为这会把派遣任务真发出去。
"""

from __future__ import annotations

import argparse
import json
import time

TARGETS = ("boss_refresh", "select", "row1", "row2", "row3", "dispatch")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="darkzone.probe",
                                 description="真机探针：截图 + 解析坐标，可选点一个目标")
    ap.add_argument("--rules", default=None,
                    help="规则表 xlsx；默认先找项目根 规则表.xlsx，再找桌面 工作簿1.xlsx")
    ap.add_argument("--block", type=int, default=0)
    ap.add_argument("--tap", default=None, choices=list(TARGETS), help="点了再截一张图")
    ap.add_argument("--allow-dispatch", action="store_true",
                    help="允许点「派遣」（默认拒绝：会真的把派遣发出去）")
    ap.add_argument("--wait", type=float, default=1.5, help="点击后等几秒再截图，默认1.5")
    ap.add_argument("--serial", default=None)
    a = ap.parse_args(argv)

    from .adb import Adb, find_adb
    from .rules import default_rules_path, parse_workbook
    from .runner import Config, Runner
    from .source import LiveSource

    book = parse_workbook(a.rules or default_rules_path())
    adb = Adb(a.serial, find_adb())
    d = adb.wait_for_device(timeout=30)
    if not d:
        print("[NG] adb 里没有设备；请插 USB、手机上点「允许 USB 调试」")
        return 3
    print(f"[OK] 设备 {d['serial']} {d['state']} {d['info']}")

    src = LiveSource(adb, work_dir="logs")
    # dry_run=True：读屏/算坐标阶段绝不点击；真正的点击在本脚本末尾显式发出。
    r = Runner(book, src, Config(dry_run=True, block_index=a.block))
    st = r._read("probe")
    plan = r.plan(st)
    print(json.dumps(plan, ensure_ascii=False, indent=2)[:4000])

    if not a.tap:
        return 0

    xy = None
    boss = plan.get("boss")
    if a.tap == "boss_refresh" and isinstance(boss, dict):
        xy = boss.get("refresh_tap")
    elif a.tap == "select" and isinstance(boss, dict):
        xy = boss.get("select_tap")
    elif a.tap.startswith("row"):
        want = int(a.tap[3:])
        for row in plan.get("rows", []):
            if row.get("slot") == want:
                xy = row.get("refresh_tap")
                break
    elif a.tap == "dispatch":
        if not a.allow_dispatch:
            print("[NG] 拒绝点「派遣」：会真的把派遣任务发出去；确实要点请加 --allow-dispatch")
            return 4
        xy = getattr(st["screen"], "dispatch", None)

    if xy is None:
        print(f"[NG] 当前界面是 {plan.get('screen')!r}，上面没有 {a.tap} 这个目标")
        return 5

    print(f"[OK] 点 {a.tap} @ ({xy[0]:.0f}, {xy[1]:.0f})")
    src.tap(xy)
    time.sleep(a.wait)
    st2 = r._read(f"after_{a.tap}")
    print(json.dumps(r.plan(st2), ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
