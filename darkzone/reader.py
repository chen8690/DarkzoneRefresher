"""屏幕读取层：截图 → OCR（带坐标）→ 归一化成池中物品名 → 分配到「第N档」。

档位归属有两种方式：
  1) 校准档（推荐）：profile 里给每一档一个区域框，物品中心落在框内即归该档；
  2) 兜底：不给区域时按 slot_order（horizontal / vertical）排序后顺序编号。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .ocr_match import canon, extract_items
from .win_ocr import ocr_lines


@dataclass
class FoundItem:
    name: str
    score: float
    cx: float
    cy: float
    raw: str
    slot: int | None = None


@dataclass
class ReadResult:
    image: str
    items: list = field(default_factory=list)
    text_lines: list = field(default_factory=list)

    def by_slot(self):
        return {it.slot: it.name for it in self.items if it.slot is not None}

    def describe(self):
        return " | ".join(f"{it.slot or '?'}:{it.name}({it.score:.2f})" for it in self.items)


def _crop(image_path, box, out_path):
    img = Image.open(image_path)
    img.crop((int(box[0]), int(box[1]), int(box[0] + box[2]), int(box[1] + box[3]))).save(out_path)
    return out_path, (int(box[0]), int(box[1]))


def read_items(image_path, vocab, region=None, slot_boxes=None, slot_order="horizontal",
               min_score=0.62, work_dir=None, lang="zh-Hans-CN"):
    """region: [x,y,w,h] 只读该区域；slot_boxes: {slot: [x,y,w,h]} 用于判档（与 region 同为原图坐标）。"""
    src = Path(image_path)
    offset = (0, 0)
    ocr_target = src
    if region:
        tmp = Path(work_dir or src.parent) / (src.stem + "_crop.png")
        ocr_target, offset = _crop(src, region, tmp)

    rows, res = ocr_lines(ocr_target, lang=lang)
    out = ReadResult(image=str(image_path), text_lines=[r[0] for r in rows])

    for text, x, y, w, h in rows:
        matches = extract_items(text, vocab, min_score=min_score)
        if not matches:
            continue
        ctext = canon(text)
        total = max(1, len(ctext))
        for name, score, pos in matches:
            cname = canon(name)
            cx = offset[0] + x + (pos + len(cname) / 2) / total * w
            cy = offset[1] + y + h / 2
            out.items.append(FoundItem(name=name, score=score, cx=cx, cy=cy, raw=text))

    # 判档
    if slot_boxes:
        for it in out.items:
            for slot, box in slot_boxes.items():
                if box[0] <= it.cx <= box[0] + box[2] and box[1] <= it.cy <= box[1] + box[3]:
                    it.slot = int(slot)
                    break
    else:
        items = sorted(out.items, key=lambda i: (i.cy, i.cx) if slot_order == "horizontal" else (i.cx, i.cy))
        for idx, it in enumerate(items, start=1):
            it.slot = idx
    return out


if __name__ == "__main__":
    import sys
    from .rules import default_rules_path, parse_workbook
    book = parse_workbook(default_rules_path())
    img = sys.argv[1]
    print(json.dumps(
        {"items": [it.__dict__ for it in read_items(img, book.vocab).items]},
        ensure_ascii=False, indent=2))
