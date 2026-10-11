"""界面解析层：把一张截图的 OCR 行解析成「可点击的 UI 结构」。

设计取舍：
- **纵向坐标全部来自 OCR**（面板标题行 / 词条行的 y），所以换分辨率、换排版都还能对上；
- **横向坐标用比例常量**（那个 ↻ 图标 OCR 经常认不出来，但游戏 UI 是固定版式），
  比例集中在 profiles/default.json，改一个数字就能重新校准；
- 点击目标一律先按「图片坐标」算，再由 CoordMapper 换算到真机坐标。

两张标定图：samples/screen1_refresh_boss.jpg、samples/screen2_refresh_entries.jpg（2844x1280）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ocr_match import canon, extract_items, similarity

HERE = Path(__file__).resolve().parent
DEFAULT_PROFILE = HERE.parent / "profiles" / "default.json"

# 面板种类（含 OCR 常见错字）
KIND_PATTERNS = [
    ("暗区首领", r"暗区首领"),
    ("精锐队员", r"精[锐说英]队员|精[锐说英]队"),
    ("普通队员", r"普通队员|普通队"),
]
SEP_CHARS = "一—–-－_．.。~～、|丨:：，,；;（()）【】[] "

# 详情页里不是装备名的固定文案
DETAIL_LABELS = {
    "特殊习惯", "预计收益", "派遣时长", "点击装备", "消耗", "派遣", "武装派遣",
    "特殊习惯", "幸运物", "财迷", "天选者", "老实人", "卓越效率", "高效执行",
    "派遣收益下限提升", "派遣时长缩短", "验证", "确认", "取消", "确定",
}
LABEL_RE = re.compile(r"^(?:[\d\-/．.]+|[?？]+|品质.*|约.*|.*[Kk]|.*时.*|.*分.*|.*%|.*％)$")

# 「点击装备」这个固定文案在 h264 视频流帧里会被编码糊掉：真机流帧实测读成「占击装备」「击装备」
# （「点」丢了），而它同时是判详情页和反推三行锚点的依据——一旦读不出来，锚点失效、几何兜底会把
# 按钮行当成词条行，↻ 坐标整体错位（真机 dry-run 出现过 y=400/522/624 而不是 296/520/744）。
# 所以对这个固定文案按「尾巴」容忍错字，并允许字间空格（Windows OCR 偶尔插空）。
DETAIL_TAP_RE = re.compile(r"[点占店]?击装备")


def is_tap_equip_label(text: str) -> bool:
    """是不是「点击装备」这个固定按钮文案（容忍 h264 流帧丢「点」）。"""
    return bool(DETAIL_TAP_RE.search("".join((text or "").split())))


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


@dataclass
class Panel:
    """列表页（界面1）里的一个派遣位。"""
    kind: str
    name: str
    cost_raw: str = ""
    refresh: tuple = (0, 0)
    select: tuple | None = None
    title_line: Line | None = None


@dataclass
class EntryRow:
    """详情页（界面2）里的一行装备词条。"""
    index: int
    raw: str
    refresh: tuple
    center: tuple
    line: Line | None = None
    matched: str | None = None
    score: float = 0.0

    @property
    def in_pool(self):
        return self.matched is not None


@dataclass
class ListScreen:
    panels: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    size: tuple = (0, 0)

    def by_kind(self, kind):
        for p in self.panels:
            if p.kind == kind:
                return p
        return None


@dataclass
class DetailScreen:
    boss: str | None = None
    rows: list = field(default_factory=list)
    dispatch: tuple | None = None
    dispatch_box: tuple | None = None      # 「派遣」那行文字的 (x0,y0,x1,y1)，安全阀用
    lines: list = field(default_factory=list)
    size: tuple = (0, 0)

    def row(self, index):
        for r in self.rows:
            if r.index == index:
                return r
        return None


@dataclass
class DialogScreen:
    """弹窗（真机实测：点词条行的 ↻ 会先弹「是否刷新特殊条件：xxx」+「确定」）。"""
    title: str | None = None
    prompt: str | None = None
    confirm: tuple | None = None          # 「确定」按钮中心（图片坐标）
    confirm_box: tuple | None = None
    cancel: tuple | None = None           # 右上角 ✕（图片坐标）
    text: str = ""
    lines: list = field(default_factory=list)
    size: tuple = (0, 0)


class CoordMapper:
    """图片坐标 → 真机坐标。"""

    def __init__(self, image_size, device_size):
        self.iw, self.ih = image_size
        self.dw, self.dh = device_size
        self.sx = self.dw / self.iw if self.iw else 1.0
        self.sy = self.dh / self.ih if self.ih else 1.0

    def pt(self, xy):
        return (int(round(xy[0] * self.sx)), int(round(xy[1] * self.sy)))

    def box(self, box):
        x, y, w, h = box
        x1, y1 = self.pt((x, y))
        x2, y2 = self.pt((x + w, y + h))
        return [x1, y1, x2 - x1, y2 - y1]


def load_profile(path=None) -> dict:
    p = Path(path) if path else DEFAULT_PROFILE
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def to_lines(rows) -> list:
    return merge_split_labels([Line(t, x, y, w, h) for t, x, y, w, h in rows])


# 标题里「某某——名字」的破折号（Windows OCR 会读成「一一」，RapidOCR 读成一个 '-' 或者干脆丢掉）
DASH_TAIL_RE = re.compile(r"[一—–\-－_~～]+$")


def _label_without_name(l: Line) -> bool:
    """这一行是不是「只有标题、名字还在后面那行」的半截行。"""
    t = l.text.strip()
    if not t:
        return False
    if DASH_TAIL_RE.search(t):          # '暗区首领-'、'普通队员——'
        return True
    kind, name = split_kind_name(t)
    return bool(kind) and not name       # 真机 RapidOCR 实测：'暗区首领' 与 '伯纳德' 分成两行，破折号丢了


def merge_split_labels(lines: list) -> list:
    """把被 OCR 拆成两行的「标题——名字」合回一行（**与 OCR 引擎无关**）。

    同一张真机截图的实测差异：
    - Windows OCR：`'暗 区 首 领 一 一 伯 纳 德'` 一行读完；
    - RapidOCR：`'暗区首领'` + `'伯纳德'` 两行（破折号丢失），或 `'暗区首领-'` + `'伯纳德'`。
    后者会让 `split_kind_name` 拿不到名字（`test_runner_offline` 场景 K 在 RapidOCR 下
    `boss_display=None`、列表页暗区首领栏 name 为空，都是这个原因）。

    合并条件很窄：左行是「只有标题/只到破折号」的半截行 + 右行在同一视觉行 +
    右行紧贴在标题右边（间隔 ≤ 2 倍字高）。win_ocr 的输出不满足条件，对它是空操作。
    """
    out = [Line(l.text, l.x, l.y, l.w, l.h) for l in lines]
    for _ in range(10):                     # 允许 'A-' + 'B-' + 'C' 这种链式拆分
        merged = False
        for l in out:
            if not _label_without_name(l):
                continue
            best = None
            for c in out:
                if c is l or not c.text.strip():
                    continue
                if abs(c.cy - l.cy) > max(l.h, c.h, 1.0) * 0.7:      # 不在同一视觉行
                    continue
                if c.x < l.x + l.w * 0.6:                            # 不在标题右边
                    continue
                if c.x - (l.x + l.w) > max(l.h, c.h, 1.0) * 2:       # 离得太远，不是同一句
                    continue
                if best is None or c.x < best.x:
                    best = c
            if best is None:
                continue
            right = max(l.x + l.w, best.x + best.w)
            top, bottom = min(l.y, best.y), max(l.y + l.h, best.y + best.h)
            l.text = l.text.rstrip() + best.text.strip()
            l.x, l.y = min(l.x, best.x), top
            l.w, l.h = right - l.x, bottom - top
            out.remove(best)
            merged = True
            break
        if not merged:
            break
    return out


def split_kind_name(text: str):
    for kind, pat in KIND_PATTERNS:
        m = re.search(pat, text)
        if m:
            rest = text[m.end():]
            rest = rest.lstrip(SEP_CHARS).strip()
            rest = re.sub(rf"^[{re.escape(SEP_CHARS)}]+", "", rest)
            return kind, rest
    return None, None


def boss_excluded(name: str, excluded) -> bool:
    """首领是否在排除名单里（OCR 名字会错，所以模糊比）。"""
    if not name:
        return False
    cn = canon(name)
    for ex in excluded or []:
        ce = canon(ex)
        if not ce:
            continue
        if ce in cn or similarity(name, ex) >= 0.6:
            return True
    return False


LABEL_FUZZY_MIN = 0.72
LABEL_WORDS = tuple(sorted(DETAIL_LABELS))


def _fuzzy_detail_label(text: str) -> bool:
    """固定文案的 OCR 变体也算标签。

    真机实测：h264 流帧把「特殊习惯」读成 `持殊习惯`（y=216），而第 1 行词条的真实位置在
    y≈296——`特/持` 一字之差就绕过了精确匹配与 startswith，结果这行表头被当成第 1 行词条
    （matched=None → 判成「不在池内」→ 去点它的 ↻，白花 500 券，还可能把已在池内的装备刷掉）。
    这里用形近归一后的字符串相似度兜底（允许前后各差一个字长度的偏差）。
    """
    t = (text or "").strip()
    if len(t) < 2:
        return False
    for w in LABEL_WORDS:
        if abs(len(t) - len(w)) <= 1 and similarity(t, w) >= LABEL_FUZZY_MIN:
            return True
    return False


def _is_detail_label(text: str) -> bool:
    t = text.strip()
    # h264 流帧会把「点击装备」糊成「占击装备」「击装备」——不认出来的话，锚点行会被当成词条行。
    if is_tap_equip_label(t):
        return True
    # OCR 常在前面粘一个残字/标点（真机实测 '；特殊习惯'、'》T03突击步枪'、'|ND战术头盔雪山版'），
    # 精确匹配与 startswith 都会被这个前导字符绕过——曾经因此把右侧面板表头「特殊习惯」当成
    # 第 1 行词条，三行整体错位（live_full_3 槽2 连刷 30 次没命中的真因）。
    t2 = t.strip(SEP_CHARS).strip()
    for cand in (t, t2) if t2 != t else (t,):
        if cand in DETAIL_LABELS:
            return True
        if cand.startswith(("特殊习惯", "特殊条件", "预计收益", "派遣时长", "点击装备",
                            "消耗", "派遣", "品质", "幸运", "财迷", "天选", "老实",
                            "卓越", "高效", "验证", "确认", "取消", "确定")):
            return True
        if LABEL_RE.match(cand):
            return True
        if _fuzzy_detail_label(cand):
            return True
    return False


def _size(lines, size=None):
    """页面尺寸优先用截图真实宽高；没给就退回文字外框（会偏小，仅兜底）。"""
    if size:
        return int(size[0]), int(size[1])
    W = max((l.x + l.w for l in lines), default=0)
    H = max((l.y + l.h for l in lines), default=0)
    return W, H


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


# --------------------------------------------------------------- 弹窗
# 真机实测：点列表页/详情页的 ↻ 都会先弹一个模态框——
#   标题「提示」+「是否刷新特殊条件：Vector45微型冲锋枪」+ 橙色「确定」+ 右上角 ✕。
# 弹窗没处理掉，点击就白点了（而且背景被压暗，OCR 只读得到弹窗本身）。
CONFIRM_WORDS = ("确定", "确认", "继续")
DIALOG_PROMPT_RE = re.compile(r"是否|确认要|将要|确定要")
DIALOG_TITLE_RE = re.compile(r"^(提示|警告|通知|确认)$")
# 绝不允许自动确认的弹窗（防手滑点到派遣/删除这类不可逆操作）
DIALOG_FORBID_RE = re.compile(r"派遣|提交|出售|购买|分解|丢弃|删除|回收|兑换|升级")


def _btn_text(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fff]", "", text or "")


def parse_dialog(rows, size=None, profile=None) -> DialogScreen:
    lines = to_lines(rows)
    W, H = _size(lines, size)
    scr = DialogScreen(lines=lines, size=(W, H))
    for l in lines:
        t = _btn_text(l.text)
        if not t:
            continue
        cy_ok = l.cy > 0.45 * H          # 弹窗按钮在下半屏
        if t in CONFIRM_WORDS and len(t) <= 4 and cy_ok and 0.08 * W <= l.cx <= 0.92 * W:
            if scr.confirm is None or l.w > scr.confirm_box[2] - scr.confirm_box[0]:
                scr.confirm = (l.cx, l.cy)
                scr.confirm_box = (l.x, l.y, l.x + l.w, l.y + l.h)
        if DIALOG_TITLE_RE.match(t) and cy_ok and not scr.title:
            scr.title = t
        if DIALOG_PROMPT_RE.search(l.text) and not scr.prompt:
            scr.prompt = l.text.strip()
    scr.text = " ".join((l.text or "").strip() for l in lines)
    return scr


def is_dialog(scr: DialogScreen) -> bool:
    """有「确定」按钮，且还有标题或「是否…」问句，才算弹窗（避免把详情页的固定文案当弹窗）。"""
    return scr.confirm is not None and (scr.prompt is not None or scr.title is not None)


def dialog_own_text(scr: DialogScreen) -> str:
    """弹窗自己说的话（标题 + 正文），**不含**被压暗的背景行。

    坑（离线假手机逮到的）：背景虽然被压暗，OCR 有时仍读得出「消耗4 派遣」这类背景文字；
    若拿整屏文本去判「含不含禁止词」，所有刷新框都会被误杀（真机上背景通常读不出，所以更难发现）。
    """
    return " ".join(t for t in (scr.title, scr.prompt) if t)


def dialog_is_refresh(scr: DialogScreen) -> bool:
    """这个弹窗是不是「刷新」确认框（允许自动点确定）。只看弹窗自己的文字。"""
    own = dialog_own_text(scr)
    return bool(re.search(r"刷新", own)) and not DIALOG_FORBID_RE.search(own)


# --------------------------------------------------------------- 列表页（界面1）
def parse_list_screen(rows, size=None, profile=None) -> ListScreen:
    profile = profile or load_profile()
    prof = profile.get("screen_list", {})
    xfrac = prof.get("refresh_x_frac", {})
    lines = to_lines(rows)
    W, H = _size(lines, size)
    scr = ListScreen(lines=lines, size=(W, H))

    # 本栏横向范围：以「本栏 ↻ 的横向位置」为锚点，取到相邻锚点的中点。
    # 坑：不能只用标题文字框 l.x..l.x+w —— OCR 会把左邻栏的「免费」和本栏标题粘成一行
    # （真机实测 '精锐队员' 那行被并成 '[免费0精锐队员'，x 落在左栏里），
    # 那样 col 会往左溢出，把左栏的「选择」按钮抢过来 → 点错人。
    anchors = sorted(W * v for v in xfrac.values()) or [W * 0.91]

    def col_of(kind):
        a = W * xfrac.get(kind, 0.91)
        lo = max([v for v in anchors if v < a - 1] or [0.0])
        hi = min([v for v in anchors if v > a + 1] or [float(W)])
        return ((lo + a) / 2, (a + hi) / 2)

    for l in lines:
        kind, name = split_kind_name(l.text)
        if not kind:
            continue
        rx = W * xfrac.get(kind, 0.91)
        panel = Panel(kind=kind, name=name, title_line=l, refresh=(rx, l.cy))
        col = col_of(kind)                 # 这一栏的横向范围，防止串栏取到邻居的按钮/数字
        # 同一行的费用（暗区首领 15000，另两栏是「免费」→ OCR 常读不出来，留空）
        for o in lines:
            if o is l or abs(o.cy - l.cy) > 40 or not (col[0] <= o.cx <= col[1]):
                continue
            if len(re.sub(r"\D", "", o.text)) >= 3:
                panel.cost_raw = o.text
                break
        # 同一栏内的「选择」按钮
        cands = [o for o in lines if o.text.strip() == "选择" and col[0] <= o.cx <= col[1]]
        if cands:
            near = max(cands, key=lambda o: o.cy)
            panel.select = (near.cx, near.cy)
        scr.panels.append(panel)
    scr.panels.sort(key=lambda p: p.title_line.x if p.title_line else 0)
    return scr


# --------------------------------------------------------------- 详情页（界面2）
def parse_detail_screen(rows, size=None, profile=None, vocab=(), min_score=0.62) -> DetailScreen:
    profile = profile or load_profile()
    prof = profile.get("screen_detail", {})
    xfrac = prof.get("refresh_x_frac", 0.91)
    right_min = prof.get("right_panel_min_x_frac", 0.60)
    lines = to_lines(rows)
    W, H = _size(lines, size)
    scr = DetailScreen(lines=lines, size=(W, H))

    for l in lines:
        kind, name = split_kind_name(l.text)
        if kind and name and not scr.boss:
            scr.boss = name
        if "派遣" in l.text and l.cy > 0.8 * H:
            scr.dispatch = (l.cx, l.cy)
            scr.dispatch_box = (l.x, l.y, l.x + l.w, l.y + l.h)

    # 右侧面板中段、像装备名的行 = 词条行（排除顶部状态栏、底部操作栏、固定文案）
    band = (0.07 * H, 0.82 * H)
    cand = [l for l in lines
            if l.x >= right_min * W and band[0] <= l.cy <= band[1]
            and len(l.text.strip()) >= 2 and _cjk_count(l.text) >= 2
            and not _is_detail_label(l.text)]
    cand.sort(key=lambda l: l.cy)

    # 行锚点：真机每行都有一个「点击装备」标签，装备名就在它上方约 0.072H（≈104px@1440）处。
    # 必须先锚定再取行，否则 OCR 偶尔多读出的面板表头会被当成第 1 行、三行整体错位
    # （live_full_3 槽2 连刷 30 次一次没命中，就是错位后拿第 1 行的武器文本去比护甲池）。
    ANCHOR_OFFSET_FRAC = 0.072
    anchors = sorted(l.cy for l in lines if is_tap_equip_label(l.text) and l.x >= right_min * W)
    row_lines = []
    if len(anchors) == 3 and cand:
        off = ANCHOR_OFFSET_FRAC * H
        tol = 0.06 * H
        for a in anchors:
            want = a - off
            best = min(cand, key=lambda l: abs(l.cy - want))
            row_lines.append(best if abs(best.cy - want) <= tol else None)
    if len(row_lines) != 3 or any(l is None for l in row_lines):
        # 兜底：按几何聚类取前 3 行（面板表头已被 _is_detail_label 挡掉）
        clusters = []
        for l in cand:
            if clusters and l.cy - clusters[-1][-1].cy < 60:
                clusters[-1].append(l)
            else:
                clusters.append([l])
        row_lines = [max(cl, key=lambda l: l.w) for cl in clusters[:3]]

    for idx, best in enumerate(row_lines, start=1):
        matches = extract_items(best.text, vocab, min_score=min_score) if vocab else []
        name, score = (matches[0][0], matches[0][1]) if matches else (None, 0.0)
        scr.rows.append(EntryRow(
            index=idx, raw=best.text, matched=name, score=score, line=best,
            center=(best.cx, best.cy),
            refresh=(W * xfrac, best.cy),
        ))
    return scr


def parse_screen(rows, size=None, profile=None, vocab=(), min_score=0.62):
    """自动判页。顺序有讲究：**弹窗优先**——弹窗是模态的，背后那层点不动，
    而且真机实测弹窗会把背景压暗、OCR 读不出「点击装备/武装派遣」，
    不先判弹窗就会把弹窗误判成列表页（真机踩过：runner 里 AttributeError）。"""
    dlg = parse_dialog(rows, size, profile)
    if is_dialog(dlg):
        return "dialog", dlg
    texts = [t for t, *_ in rows]
    joined = " ".join(texts)
    if is_tap_equip_label(joined) or "武装派遣" in joined:
        return "detail", parse_detail_screen(rows, size, profile, vocab, min_score)
    return "list", parse_list_screen(rows, size, profile)
