"""主循环端到端离线测试：假手机（fake_screens.FakePhone）当被控端，真规则表当校验源。

场景 A–W（头四个举例，全量表见 README 的「离线测试」一节）：
  A 正常路径     ：首领刷到「德尔文」→ 进详情 → 逐行刷词条 → 三行就绪 → 停手不点派遣
  B 点歪了       ：热区整体偏 5% 宽（关掉扫描兜底）→ 必须连续 3 次没变化就报错停止，不能硬刷
  C 扫描兜底     ：热区偏 2.4% 宽（正好落在 tap_sweep 的候选点上）→ 记 sweep_hit 并继续
  D dry-run      ：一次都不许点，只出「下一步会点哪里」的计划
"""

from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 受控台在中文 Windows 上默认是 GBK，而本套件的日志里有 ↻ 这类字符：
# 末尾那次 print 会 UnicodeEncodeError 把整套件的退出码打成 1（断言其实全过）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from darkzone.fake_screens import IMG_H, IMG_W, FakePhone          # noqa: E402
from darkzone.rules import default_rules_path, parse_workbook      # noqa: E402
from darkzone.runner import Config, Runner                         # noqa: E402

XLSX = default_rules_path()
OUT = ROOT / "runner_test_out.txt"
WORK = ROOT / "logs_test"

buf = []
fails = []


def out(s=""):
    buf.append(str(s))


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        fails.append(f"{name} {detail}")
    out(f"[{tag}] {name} {detail}")


book = parse_workbook(XLSX)
block = book.blocks[0]
out(f"规则块：{block.title} / {block.condition} / 池={ {k: len(v) for k, v in block.pools.items()} } "
    f"/ 排除={block.excluded}")

# 词条池里的样本（拿规则表自己的词，保证一定在池内）
POOL1 = block.pools[1][2]      # AUG突击步枪
POOL2 = block.pools[2][0]      # BT101战术防弹衣
POOL3 = block.pools[3][1]      # 野战登山背包
out(f"取池内样本：{POOL1} / {POOL2} / {POOL3}")


def make_phone(tag, **kw):
    kw.setdefault("boss_queue", ("阿贾克斯", "雷诺伊尔", "德尔文"))
    kw.setdefault("row_queues", {1: ["M4A1突击步枪", "T03突击步枪", POOL1],
                                 2: [POOL2],
                                 3: ["03重型战术头盔", POOL3]})
    kw.setdefault("work_dir", WORK / tag)
    return FakePhone(**kw)


def run(phone, **cfgkw):
    cfgkw.setdefault("log_dir", str(WORK / "logs"))
    cfgkw.setdefault("work_dir", str(WORK / "logs"))
    cfgkw.setdefault("wait_after_tap", 0.0)
    cfgkw.setdefault("wait_after_screen", 0.0)
    cfgkw.setdefault("read_retry_delay", 0.0)   # 测试里别真的睡
    cfgkw.setdefault("read_retries", 2)         # 测试里别多读太多次（每读一次都要 OCR）
    sink = io.StringIO()
    with redirect_stdout(sink):
        result = Runner(book, phone, Config(**cfgkw)).run()
    return result, sink.getvalue()


# ---------------------------------------------------------------- A 正常路径
out("\n=== A 正常路径 ===")
phone = make_phone("A")
res, log = run(phone, dry_run=False, max_refresh_per_row=10)
out(json.dumps({k: v for k, v in res.items() if k != "events"}, ensure_ascii=False, indent=2))
check("A 状态=ready", res.get("status") == "ready", str(res.get("status")))
check("A 首领刷到德尔文", res.get("boss") == "德尔文", str(res.get("boss")))
check("A 首领刷新 2 次", res.get("boss_refreshes") == 2, str(res.get("boss_refreshes")))
rows = {r["slot"]: r for r in res.get("rows", [])}
check("A 槽1 命中池内且刷 2 次", rows.get(1, {}).get("item") == POOL1
      and rows[1]["refreshes"] == 2, str(rows.get(1)))
check("A 槽2 本来就在池内（0 次）", rows.get(2, {}).get("refreshes") == 0, str(rows.get(2)))
check("A 槽3 刷 1 次命中", rows.get(3, {}).get("refreshes") == 1, str(rows.get(3)))
check("A 规则复核通过且无 NG", bool(res.get("reports")) and all("→ 通过" in s and "[NG" not in s
      for s in res.get("reports", [])), str(res.get("reports")))
check("A 没有点派遣", phone.dispatch_tapped is False)
check("A 每一次点击都落在热区上（坐标没算歪）",
      all(hit is not None for _, hit in phone.taps),
      str([t for t in phone.taps if t[1] is None]))
check("A 刷新次数=2+2+0+1=5", phone.refresh_count == 5, str(phone.refresh_count))
cost_a = res.get("cost") or {}
check("A 花销台账：行刷 3 次 = 1500 券", cost_a.get("row_charged") == 3
      and cost_a.get("spent_row") == 1500 and cost_a.get("unit_row_cost") == 500, str(cost_a))
check("A 日志里有 charge 流水笔数 = 行3+首领2", len(cost_a.get("ledger") or []) == 5,
      str(len(cost_a.get("ledger") or [])))
check("A 有 stop_before_dispatch 事件", "stop_before_dispatch" in log)

# ---------------------------------------------------------------- B 点歪了
out("\n=== B 点歪了（无兜底）→ 必须停下报错 ===")
phone_b = make_phone("B", hot_shift=(0.05 * IMG_W, 0))
res_b, log_b = run(phone_b, dry_run=False, max_refresh_per_row=10, tap_sweep=False, stale_limit=3)
out(json.dumps(res_b, ensure_ascii=False, indent=2))
check("B 状态=error", res_b.get("status") == "error", str(res_b.get("status")))
check("B 原因=no_change", res_b.get("reason") == "no_change", str(res_b.get("reason")))
check("B 只点了 3 次就停（不硬刷）", len(phone_b.taps) == 3, str(len(phone_b.taps)))
check("B 全部点空", all(hit is None for _, hit in phone_b.taps))
check("B 日志里有 abort", "abort" in log_b)

# ---------------------------------------------------------------- C 扫描兜底
out("\n=== C 扫描兜底 → 偏移撞上候选点 ===")
phone_c = make_phone("C", boss_queue=("阿贾克斯",), hot_shift=(0.024 * IMG_W, 0),
                     row_queues={1: [POOL1], 2: [POOL2], 3: [POOL3]})
res_c, log_c = run(phone_c, dry_run=False, max_refresh_per_row=1, tap_sweep=True, stale_limit=3)
out(json.dumps(res_c, ensure_ascii=False, indent=2))
check("C 记到 sweep_hit", "sweep_hit" in log_c)
check("C 上限 1 次后 give_up", res_c.get("status") == "give_up", str(res_c.get("status")))
check("C 扫描确实点到了热区", any(hit is not None for _, hit in phone_c.taps),
      str(phone_c.taps))

# ---------------------------------------------------------------- D dry-run
out("\n=== D dry-run：一次都不许点 ===")
phone_d = make_phone("D")
res_d, log_d = run(phone_d, dry_run=True)
out(json.dumps(res_d, ensure_ascii=False, indent=2))
check("D 状态=dry_run", res_d.get("status") == "dry_run", str(res_d.get("status")))
check("D 一次都没点", phone_d.taps == [], str(phone_d.taps))
bx = res_d.get("boss") if isinstance(res_d.get("boss"), dict) else {}
check("D 计划含首领刷新坐标", isinstance(bx.get("refresh_tap"), list), str(res_d.get("boss")))
rt = bx.get("refresh_tap") or (0, 0)
check("D 首领↻真机坐标≈(2912,193)", abs(rt[0] - 2912) <= 20 and abs(rt[1] - 193) <= 20, str(rt))
check("D 首领=阿贾克斯 判为排除", bx.get("excluded") is True, str(bx.get("excluded")))
check("D 写出了 plan 事件", '"kind": "plan"' in log_d)
check("D 预演花销=0 券", (res_d.get("cost") or {}).get("spent") == 0, str(res_d.get("cost")))

