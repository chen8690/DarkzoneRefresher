"""Windows 自带 OCR（Windows.Media.Ocr）的 Python 封装：无需联网、无需安装。

理由：本机无外网（pip 超时）、无 tesseract，但 Windows 10/11 自带 zh-Hans-CN 识别引擎，
实测能读准中文（字母数字有噪声，交给 ocr_match 的闭集匹配消歧）。

2026-10-11 提速（用户要求「识别慢的根源是扫描了太多无关区域」）：
实测发现慢的根源其实**不是面积**，而是每次调用都要重新付「powershell 进程启动 + WinRT 引擎加载」
约 0.6 秒（160x72 的小图也要 0.54 s，全屏 0.87 s）。所以默认改用**常驻 worker**
（win_ocr_worker.ps1，引擎只加载一次，一行一个请求）：全屏 0.87 → 0.27 秒，
右面板 0.13 秒，右面板三行 0.07 秒。worker 起不来或中途死掉会自动回退到一次性进程，
可用环境变量 DARKZONE_OCR_WORKER=0 强制走老路。
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS1 = HERE / "win_ocr.ps1"
WORKER_PS1 = HERE / "win_ocr_worker.ps1"
DEFAULT_LANG = "zh-Hans-CN"


def _ps_cmd():
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass"]


class _Worker:
    """常驻 win_ocr_worker.ps1 进程：启动一次，之后每次 OCR 只发一行请求。"""

    def __init__(self, lang: str, timeout: float = 90.0):
        self.lang = lang
        self.timeout = timeout
        self.proc = None
        self.tmp = Path(tempfile.mkdtemp(prefix="win_ocr_worker_"))
        self.seq = 0
        self.start()

    def start(self):
        self.close()
        ps1 = WORKER_PS1 if WORKER_PS1.exists() else PS1
        args = _ps_cmd() + ["-File", str(ps1)]
        if ps1 == WORKER_PS1:
            args += ["-Lang", self.lang]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        first = self._readline()
        if not first.startswith("READY"):
            raise RuntimeError(f"win_ocr worker 没就绪: {first[:200]}")

    def _readline(self):
        """带超时的读一行：worker 卡住时不能让主程序陪着一起挂。"""
        box = {}

        def rd():
            try:
                box["line"] = self.proc.stdout.readline()
            except Exception as e:                      # pragma: no cover
                box["err"] = e

        th = threading.Thread(target=rd, daemon=True)
        th.start()
        th.join(self.timeout)
        if th.is_alive():
            raise TimeoutError(f"win_ocr worker {self.timeout:.0f}s 没回话")
        if "err" in box:
            raise RuntimeError(f"win_ocr worker 读失败: {box['err']}")
        return (box.get("line") or "").strip()

    def ocr(self, image_path, lang: str | None = None):
        if lang and lang != self.lang:
            self.lang = lang
            self.start()
        self.seq += 1
        out = self.tmp / f"out_{self.seq}.json"
        self.proc.stdin.write(f"{Path(image_path).resolve()}\t{out.resolve()}\n")
        self.proc.stdin.flush()
        ans = self._readline()
        if not ans.startswith("OK"):
            raise RuntimeError(f"win_ocr worker 报错: {ans[:300]}")
        return json.loads(out.read_text(encoding="utf-8"))

    def close(self):
        proc, self.proc = self.proc, None
        if not proc:
            return
        for step in (lambda: proc.stdin.close(), proc.terminate, proc.wait):
            try:
                step()
            except Exception:
                pass


_WORKER: _Worker | None = None
_WORKER_DEAD = False
_WORKER_LOCK = threading.Lock()


def worker_enabled() -> bool:
    return os.environ.get("DARKZONE_OCR_WORKER", "1") != "0"


def worker_status():
    """给日志/自检用：现在走的是常驻 worker 还是一次性进程。"""
    if not worker_enabled():
        return "off(环境变量)"
    return "ok" if _WORKER and _WORKER.proc else ("dead(已回退)" if _WORKER_DEAD else "未启动")


def shutdown():
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER:
            _WORKER.close()
            _WORKER = None


atexit.register(shutdown)


def _ocr_once(image_path, lang: str, timeout: int):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out = Path(tf.name)
    try:
        proc = subprocess.run(
            _ps_cmd() + ["-File", str(PS1),
                        "-Image", str(image_path), "-Out", str(out), "-Lang", lang],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"win_ocr failed rc={proc.returncode} stderr={proc.stderr[-800:]}")
        return json.loads(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)


def ocr_image(image_path, lang: str = DEFAULT_LANG, timeout: int = 90):
    """对图片做 OCR，返回 {"engine":..,"width":..,"height":..,"lines":[{text,x,y,w,h,words:[..]}]}

    默认走常驻 worker；worker 不可用（或中途出错）就自动回退一次性进程，不影响结果。
    """
    global _WORKER, _WORKER_DEAD
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    if not worker_enabled() or WORKER_PS1.exists() is False:
        return _ocr_once(image_path, lang, timeout)
    with _WORKER_LOCK:
        try:
            if _WORKER is None:
                _WORKER = _Worker(lang, timeout=timeout)
            return _WORKER.ocr(image_path, lang)
        except Exception as e:                 # worker 坏了：关掉并标记，回退一次性进程
            if _WORKER:
                _WORKER.close()
                _WORKER = None
            if not _WORKER_DEAD:
                _WORKER_DEAD = True
                print(f"[warn] 常驻 OCR worker 不可用（{type(e).__name__}: {e}），"
                      f"本次起回退一次性 powershell 进程")
    return _ocr_once(image_path, lang, timeout)


def line_text(line) -> str:
    """把 OCR 行文本去掉字间空格（中文逐字识别会插空格）。"""
    return "".join(line["text"].split())


def ocr_lines(image_path, lang: str = DEFAULT_LANG):
    """返回 [(text_no_space, x, y, w, h)]，按 y 再 x 排序。"""
    res = ocr_image(image_path, lang)
    rows = [(line_text(l), l["x"], l["y"], l["w"], l["h"]) for l in res.get("lines", [])]
    rows.sort(key=lambda r: (round(r[2] / 40), r[1]))
    return rows, res


if __name__ == "__main__":
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent / "ocr_test.png")
    rows, res = ocr_lines(img)
    print(f"engine={res['engine']} {res['width']}x{res['height']} lines={len(rows)}")
    for t, x, y, w, h in rows:
        print(f"  ({x:7.1f},{y:7.1f}) {w:6.1f}x{h:5.1f}  {t}")
