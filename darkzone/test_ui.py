"""界面解析层离线测试：用两张真机截图验证「能不能解析出可点的按钮 / 词条行」。

没有手机也能跑（这是标定阶段的主力测试）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from darkzone.rules import default_rules_path, parse_workbook   # noqa: E402
from darkzone.ui import CoordMapper, load_profile, parse_screen  # noqa: E402
from darkzone.win_ocr import ocr_lines                 # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
XLSX = default_rules_path()
SAMPLES = ROOT / "samples"
OUT = ROOT / "ui_test_out.txt"
DEVICE = (3200, 1440)          # 手机横屏真机像素（连上后用 wm size 核实）

buf = []
fails = []


def out(s=""):
    buf.append(str(s))


book = parse_workbook(XLSX)
block = book.blocks[0]
pools = {int(s): set(v) for s, v in block.pools.items()}
profile = load_profile()

for name, expect_kind in [("screen1_refresh_boss.jpg", "list"),
                          ("screen2_refresh_entries.jpg", "detail")]:
    img = SAMPLES / name
    rows, res = ocr_lines(img)
    size = (res["width"], res["height"])
    kind, scr = parse_screen(rows, size, profile, vocab=book.vocab)
    mapper = CoordMapper(size, DEVICE)
    out("=" * 88)
    out(f"{name}  解析类型={kind}  图片={res['width']}x{res['height']}  真机换算={DEVICE}")
    out("=" * 88)
    if kind != expect_kind:
        fails.append(f"{name}: 页面类型判成 {kind}，期望 {expect_kind}")

    if kind == "list":
        out(f"排除名单={block.excluded}")
        for p in scr.panels:
            r = mapper.pt(p.refresh)
            s = mapper.pt(p.select) if p.select else None
            out(f"  [{p.kind}] 名字={p.name!r} 费用={p.cost_raw!r}")
            out(f"      ↻ 图内({p.refresh[0]:.0f},{p.refresh[1]:.0f}) -> 真机{r}")
            out(f"      选择 图内{p.select} -> 真机{s}")
            out(f"      排除? {p.name in block.excluded or False}")
        kinds = [p.kind for p in scr.panels]
        if kinds != ["普通队员", "精锐队员", "暗区首领"]:
            fails.append(f"{name}: 面板解析成 {kinds}")
        boss = scr.by_kind("暗区首领")
        if not boss or boss.name != "德尔文":
            fails.append(f"{name}: 首领名字解析成 {boss.name if boss else None!r}，期望 德尔文")
        if not boss or not boss.select:
            fails.append(f"{name}: 没找到首领面板的「选择」按钮")
        # 标定值：↻ 在图片 x≈2594（比例 0.9120，真机实测 2918/3200），费用是 15000，另两栏是「免费」
        if boss and abs(boss.refresh[0] - 2594) > 35:
            fails.append(f"{name}: 首领 ↻ 横坐标 {boss.refresh[0]:.0f}，期望 ≈2594")
        if boss and "15000" not in boss.cost_raw:
            fails.append(f"{name}: 首领费用解析成 {boss.cost_raw!r}，期望含 15000")
        if boss and boss.name in block.excluded:
            fails.append(f"{name}: 德尔文 被误判为排除名单内")
        for p in scr.panels:
            if p.kind != "暗区首领" and p.cost_raw:
                fails.append(f"{name}: {p.kind} 是免费位，却解析出费用 {p.cost_raw!r}（串栏）")
        # 排除名单的模糊匹配要能认出 OCR 错字
        from darkzone.ui import boss_excluded
        for ex_name, bad in [("阿贾克斯", True), ("雷诺伊尔", True), ("阿贾克 斯", True),
                             ("雷诺伊雨", True), ("德尔文", False), ("加文·麦康奈尔", False)]:
            if boss_excluded(ex_name, block.excluded) != bad:
                fails.append(f"{name}: boss_excluded({ex_name!r}) 结果不对")
    else:
        out(f"首领={scr.boss!r}   派遣按钮图内={scr.dispatch}")
        out(f"词条行 {len(scr.rows)} 行：")
        for r in scr.rows:
            inpool = [s for s, v in pools.items() if r.matched in v]
            out(f"  第{r.index}行 图内y={r.line.cy:.0f} 原文={r.raw!r}")
            out(f"       命中={r.matched!r} score={r.score:.2f} 落池={inpool}")
            out(f"       ↻ 图内({r.refresh[0]:.0f},{r.refresh[1]:.0f}) -> 真机{mapper.pt(r.refresh)}")
        if len(scr.rows) != 3:
            fails.append(f"{name}: 词条行解析出 {len(scr.rows)} 行，期望 3 行")
        if scr.boss != "德尔文":
            fails.append(f"{name}: 首领解析成 {scr.boss!r}")
        for r in scr.rows:
            if r.in_pool:
                fails.append(f"{name}: 第{r.index}行 {r.raw!r} 被误判为池内 {r.matched!r}")
            if abs(r.refresh[0] - 2610) > 35:
                fails.append(f"{name}: 第{r.index}行 ↻ 横坐标 {r.refresh[0]:.0f}，期望 ≈2610"
                             f"（真机实测 2937/3200 换算到 2844 宽）")
        want_y = [262, 462, 661]
        got_y = [round(r.line.cy) for r in scr.rows]
        for i, (g, wnt) in enumerate(zip(got_y, want_y), start=1):
            if abs(g - wnt) > 25:
                fails.append(f"{name}: 第{i}行 y={g}，期望 ≈{wnt}")
        if not scr.dispatch:
            fails.append(f"{name}: 没解析出底部「派遣」按钮")
    out()

out()
# —— 固定文案的 OCR 变体必须都算「标签」（h264 视频流帧会丢字/换字，真机实测）——
#    真机事故：流帧把面板表头「特殊习惯」读成 `持殊习惯`（y=216），它离第 1 行词条的期望位置
#    y≈296 只有 80px（在 0.06H=86px 容差内），于是被当成第 1 行词条 → matched=None → 被判
#    「不在池内」→ 去点它的 ↻（白花 500 券，还可能把已经在池内的装备刷掉）。
from darkzone.ui import _is_detail_label                    # noqa: E402
for text, want in [("点击装备", True), ("占击装备", True), ("击装备", True),
                   ("点 击 装 备", True), ("持殊习惯", True), ("特殊习惯", True),
                   ("特殊条件", True), ("预计收益", True), ("派遣时长", True),
                   ("武装派遣", True), ("消耗", True), ("确定", True),
                   ("AUG突击步枪", False), ("钢铁新星战术头盔", False),
                   ("FA多功能挂", False), ("Med野战背包", False),
                   ("6BL3防弹衣", False), ("SW-5半面罩防毒面具", False)]:
    got = _is_detail_label(text)
    out(f"标签判定 {text!r} -> {got}（期望 {want}）")
    if got != want:
        fails.append(f"_is_detail_label({text!r}) = {got}，期望 {want}")
# 规则池里的装备名一个都不能被当成固定文案
for item in sorted(book.vocab):
    if _is_detail_label(item):
        fails.append(f"规则池装备 {item!r} 被误判成固定文案标签")
out(f"规则池 {len(book.vocab)} 个装备名：没有一个是标签")

out("FAIL 数 = %d" % len(fails))
for f in fails:
    out("  - " + f)
OUT.write_text("\n".join(buf), encoding="utf-8")
print("FAIL", len(fails))
sys.exit(1 if fails else 0)