# ---------------------------------------------------------------- E 竖屏保护
out("\n=== E 竖屏（游戏没横过来）→ 必须直接拒绝，不能瞎点 ===")
phone_e = make_phone("E", image_size=(1280, 2844))
res_e, log_e = run(phone_e, dry_run=True)
out(json.dumps(res_e, ensure_ascii=False, indent=2))
check("E 状态=error", res_e.get("status") == "error", str(res_e.get("status")))
check("E 原因=portrait_screen", res_e.get("reason") == "portrait_screen", str(res_e.get("reason")))
check("E 一次都没点", phone_e.taps == [], str(phone_e.taps))

# ---------------------------------------------------------------- F 派遣安全阀
out("\n=== F 派遣安全阀：任何离「派遣」太近的目标一律拒点 ===")
from darkzone.runner import SafetyError                        # noqa: E402
from darkzone.ui import CoordMapper                            # noqa: E402

phone_f = make_phone("F")
guard = Runner(book, phone_f, Config(dry_run=False, log_dir=str(WORK / "logs"),
                                     work_dir=str(WORK / "logs")))
guard.mapper = CoordMapper((2844, 1280), (3200, 1440))
guard.dispatch_xy = (2385, 1187)
raised, detail = False, ""
try:
    guard._tap((2400, 1190), "guard_test")
except SafetyError as e:
    raised, detail = True, str(e)
check("F 靠近派遣被拒绝并抛 SafetyError", raised, detail)
guard._tap((2588, 661), "far_test")
check("F 远处正常放行（假手机确实被点到）", len(phone_f.taps) == 1, str(phone_f.taps))
res_f, log_f = run(make_phone("F2", hot_shift=(0.05 * IMG_W, 0)), dry_run=False,
                   tap_sweep=False, stale_limit=1)
check("F 正常流程里没触发安全阀", res_f.get("reason") != "safety_guard", str(res_f))

# ---------------------------------------------------------------- G 刷新确认弹窗
# 真机实测（m00551–m00637）：点 ↻ 不会直接刷新，而是先弹「是否刷新特殊条件：xxx」+「确定」。
out("\n=== G 点 ↻ 会先弹确认框：必须点「确定」才真的刷新 ===")
phone_g = make_phone("G")
res_g, log_g = run(phone_g, dry_run=False, max_refresh_per_row=10)
out(json.dumps({k: v for k, v in res_g.items() if k != "events"}, ensure_ascii=False, indent=2))
seq = [h for _, h in phone_g.taps]
check("G 正常跑完且 ready", res_g.get("status") == "ready", str(res_g.get("status")))
check("G 弹了 5 次确认框（2 次首领 + 3 次词条）", phone_g.dialog_count == 5,
      str(phone_g.dialog_count))
check("G 点「确定」5 次、刷新计数 5",
      seq.count("dialog_confirm") == 5 and phone_g.refresh_count == 5,
      f"confirm={seq.count('dialog_confirm')} refresh_count={phone_g.refresh_count}")
nondialog = [i for i, h in enumerate(seq) if h != "dialog_confirm"]
refresh_taps = [i for i, h in enumerate(seq) if h and h.startswith(("list_refresh", "row"))]
check("G 每次点刷新 ↻ 后紧跟着点「确定」",
      len(refresh_taps) == 5 and len(nondialog) == 6
      and all(seq[i + 1] == "dialog_confirm" for i in refresh_taps), str(seq))
check("G 点「选择」不弹确认框（只进详情页）",
      seq[seq.index("list_select:暗区首领") + 1] != "dialog_confirm", str(seq))
check("G 弹窗期间没点空（全是热区命中）", all(h is not None for h in seq), str(seq))
check("G 日志里有 auto_confirm", "auto_confirm" in log_g)
check("G 依然没点派遣", phone_g.dispatch_tapped is False)

# ---------------------------------------------------------------- H 弹窗取消不花券
out("\n=== H 弹窗点 ✕ 取消：不该花券、不该刷新 ===")
phone_h = make_phone("H")


def tap_hot(p, name):
    hx, hy = p.hotspots()[name]
    p.tap((hx * p.sx, hy * p.sy))


tap_hot(phone_h, "list_refresh:暗区首领")
check("H 点 ↻ 只弹窗、还没刷新", phone_h.dialog is not None and phone_h.refresh_count == 0,
      f"dialog={phone_h.dialog} refresh_count={phone_h.refresh_count}")
tap_hot(phone_h, "dialog_cancel")
check("H 点 ✕ 后弹窗关掉且没花券",
      phone_h.dialog is None and phone_h.refresh_count == 0 and phone_h.resource == 1_827_000,
      f"refresh_count={phone_h.refresh_count} res={phone_h.resource}")
tap_hot(phone_h, "list_refresh:暗区首领")
tap_hot(phone_h, "dialog_confirm")
check("H 点「确定」才真的刷新（-15000 券）",
      phone_h.refresh_count == 1 and phone_h.resource == 1_812_000,
      f"refresh_count={phone_h.refresh_count} res={phone_h.resource}")

# ---------------------------------------------------------------- I 刷新前体检：符合的槽位跳过
out("\n=== I 符合规则的槽位直接跳过（每次点击前全槽位体检）===")
phone_i = make_phone("I", row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]})
res_i, log_i = run(phone_i, dry_run=False, max_refresh_per_row=5)
rows_i = {r["slot"]: r for r in res_i.get("rows", [])}
check("I 状态=ready", res_i.get("status") == "ready", str(res_i.get("status")))
check("I 槽1 刷 1 次命中池内", rows_i.get(1, {}).get("item") == POOL1
      and rows_i[1]["refreshes"] == 1, str(rows_i.get(1)))
check("I 槽2/槽3 开工前就在池内 → 0 次刷新",
      rows_i.get(2, {}).get("refreshes") == 0 and rows_i.get(3, {}).get("refreshes") == 0,
      str([rows_i.get(s) for s in (2, 3)]))
check("I 槽2/槽3 的 ↻ 一次都没被点（连体检后也跳过）",
      not any(h in ("row2", "row3") for _, h in phone_i.taps), str(phone_i.taps))
check("I 有 precheck 体检事件（skip=True）", "precheck" in log_i)
check("I 花销台账：只刷了槽1 → 500 券", (res_i.get("cost") or {}).get("spent_row") == 500
      and (res_i.get("cost") or {}).get("row_charged") == 1, str(res_i.get("cost")))
check("I 依然没点派遣", phone_i.dispatch_tapped is False)

# ---------------------------------------------------------------- J 消费台账跨次累计
out("\n=== J 消费台账：跨次累计（task_ledger.jsonl）===")
import shutil                                                   # noqa: E402
led_dir = WORK / "ledger_j"
if led_dir.exists():
    shutil.rmtree(led_dir)
r1 = Runner(book, phone_d, Config(dry_run=True, log_dir=str(led_dir), work_dir=str(led_dir)))
r1._charge("row", 500, slot=1)
r1._charge("row", 500, slot=2)
s1 = r1.cost_summary([])
r2 = Runner(book, phone_d, Config(dry_run=True, log_dir=str(led_dir), work_dir=str(led_dir)))
check("J 第一跑：本次 1000 / 累计 1000",
      s1["spent"] == 1000 and s1["spent_cumulative"] == 1000, str(s1["spent"]))
check("J 台账文件里正好 2 笔",
      (led_dir / "task_ledger.jsonl").read_text(encoding="utf-8").strip().count("\n") == 1,
      str((led_dir / "task_ledger.jsonl").read_text(encoding="utf-8")))
check("J 第二跑开工就读到历史 1000 券", r2.spent_before == 1000, str(r2.spent_before))
r2._charge("row", 500, slot=3)
check("J 第二跑：累计 1500 券", r2.cost_summary([])["spent_cumulative"] == 1500,
      str(r2.cost_summary([])["spent_cumulative"]))

