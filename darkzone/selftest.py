"""规则表解析 + 校验自测：不依赖手机，验证第 1 步（规则引擎）正确。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkzone.rules import default_rules_path, parse_workbook, validate  # noqa: E402
from darkzone.ocr_match import extract_items  # noqa: E402

XLSX = default_rules_path()


def main():
    import io
    from pathlib import Path
    buf = io.StringIO()

    def out(s=""):
        print(s)
        buf.write(s + "\n")

    book = parse_workbook(XLSX)
    out("== 解析出的规则块 ==")
    out(json.dumps(book.to_dict(), ensure_ascii=False, indent=2))
    vocab = book.vocab
    out(f"\n== 词表 {len(vocab)} 项 ==")
    out(str(vocab))

    out("\n== 校验用例 ==")
    cases = [
        ("全对", dict(boss="多斯", items={1: "AUG突击步枪", 2: "BT101战术防弹衣", 3: "钢铁新星战术头盔"})),
        ("一档错", dict(boss="多斯", items={1: "M4A1突击步枪", 2: "BT101战术防弹衣", 3: "钢铁新星战术头盔"})),
        ("被排除的首领", dict(boss="阿贾克斯", items={1: "AUG突击步枪", 2: "BT101战术防弹衣", 3: "钢铁新星战术头盔"})),
        ("缺一档", dict(boss="多斯", items={1: "MK14射手步枪", 2: "AL指挥官弹挂甲"})),
    ]
    for name, c in cases:
        reps = validate(book, c["items"], c["boss"])
        out(f"[{name}]")
        for r in reps:
            out("   " + r.summary())

    out("\n== OCR 归一化用例（Windows OCR 真实输出） ==")
    ocr_lines = [
        "AU G 突 击 步 枪 S 」 1 6 栓 动 步 枪",
        "BTI 01 战 术 防 弹 衣 治 安 官 重 型 防 弹 衣",
        "钢 铁 新 星 战 术 头 盔 野 战 登 山 背 包",
        "A R 3 0 栓 动 步 枪 K N 复 合 防 弹 衣",
    ]
    for line in ocr_lines:
        got = extract_items(line, vocab)
        out(f"   {line}\n      -> {[(n, round(s, 3)) for n, s, _ in got]}")

    Path(__file__).resolve().parent.parent.joinpath("selftest_out.txt").write_text(
        buf.getvalue(), encoding="utf-8")
    print("written: selftest_out.txt")


if __name__ == "__main__":
    main()
