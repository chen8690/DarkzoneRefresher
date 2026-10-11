"""换 OCR 引擎回归：用 samples/ocr_rows_rapid.json 里**固化的真实 OCR 行**，
断言「同一张真机截图，Windows OCR 与 RapidOCR 读出来的东西解析结果一致」。

为什么不并进别的套件：RapidOCR 只装在项目 .venv（`pip install rapidocr-onnxruntime`），
而套件要能用 runtime python 跑。把真机截图上的真实 OCR 行固化成 JSON 之后，
不装引擎也能回归「换引擎后界面还解析得对吗」。

这个套件要防的三类历史事故：
1. RapidOCR 把标题拆成两行（`'暗区首领'` + `'伯纳德'`，破折号还会丢），
   导致详情页 boss=None、列表页暗区首领栏名字为空 → `ui.merge_split_labels` 修的就是它；
2. 右侧面板表头混进词条行 → 三行整体错位（live_full_3 槽2 连刷 30 次没命中）；
3. `'Med野战背包'` 被判成池内「野战登山背包」→ 假阳性（误判 #3）。

    python -m darkzone.test_ocr_rows        # 结果同时写 ocr_rows_test_out.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .ocr_match import similarity
from .rules import default_rules_path, parse_workbook
from .ui import (dialog_is_refresh, dialog_own_text, is_dialog, load_profile,
                 parse_screen)

# 控制台在中文 Windows 上默认 GBK，而本套件的断言名里带 ↻：
# 那行 print 会 UnicodeEncodeError 把整套件的退出码打成 1（断言其实全过）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "samples" / "ocr_rows_rapid.json"
RULES = default_rules_path()
OUT_PATH = ROOT / "ocr_rows_test_out.txt"

TOL_REFRESH = 8          # 两个引擎算出来的 ↻ 横坐标允许差多少像素
BOSS = "伯纳德"

LINES: list[str] = []
FAILS: list[str] = []


def out(s: str = ""):
    print(s)
    LINES.append(s)


def check(name: str, ok: bool, detail: str = ""):
    tag = "OK  " if ok else "FAIL"
    out(f"[{tag}] {name}" + (f"  ::  {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name} :: {detail}")


def main():
    if not FIXTURE.exists():
        out(f"[NG] 缺少基准数据 {FIXTURE}")
        return 3
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    book = parse_workbook(RULES)
    profile = load_profile()
    detail_x = profile["screen_detail"]["refresh_x_frac"]
    list_x = profile["screen_list"]["refresh_x_frac"]

    def parse(img, engine):
        e = data[img][engine]
        rows = [tuple(r) for r in e["rows"]]
        kind, scr = parse_screen(rows, (e["width"], e["height"]), profile,
                                 vocab=book.vocab, min_score=0.62)
        return kind, scr, e

    want_kind = {"live_list": "list", "live_detail": "detail", "live_detail_hdr": "detail",
                 "fp_med_yezhan": "detail", "fake_detail_K": "detail", "live_dialog": "dialog"}

    out("=== 1. 每个引擎、每张图的解析结果 ===")
    for img, kind_want in want_kind.items():
        if img not in data:
            check(f"{img} 在基准数据里", False, "缺失")
            continue
        for engine in ("win", "rapid"):
            if engine not in data[img]:
                continue
            kind, scr, e = parse(img, engine)
            W, H = e["width"], e["height"]
            check(f"{img}/{engine} 判页={kind_want}", kind == kind_want, f"实际 {kind}")
            if kind == "detail":
                boss = scr.boss or ""
                check(f"{img}/{engine} boss 解析出「{BOSS}」",
                      similarity(boss, BOSS) >= 0.7, f"boss={boss!r}")
                check(f"{img}/{engine} 恰好 3 行词条", len(scr.rows) == 3,
                      f"{len(scr.rows)} 行：{[r.raw for r in scr.rows]}")
                xs = [r.refresh[0] for r in scr.rows]
                check(f"{img}/{engine} 三行 ↻ 横坐标=配置 {W * detail_x:.0f}",
                      all(abs(x - W * detail_x) <= 1 for x in xs), f"{xs}")
                check(f"{img}/{engine} 有「派遣」按钮（只用来做禁点区）",
                      scr.dispatch is not None and scr.dispatch_box is not None,
                      f"dispatch={scr.dispatch} box={scr.dispatch_box}")
                ys = [round(r.refresh[1]) for r in scr.rows]
                check(f"{img}/{engine} 三行 y 递增且在面板中段",
                      ys == sorted(ys) and all(0.07 * H <= y <= 0.82 * H for y in ys), f"{ys}")
            elif kind == "dialog":
                check(f"{img}/{engine} 认出弹窗（有确定 + 标题/问句）", is_dialog(scr),
                      f"confirm={scr.confirm} title={scr.title!r} prompt={scr.prompt!r}")
                check(f"{img}/{engine} 判成「刷新」框（允许自动点确定）", dialog_is_refresh(scr),
                      f"own={dialog_own_text(scr)!r}")
                check(f"{img}/{engine} 弹窗自己的文字里没有禁止词（派遣等）",
                      not any(w in dialog_own_text(scr) for w in ("派遣", "提交", "购买")),
                      f"own={dialog_own_text(scr)!r}")
                check(f"{img}/{engine} 正文含「刷新」且带词条名",
                      "刷新" in (scr.prompt or "") and len(scr.prompt or "") >= 10,
                      f"prompt={scr.prompt!r}")
                check(f"{img}/{engine} 「确定」坐标≈(1592,1032)",
                      scr.confirm is not None and abs(scr.confirm[0] - 1592) <= 25
                      and abs(scr.confirm[1] - 1032) <= 25, f"{scr.confirm}")
            else:
                kinds = [p.kind for p in scr.panels]
                check(f"{img}/{engine} 三栏 = 普通/精锐/暗区首领",
                      kinds == ["普通队员", "精锐队员", "暗区首领"], f"{kinds}")
                boss_panel = scr.by_kind("暗区首领")
                check(f"{img}/{engine} 暗区首领栏名字解析出「{BOSS}」",
                      boss_panel is not None and similarity(boss_panel.name, BOSS) >= 0.7,
                      f"name={boss_panel.name!r}" if boss_panel else "没这一栏")
                check(f"{img}/{engine} 暗区首领 ↻ 横坐标=配置 {W * list_x['暗区首领']:.0f}",
                      boss_panel is not None
                      and abs(boss_panel.refresh[0] - W * list_x["暗区首领"]) <= 1,
                      f"{boss_panel.refresh if boss_panel else None}")
                check(f"{img}/{engine} 三栏「选择」都在 y≈0.928H",
                      all(p.select and abs(p.select[1] - 0.928 * H) < 10 for p in scr.panels),
                      f"{[p.select for p in scr.panels]}")

    out("\n=== 2. 两个引擎的结论必须一致（换引擎不能换结果）===")
    for img in want_kind:
        if img not in data or "win" not in data[img] or "rapid" not in data[img]:
            continue
        kw, sw, _ = parse(img, "win")
        kr, sr, _ = parse(img, "rapid")
        check(f"{img} 两引擎判页一致", kw == kr, f"win={kw} rapid={kr}")
        if kw == "detail" and kr == "detail":
            check(f"{img} 两引擎行数一致", len(sw.rows) == len(sr.rows),
                  f"{len(sw.rows)} vs {len(sr.rows)}")
            check(f"{img} 两引擎 boss 一致",
                  similarity(sw.boss or "", sr.boss or "") >= 0.9,
                  f"win={sw.boss!r} rapid={sr.boss!r}")
            d = max(abs(a.refresh[0] - b.refresh[0]) for a, b in zip(sw.rows, sr.rows))
            check(f"{img} 两引擎三行 ↻ 横坐标差 ≤ {TOL_REFRESH}px", d <= TOL_REFRESH, f"最大差 {d:.1f}px")
            # 词条串本身两个引擎会有差异（win 会插空格/认错字母），只要求「池内判定」一致
            same_pool = [a.matched for a in sw.rows] == [b.matched for b in sr.rows]
            check(f"{img} 两引擎的「是否在池内」判定一致", same_pool,
                  f"win={[a.matched for a in sw.rows]} rapid={[b.matched for b in sr.rows]}")
        elif kw == "list" and kr == "list":
            check(f"{img} 两引擎三栏种类一致",
                  [p.kind for p in sw.panels] == [p.kind for p in sr.panels],
                  f"{[p.kind for p in sw.panels]} vs {[p.kind for p in sr.panels]}")
        elif kw == "dialog" and kr == "dialog":
            check(f"{img} 两引擎都允许自动确认刷新框",
                  dialog_is_refresh(sw) and dialog_is_refresh(sr),
                  f"win={dialog_is_refresh(sw)} rapid={dialog_is_refresh(sr)}")
            check(f"{img} 两引擎「确定」坐标差 ≤ 6px",
                  abs(sw.confirm[0] - sr.confirm[0]) <= 6 and abs(sw.confirm[1] - sr.confirm[1]) <= 6,
                  f"win={sw.confirm} rapid={sr.confirm}")
        if kw in ("detail", "list") and kr == kw:
            check(f"{img} 两引擎都解析出首领名",
                  similarity((sw.boss if kw == "detail" else sw.by_kind("暗区首领").name) or "",
                             (sr.boss if kr == "detail" else sr.by_kind("暗区首领").name) or "") >= 0.9)

    out("\n=== 3. 具体历史事故的钉子 ===")
    for engine in ("win", "rapid"):
        if "fp_med_yezhan" in data and engine in data["fp_med_yezhan"]:
            _, scr, _ = parse("fp_med_yezhan", engine)
            row3 = scr.rows[2]
            check(f"{engine}: 「Med野战背包」不能判成池内（误判 #3）",
                  row3.matched is None, f"matched={row3.matched!r} score={row3.score:.3f}")
            check(f"{engine}: 同一张图的第 2 行「KN复合防弹衣」要判在池内",
                  scr.rows[1].matched == "KN复合防弹衣", f"{scr.rows[1].matched!r}")
        if "live_detail_hdr" in data and engine in data["live_detail_hdr"]:
            _, scr, _ = parse("live_detail_hdr", engine)
            check(f"{engine}: 面板表头混入也不能错位（3 行、第 3 行=钢铁新星）",
                  len(scr.rows) == 3 and scr.rows[2].matched == "钢铁新星战术头盔",
                  f"{[(r.raw, r.matched) for r in scr.rows]}")
        if "fake_detail_K" in data and engine in data["fake_detail_K"]:
            _, scr, _ = parse("fake_detail_K", engine)
            check(f"{engine}: 合成假详情页 boss=伯纳德（RapidOCR 曾在这里丢名字）",
                  similarity(scr.boss or "", BOSS) >= 0.7, f"{scr.boss!r}")
        if "live_dialog" in data and engine in data["live_dialog"]:
            kind, scr, _ = parse("live_dialog", engine)
            check(f"{engine}: 真机刷新框仍判成弹窗（RapidOCR 会读出背景的「消耗4派遣」）",
                  kind == "dialog" and is_dialog(scr), f"kind={kind}")
            check(f"{engine}: 背景里的「派遣」二字不能把刷新框误杀成危险弹窗",
                  dialog_is_refresh(scr) and "派遣" not in dialog_own_text(scr),
                  f"own={dialog_own_text(scr)!r}")

    out("\n" + "=" * 60)
    out(f"FAIL {len(FAILS)}")
    for f in FAILS:
        out(f"  - {f}")
    OUT_PATH.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    out(f"\n结果已写入 {OUT_PATH}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
