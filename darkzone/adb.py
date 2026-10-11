"""adb 控制层：不经过 Escrcpy 界面，直接用 adb 驱动手机。

Escrcpy 已记录设备 869a…4ca（小米 14 Pro，1440x3200）。本模块只做四件事：
取屏、点击、滑动、按键，全部走 adb，方便后台无人值守跑。
"""

from __future__ import annotations

import struct
import os
import shutil
import subprocess
import time
import zlib
from pathlib import Path

_HOME = Path(os.path.expanduser("~"))

# 常见安装位置（按当前用户家目录拼，不写死某台机器的用户名）；
# 都没命中时再退到 PATH，最后可以在 GUI 里手工指定 adb 路径。
ADB_CANDIDATES = [
    str(_HOME / "Apps" / "scrcpy" / "adb.exe"),
    str(_HOME / "AppData" / "Local" / "Programs" / "Escrcpy"
        / "resources" / "extra" / "win" / "scrcpy" / "adb.exe"),
]


def find_adb():
    for p in ADB_CANDIDATES:
        if Path(p).exists():
            return p
    which = shutil.which("adb")
    if which:
        return which
    raise FileNotFoundError(
        "找不到 adb.exe（找过: " + ", ".join(ADB_CANDIDATES)
        + "，PATH 里也没有）；请在 GUI 里填 adb 路径，或把 adb 加进 PATH")


