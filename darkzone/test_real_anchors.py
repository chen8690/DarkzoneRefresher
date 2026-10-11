"""真机锚点回归：拿两张真机截屏当基准，核对「配置里的比例」与「像素里的按钮」是否一致。

为什么不并进 test_ui：test_ui 喂的是合成图（假手机），只能证明解析逻辑自洽；
这里直接量真机像素，能逮住「比例写错 / 游戏改版挪了按钮 / OCR 行中心和图标对不上」这类真问题。

    python -m darkzone.test_real_anchors      # 结果同时写 real_anchor_test_out.txt

基准图（真机 adb 截屏，3200x1440，横屏）：
  samples/live_list.png    列表页：三栏 ↻ + 三个「选择」橙色按钮
  samples/live_detail.png  详情页：三行 ↻ + 底部「派遣」橙色按钮
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

from .rules import default_rules_path, parse_workbook
from .ui import load_profile, parse_screen

# 控制台在中文 Windows 上默认 GBK，而本套件的断言名里带 ↻：
# 那行 print 会 UnicodeEncodeError 把整套件的退出码打成 1（断言其实全过）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_LIST = ROOT / "samples" / "live_list.png"
SAMPLE_DETAIL = ROOT / "samples" / "live_detail.png"
SAMPLE_DETAIL_HDR = ROOT / "samples" / "live_detail_hdr.png"
SAMPLE_DIALOG = ROOT / "samples" / "live_dialog.png"
RULES = default_rules_path()
OUT_PATH = ROOT / "real_anchor_test_out.txt"

TOL_ICON = 12          # ↻ 图标：配置值 vs 像素实测
TOL_BTN = 30           # 「选择」按钮：OCR 行中心 vs 橙色按钮像素中心
TOL_DISPATCH = 70      # 「派遣」：OCR 行中心 vs 橙色按钮像素中心（按钮很宽，容差给大点）
TOL_ROW_Y = 15         # 词条行：OCR 行中心 y vs ↻ 图标像素中心 y

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


# ------------------------------------------------------------------ 像素工具
def grey(img) -> np.ndarray:
    return np.asarray(img.convert("L")).astype(int)


def orange_mask(img) -> np.ndarray:
    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return (r > 110) & (r > g * 1.7) & (g >= b) & (r - b > 60)


def bright_runs(g: np.ndarray, y0: int, y1: int, x0: int, x1: int,
                thr: int = 150, min_w: int = 12):
    """在 [y0,y1)x[x0,x1) 里找「亮像素列」的连续段，返回 [(xa,xb), ...]。"""
    cols = (g[y0:y1, x0:x1] > thr).sum(axis=0)
    runs, cur = [], None
    for i, c in enumerate(cols):
        if c > 0 and cur is None:
            cur = i
        if c == 0 and cur is not None:
            if i - cur >= min_w:
                runs.append((x0 + cur, x0 + i - 1))
            cur = None
    if cur is not None and len(cols) - cur >= min_w:
        runs.append((x0 + cur, x0 + len(cols) - 1))
    return runs


def icon_center(g: np.ndarray, y0: int, y1: int, x0: int, x1: int):
    """最右侧亮段（= ↻ 字形）的中心。"""
    runs = bright_runs(g, y0, y1, x0, x1)
    if not runs:
        return None
    xa, xb = runs[-1]
    cx = (xa + xb) / 2
    ys, _ = np.where(g[y0:y1, xa:xb + 1] > 150)
    cy = y0 + ys.mean() if len(ys) else (y0 + y1) / 2
    return cx, cy, (xa, xb)


def orange_center(mask: np.ndarray, y0: int, y1: int, x0: int, x1: int):
    sub = mask[y0:y1, x0:x1]
    ys, xs = np.where(sub)
    if not len(xs):
        return None
    return (x0 + (xs.min() + xs.max()) / 2, y0 + (ys.min() + ys.max()) / 2,
            (x0 + xs.min(), x0 + xs.max(), y0 + ys.min(), y0 + ys.max()))


# ------------------------------------------------------------------ 主流程
def main() -> int:
    from .win_ocr import ocr_lines

    prof = load_profile()
    book = parse_workbook(RULES)
    lst = Image.open(SAMPLE_LIST)
    det = Image.open(SAMPLE_DETAIL)
    out(f"基准图 列表={SAMPLE_LIST.name} {lst.size}  详情={SAMPLE_DETAIL.name} {det.size}")
    check("两张基准图都是真机横屏 3200x1440",
          lst.size == (3200, 1440) and det.size == (3200, 1440),
          f"{lst.size} / {det.size}")
    W, H = lst.size
    g_l, g_d = grey(lst), grey(det)
    m_l, m_d = orange_mask(lst), orange_mask(det)

    # ---------------- 列表页 ----------------
    out("\n== 列表页 samples/live_list.png ==")
    rows_l, res_l = ocr_lines(SAMPLE_LIST, lang="zh-Hans-CN")
    kind_l, scr_l = parse_screen(rows_l, lst.size, prof, vocab=book.vocab)
    check("列表页被判成 list", kind_l == "list", kind_l)

    xf = prof["screen_list"]["refresh_x_frac"]
    bands = {"普通队员": (900, 1160), "精锐队员": (1800, 2070), "暗区首领": (2700, 3020)}
    for kind, (xa, xb) in bands.items():
        got = icon_center(g_l, 140, 200, xa, xb)
        want = W * xf[kind]
        check(f"列表 {kind} ↻ 配置=像素",
              got is not None and abs(got[0] - want) <= TOL_ICON,
              f"像素中心 x={got[0]:.1f} (字形 {got[2]}) vs 配置 x={want:.1f}")

    # 三个「选择」按钮：橙色像素中心 vs OCR 行中心
    panel_x = {"普通队员": (0, 1067), "精锐队员": (1067, 2133), "暗区首领": (2133, 3200)}
    for p in scr_l.panels:
        oc = orange_center(m_l, 1290, 1390, *panel_x[p.kind]) if p.kind in panel_x else None
        check(f"列表 {p.kind} 「选择」按钮 = 橙色像素块",
              p.select is not None and oc is not None
              and abs(p.select[0] - oc[0]) <= TOL_BTN,
              f"OCR 中心={tuple(round(v) for v in p.select) if p.select else None} vs "
              f"橙色中心=({oc[0]:.0f},{oc[1]:.0f})" if oc else "找不到橙色按钮")
    check("列表首领名字解析正确",
          (scr_l.by_kind("暗区首领") or None) is not None
          and scr_l.by_kind("暗区首领").name == "伯纳德",
          f"解析出 {(scr_l.by_kind('暗区首领') or None) and scr_l.by_kind('暗区首领').name!r}")

    # ---------------- 详情页 ----------------
    out("\n== 详情页 samples/live_detail.png ==")
    rows_d, res_d = ocr_lines(SAMPLE_DETAIL, lang="zh-Hans-CN")
    kind_d, scr_d = parse_screen(rows_d, det.size, prof, vocab=book.vocab)
    check("详情页被判成 detail", kind_d == "detail", kind_d)
    check("详情页解析出 3 行词条", len(scr_d.rows) == 3,
          f"{len(scr_d.rows)} 行: {[r.raw for r in scr_d.rows]}")

    xd = W * prof["screen_detail"]["refresh_x_frac"]
    for r in scr_d.rows:
        got = icon_center(g_d, int(r.line.cy) - 40, int(r.line.cy) + 40, 2890, 3010)
        check(f"详情 第{r.index}行 ↻ 配置=像素",
              got is not None and abs(got[0] - xd) <= TOL_ICON,
              f"像素中心 x={got[0]:.1f} y={got[1]:.1f} (字形 {got[2]}) vs 配置 x={xd:.1f}")
        check(f"详情 第{r.index}行 ↻ 纵向对齐 OCR 行",
              got is not None and abs(got[1] - r.line.cy) <= TOL_ROW_Y,
              f"像素 y={got[1]:.1f} vs OCR 行 y={r.line.cy:.1f}")
        out(f"       原文={r.raw!r} 命中={r.matched!r} 落池={r.in_pool}")

    oc = orange_center(m_d, 1280, 1440, 0, W)
    bx = oc[2] if oc else None
    out(f"「派遣」橙色按钮 bbox x=[{bx[0]},{bx[1]}] y=[{bx[2]},{bx[3]}] 中心=({oc[0]:.0f},{oc[1]:.0f})"
        if oc else "找不到「派遣」橙色按钮")
    check("详情「派遣」OCR 点落在橙色按钮范围内",
          bx is not None and scr_d.dispatch is not None
          and bx[0] <= scr_d.dispatch[0] <= bx[1] and bx[2] <= scr_d.dispatch[1] <= bx[3],
          f"OCR 点={tuple(round(v) for v in scr_d.dispatch) if scr_d.dispatch else None} vs "
          f"按钮 bbox={bx}")

    # 安全阀：禁点区必须整颗盖住橙色按钮，而且真的点不动
    from .runner import Config, Runner, SafetyError
    from .source import ImageSource
    from .ui import CoordMapper

    r = Runner(book, ImageSource(SAMPLE_DETAIL, (3200, 1440)), Config(dry_run=True))
    r.mapper = CoordMapper(det.size, (3200, 1440))
    r.dispatch_box = scr_d.dispatch_box
    keep = r._dispatch_keepout()
    check("安全阀禁点区整颗盖住「派遣」按钮",
          keep is not None and bx is not None
          and keep[0] <= bx[0] and keep[2] >= bx[1] and keep[1] <= bx[2] and keep[3] >= bx[3],
          f"禁点区={tuple(round(v) for v in keep) if keep else None} vs 按钮 bbox={bx}")
    refused = False
    try:
        r._tap(scr_d.dispatch, "should_refuse")
    except SafetyError:
        refused = True
    check("点「派遣」按钮会被安全阀拦下", refused,
          f"目标={tuple(round(v) for v in scr_d.dispatch) if scr_d.dispatch else None}")

    # 安全阀：正常行刷新离「派遣」足够远，不会被误拦
    guard_r = 0.03 * W
    for r in scr_d.rows:
        d = ((r.refresh[0] - scr_d.dispatch[0]) ** 2
             + (r.refresh[1] - scr_d.dispatch[1]) ** 2) ** 0.5
        check(f"详情 第{r.index}行 ↻ 距「派遣」> 安全阀 {guard_r:.0f}px",
              d > guard_r, f"距离 {d:.0f}px")

    # ---------------- 行锚定：面板表头不能被当成词条行 ----------------
    # 真机实测（live_full_3）：OCR 有时把右侧面板表头「特殊习惯」也读出来、x 又落在右侧面板里，
    # 旧实现按几何顺序取行 → 四行、三行整体错位（slot1=表头、slot2=真第 1 行…）；结果槽2
    # 拿第 1 行的武器文本去比护甲池，连刷 30 次一次没命中、白花 15000 券。
    # 现在按每行「点击装备」标签反推锚点（装备名在其上方 ≈0.072H）。
    out("\n== 行锚定回归 samples/live_detail_hdr.png ==")
    from .ui import _is_detail_label

    rows_h, res_h = ocr_lines(SAMPLE_DETAIL_HDR, lang="zh-Hans-CN")
    kind_h, scr_h = parse_screen(rows_h, det.size, prof, vocab=book.vocab)
    check("带面板表头的详情页仍被判成 detail", kind_h == "detail", kind_h)
    check("带面板表头时仍只解析出 3 行", len(scr_h.rows) == 3,
          f"{len(scr_h.rows)} 行: {[r.raw for r in scr_h.rows]}")
    if len(scr_h.rows) == 3:
        check("第 1 行不是面板表头/固定文案",
              not _is_detail_label(scr_h.rows[0].raw), f"第1行={scr_h.rows[0].raw!r}")
        check("第 2 行锚到真机画面上的 6BL3防弹衣",
              "6BL3" in scr_h.rows[1].raw, f"第2行={scr_h.rows[1].raw!r}")
        check("第 3 行锚到 钢铁新星战术头盔 且命中规则池",
              scr_h.rows[2].matched == "钢铁新星战术头盔",
              f"原文={scr_h.rows[2].raw!r} 命中={scr_h.rows[2].matched!r}")

    # ---------------- 刷新确认弹窗 ----------------
    # 真机实测：点 ↻ 不会直接刷新，而是先弹「提示 / 是否刷新特殊条件：xxx / 确定」。
    out("\n== 刷新确认弹窗 samples/live_dialog.png ==")
    from .ui import dialog_is_refresh, is_dialog, parse_dialog

    dlg_img = Image.open(SAMPLE_DIALOG)
    rows_g, _ = ocr_lines(SAMPLE_DIALOG, lang="zh-Hans-CN")
    kind_g, scr_g = parse_screen(rows_g, dlg_img.size, prof, vocab=book.vocab)
    check("弹窗截屏被判成 dialog（而不是 list）", kind_g == "dialog", kind_g)
    check("弹窗解析出「确定」按钮", getattr(scr_g, "confirm", None) is not None,
          f"confirm={getattr(scr_g, 'confirm', None)}")
    check("弹窗正文含「刷新」且允许自动确认",
          is_dialog(scr_g) and dialog_is_refresh(scr_g),
          f"prompt={getattr(scr_g, 'prompt', None)!r} title={getattr(scr_g, 'title', None)!r}")
    if getattr(scr_g, "confirm", None):
        # 「确定」按钮的橙色像素块应当就在 OCR 文字位置上（±40px）
        oc_g = orange_center(orange_mask(dlg_img), int(scr_g.confirm[1]) - 60,
                             int(scr_g.confirm[1]) + 60, 1400, 1800)
        check("弹窗「确定」= 橙色像素块",
              oc_g is not None and abs(oc_g[0] - scr_g.confirm[0]) <= 40
              and abs(oc_g[1] - scr_g.confirm[1]) <= 40,
              f"OCR 中心=({scr_g.confirm[0]:.0f},{scr_g.confirm[1]:.0f}) vs "
              f"橙色中心={tuple(round(v) for v in oc_g[:2]) if oc_g else None}")

    dlg_runner = Runner(book, ImageSource(SAMPLE_DIALOG, (3200, 1440)), Config(dry_run=True))
    st_dlg = dlg_runner._read("dlg")
    check("读到弹窗时安全阀的「派遣」坐标被清空（否则会误拦确定键）",
          st_dlg["kind"] == "dialog" and dlg_runner.dispatch_box is None
          and dlg_runner.dispatch_xy is None,
          f"kind={st_dlg['kind']} box={dlg_runner.dispatch_box}")
    tapped = dlg_runner._tap(st_dlg["screen"].confirm, "dlg_confirm")
    check("弹窗「确定」不被安全阀拦下（dry-run 只记 would_tap）", tapped is False, f"{tapped}")
    st2, confirmed = dlg_runner._handle_dialog(st_dlg, "dlg")
    check("刷新弹窗走自动确认路径（dry-run 返回 confirmed=True）", confirmed is True, f"{confirmed}")

    # 反向安全性：万一弹出的是「是否派遣」这种不可逆确认框，必须拒绝自动点确定
    fake_rows = [("提示", 959, 359, 93, 45), ("是否派遣该队伍", 1198, 669, 804, 42),
                 ("确定", 1545, 1009, 94, 46)]
    fake_dlg = parse_dialog(fake_rows, (3200, 1440))
    check("「是否派遣」也被识别成弹窗", is_dialog(fake_dlg), f"{fake_dlg.prompt!r}")
    check("「是否派遣」弹窗禁止自动确认", not dialog_is_refresh(fake_dlg), "")
    fake_st = {"kind": "dialog", "screen": fake_dlg, "rows": fake_rows,
               "image": "synthetic", "size": (3200, 1440)}
    dlg_refused = False
    try:
        dlg_runner._handle_dialog(fake_st, "fake")
    except SafetyError:
        dlg_refused = True
    check("自动确认遇到派遣弹窗会拒绝（抛 SafetyError）", dlg_refused, "")

    # ---------------- 视频流帧（h264 取帧）解析回归 ----------------
    # 视频流取帧快得多（读一帧 0.01s vs gzip 1.3s），但 h264 会把小字糊掉：
    #   ① 「点击装备」丢「点」→ 读成「占击装备 / 击装备」；
    #   ② 首帧（刚起流的关键帧）质量最差，实测会**整行漏掉**第 1 行装备名，
    #      右侧面板表头则被读成 `持殊习惯` 顶上来当第 1 行 → ↻ y 变成 400/522/624。
    # 这两条都必须被挡住：宁可行数不对让 runner 重读/停手，也不能把表头当词条去点 ↻。
    out("\n== 视频流帧 samples/stream_*.jpg（h264 取帧） ==")
    from .ocr_match import similarity

    s_first = ROOT / "samples" / "stream_first_frame.jpg"
    s_steady = ROOT / "samples" / "stream_steady.jpg"
    s_gzip = ROOT / "samples" / "gzip_detail_same_screen.jpg"
    want_rows = [("AUG突击步枪", "AUG突击步枪"), ("FA多功能", None),
                 ("钢铁新星战术头盔", "钢铁新星战术头盔")]
    for name in ("stream_steady.jpg", "stream_first_frame.jpg", "gzip_detail_same_screen.jpg"):
        img_p = ROOT / "samples" / name
        rows_s, res_s = ocr_lines(img_p, lang="zh-Hans-CN")
        size_s = (res_s.get("width") or Image.open(img_p).size[0],
                  res_s.get("height") or Image.open(img_p).size[1])
        kind_s, scr_s = parse_screen(rows_s, size_s, prof, vocab=book.vocab)
        raws = [r.raw for r in scr_s.rows]
        ys = [round(r.line.cy) for r in scr_s.rows]
        check(f"{name} 判成 detail", kind_s == "detail", kind_s)
        check(f"{name} 没有任何一行是固定文案/面板表头（不能拿表头当词条）",
              not any(_is_detail_label(r.raw) for r in scr_s.rows), f"{raws}")
        if name != "stream_first_frame.jpg":
            check(f"{name} 三行齐全", len(scr_s.rows) == 3, f"{raws}")
        if len(scr_s.rows) == 3:
            check(f"{name} 三行 y 落在真机位置 296/520/744",
                  all(abs(y - w) <= 12 for y, w in zip(ys, (296, 520, 744))),
                  f"{ys}")
            for r, (want_raw, want_match) in zip(scr_s.rows, want_rows):
                # 流帧的原文一定有 OCR 噪声（实测 '钢铁新星战木头盔'），所以按形近比，
                # 关键是命中（matched）必须与 gzip 路一致——池内判定不能因取屏路而变。
                check(f"{name} 第{r.index}行 原文与 {want_raw!r} 形近",
                      similarity(r.raw, want_raw) >= 0.6, f"{r.raw!r}")
                check(f"{name} 第{r.index}行 命中={want_match!r}",
                      r.matched == want_match, f"命中={r.matched!r} score={r.score:.2f}")
        else:
            # 首帧允许「行数不足」（行数守卫会让 runner 重读），但不允许错位成表头
            check(f"{name} 行数不足时也不许把固定文案当词条",
                  all(not _is_detail_label(x) for x in raws), f"{raws}")

    out(f"\n{'=' * 60}\nFAIL {len(FAILS)}")
    for f in FAILS:
        out(f"  - {f}")
    OUT_PATH.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    out(f"结果已写 {OUT_PATH}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
