"""把屏幕 OCR 文本归一化成规则表里的标准物品名。

背景：本机无法安装 OCR 引擎（无外网），改用 Windows 自带 Windows.Media.Ocr（zh-Hans-CN）。
它的中文识别很准，但字母数字容易出错、且会在字间插空格，例如：
    「AUG突击步枪 SJ16栓动步枪」 → 「AU G 突 击 步 枪 S 」 1 6 栓 动 步 枪」
    「BT101战术防弹衣」          → 「BTI 01 战 术 防 弹 衣」
    「派遣任务」                 → 「派遣亻壬务」

因为物品名是一个**已知的闭集**（规则表里的池），所以不做通用 OCR 纠错，而是
「归一化 + 相似度打分 + 滑窗对齐」把文本映射回池中的标准名。
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# OCR 常见连写/拆分噪声
SPLIT_FIX = {
    "亻壬": "任",
    "氵去": "法",
    "扌安": "按",
}
# 字母/数字形近归并（两侧同时归并，因此不改变可区分性）
CONFUSE = str.maketrans({
    "A": "4", "a": "4", "B": "8", "b": "8", "G": "6", "g": "6",
    "I": "1", "i": "1", "l": "1", "|": "1", "!": "1",
    "O": "0", "o": "0", "Q": "0", "q": "0", "D": "0",
    "S": "5", "s": "5", "Z": "2", "z": "2", "T": "7", "t": "7",
    "」": "J", "』": "J", "【": "[", "】": "]",
})
KEEP_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\[\]]")
# 这些符号在 OCR 里会跟字母混淆，先归成字母再走 KEEP_RE，否则会被当标点删掉
PRE_MAP = {"」": "J", "』": "J", "【": "[", "】": "]"}

# 类别词（物品名的公共尾巴）。同一类别里的东西只有前缀不同，而 OCR 对前缀的
# 字母数字最容易认错，所以判定时必须先剥掉尾巴、只比前缀，否则「T03突击步枪」
# 会被判成「AUG突击步枪」（尾巴全同、滑窗放宽后能凑到 0.67）。
#
# 纪律：这里只能放**真正的类别词**，不能放带修饰语的名字片段。
# 真机踩过的坑：把「复合防弹衣」当尾巴后，「KN复合防弹衣」的核心只剩 2 个字
# 母「KN」，于是真机上的「KN野战指挥官防弹衣」（不在池内）被 core=KN 精确命中、
# 尾巴「复合」→「挥官」只丢 3 个字也过线，score 0.86 → 误判成池内物品而**提前停刷**。
# 所以「战术/重型/复合」这类修饰语一律留在核心前缀里，只留「防弹衣、头盔、背包…」。
CATEGORY_TAILS = sorted([
    "精确射手步枪", "突击步枪", "栓动步枪", "射手步枪", "微型冲锋枪", "冲锋枪",
    "卡宾枪", "霰弹枪", "轻机枪", "重机枪", "手枪", "步枪",
    "防弹衣", "弹挂甲", "胸挂甲", "胸挂",
    "军用背包", "登山背包", "背包", "头盔", "防毒面罩", "防毒面具", "面罩",
], key=len, reverse=True)


def strip_noise(s: str) -> str:
    """去掉空格与标点，修正拆字噪声。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for k, v in SPLIT_FIX.items():
        s = s.replace(k, v)
    for k, v in PRE_MAP.items():
        s = s.replace(k, v)
    return KEEP_RE.sub("", s)


def split_core(name: str):
    """把物品名切成 (区分性前缀, 类别尾巴)，例如 BT101战术防弹衣 -> ('BT101','战术防弹衣')。"""
    n = strip_noise(name)
    for t in CATEGORY_TAILS:
        ct = strip_noise(t)
        if len(n) > len(ct) and n.endswith(ct):
            return n[: len(n) - len(ct)], n[len(n) - len(ct):]
    return n, ""


def canon(s: str) -> str:
    """形态归一：用于比较，不用于展示。"""
    return strip_noise(s).upper().translate(CONFUSE)


def similarity(a: str, b: str) -> float:
    ca, cb = canon(a), canon(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, ca, cb).ratio()
    if ca in cb or cb in ca:
        ratio = max(ratio, 0.80 + 0.20 * min(len(ca), len(cb)) / max(len(ca), len(cb)))
    return ratio


def _best_window(needle: str, hay: str, offset: int = 0):
    """在 hay 里找最像 needle 的窗口，返回 (ratio, start, 尝试的窗口长度集合)。"""
    best = (0.0, -1)
    ln = len(needle)
    if ln == 0:
        return best
    lens = [ln] if ln <= 3 else [ln - 1, ln, ln + 1]
    for w in lens:
        if w <= 0 or w > len(hay):
            continue
        for start in range(0, len(hay) - w + 1):
            r = difflib.SequenceMatcher(None, needle, hay[start:start + w]).ratio()
            if r > best[0]:
                best = (r, start + offset)
    return best