class Adb:
    def __init__(self, serial: str | None = None, adb_path: str | None = None, timeout: int = 30):
        self.adb = adb_path or find_adb()
        self.serial = serial
        self.timeout = timeout

    # ------------------------------------------------------------ 基础
    def run(self, *args, timeout=None, binary_out: Path | None = None):
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        if binary_out is not None:
            with open(binary_out, "wb") as f:
                proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=timeout or self.timeout)
            return proc
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
        return proc

    def shell(self, *args, timeout=None) -> str:
        proc = self.run("shell", *args, timeout=timeout)
        return proc.stdout.decode("utf-8", "replace").strip()

    # ------------------------------------------------------------ 设备
    @staticmethod
    def devices(adb_path: str | None = None):
        adb = adb_path or find_adb()
        proc = subprocess.run([adb, "devices", "-l"], capture_output=True, timeout=30)
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()[1:]
        res = []
        for line in out:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            res.append({"serial": parts[0], "state": parts[1] if len(parts) > 1 else "?",
                        "info": " ".join(parts[2:])})
        return res

    def wait_for_device(self, timeout: float = 30.0, interval: float = 1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for d in self.devices(self.adb):
                if d["state"] == "device" and (not self.serial or d["serial"] == self.serial):
                    self.serial = self.serial or d["serial"]
                    return d
            time.sleep(interval)
        return None

    def require(self):
        d = self.wait_for_device(timeout=5)
        if not d:
            raise RuntimeError("没有可用的 adb 设备；请连接手机并开启 USB 调试（或无线调试配对）")
        return d

    # ------------------------------------------------------------ 屏幕
    def screen_size(self):
        out = self.shell("wm", "size")
        for token in out.replace("\r", " ").replace("\n", " ").split():
            if "x" in token and token.replace("x", "").isdigit():
                w, h = token.split("x")
                return int(w), int(h)
        return None

    def screencap(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run("exec-out", "screencap", "-p", binary_out=path)
        if not path.exists() or path.stat().st_size < 1000:
            raise RuntimeError(f"screencap 失败或文件过小: {path}")
        return path

    def screencap_fast(self, path, quality: int = 90) -> Path:
        """取屏快路：抓「原始 RGBA 帧」在本地转 JPEG，比设备端 PNG 快约 0.5 秒。

        实测（3200x1440、USB，2026-10-11）：
          `exec-out screencap -p`（设备端 PNG 编码）2.14 s
          `exec-out screencap`（原始帧 18.4 MB，37 MB/s）1.60 s + 本地转 JPEG 0.06 s
        两种取法的 OCR 结果一致（同一行文本字面相同，只有状态栏标点这类噪声差异）。

        原始帧头部是 uint32 `w,h,format`（format=1 表示 RGBA_8888），Android 10+ 还会多
        一个 colorspace，所以头部是 12 或 16 字节——这里用「文件大小 - w*h*4」反推，
        对不上就抛异常，交给调用方回退到 PNG 路。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = path.with_suffix(".raw")
        try:
            self.run("exec-out", "screencap", binary_out=raw)
            size = raw.stat().st_size
            with open(raw, "rb") as f:
                blob = f.read()
            w, h, fmt = struct.unpack("<III", blob[:12])
            hdr = size - w * h * 4
            if fmt != 1 or hdr not in (12, 16) or len(blob) != size:
                raise ValueError(f"原始帧格式不认识: size={size} w={w} h={h} fmt={fmt}")
            from PIL import Image                    # 只有快路才需要 PIL
            Image.frombytes("RGBA", (w, h), blob[hdr:]).convert("RGB").save(path, quality=quality)
        finally:
            raw.unlink(missing_ok=True)
        if not path.exists() or path.stat().st_size < 1000:
            raise RuntimeError(f"screencap_fast 失败或文件过小: {path}")
        return path

    def screencap_gz(self, path, quality: int = 90, level: int = 3) -> Path:
        """取屏最快路：设备端 `gzip` 压完再传（传输才是瓶颈），本地解压后转 JPEG。

        实测（3200x1440、USB，2026-10-11，各 3 次取中位；见 logs\\cap_speed2_out.txt）：
          原始帧直传   1.68 s / 18.43 MB
          gzip -1      1.16 s /  5.50 MB
          gzip -3      1.15 s /  5.05 MB   ← 默认（本地解压 0.08 s）
          gzip -6      1.48 s /  4.32 MB
          设备端 PNG   2.13 s /  3.41 MB
        说明：设备端 `screencap > /dev/null` 只要 0.30 s，直传 18.43 MB 的 1.38 s 全花在
        ≈11 MB/s 的 adb 传输上；deflate 大约只花 0.3 s，所以压一下净赚约 0.5 s/屏。
        像素与 `screencap_fast` 完全同源（同一个原始帧），不改变 OCR 结果。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        gz = path.with_suffix(".raw.gz")
        try:
            self.run("exec-out", f"screencap | gzip -{int(level)}", binary_out=gz)
            blob = gz.read_bytes()
            if not blob:
                raise RuntimeError("screencap | gzip 没有任何输出")
            raw = zlib.decompress(blob, 16 + zlib.MAX_WBITS)      # 16 = 允许 gzip 头
            w, h, fmt = struct.unpack("<III", raw[:12])
            hdr = len(raw) - w * h * 4
            if fmt != 1 or hdr not in (12, 16):
                raise ValueError(f"原始帧格式不认识: size={len(raw)} w={w} h={h} fmt={fmt}")
            from PIL import Image                    # 只有快路才需要 PIL
            Image.frombytes("RGBA", (w, h), raw[hdr:]).convert("RGB").save(path, quality=quality)
        finally:
            gz.unlink(missing_ok=True)
        if not path.exists() or path.stat().st_size < 1000:
            raise RuntimeError(f"screencap_gz 失败或文件过小: {path}")
        return path

    # ------------------------------------------------------------ 操作
    def tap(self, x, y, delay=0.0):
        if delay:
            time.sleep(delay)
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, ms=300, delay=0.0):
        if delay:
            time.sleep(delay)
        self.shell("input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms)))

    def keyevent(self, code, delay=0.0):
        if delay:
            time.sleep(delay)
        self.shell("input", "keyevent", str(code))

    def back(self):
        self.keyevent(4)

    def home(self):
        self.keyevent(3)

    def current_app(self):
        out = self.shell("dumpsys", "window", "windows")
        for line in out.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                return line.strip()
        return out[:400]


if __name__ == "__main__":
    a = Adb()
    print("adb:", a.adb)
    print("devices:", Adb.devices(a.adb))
    print("size:", a.screen_size() if Adb.devices(a.adb) else "no device")