# ------------------------------------------------------- K 只管详情页（默认模式）
# 用户 2026-10-11 的决定：「暗区首领那一栏彻底不管」「这条刷完就停手，我手动点派遣」。
out("\n=== K 只管详情页（默认）：从详情页开始，只点行的 ↻ ===")
phone_k = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "K")
res_k, log_k = run(phone_k, dry_run=False, max_refresh_per_row=10, detail_only=True)
out(json.dumps({k: v for k, v in res_k.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_k = {r["slot"]: r for r in res_k.get("rows", [])}
check("K 状态=ready", res_k.get("status") == "ready", str(res_k.get("status")))
check("K 三行都判成池内", len(rows_k) == 3 and all(r["status"] == "ok" for r in rows_k.values()),
      str(rows_k))
allowed_k = {"row1", "row2", "row3", "dialog_confirm"}
check("K 只点过行的 ↻ 与弹窗「确定」", all(h in allowed_k for _, h in phone_k.taps),
      str(phone_k.taps))
check("K 一次都没碰列表/首领栏/派遣",
      not any(h and (h.startswith("list_") or h == "dispatch") for _, h in phone_k.taps)
      and phone_k.dispatch_tapped is False, str(phone_k.taps))
check("K 没有首领刷新这回事", res_k.get("boss_refreshes") is None, str(res_k.get("boss_refreshes")))
check("K 只刷了槽1 → 500 券", (res_k.get("cost") or {}).get("spent") == 500
      and (res_k.get("cost") or {}).get("boss_charged") == 0, str(res_k.get("cost")))
check("K 报告里不再判排除名单（排除/不适用 都不出现）",
      bool(res_k.get("reports")) and all("排除" not in s and "不适用" not in s
                                        for s in res_k["reports"]), str(res_k.get("reports")))
check("K 输出标注 excluded_enforced=False", res_k.get("excluded_enforced") is False,
      str(res_k.get("excluded_enforced")))
check("K 首领名仍打印（仅供参考）", res_k.get("boss") == "伯纳德", str(res_k.get("boss")))

# ------------------------------------------------- L 只管详情页：列表页上一步都不走
out("\n=== L 只管详情页：当前是列表页 → 一步都不走，让人手动进详情页 ===")
phone_l = make_phone("L")
res_l, log_l = run(phone_l, dry_run=False, detail_only=True)
out(json.dumps({k: v for k, v in res_l.items() if k != "events"}, ensure_ascii=False, indent=2))
check("L 状态=error", res_l.get("status") == "error", str(res_l.get("status")))
check("L 理由=detail_only", res_l.get("reason") == "detail_only", str(res_l.get("reason")))
check("L 一次都没点", phone_l.taps == [], str(phone_l.taps))
check("L 报出当前画面种类=list", res_l.get("got") == "list", str(res_l.get("got")))
check("L 日志里让他手动点「选择」进详情页", "详情页" in log_l, log_l[-200:])

# ------------------------------- M 读早了：重读安全网（不许当「点空了」再点一次）
# 2026-10-11 提速：等待从 0.6+1.2 压到 0.25+0.55。后果是可能读到「旧画面」，
# 这里用慢手机模拟：点击生效后接下来 2 次读屏仍返回旧图。
out("\n=== M 画面延迟更新（slow_reads=2）：重读重试，不许多点一次 ===")
phone_m = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "M", slow_reads=2)
res_m, log_m = run(phone_m, dry_run=False, max_refresh_per_row=10, detail_only=True,
                   read_retries=4)
out(json.dumps({k: v for k, v in res_m.items() if k != "events"}, ensure_ascii=False, indent=2))
row_taps_m = [h for _, h in phone_m.taps if h and h.startswith("row")]
check("M 状态=ready", res_m.get("status") == "ready", str(res_m.get("status")))
check("M 三行都判成池内", all(r["status"] == "ok" for r in res_m.get("rows", [])),
      str(res_m.get("rows")))
check("M 槽1 只点了一次 ↻（没被误判成点空）", row_taps_m.count("row1") == 1, str(phone_m.taps))
check("M 没有走扫描兜底", "sweep_hit" not in log_m, "不该出现 sweep_hit")
check("M 日志里确实走了重读重试（盲点默认路下这层是 `wait_change`：点完确定那一屏还是旧的，"
      "继续读到真的换了才算）",
      '"kind": "read_retry"' in log_m or '"kind": "wait_change"' in log_m, log_m[-200:])
check("M 花销=槽1 一次 500 券", (res_m.get("cost") or {}).get("spent") == 500,
      str(res_m.get("cost")))

# ------------------------- N 盲点快路（现在是**默认** dialog_mode="blind"）：确认框不读那一屏，
# 但仍必须真刷到。2026-10-11 用户点头实现；2026-10-11 深夜改成默认。
out("\n=== N 盲点快路（dialog_mode=blind，默认）：省掉弹窗那次读屏 ===")
phone_n = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "N")
res_n, log_n = run(phone_n, dry_run=False, max_refresh_per_row=10, detail_only=True,
                   wait_dialog=0.0)
out(json.dumps({k: v for k, v in res_n.items() if k != "events"}, ensure_ascii=False, indent=2))
check("N 状态=ready", res_n.get("status") == "ready", str(res_n.get("status")))
check("N 三行都判成池内", len(res_n.get("rows") or []) == 3
      and all(r["status"] == "ok" for r in res_n["rows"]), str(res_n.get("rows")))
check("N 日志里确实走了盲点", "fast_confirm" in log_n, log_n[-300:])
check("N 盲点直接命中：日志里**没有**再走读框那条路（没补点第二次确定）",
      '"kind": "auto_confirm"' not in log_n, log_n[-400:])
check("N 点了弹窗的「确定」热区", any(h == "dialog_confirm" for _, h in phone_n.taps),
      str(phone_n.taps))
check("N 刷新生效（扣 500 券）", (res_n.get("cost") or {}).get("spent") == 500,
      str(res_n.get("cost")))
row_taps_n = [h for _, h in phone_n.taps if h and h.startswith("row")]
check("N 槽1 只点了一次 ↻", row_taps_n.count("row1") == 1, str(phone_n.taps))
check("N 没有走重读安全网（省的就是这次读屏）", '"kind": "read_retry"' not in log_n, log_n[-300:])
check("N 没碰派遣", phone_n.dispatch_tapped is False, str(phone_n.taps))

# ------------------------- P 盲点快路的时序 + 点空回落：确认框「过一会儿才画出来」时会发生什么
# 这一段是这次改动的主角。真机实测确认框是渐显（假手机用 dialog_delay_reads 模拟）：
# - `--wait-dialog 0`（= 不等）→ 盲点必然点空，但**不许出事**：交回读框那条路补点一次「确定」，
#   刷新照样生效、券照样记、依旧不许多点一次 ↻；
# - 这里把 `blind_settle=0`，专门量「盲点那一下 + 读框回落」的纯时序（默认 2.5 s 的受理等待
#   会让这一发多花 1.8 s，那是 `--blind-settle` 的事，见盲点默认路的场景 N/R3）。
out("\n=== P 盲点快路时序：确认框 2 屏后才画出来 ===")
phone_p = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "P", dialog_delay_reads=2)
_t0 = time.time()
res_p, log_p = run(phone_p, dry_run=False, max_refresh_per_row=1, detail_only=True,
                   wait_dialog=0.0, blind_settle=0.0, tap_sweep=False)
_confirm_wait = time.time() - _t0
out(json.dumps({k: v for k, v in res_p.items() if k != "events"}, ensure_ascii=False, indent=2))
check("P 走了盲点路", "fast_confirm" in log_p, log_p[-300:])
check("P 盲点 + 读框回落加起来不到 1.8 s（这一下不读屏，所以比 read 模式快）",
      _confirm_wait < 1.8, f"{_confirm_wait:.2f}s")
check("P 状态=ready", res_p.get("status") == "ready", str(res_p.get("status")))
check("P 槽1 判成池内且只刷 1 次",
      {r["slot"]: r for r in res_p.get("rows", [])}.get(1, {}).get("status") == "ok"
      and ({r["slot"]: r for r in res_p.get("rows", [])}[1]["refreshes"] == 1),
      str(res_p.get("rows")))
check("P 刷新照样生效（扣 500 券）", (res_p.get("cost") or {}).get("spent") == 500,
      str(res_p.get("cost")))
check("P 确认框被点掉、没有卡住", phone_p.dialog is None and phone_p.dialog_count >= 1,
      f"dialog={phone_p.dialog} count={phone_p.dialog_count}")

out("\n=== P2 对照：dialog_mode=read（老行为）必须还能跑，且**不**走盲点 ===")
phone_p2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "P2")
res_p2, log_p2 = run(phone_p2, dry_run=False, max_refresh_per_row=1, detail_only=True,
                     dialog_mode="read", tap_sweep=False)
check("P2 read 模式下不走盲点", "fast_confirm" not in log_p2, log_p2[-300:])
check("P2 read 模式先读框再点确定", '"kind": "auto_confirm"' in log_p2, log_p2[-400:])
check("P2 read 模式也刷到了（扣 500 券）", (res_p2.get("cost") or {}).get("spent") == 500,
      str(res_p2.get("cost")))