def core_threshold(ln: int) -> float:
    """核心前缀允许的**加权编辑代价**上限（越短越严）。

    为什么不用 difflib 相似度（真机踩过两次，都是**提前停刷**）：
    ① 相似度会被「缩窗」钻空子：池里「KN复合防弹衣」核心是「KN复合」，屏上是池外的
       「SEK复合防弹衣」，缩成 3 字窗口「K复合」后拿到 0.857，刚好越过 0.85 → 误判命中。
    ② 汉字与字母数字的认错代价不同：真机上「钢帙新星战术头盔」是「钢铁新星…」少认一笔，
       这种必须容；而「FAMAS突击步枪」的窗口「4M45」和池里 A545 的「4545」只差一个字母，
       这种**绝不能容**（canon 表已经把形近的字母数字归并过了，剩下的就是真差异）。
    所以改成带字类权重的编辑距离：字母数字互错 1.0，汉字互错 0.5，增删 1.0。
    """
    if ln <= 3:
        return 0.0            # 必须精确，否则 M4A1/MK14 这种会互相撞
    if ln == 4:
        return 0.5            # 只容「一个汉字认错」，不容字母数字错、不容少字
    return max(1.0, 0.20 * ln)


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if _is_ascii_alnum(a) and _is_ascii_alnum(b):
        return 1.0            # 字母数字之间认错：canon 已归并形近的，剩下的算真差异
    if (not _is_ascii_alnum(a)) and (not _is_ascii_alnum(b)):
        return 0.5            # 汉字认错：容一个字
    return 1.0


def _weighted_lev(a: str, b: str) -> float:
    """带字类权重的 Levenshtein（串都很短，直接 DP）。"""
    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + _sub_cost(ca, cb)))
        prev = cur
    return prev[-1]


def _best_core_window(needle: str, hay: str):
    """在 hay 里找与 needle 代价最小的窗口，返回 (代价, 起点, 窗口长度)。"""
    ln = len(needle)
    if not ln or not hay:
        return 9.9, -1, 0
    best = (9.9, -1, 0)
    for w in (ln - 1, ln, ln + 1):
        if w <= 0 or w > len(hay):
            continue
        for start in range(0, len(hay) - w + 1):
            c = _weighted_lev(needle, hay[start:start + w])
            if c < best[0]:
                best = (c, start, w)
    return best


def score_item(text_can: str, name: str):
    """返回 (score, pos, core_ratio, tail_ratio)；不通过核心门控时 score 为 0。"""
    core_raw, tail_raw = split_core(name)
    core_c, tail_c = canon(core_raw), canon(tail_raw)
    if not core_c:
        return 0.0, -1, 0.0, 0.0
    core_cost, core_pos, core_w = _best_core_window(core_c, text_can)
    if core_cost > core_threshold(len(core_c)):
        return 0.0, core_pos, 0.0, 0.0
    core_r = max(0.0, 1.0 - core_cost / max(1, len(core_c)))
    tail_r = 1.0
    if tail_c:
        # 尾巴必须**紧跟核心窗口之后**，不许往前滑进核心区。
        # 真机踩过的第三个误判（提前停刷）：池里「野战登山背包」的核心是「野战」、尾巴是
        # 「登山背包」；屏上池外的「Med野战背包」让核心「野战」精确命中后，尾巴窗口若从核心
        # 起点开始搜，就能和窗口「战背包」凑出 0.571 ≥ 0.45 → score 0.85 → 误判命中。
        # 从 core_pos + core_w 之后开始搜，hay 只剩「背包」，窗口长度 3/4/5 都放不下 → 0.0。
        tail_start = core_pos + core_w if core_pos >= 0 else 0
        tail_r, _ = _best_window(tail_c, text_can[tail_start:], tail_start)
        if tail_r < 0.45:
            return 0.0, core_pos, core_r, tail_r
    score = 0.65 * core_r + 0.35 * tail_r if tail_c else core_r
    return score, core_pos, core_r, tail_r


def extract_items(text: str, vocab, min_score: float = 0.62):
    """从一行（或多行）文本里按出现顺序抽出所有池中物品名，返回 [(name, score, pos)]。"""
    ct = canon(text)
    cands = []
    for name in vocab:
        score, pos, core_r, tail_r = score_item(ct, name)
        if score >= min_score and pos >= 0:
            cands.append((name, score, pos, len(canon(split_core(name)[0]))))

    # 按分数优先去掉重叠命中（同一段文字只认领先者）
    cands.sort(key=lambda x: -x[1])
    taken = []
    used = []
    for name, score, pos, ln in cands:
        span = (pos, pos + ln)
        if any(not (span[1] <= a or span[0] >= b) for a, b in used):
            continue
        used.append(span)
        taken.append((name, score, pos))
    taken.sort(key=lambda x: x[2])
    return taken


if __name__ == "__main__":
    vocab = ["AUG突击步枪", "SJ16栓动步枪", "AR30栓动步枪", "A545突击步枪", "MK14射手步枪",
             "BT101战术防弹衣", "治安官重型防弹衣", "KN复合防弹衣", "AL指挥官弹挂甲",
             "钢铁新星战术头盔", "野战登山背包", "RAL重型军用背包"]
    for line in ["AU G 突 击 步 枪 S 」 1 6 栓 动 步 枪",
                 "BTI 01 战 术 防 弹 衣 治 安 官 重 型 防 弹 衣",
                 "钢 铁 新 星 战 术 头 盔 野 战 登 山 背 包"]:
        print(line)
        print("  ->", extract_items(line, vocab))
