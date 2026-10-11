"""RapidOCR 后端（PP-OCR 模型，CPU 推理）：与 `win_ocr.py` 完全同接口，可直接互换。

为什么要换掉 Windows 自带 OCR：
Windows.Media.Ocr 的中文认得还行，但**字母数字经常认错、还会在汉字之间插空格**，
我们已经被它坑过三次假阳性（KN野战指挥官→KN复合、SEK复合→KN复合、Med野战背包→野战登山背包）。
RapidOCR 用的是 PaddleOCR 的 PP-OCR 检测+识别模型（v4 mobile 版模型直接打在 whl 里，约 14M，
不需要联网下载），对「字母数字 + 汉字混排」的准确率高一个档次。

安装（本机 github/pypi 直连不通，用国内镜像；装在项目 `.venv` 里，不污染 DSH 运行时）：
    py -m venv --system-site-packages .venv
    .venv\\Scripts\\python.exe -m pip install rapidocr-onnxruntime -i https://pypi.tuna.tsinghua.edu.cn/simple/

依赖：onnxruntime / opencv-python / pyclipper / shapely / pyyaml / six（都是纯 CPU，无需显卡）。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LANG = "zh"          # PP-OCR 的中英文混合模型只有一个，与 win_ocr 的 lang 参数保持兼容
_ENGINE = None
_ENGINE_KIND = None


def _build_engine():
    """优先用 v1.3.x 的 `rapidocr_onnxruntime`（模型内置），退而用 v3 的 `rapidocr`。"""
    global _ENGINE, _ENGINE_KIND
    if _ENGINE is not None:
        return _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR      # 模型内置在 whl 里，离线可用
        _ENGINE = RapidOCR()
        _ENGINE_KIND = "rapidocr_onnxruntime"
        return _ENGINE
    except Exception as e1:                            # noqa: BLE001
        try:
            from rapidocr import RapidOCR              # v3.x：模型首次使用时下载（ModelScope）
            _ENGINE = RapidOCR()
            _ENGINE_KIND = "rapidocr"
            return _ENGINE
        except Exception as e2:                        # noqa: BLE001
            raise RuntimeError(
                "没有可用的 RapidOCR 后端。请先安装：\n"
                "  .venv\\Scripts\\python.exe -m pip install rapidocr-onnxruntime "
                "-i https://pypi.tuna.tsinghua.edu.cn/simple/\n"
                f"rapidocr_onnxruntime 导入失败：{e1}\n rapidocr 导入失败：{e2}"
            ) from e2


def _poly_to_box(poly):
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def ocr_image(image_path, lang: str = DEFAULT_LANG, timeout: int = 90):
    """对图片做 OCR，返回 {"engine":..,"width":..,"height":..,"lines":[{text,x,y,w,h,words}]}。

    与 `win_ocr.ocr_image` 同结构，因此 `ui.py` / `reader.py` 不用改。
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    from PIL import Image
    with Image.open(image_path) as im:
        width, height = im.size

    engine = _build_engine()
    raw = engine(str(image_path))
    # v1.3.x: (result, elapse)，result = [[box, text, score], ...]
    # v3.x  : 结果对象带 .boxes/.txts/.scores
    if isinstance(raw, tuple):
        result = raw[0]
    elif hasattr(raw, "boxes"):
        result = [[b, t, s] for b, t, s in zip(raw.boxes, raw.txts, raw.scores)]
    else:
        result = raw

    lines = []
    for item in result or []:
        if len(item) < 2:
            continue
        box, text = item[0], item[1]
        score = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
        x, y, w, h = _poly_to_box(box)
        lines.append({"text": str(text), "x": x, "y": y, "w": w, "h": h,
                      "score": round(score, 4), "words": []})
    return {"engine": _ENGINE_KIND or "rapidocr", "width": width, "height": height, "lines": lines}


def line_text(line) -> str:
    """与 win_ocr 一致：去掉字间空格（PP-OCR 一般不加空格，但保险）。"""
    return "".join(line["text"].split())


def ocr_lines(image_path, lang: str = DEFAULT_LANG):
    """返回 [(text_no_space, x, y, w, h)]，按 y 再 x 排序（与 win_ocr.ocr_lines 同序）。"""
    res = ocr_image(image_path, lang)
    rows = [(line_text(l), l["x"], l["y"], l["w"], l["h"]) for l in res.get("lines", [])]
    rows.sort(key=lambda r: (round(r[2] / 40), r[1]))
    return rows, res


def available() -> bool:
    try:
        _build_engine()
        return True
    except Exception:                                  # noqa: BLE001
        return False


if __name__ == "__main__":
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "ocr_test.png")
    rows, res = ocr_lines(img)
    print(f"engine={res['engine']} {res['width']}x{res['height']} lines={len(rows)}")
    for t, x, y, w, h in rows:
        print(f"  ({x:7.1f},{y:7.1f}) {w:6.1f}x{h:5.1f}  {t}")
