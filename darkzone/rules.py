"""暗区突围・派遣刷新辅助 —— 规则表解析与校验（数据驱动，不写死具体物品）。

规则表结构约定（来自 工作簿1.xlsx / Sheet1）：
  - 表头行：A 列写类别（如「暗区首领」），B 列写时机（如「验证完暗区首领后」），
    其余列写装备池表头（如「第一行装备选择」「第二行装备选择」「第三行装备选择」）。
  - 数据行：A 列写该块的限定条件（如「除了阿贾克斯和雷诺伊尔」），池列写可选装备。
  - 一个池列 = 一档（slot）；同一表头行里连续的池列归为一个规则块；
    后面再出现表头行就是新规则块（块可以上下堆叠、也可以左右并排）。

校验语义（已与用户确认）：各档独立判定，装备只需落在该档自己的池内，行号之间无对应关系。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from itertools import product
from pathlib import Path

import openpyxl

POOL_HEADER_RE = re.compile(r"(装备选择|装备池|选项|装备)")
SLOT_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*[行档个]")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
SPLIT_RE = re.compile(r"[和及、,，/]|以及")


def slot_index(text: str):
    """从「第三行装备选择」里取出 3。"""
    if not text:
        return None
    m = SLOT_RE.search(text)
    if not m:
        return None
    t = m.group(1)
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if "十" in t:
        a, b = t.split("十", 1)
        return CN_NUM.get(a, 1) * 10 + (CN_NUM.get(b, 0) if b else 0)
    return CN_NUM.get(t)


def cell_text(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


@dataclass
class RuleBlock:
    sheet: str
    header_row: int          # 1-based
    first_row: int           # 数据起始行（1-based）
    last_row: int            # 数据结束行（1-based）
    title: str               # A 列表头 / 类别
    timing: str              # B 列：触发时机
    condition: str           # A 列数据行的限定条件（可能是多行，用「；」连接）
    pools: dict              # {slot:int -> [item, ...]}
    slot_labels: dict        # {slot:int -> 表头原文}
    columns: dict            # {slot:int -> 列号(1-based)}

    @property
    def excluded(self):
        """把「除了阿贾克斯和雷诺伊尔」解析成排除名单。"""
        c = self.condition
        if not c:
            return []
        c = re.sub(r"^除(?:了|开)?", "", c)
        c = re.sub(r"(以?外|之外).*$", "", c)
        return [x for x in (p.strip() for p in SPLIT_RE.split(c)) if x]

    def includes_boss(self, boss: str | None) -> bool:
        if boss is None:
            return True
        return boss not in self.excluded

    def combos(self):
        """该块允许的全部组合（笛卡尔积）。"""
        slots = sorted(self.pools)
        for combo in product(*[self.pools[s] for s in slots]):
            yield dict(zip(slots, combo))

    def to_dict(self):
        d = asdict(self)
        d["excluded"] = self.excluded
        return d


@dataclass
class RuleBook:
    source: str
    blocks: list = field(default_factory=list)

    @property
    def vocab(self):
        out = []
        for b in self.blocks:
            for items in b.pools.values():
                for it in items:
                    if it not in out:
                        out.append(it)
        return out

    @property
    def slot_labels(self):
        out = {}
        for b in self.blocks:
            out.update(b.slot_labels)
        return out

    def applicable(self, boss=None):
        return [b for b in self.blocks if b.includes_boss(boss)]

    def to_dict(self):
        return {"source": self.source, "blocks": [b.to_dict() for b in self.blocks],
                "vocab": self.vocab}


def _grid(ws):
    rows = []
    for r in ws.iter_rows(values_only=True):
        rows.append([cell_text(v) for v in r])
    return rows


def _is_pool_header(t: str) -> bool:
    return bool(t) and bool(POOL_HEADER_RE.search(t))


def parse_sheet(ws) -> list:
    rows = _grid(ws)
    ncol = max((len(r) for r in rows), default=0)
    for r in rows:
        r.extend([""] * (ncol - len(r)))

    header_rows = [i for i, r in enumerate(rows) if sum(1 for t in r if _is_pool_header(t)) >= 1]
    blocks = []
    for hi in header_rows:
        header = rows[hi]
        pool_cols = [c for c, t in enumerate(header) if _is_pool_header(t)]
        if not pool_cols:
            continue
        # 连续列归为一个规则块
        groups, cur = [], [pool_cols[0]]
        for c in pool_cols[1:]:
            if c == cur[-1] + 1:
                cur.append(c)
            else:
                groups.append(cur)
                cur = [c]
        groups.append(cur)

        # 数据区：表头下一行起，直到全空行或下一个表头行
        end = len(rows)
        for r in range(hi + 1, len(rows)):
            if any(_is_pool_header(t) for t in rows[r]):
                end = r
                break
            if not any(rows[r]):
                end = r
                break
        data_rows = list(range(hi + 1, end))

        title = next((t for t in header if t and not _is_pool_header(t)), "")
        # B 列（列号 2）在表头行或数据行中的第一个非空值 = 触发时机
        timing = ""
        if ncol >= 2:
            for r in range(hi, end):
                if rows[r][1]:
                    timing = rows[r][1]
                    break
        conds = []
        for r in data_rows:
            if rows[r][0]:
                conds.append(rows[r][0])

        for g in groups:
            pools, labels, columns = {}, {}, {}
            for pos, c in enumerate(g, start=1):
                label = header[c]
                s = slot_index(label) or pos
                items = []
                for r in data_rows:
                    v = rows[r][c]
                    if v and v not in items:
                        items.append(v)
                if not items:
                    continue
                pools[s] = items
                labels[s] = label
                columns[s] = c + 1
            if not pools:
                continue
            blocks.append(RuleBlock(
                sheet=ws.title, header_row=hi + 1,
                first_row=(data_rows[0] + 1) if data_rows else hi + 2,
                last_row=(data_rows[-1] + 1) if data_rows else hi + 1,
                title=title, timing=timing, condition="；".join(conds),
                pools=pools, slot_labels=labels, columns=columns,
            ))
    return blocks


def parse_workbook(path, sheet=None) -> RuleBook:
    wb = openpyxl.load_workbook(path, data_only=True)
    book = RuleBook(source=str(path))
    for ws in wb.worksheets:
        if sheet and ws.title != sheet:
            continue
        book.blocks.extend(parse_sheet(ws))
    if not book.blocks:
        raise ValueError(f"未在 {path} 中解析出任何规则块（找不到装备池表头）")
    return book


# ---------------------------------------------------------------- 校验

@dataclass
class Check:
    slot: int
    label: str
    item: str
    ok: bool
    reason: str


@dataclass
class Report:
    boss: str | None
    block: RuleBlock | None
    passed: bool | None          # None = 该块不适用
    checks: list
    notes: list

    def __bool__(self):
        return bool(self.passed)

    def summary(self):
        head = f"首领={self.boss or '(未指定)'} 规则块={'/'.join(filter(None, [self.block.title, self.block.condition])) if self.block else '-'}"
        if self.passed is None:
            return head + " → 不适用：" + "；".join(self.notes)
        body = "，".join(f"{c.label}:{c.item}{'[OK]' if c.ok else '[NG:' + c.reason + ']'}" for c in self.checks)
        return head + (" → 通过" if self.passed else " → 不通过") + "  " + body + \
               ("" if not self.notes else "  [注:" + "；".join(self.notes) + "]")


def validate(book: RuleBook, items: dict, boss: str | None = None, require_all_slots=True):
    """items: {slot:int|str -> 物品名}。返回 Report 列表（每个规则块一个）。"""
    norm = {}
    for k, v in items.items():
        s = slot_index(str(k))
        if s is None:
            try:
                s = int(k)
            except (TypeError, ValueError):
                s = str(k)
        norm[s] = v
    out = []
    for b in book.blocks:
        notes = []
        if boss is not None and not b.includes_boss(boss):
            out.append(Report(boss, b, None, [], [f"{boss} 在排除名单 {b.excluded} 内，本规则块不适用"]))
            continue
        checks = []
        for s in sorted(b.pools):
            label = b.slot_labels.get(s, f"第{s}行")
            it = norm.get(s)
            if it in (None, ""):
                if require_all_slots:
                    checks.append(Check(s, label, "", False, "缺该项"))
                else:
                    notes.append(f"{label} 未提供，跳过")
                continue
            ok = it in b.pools[s]
            checks.append(Check(s, label, it, ok,
                                "在池内" if ok else f"不在池内（池：{'/'.join(b.pools[s])}）"))
        for s, v in norm.items():
            if s not in b.pools and v:
                notes.append(f"多出的档位 {s}={v} 不在本规则块中")
        passed = all(c.ok for c in checks) if checks else False
        out.append(Report(boss, b, passed, checks, notes))
    return out


def default_rules_path() -> Path:
    """找规则表：项目根 `规则表.xlsx` 优先，其次桌面 `工作簿1.xlsx`。

    以前这里写死本机桌面绝对路径，换台机器/换个目录就找不到表；现在先把
    项目自带的 `规则表.xlsx`（`build_app.ps1` 也会把它打进包）用起来，
    只有在项目里没有时才退回家目录桌面那份。
    """
    here = Path(__file__).resolve().parent.parent
    candidates = [here / "规则表.xlsx"]
    desktop = Path.home() / "Desktop"
    candidates += [desktop / "工作簿1.xlsx", desktop / "规则表.xlsx"]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]      # 都没有：返回期望位置，让调用方报清楚的错


def main(argv=None):
    import sys
    argv = argv or sys.argv[1:]
    path = argv[0] if argv else str(default_rules_path())
    book = parse_workbook(path)
    print(json.dumps(book.to_dict(), ensure_ascii=False, indent=2))
    if len(argv) > 1:
        payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        for case in payload:
            for rep in validate(book, case.get("items", {}), case.get("boss")):
                print(rep.summary())


if __name__ == "__main__":
    main()