# ------------- O 视频流取帧（--capture stream）：动画中间态「券已扣、词条还是旧的」
# 2026-10-11 加 --capture stream：流里读到的是 ~0.35 s 前的画面，而真机刷新的顺序是
# 「先扣券（顶栏立刻变）→ 新词条才画出来」。所以整屏签名变了 ≠ 结果画出来了。
# 假手机用 anim_reads=2 模拟：生效后 2 次读屏都返回「顶栏已变、三行还是旧词条」。
out("\n=== O 动画中间态（anim_reads=2 + settle_reads=2）：必须等新词条画出来才算数 ===")
phone_o = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "O", anim_reads=2)
res_o, log_o = run(phone_o, dry_run=False, max_refresh_per_row=10, detail_only=True,
                   settle_reads=2, settle_delay=0.0)
out(json.dumps({k: v for k, v in res_o.items() if k != "events"}, ensure_ascii=False, indent=2))
row_taps_o = [h for _, h in phone_o.taps if h and h.startswith("row")]
rows_o = {r["slot"]: r for r in res_o.get("rows", [])}
check("O 状态=ready", res_o.get("status") == "ready", str(res_o.get("status")))
check("O 三行都判成池内（没拿旧词条当结果）",
      len(rows_o) == 3 and all(r["status"] == "ok" for r in rows_o.values()), str(rows_o))
check("O 槽1 读到的是新词条", rows_o.get(1, {}).get("item") == POOL1, str(rows_o.get(1)))
check("O 槽1 只点了一次 ↻（中间态没骗出第二次刷新）", row_taps_o.count("row1") == 1,
      str(phone_o.taps))
check("O 花销=500 券", (res_o.get("cost") or {}).get("spent") == 500, str(res_o.get("cost")))
check("O 日志里走了 settle 稳定复读", '"kind": "settle"' in log_o, log_o[-300:])
check("O 没有走扫描兜底", "sweep_hit" not in log_o, "不该出现 sweep_hit")

# 对照组：关掉 settle_reads（= 今天的默认）就会被中间态骗到——多刷一次、白花 500 券。
# 留着它是为了记住「为什么要加这一层」，不是为了跑通。
# 2026-10-13：盲点快路成了默认路，它「点完确定先读一屏」正好也绕开了中间态 → 这条对照
# 在 blind 下不再成立（实测 spent=500、只刷一次）。为了让「settle 这一层到底防什么」
# 这个记录继续有效，这里**钉死走读框老路**（`dialog_mode="read"`）。
out("\n=== O2 对照（dialog_mode=read）：settle_reads=0 时同一台假手机确实会被骗（白刷一次）===")
phone_o2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "O2", anim_reads=2)
res_o2, log_o2 = run(phone_o2, dry_run=False, max_refresh_per_row=10, detail_only=True,
                     settle_reads=0, dialog_mode="read")
row_taps_o2 = [h for _, h in phone_o2.taps if h and h.startswith("row")]
check("O2 关掉稳定复读 → 多刷一次（1000 券），这就是要修的病",
      (res_o2.get("cost") or {}).get("spent") == 1000 and row_taps_o2.count("row1") == 2,
      f"spent={(res_o2.get('cost') or {}).get('spent')} taps={phone_o2.taps}")

# ------------- P 流帧噪声（--capture stream）：「本来在池内」被读成「不在池内」
# 2026-10-11 深夜真机事故（logs\live_stream_1.txt）：流帧把槽1 的 `AUG突击步枪` 读成
# `AIJG突击步枪`（h264 把小字笔画糊了），模糊匹配对 3 个字母的核心容不下这种错 →
# 判「不在池内」→ 白点一次 ↻ 花 500 券，而那一行本来就是好的（券也就白花了）。
# 假手机用 flaky_rows 模拟：这一行**第一次渲染**故意画成那个坏名字，之后恢复真名。
# confirm_reads=2 要求连续两次都读到「不在池内」才允许花券 → 再读一帧就看到真名、跳过。
out("\n=== P 流帧读糊（flaky_rows + confirm_reads=2）：不许为一次坏读花 500 券 ===")
phone_p = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "P",
                    flaky_rows={1: ("AIJG突击步枪", 1)})
res_p, log_p = run(phone_p, dry_run=False, max_refresh_per_row=10, detail_only=True,
                   confirm_reads=2)
