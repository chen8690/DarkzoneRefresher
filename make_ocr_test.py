from PIL import Image, ImageDraw, ImageFont
import os
from pathlib import Path

out = str(Path(__file__).resolve().parent / "ocr_test.png")
img = Image.new("RGB", (900, 260), (18, 18, 18))
d = ImageDraw.Draw(img)

font = None
for p in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\Deng.ttf"]:
    if os.path.exists(p):
        try:
            font = ImageFont.truetype(p, 44)
            print("font:", p)
            break
        except Exception as e:
            print("font fail", p, e)
if font is None:
    font = ImageFont.load_default()
    print("using default font")

lines = [
    "AUG突击步枪  SJ16栓动步枪",
    "BT101战术防弹衣 治安官重型防弹衣",
    "钢铁新星战术头盔 野战登山背包",
    "刷新 派遣任务 暗区首领",
]
d.text((20, 20), lines[0], font=font, fill=(235, 235, 235))
d.text((20, 80), lines[1], font=font, fill=(235, 235, 235))
d.text((20, 140), lines[2], font=font, fill=(235, 235, 235))
d.text((20, 200), lines[3], font=font, fill=(235, 235, 235))
img.save(out)
print("saved", out, img.size)
