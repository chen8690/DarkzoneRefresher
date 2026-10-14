"""旁白自检：给每个事件 kind 造一条样本，确认都能翻成人话、且不抛异常。

顺便守住两条硬约定（见 darkzone/narrate.py 文件头）：
  1. 翻出来的中文里不许出现事件 kind 的英文字面量（离线套件的反向断言靠这条）；
  2. 字段缺失/为 None 时不许炸。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from darkzone.narrate import narrate                     # noqa: E402

# 反向断言会数到的那些英文标识符：旁白里一律不许出现
FORBIDDEN = ("fast_confirm", "read_retry", "sweep_hit", "auto_confirm", "settle_guard",
             "blind_unclear", "blind_missed", "wait_change", "no_change", "precheck",
             "pending_dialog", "final_recheck", "confirm_in_pool", "confirm_unclear",
             "row_check", "would_tap", "charge", "abort", "plan", "tap")

SAMPLES = [
    {"kind": "start", "dry_run": False, "dialog_mode": "blind", "blind_settle": 2.2,
     "limit": 40, "ocr": "win_ocr", "win_ocr_worker": "ok"},
    {"kind": "start", "dry_run": True, "dialog_mode": "read", "limit": 5, "ocr": "rapid"},
    {"kind": "detail", "chain": "stream", "win_ocr_worker": "ok"},
    {"kind": "list"}, {"kind": "dialog"},
    {"kind": "tap", "tag": "slot2_refresh3", "at": [2937, 520]},
    {"kind": "tap", "tag": "boss_refresh1", "at": [2920, 260]},
    {"kind": "tap", "tag": "select", "at": [900, 1315]},
    {"kind": "tap", "tag": "slot1_refresh1_fastok", "at": [1592, 1032]},
    {"kind": "would_tap", "tag": "slot1_refresh1", "at": [2937, 296]},
    {"kind": "refuse_tap", "tag": "slot1_refresh1", "reason": "停止位"},
    {"kind": "sweep_hit", "tag": "slot2_refresh1", "tries": 2, "offset": [-0.012, 0.0]},
    {"kind": "confirm_dialog", "tag": "slot2_refresh1_settle", "at": [1592, 1042]},
    {"kind": "fast_confirm", "at": [1592, 1032]},
    {"kind": "would_confirm", "prompt": "是否刷新特殊条件：M4A1突击步枪"},
    {"kind": "auto_confirm", "prompt": "是否刷新特殊条件：M4A1突击步枪", "at": [1592, 1032]},
    {"kind": "dialog_ready", "tries": 2},
    {"kind": "dialog_gone"},
    {"kind": "dialog_stuck", "prompt": "是否刷新特殊条件：X"},
    {"kind": "refuse_dialog", "prompt": "购买金币"},
    {"kind": "blind_missed", "note": "…"},
    {"kind": "blind_unclear", "note": "…"},
    {"kind": "settle_guard", "wait": 0.9, "why": "blind_confirm"},
    {"kind": "settle_guard", "wait": 0.9, "why": "dialog_ok"},
    {"kind": "read_retry", "tries": 2, "wait": 0.7},
    {"kind": "wait_change", "slot": 2, "reads": 2, "waited": 0.59, "changed": True,
     "raw": "AR30栓动步枪"},
    {"kind": "wait_change", "slot": 2, "reads": 0, "changed": False, "raw": "M4A1突击步枪"},
    {"kind": "wait_change", "slot": 2, "screen_kind": "dialog", "reads": 1},
    {"kind": "settle", "slot": 1, "reads": 2, "settled": True, "rows_changed": True},
    {"kind": "settle", "slot": 1, "reads": 3, "settled": False, "rows_changed": False},
    {"kind": "row_check", "slot": 1, "attempt": 0, "raw": "AUG突击步枪", "in_pool": True,
     "precheck": True, "skip": True},
    {"kind": "row_check", "slot": 1, "attempt": 0, "raw": "AUG突击步枪", "in_pool": True},
    {"kind": "row_check", "slot": 1, "attempt": 0, "raw": "M4A1突击步枪", "in_pool": False},
    {"kind": "row_check", "slot": 1, "attempt": 2, "raw": "AR30栓动步枪", "in_pool": True},
    {"kind": "row_check", "slot": 1, "attempt": 2, "raw": "T03突击步枪", "in_pool": False},
    {"kind": "confirm_out", "slot": 1, "raw": "AUG突击步枪", "agree": 2, "need": 2},
    {"kind": "confirm_in_pool", "slot": 1, "raw": "AUG突击步枪"},
    {"kind": "confirm_unclear", "got": "detail", "rows": 2},
    {"kind": "row_missing", "slot": 3},
    {"kind": "row_count_unexpected", "got": 2, "want": 3},
    {"kind": "unexpected_screen", "got": "list"},
    {"kind": "no_change", "stale": 3, "tag": "slot1_refresh1"},
    {"kind": "boss_check", "name": "阿贾克斯", "excluded": True},
    {"kind": "boss_check", "name": "德尔文", "excluded": False},
    {"kind": "entered_detail", "boss": "德尔文"},
    {"kind": "charge", "charge_kind": "row", "slot": 1, "attempt": 2, "amount": 500,
     "total": 5000, "item": "M4A1突击步枪", "sec": 4.86},
    {"kind": "row", "boss": True, "attempt": 1, "amount": 15000, "total": 15000},
    {"kind": "give_up", "phase": "row", "slot": 1, "attempts": 40, "item": "M4A1突击步枪"},
    {"kind": "give_up", "phase": "list", "attempts": 40, "name": "阿贾克斯"},
    {"kind": "validate", "summary": "首领=德尔文 规则块=暗区首领 → 通过"},
    {"kind": "stop_before_dispatch", "ready": True},
    {"kind": "stop_before_dispatch", "ready": False},
    {"kind": "pending_dialog", "slot": 2, "prompt": "是否刷新特殊条件：X"},
    {"kind": "pending_dialog_left", "prompt": "购买金币", "slot": 2},
    {"kind": "final_recheck", "slot": 2, "raw": "BT101战术防弹衣", "in_pool": True},
    {"kind": "done", "status": "ready", "elapsed_s": 16.7, "report": "logs\\run_x.json"},
    {"kind": "abort", "reason": "detail 页面读到 2 行", "got": "detail", "slot": 3},
    {"kind": "warn", "msg": "常驻 OCR worker 起不来，退回一次性进程"},
    {"kind": "plan", "block": {"title": "暗区首领", "timing": "验证完暗区首领后",
                               "condition": "除了阿贾克斯和雷诺伊尔", "columns": [1, 2, 3],
                               "excluded": ["阿贾克斯", "雷诺伊尔"]},
     "limit": 40, "screen": "detail", "chain": "stream",
     "rows": [{"slot": 1, "raw": "M4A1突击步枪", "in_pool": False,
               "refresh_tap": [2937, 296], "pool": ["AUG突击步枪"]}],
     "boss": {"name": "伯纳德", "excluded": False}},
    {"kind": "没见过的种类"},
    {"kind": None},
    {},
]

bad = 0
for ev in SAMPLES:
    try:
        text = narrate(ev)
    except Exception as exc:                     # noqa: BLE001
        bad += 1
        print(f"[FAIL] {ev.get('kind')} 抛异常 {type(exc).__name__}: {exc}")
        continue
    if text is None:
        print(f"[skip] {ev.get('kind')} → 不打印（认不出来，符合约定）")
        continue
    hit = [w for w in FORBIDDEN if w in text]
    if hit:
        bad += 1
        print(f"[FAIL] {ev.get('kind')} 旁白里出现英文标识符 {hit}: {text}")
    print(f"[ok  ] {text}")

# 顺带确认：值缺失也不会炸（每个样本都砍掉字段再翻一遍）
for ev in SAMPLES:
    try:
        narrate({k: v for k, v in ev.items() if k == "kind"})
    except Exception as exc:                     # noqa: BLE001
        bad += 1
        print(f"[FAIL] 只剩 kind 时 {ev.get('kind')} 炸了：{type(exc).__name__}: {exc}")

print(f"\n{'全部通过' if not bad else f'失败 {bad} 条'}")