out(json.dumps({k: v for k, v in res_p.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_p = {r["slot"]: r for r in res_p.get("rows", [])}
row_taps_p = [h for _, h in phone_p.taps if h and h.startswith("row")]
check("P 状态=ready（三行都在池内）", res_p.get("status") == "ready", str(res_p.get("status")))
check("P 槽1 判成池内、refreshes=0",
      rows_p.get(1, {}).get("status") == "ok" and rows_p.get(1, {}).get("refreshes") == 0,
      str(rows_p.get(1)))
check("P 一次 ↻ 都没点（没为坏读花券）", row_taps_p == [] and phone_p.refresh_count == 0,
      f"taps={phone_p.taps} refresh_count={phone_p.refresh_count}")
check("P 花销=0 券", (res_p.get("cost") or {}).get("spent") == 0, str(res_p.get("cost")))
check("P 日志里确实走了「再确认」并发现其实在池内",
      '"kind": "confirm_in_pool"' in log_p, log_p[-400:])

# 对照组：confirm_reads=1（老行为）= 信一次读 → 白刷一次、花 500 券。
out("\n=== P2 对照：confirm_reads=1 时同一台假手机确实会白刷（500 券）===")
phone_p2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: [POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "P2",
                     flaky_rows={1: ("AIJG突击步枪", 1)})
res_p2, log_p2 = run(phone_p2, dry_run=False, max_refresh_per_row=10, detail_only=True,
                     confirm_reads=1)
row_taps_p2 = [h for _, h in phone_p2.taps if h and h.startswith("row")]
check("P2 只看一次坏读 → 白刷一次（500 券），这就是要修的病",
      (res_p2.get("cost") or {}).get("spent") == 500 and row_taps_p2.count("row1") == 1,
      f"spent={(res_p2.get('cost') or {}).get('spent')} taps={phone_p2.taps}")

# ------------------------------------------- Q 复读次数按「实际取屏路」自动定
# auto 档现在首选视频流，但没 ffmpeg 会自动退 gzip。多读几帧在流路几乎免费（0.01 s/帧），
# 在 gzip 路一帧 1.4 s 就纯浪费 —— 所以默认次数必须看**实际走的那一档**，不是看 --capture。
out("\n=== Q settle_reads/confirm_reads 自动档按 last_chain 决定 ===")
phone_q = make_phone("Q")
phone_q.last_chain = None
r_q = Runner(book, phone_q, Config(dry_run=True, log_dir=str(WORK / "logs"),
                                   work_dir=str(WORK / "logs")))
check("Q 没有 last_chain（离线/别家来源）→ 自动档等于关",
      (r_q.eff_settle_reads(), r_q.eff_confirm_reads()) == (0, 1),
      f"{r_q.eff_settle_reads()}/{r_q.eff_confirm_reads()}")
phone_q.last_chain = "gzip"
check("Q 实际走 gzip → 不额外读（每帧 1.4 s，读多了纯浪费）",
      (r_q.eff_settle_reads(), r_q.eff_confirm_reads()) == (0, 1),
      f"{r_q.eff_settle_reads()}/{r_q.eff_confirm_reads()}")
phone_q.last_chain = "stream"
check("Q 实际走视频流 → 自动 settle=2 / confirm=2（读一帧 0.01 s）",
      (r_q.eff_settle_reads(), r_q.eff_confirm_reads()) == (2, 2),
      f"{r_q.eff_settle_reads()}/{r_q.eff_confirm_reads()}")
r_q2 = Runner(book, phone_q, Config(dry_run=True, log_dir=str(WORK / "logs"),
                                    work_dir=str(WORK / "logs"),
                                    settle_reads=3, confirm_reads=5))
check("Q 显式配置优先于自动档（3/5 不会被流路覆盖成 2/2）",
      (r_q2.eff_settle_reads(), r_q2.eff_confirm_reads()) == (3, 5),
      f"{r_q2.eff_settle_reads()}/{r_q2.eff_confirm_reads()}")

# ------------------------------------------- R 时间闸门：点完「确定」别信动画中间帧
# 真机事故（logs/live_stream_1.txt，2026-10-11）：点 ↻ 前把**已经在池内**的 `AUG突击步枪`
# 读成了 `AIJG突击步枪`（h264 糊字）→ 判池外 → 点 ↻ 花 500 券；点完「确定」后 0.01 s 就读屏，
# 读到的是**刷新前的旧画面**（渲染真名 `AUG突击步枪`）——它和点前的噪声文本（`AIJG...`）不一样，
# 于是「签名变了」和「settle 两帧稳定」双双被骗过 → 工具把**旧词条**当成刷新结果、误报命中；
# 而这一行其实被刷成了池外的 `AN94突击步枪`（下一次预演才露出来）。
# 结论：文本层不可靠（噪声读法与真换件的相似度**间隙 0.000**，见 logs/same_item_probe_out.txt），
# 只能用时间闸门 —— 点完「确定」先等过动画，再信任任何一帧。
# 假手机用 anim_seconds 按**时间**模拟动画窗口（按次数的 anim_reads 骗不过 sleep）。
out("\n=== R 时间闸门（anim_seconds=0.6 + settle_guard=0.9）：看到的必须是真的新词条 ===")
phone_r = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1, "M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "R",
                    flaky_rows={1: ("AIJG突击步枪", 3)},   # 首读 + 两次再确认都读糊
                    anim_seconds=0.6)
res_r, log_r = run(phone_r, dry_run=False, max_refresh_per_row=10, detail_only=True,
                   settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.9)
out(json.dumps({k: v for k, v in res_r.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_r = {r["slot"]: r for r in res_r.get("rows", [])}
check("R 槽1 真刷到池内、refreshes=2（池外那次被看见了）",
      rows_r.get(1, {}).get("item") == POOL1 and rows_r[1]["refreshes"] == 2, str(rows_r.get(1)))
check("R 日志里出现过真正抽到的那件池外装备（说明看到的是新词条）",
      "M4A1突击步枪" in log_r, log_r[-500:])
check("R 日志里有 settle_guard 事件", '"kind": "settle_guard"' in log_r, log_r[-400:])
check("R 花销=2 次 × 500 = 1000 券",
      (res_r.get("cost") or {}).get("spent") == 1000, str(res_r.get("cost")))
check("R 没有点派遣", phone_r.dispatch_tapped is False)

# 对照组：闸门关掉（settle_guard=0）→ 旧帧被骗进结论，是**最坏**的那种错：
# 工具报「这一行在池内 / 可以派遣」，而真机上那一行是池外的 M4A1。
# 2026-10-13：这一组显式用 `dialog_mode="read"`。盲点默认路的受理等待（`blind_settle`）会等满
# 再读结果，正好读掉动画窗口 —— 于是它**不会**被骗（同一台假手机上刷了 2 次、收在池内那件），
# 这条对照钉的病随之不成立；要留的是「闸门关掉会出事」这个证据，所以把它钉在 read 老路上。
out("\n=== R2 对照：settle_guard=0 时同一台假手机会把旧帧当结果（假 ready）===")
phone_r2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1, "M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "R2",
                     flaky_rows={1: ("AIJG突击步枪", 3)},
                     anim_seconds=2.0)
res_r2, log_r2 = run(phone_r2, dry_run=False, max_refresh_per_row=10, detail_only=True,
                     settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                     dialog_mode="read")
rows_r2 = {r["slot"]: r for r in res_r2.get("rows", [])}
check("R2 工具以为槽1=池内、只刷 1 次（旧帧 AUG 骗过了它）",
      rows_r2.get(1, {}).get("item") == POOL1 and rows_r2[1]["refreshes"] == 1,
      str(rows_r2.get(1)))
check("R2 但假手机那一行其实是池外的 M4A1突击步枪 —— 这就是闸门必须开着的理由",
      phone_r2.rows()[0] == "M4A1突击步枪", str(phone_r2.rows()))
check("R2 结论与真机不一致（status=ready 而实际上没中）",
      res_r2.get("status") == "ready" and res_r2.get("ready") is True,
      f"status={res_r2.get('status')} ready={res_r2.get('ready')}")

# 真机 `logs\live_guard_1.txt`（2026-10-11 17:21）暴露的另一条路：点 ↻ 之后**第一屏还不是弹窗**
# （顶栏先变、弹窗 ~2 s 才画出来）。当时闸门写在 `_tap_wait_read` 且绑在「整屏签名变了」上，
# 于是「顶栏一变」就被当成「点生效了」→ 调用方先记 charge、弹窗留给下一轮才确认（那次活闸门
# 一次都没跑）。修法：闸门挪进 `_handle_dialog`（唯一按「确定」的地方）与盲点分支；
# 后来再加 `_tap_progressed` 的行级判据 + 重读网**真的等弹窗**（默认 8 × 0.7 s = 5.6 s）。
# 所以 S 现在钉的是：晚到 2 帧的确认框由重读网抓到、**就在这一下 ↻ 里**点掉（不给下一轮留隐患），
# 并且记账发生在点「确定」之后；S2 钉反面（闸门关掉 → 读旧帧 → 结论错 + 白花券）。
# 2026-10-13 盲点成默认后口径改了：盲点那一屏（`_fastok`）读到框还在等于「等太早了」，
# `_tap_progressed` 直接认「读到弹窗」为真、跳过重读网，框由稳定复读那层（`_settle_dlg` 路，
# 同一个 `_handle_dialog`）抓着点掉 → 所以断言只看「`_fastok` 之后第一次读到框就有人点」
# 与「记账在点确定之后」，不再要求日志里出现 `read_retry`（盲点路下它本来就不会触发）。
# `read_retry` 这条路仍由 V（读框老路，`read_retries=4`）钉着。
out("\n=== S 晚到的确认框：重读网抓到 + 当轮点掉 + 记账在点确定之后 ===")
# 动画窗口是**按时间**的（anim_seconds），所以量要给足：4.0 s 保证「点完确定后的那一串读屏」
# 一定落在窗口里（读屏是毫秒级的），S 的闸门则必须比窗口更长（5.0 > 4.0）才能睡过去。
# 早先这里写 1.5 s，机器一忙就会有几帧读到真词条 → 断言随负载时通时不通（flaky）。
phone_s = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "S",
                    anim_seconds=4.0, dialog_delay_reads=2)
res_s, log_s = run(phone_s, dry_run=False, max_refresh_per_row=3, detail_only=True,
                   settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=5.0,
                   settle_wait_change=0.0)   # 这两场专门验 settle_guard，把新加的
                                             # 「等名字真的变了」关掉，免得两个网互相掩护
out(json.dumps({k: v for k, v in res_s.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_s = {r["slot"]: r for r in res_s.get("rows", [])}
check("S 晚到的确认框是重读网抓到的、当轮就点掉了（没留给下一轮）",
      '"kind": "auto_confirm"' in log_s
      and '"tag": "slot1_refresh1_fastok"' in log_s
      and '"kind": "dialog"' not in log_s[log_s.find('"tag": "slot1_refresh1_fastok"'):]
      and '_settle_dlg' not in log_s, log_s[-1400:])
check("S 记账发生在点「确定」之后（行级判据：没确认就不许记账）",
      0 <= log_s.find('"kind": "auto_confirm"') < log_s.find('"kind": "charge"'), log_s[-900:])
check("S 闸门在「点确定」这个动作里跑了（settle_guard 事件 why=dialog_ok）",
      '"kind": "settle_guard"' in log_s and '"why": "dialog_ok"' in log_s, log_s[-700:])
check("S 只刷 1 次就看到真词条（闸门 5.0 s > 动画 4.0 s）",
      rows_s.get(1, {}).get("item") == POOL1 and rows_s[1]["refreshes"] == 1, str(rows_s.get(1)))
check("S 花销=1 次 × 500 = 500 券", (res_s.get("cost") or {}).get("spent") == 500,
      str(res_s.get("cost")))
check("S 没有点派遣", phone_s.dispatch_tapped is False)

out("\n=== S2 对照（dialog_mode=read）：同一台假手机、闸门关掉 → 读旧帧 → 结论错 + 白花券 ===")
# 动画窗口 4.0 s，而这一跑全程只有零点几秒 → 工具**每一次**读屏都落在窗口里，结果与负载无关：
# 它永远看不到真词条，只会把刷新前那件池外的 M4A1 当成结果（于是继续刷、直到触顶）。
# 这正是闸门必须开着的理由 —— 真实那一行其实第 1 次刷新就已经是池内的 POOL1 了。
# 动画窗口开到 30 s（远长于这一跑）：**任何**读屏都不可能读到真词条，所以断言与机器快慢无关。
# 早先这里写 4.0 s 是错的 —— 稳定复读最多 8 次、每次都要 OCR（合计能到 4 s 上下），
# 机器慢一点就会把窗口熬过去、读到真词条，于是断言时通时不通（这一次就是这么挂的）。
phone_s2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: ["M4A1突击步枪", POOL1], 2: [POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "S2",
                     anim_seconds=30.0, dialog_delay_reads=2)
res_s2, log_s2 = run(phone_s2, dry_run=False, max_refresh_per_row=3, detail_only=True,
                     settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                     settle_wait_change=0.0,   # 这里要的是「没有时间闸门」的世界，
                                               # 新加的「等名字真的变了」也一起关掉
                     dialog_mode="read")       # 2026-10-13：盲点路会先读一屏复核，
                                               # 30 s 动画窗里它必然读到旧词条 →
                                               # 这一场改钉读框老路的病（结论错 + 白花券）
rows_s2 = {r["slot"]: r for r in res_s2.get("rows", [])}
check("S2 假手机槽1 第 1 次刷新后就已经是池内 POOL1", phone_s2.rows()[0] == POOL1,
      str(phone_s2.rows()))
check("S2 但工具从头到尾没匹配到真词条（没有一条 matched=POOL1 的读屏）",
      f'"matched": "{POOL1}"' not in log_s2, log_s2[-500:])
check("S2 结论是刷新前那件池外的旧词条（S 是 1 次就命中）",
      rows_s2.get(1, {}).get("item") == "M4A1突击步枪" and rows_s2[1]["status"] != "ok",
      str(rows_s2.get(1)))
check("S2 还白花了一次券（500）", (res_s2.get("cost") or {}).get("spent") == 500,
      str(res_s2.get("cost")))

# 真机 `--limit 40` 第一次启动 abort（2026-10-11 17:37，logs/live_slot2_round2.txt 被 `*>` 写成
# 二进制，看 logs/events.jsonl + logs/run_*.json）：
#   abort phase=start reason=界面上有个模态弹窗，先手动关掉再跑
#   prompt="是否刷新特殊条亻：SEK堡垒防弹衣"
# 成因：那一轮**最后一次** ↻ 的确认框在工具收尾之后才画出来（弹窗要 ~2 s），`charge` 已经记了、
# 行却没提交 → 下一轮启动被模态框挡下（好在那一次的 abort 是正确行为：启动就看到弹窗，先停手）。
# 修法 = 收尾补确认（`Config.flush_pending`，默认开）：出报告前多读一屏，还挂着刷新确认框就补点
# 「确定」并按新词条重判那一行（行是拿旧词条判的 → 结论可能是错的）。场景 T/T2 分别钉住药与病。
out("\n=== T 收尾补确认：最后一次 ↻ 的确认框跑完才画出来 ===")
phone_t = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1], 2: [ "M4A1突击步枪", POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "T",
                    dialog_delay_reads=5)      # 弹窗正好在收尾那一次 final_peek 才画出来
                                               # （窗口很窄：python logs\_t_window_probe.py 扫出唯一解
                                               #   N=5 —— 1..4 会在点 ↻ 那一轮里被复核/重读网抓到并确认，
                                               #   ≥6 收尾的 final_peek 也看不到 → 窗口就空了。
                                               #   盲点路复核多读一屏，窗口比旧的 N=4 右移了 1 帧。）
res_t, log_t = run(phone_t, dry_run=False, max_refresh_per_row=1, detail_only=True,
                   settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                   settle_wait_change=0.0, tap_sweep=False)
# tap_sweep=False 是必须的：扫描的候选点会打到这一行的 ↻ 热区，每次 `_ask_dialog` 都把
# `_dialog_in` 重置回 N（见 fake_screens.render），弹窗就永远画不出来（探针第一轮 N=6..30 全灭）。
out(json.dumps({k: v for k, v in res_t.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_t = {r["slot"]: r for r in res_t.get("rows", [])}
check("T 收尾时发现并补确认了那个弹窗",
      '"kind": "pending_dialog"' in log_t and '"kind": "final_recheck"' in log_t, log_t[-900:])
check("T 弹窗确实被点掉了（dialog_count 有、且没留给人工）",
      phone_t.dialog is None and phone_t.dialog_count >= 1, f"{phone_t.dialog_count}")
check("T 槽2 按补确认之后的真词条重判为池内（原来是拿旧词条判的）",
      rows_t.get(2, {}).get("status") == "ok" and rows_t[2].get("item") == POOL2,
      str(rows_t.get(2)))
check("T 报告里记了这次补确认", (res_t.get("pending_dialog") or {}).get("kind") == "confirmed",
      str(res_t.get("pending_dialog")))
check("T 三行全就绪（ready=True）", res_t.get("ready") is True and res_t.get("status") == "ready",
      f"ready={res_t.get('ready')} status={res_t.get('status')}")
check("T 只花 1 次刷新 = 500 券（补确认不算第二次花钱）",
      (res_t.get("cost") or {}).get("spent") == 500, str(res_t.get("cost")))
check("T 计费记的还是刷前那件旧词条（说明原来那一轮确实读的是旧行）",
      ((res_t.get("cost") or {}).get("ledger") or [{}])[0].get("item") == "M4A1突击步枪",
      str((res_t.get("cost") or {}).get("ledger")))
check("T 没有点派遣", phone_t.dispatch_tapped is False)

out("\n=== T2 对照：flush_pending=False → 弹窗留给人工，那一行结论是错的 ===")
phone_t2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "T2",
                     dialog_delay_reads=5)      # 同上：5 帧时工具收摊前弹窗都没画出来（`flush_pending=False`
                                                # 连收尾那一屏都不读），弹窗就留给人工了
res_t2, log_t2 = run(phone_t2, dry_run=False, max_refresh_per_row=1, detail_only=True,
                     settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                     settle_wait_change=0.0, tap_sweep=False, flush_pending=False)
rows_t2 = {r["slot"]: r for r in res_t2.get("rows", [])}
check("T2 弹窗没人确认（下一轮会像真机那样被它挡下）",
      phone_t2.dialog_count >= 1 and (phone_t2.dialog is not None
                                      or phone_t2._pending_dialog is not None),
      f"dialog_count={phone_t2.dialog_count} dialog={phone_t2.dialog} "
      f"pending={phone_t2._pending_dialog}")
check("T2 没有补确认（日志里没有 pending_dialog/final_recheck）",
      '"kind": "pending_dialog"' not in log_t2 and '"kind": "final_recheck"' not in log_t2,
      log_t2[-500:])
check("T2 槽2 停在旧词条的 give_up（抽签还没落地，那一行其实没提交）",
      rows_t2.get(2, {}).get("status") == "give_up" and res_t2.get("ready") is not True,
      str(rows_t2.get(2)))

# 真机 `logs\run_20261011_182111.json`（18:14–18:21，`--limit 40` 刷下一轮）暴露的坑：
# 83 次刷新里有 **21 次 = 25.3%**「刷完读到的名字」和「刷卡前」一模一样（槽2 护甲真机出现过
# 62 种，随机重名只该 ~1.6%）。**旧→旧只是白刷**（本来就要刷），**「真抽中了却读到旧名字」
# 会把命中当成没中、再刷一次把命中刷没了**。修法 = `Config.settle_wait_change`（默认 3.0 s）：
# 确认刷卡后，只要这一行还跟刷卡前一模一样就继续重读，直到它变。场景 U/U2 分别钉住药与病。
# （这 25.3% 的**大头**其实是「确认框晚到被当成已生效」，见下面 V/V2。）
# 2026-10-13：盲点快路成默认后，它「点完确定先读一屏」就已经把这一行的新名字等到了
# （实测 blind 下一次就命中），这条 `wait_change` 安全网只在读框老路上才会真正跑到 →
# 场景 U/U2 **钉死读框老路**，把这一层的行为原样留住。
out("\n=== U（dialog_mode=read）刷完等这一行的名字真的变了（真机 25.3% 的刷新读到旧一帧） ===")
# 动画用**按次数**的 anim_reads=10（不随机器快慢变）：稳定复读循环最多读 8 次
# （`extra < settle_reads + 6`），所以「只看 settle」必然还是旧词条；wait_change 的预算
# （5.0 s）比它长，足够等到第 11 帧把真词条画出来。
phone_u = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2, "AK12突击步枪"],
                                3: [POOL3]},
                    screen="detail", work_dir=WORK / "U",
                    anim_reads=10)
res_u, log_u = run(phone_u, dry_run=False, max_refresh_per_row=5, detail_only=True,
                   settle_reads=2, settle_delay=0.05, confirm_reads=2, settle_guard=0.0,
                   settle_wait_change=5.0, dialog_mode="read")
out(json.dumps({k: v for k, v in res_u.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_u = {r["slot"]: r for r in res_u.get("rows", [])}
check("U 日志里有 wait_change 且等到了变化",
      '"kind": "wait_change"' in log_u and '"changed": true' in log_u, log_u[-900:])
check("U 槽2 判成池内、只刷 1 次", rows_u.get(2, {}).get("status") == "ok"
      and rows_u[2].get("item") == POOL2 and rows_u[2]["refreshes"] == 1, str(rows_u.get(2)))
check("U 只花 1 次 = 500 券", (res_u.get("cost") or {}).get("spent") == 500, str(res_u.get("cost")))
check("U 三行全就绪（ready=True）", res_u.get("ready") is True, f"ready={res_u.get('ready')}")
check("U 没有点派遣", phone_u.dispatch_tapped is False)

out("\n=== U2 对照（dialog_mode=read）：settle_wait_change=0 → 拿旧词条判池，把已经命中的一行判成没中 ===")
# 同一台假手机、同样的动画窗口，只是不等名字变：工具永远只看到刷卡前那件池外的 M4A1
# → 触顶 give_up，而那一行**其实第 1 次刷新就已经是池内的 POOL2 了**（限次设成 1，
# 好让结论停在「漏判」；限次再大就会像真机那样把已经命中的一行继续刷掉）。
phone_u2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2, "AK12突击步枪"],
                                 3: [POOL3]},
                     screen="detail", work_dir=WORK / "U2",
                     anim_reads=10)
res_u2, log_u2 = run(phone_u2, dry_run=False, max_refresh_per_row=1, detail_only=True,
                     settle_reads=2, settle_delay=0.05, confirm_reads=2, settle_guard=0.0,
                     settle_wait_change=0.0, dialog_mode="read")
rows_u2 = {r["slot"]: r for r in res_u2.get("rows", [])}
check("U2 假手机槽2 第 1 次刷新后就已经是池内 POOL2",
      phone_u2.rows()[1] == POOL2, str(phone_u2.rows()))
check("U2 但工具拿旧词条判成池外（结论不是 ok）",
      rows_u2.get(2, {}).get("status") != "ok"
      and rows_u2[2].get("item") == "M4A1突击步枪", str(rows_u2.get(2)))
check("U2 日志里没有 wait_change（这一跑没等多）",
      '"kind": "wait_change"' not in log_u2, log_u2[-500:])
check("U2 没就绪（漏判 → ready 非真）", res_u2.get("ready") is not True,
      f"ready={res_u2.get('ready')}")

# ------------------------- R3 盲点路的**点空回落**：确认框已点掉、但新词条还没画出来
# 盲点快路唯一的风险就是「点完确定那一屏读到旧词条」：真机动画窗口里那一屏就是旧的
# （`land_probe3` 量过新词条 0.7 s 落屏）。这里把 `_blind_confirm` 的 `blind_unclear`
# 回落钉住：框没了、行没变 → **不许记券**，按 `settle_wait_change` 的预算等到真词条；
# 等不到就返回 False，由重读网 + 扫描兜底接住（那时才会多花一次 ↻，是有据可依的）。
out("\n=== R3 盲点路点空回落：确认框已点掉但词条还没画出来 → 不记券、等到新词条 ===")
phone_r3 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "R3",
                     anim_reads=10)
res_r3, log_r3 = run(phone_r3, dry_run=False, max_refresh_per_row=3, detail_only=True,
                     settle_reads=2, settle_delay=0.05, settle_guard=0.0, tap_sweep=False,
                     settle_wait_change=5.0)
rows_r3 = {r["slot"]: r for r in res_r3.get("rows", [])}
check("R3 走了盲点路", "fast_confirm" in log_r3, log_r3[-300:])
check("R3 盲点第一屏读到旧词条 → 走 blind_unclear、不许在这一屏记账",
      '"kind": "blind_unclear"' in log_r3
      and log_r3.find('"kind": "blind_unclear"') < log_r3.find('"kind": "charge"'),
      log_r3[-500:])
check("R3 blind_unclear 之后等了名字变（wait_change 等到 changed=true）",
      '"kind": "wait_change"' in log_r3 and '"changed": true' in log_r3, log_r3[-400:])
check("R3 槽2 判成池内、只刷 1 次（没有因为读到旧词条白刷第二次）",
      rows_r3.get(2, {}).get("status") == "ok"
      and rows_r3[2].get("item") == POOL2 and rows_r3[2]["refreshes"] == 1, str(rows_r3.get(2)))
check("R3 只花 1 次 = 500 券", (res_r3.get("cost") or {}).get("spent") == 500,
      str(res_r3.get("cost")))
check("R3 没有走扫描兜底（坐标没歪，白点一次不该触发扫描）", "sweep_hit" not in log_r3,
      log_r3[-300:])
check("R3 没有点派遣", phone_r3.dispatch_tapped is False, str(phone_r3.taps))

# ------------------------- R4 盲点的受理等待：等满 `blind_settle` 再读结果，一屏落到最终态
# 2026-10-13 真机 5 次刷新（10 发）实测：只等 0.8 s 就读的那版**每一发**都落到
# `blind_unclear` → 再等一次 `wait_change` → 再走 1~2 次 `read_retry`（每次 +0.7 s），
# 10 发里 6 发其实框早自己没了、根本不需要补点「确定」。真机续测（2026-10-13，共 34 发）：
# 1.7/1.8/2.0 s 复核那一屏仍可能看到框（blind_missed），2.2 s 接近干净，**2.5 s 完全干净**
# （12 发 blind_missed=0 / read_retry=0），故 `Config.blind_settle` 默认 2.5。
# 这里用 `anim_seconds=0.8` 复刻：0.85 s 复核那时框已经点掉、新词条还在画（顶栏已扣券），
# 2.5 s 的受理等待之后词条已换 → 盲点命中，**一次 `read_retry` 都不该有**。
# `dialog_delay_reads=0`：确认框在点 ↻ 那一瞬间就画出来（盲点那一下才有可能点中它）。
# 「框过几帧才画出来」的时序由场景 S 钉（它会读框、走回落确认），不在这里重复。
out("\n=== R4 盲点受理等待：等满 blind_settle 再读，一屏就是最终态（无 read_retry）===")
phone_r4 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "R4",
                     dialog_delay_reads=0, anim_seconds=0.8)
res_r4, log_r4 = run(phone_r4, dry_run=False, max_refresh_per_row=3, detail_only=True,
                     settle_reads=2, settle_delay=0.05, settle_guard=0.0, tap_sweep=False)
rows_r4 = {r["slot"]: r for r in res_r4.get("rows", [])}
check("R4 走了盲点路", "fast_confirm" in log_r4, log_r4[-300:])
check("R4 盲点命中（没有 blind_unclear / blind_missed）",
      '"kind": "blind_unclear"' not in log_r4 and '"kind": "blind_missed"' not in log_r4,
      log_r4[-500:])
check("R4 一次 read_retry 都没走（受理等待省掉的就是这几次读屏）",
      '"kind": "read_retry"' not in log_r4, log_r4[-500:])
check("R4 槽2 判成池内、只刷 1 次", rows_r4.get(2, {}).get("status") == "ok"
      and rows_r4[2].get("item") == POOL2 and rows_r4[2]["refreshes"] == 1, str(rows_r4.get(2)))
check("R4 只花 1 次 = 500 券", (res_r4.get("cost") or {}).get("spent") == 500,
      str(res_r4.get("cost")))
check("R4 确认框被点掉、没有卡住（只点了 1 次「确定」）",
      phone_r4.dialog is None and phone_r4.dialog_count == 1,
      f"dialog={phone_r4.dialog} count={phone_r4.dialog_count}")
check("R4 没有点派遣", phone_r4.dispatch_tapped is False, str(phone_r4.taps))

# 2026-10-11 18:30 的定向实测（logs\_land_probe.py / _land_probe2.py / _land_probe3.py，
# 结果在 logs\land_probe*_out.txt）把「25.3% 刷完名字没变」的时序彻底量清楚了：
#   点 ↻ → 0.8 s 时**还是 detail、没有弹窗**（顶栏/模糊字让整屏签名已经不同了）→
#   真机实测确认框 **2.58 s** 才画出来（服务器回包才画）→ 点「确定」→ 新词条 **0.7 s** 落屏。
# 于是旧判据（整屏签名变了 = 点生效了）会把「确认框还没画出来」当成「刷完了」：`detail_phase`
# 记 500 券、游戏那一下根本没抽（确认框一直挂着没人点）→ 行上名字不变 = 那 25.3% 的大头。
# 修法 = `_tap_progressed`：刷某一行时「是不是生效了」只看**这一行的文字**（或读到弹窗），
# 不看整屏签名；重读网按这个判据一直读到真出弹窗（默认 8 × 0.7 s = 5.6 s，覆盖 2.6 s）。
# 场景 V/V2 分别钉住药与病。
out("\n=== V 确认框晚到：不能拿整屏签名噪声当「点生效了」 ===")
phone_v = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "V",
                    dialog_delay_reads=3)      # 确认框过 3 帧才画出来（真机 0.8~2.6 s）
res_v, log_v = run(phone_v, dry_run=False, max_refresh_per_row=3, detail_only=True,
                   settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                   read_retries=4)
out(json.dumps({k: v for k, v in res_v.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_v = {r["slot"]: r for r in res_v.get("rows", [])}
check("V 等到了确认框（日志里有 read_retry，且那一帧还是 detail）",
      '"kind": "read_retry"' in log_v and '"screen_kind": "detail"' in log_v, log_v[-900:])
_i_ok = log_v.find('"kind": "auto_confirm"')
_i_pay = log_v.find('"kind": "charge"')
check("V 「点确定」发生在记账之前（旧判据是反过来的：先记账、压根没确认）",
      0 <= _i_ok < _i_pay, f"auto_confirm@{_i_ok} charge@{_i_pay}")
check("V 确认框真被点了", phone_v.dialog_count >= 1 and phone_v.dialog is None,
      f"dialog_count={phone_v.dialog_count} dialog={phone_v.dialog}")
check("V 槽2 判成池内、只刷 1 次", rows_v.get(2, {}).get("status") == "ok"
      and rows_v[2].get("item") == POOL2 and rows_v[2]["refreshes"] == 1, str(rows_v.get(2)))
check("V 只花 1 次 = 500 券", (res_v.get("cost") or {}).get("spent") == 500, str(res_v.get("cost")))
check("V 三行全就绪（ready=True）", res_v.get("ready") is True, f"ready={res_v.get('ready')}")
check("V 没有点派遣", phone_v.dispatch_tapped is False)

out("\n=== V2 对照：看不到确认框、这一行也没变 → 一分钱都不许记 ===")
# 确认框 40 帧都不出现（整跑都看不到），行也不会变：旧判据（顶栏一出现「刷新中」= 签名变了）
# 会**每一轮都记 500 券**，而游戏一次抽签都没发生 —— 钱白记、账目全乱。
# 新判据下 `changed=False` → 不记账 → 走 no_change/停手。
phone_v2 = FakePhone(boss_queue=("伯纳德",),
                     row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                     screen="detail", work_dir=WORK / "V2",
                     dialog_delay_reads=40)
res_v2, log_v2 = run(phone_v2, dry_run=False, max_refresh_per_row=3, detail_only=True,
                     settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                     read_retries=0)
rows_v2 = {r["slot"]: r for r in res_v2.get("rows", [])}
check("V2 一次都没记账（spent=0、日志里没有 charge 事件）",
      (res_v2.get("cost") or {}).get("spent") == 0 and '"kind": "charge"' not in log_v2,
      str(res_v2.get("cost")) + " | " + log_v2[-400:])
check("V2 假手机槽2 那件池外的 M4A1 一动没动（没人确认过弹窗）",
      phone_v2.rows()[1] == "M4A1突击步枪" and phone_v2.dialog is None,
      f"{phone_v2.rows()} dialog={phone_v2.dialog}")
check("V2 工具没敢说这一行池内（结论不是 ok），也没就绪",
      rows_v2.get(2, {}).get("status") in ("give_up", "error_no_change")
      and res_v2.get("ready") is not True, str(rows_v2.get(2)))

# 2026-10-11 18:51 的真机跑（`logs\run_20261011_185124.json`）把另一条来路量了出来：
# 30 次 charge 里**有 4 次根本没有 `auto_confirm`** —— 点 ↻ 之后 1.9 s 就记了 500 券，
# 而游戏那一下连确认框都没弹。根因是当时的行级判据用**原始字符串不相等**判「点生效了」，
# 而 h264 流帧会让同一件装备换个写法（`SEK复合防弹衣` → `SEK合防弹衣`），于是「名字变了」
# 为真 → 记账，可 `rows()` 里那件装备一动没动（游戏没抽）。同一轮 30 对「刷卡前 / 刷卡后」
# 实测：**同一件装备的噪声写法 6 对 ≥0.842**，**真换件最高只有 0.462** → 两簇之间有干净空档。
# 修法 = ① 刷某一行的 ↻ **只认弹窗**（`_tap_progressed` 的 row 分支直接 return False）；
#        ② `settle_wait_change` 的「还是旧名字」改用 `similarity(canon(now), canon(old)) >=
#           ROW_SAME_MIN(0.72)` 判，不再用字符串相等。
# 场景 W 钉住幽灵记账：点 ↻ 之后弹窗 40 帧都不画（整跑都看不到），但那几帧该行故意画成
# 另一个写法（`noise_when_pending`，窗口自动对齐这次点击，`rows()` 里那件装备没变）。
# 旧代码：行文字变了 → 记 500 券（幽灵记账）。新代码：没读到弹窗 → 一分钱不许记。
out("\n=== W 幽灵记账：看不到确认框时，行文字噪声绝不许当「点生效了」 ===")
phone_w = FakePhone(boss_queue=("伯纳德",),
                    row_queues={1: [POOL1], 2: ["M4A1突击步枪", POOL2], 3: [POOL3]},
                    screen="detail", work_dir=WORK / "W",
                    dialog_delay_reads=40,          # 确认框整跑都不出现（真机那 4 次就是这样）
                    noise_when_pending={2: "M4A1突击步枱"})   # 同一件装备的噪声写法
res_w, log_w = run(phone_w, dry_run=False, max_refresh_per_row=1, detail_only=True,
                   settle_reads=2, settle_delay=0.0, confirm_reads=2, settle_guard=0.0,
                   settle_wait_change=0.0, tap_sweep=False, read_retries=2,
                   flush_pending=False)
out(json.dumps({k: v for k, v in res_w.items() if k != "events"}, ensure_ascii=False, indent=2))
rows_w = {r["slot"]: r for r in res_w.get("rows", [])}
check("W 一次都没记账（spent=0、日志里没有 charge 事件）——旧代码会在这里记 500 券",
      (res_w.get("cost") or {}).get("spent") == 0 and '"kind": "charge"' not in log_w,
      str(res_w.get("cost")) + " | " + log_w[-400:])
check("W 走的是「没变化」这条诚实路（日志里有 no_change）",
      '"kind": "no_change"' in log_w, log_w[-600:])
check("W 假手机槽2 那件池外的 M4A1 一动没动、弹窗也没被点过",
      phone_w.rows()[1] == "M4A1突击步枪" and phone_w.dialog is None,
      f"{phone_w.rows()} dialog={phone_w.dialog}")
check("W 工具没敢说这一行池内（结论不是 ok），也没就绪",
      rows_w.get(2, {}).get("status") != "ok" and res_w.get("ready") is not True,
      str(rows_w.get(2)))

# ---------------------------------------------------------------- 收尾
out("\n" + ("=" * 60))
out(f"FAIL {len(fails)}")
for f in fails:
    out("  " + f)
OUT.write_text("\n".join(buf), encoding="utf-8")
print("\n".join(buf[-40:]))
print(f"\n[exit {0 if not fails else 2}] 结果已写入 {OUT}")
sys.exit(0 if not fails else 2)
