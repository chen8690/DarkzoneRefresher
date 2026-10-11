"""把标定图里的关键小区域放大裁出来，用来肉眼定位那个 OCR 认不出的 ↻ 图标。"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "crops"
OUT.mkdir(exist_ok=True)

JOBS = [
    ("screen2_refresh_entries.jpg", "s2_rows_right", (2400, 200, 2720, 720), 3),
    ("screen2_refresh_entries.jpg", "s2_row1_right", (2450, 220, 2700, 300), 6),
    ("screen1_refresh_boss.jpg", "s1_boss_header", (2360, 110, 2720, 220), 4),
    ("screen1_refresh_boss.jpg", "s1_free_header", (820, 110, 1050, 220), 5),
]

for src, tag, box, zoom in JOBS:
    im = Image.open(SAMPLES / src).convert("RGB").crop(box)
    im = im.resize((im.width * zoom, im.height * zoom), Image.LANCZOS)
    p = OUT / f"{tag}.png"
    im.save(p)
    print(tag, box, "->", p, im.size)
