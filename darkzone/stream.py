"""视频流取帧：把手机画面变成「本地随时可读的最新一帧」。

管道（两条子进程对接，实测可用）：

    adb -s <dev> exec-out "screenrecord --output-format=h264 --bit-rate 8M --time-limit 170 -"
        | ffmpeg -hide_banner -loglevel error -f h264 -i - -vf fps=4 -q:v 5 -f image2 -update 1 -y <frame>

真机实测（869a…4ca，3200x1440，2026-10-11，logs\\stream_probe_out.txt）：

    第一帧           2.5 s（含 screenrecord 启动）
    读最新一帧       31 ms 中位（对照：gzip 取屏 1.39 s）
    画面变化→新帧    0.35 s
    帧格式           3200x1440 JPEG ≈184 KB

两个坑（都踩过）：

1. **必须显式 `-f h264`**：管道不可 seek，ffmpeg 猜不出格式时一帧都不出，
   而且 loglevel=error 下 stderr 也是空的——看上去像「手机没画面」，其实是没指定格式。
2. screenrecord 有 `--time-limit` 上限（本例 170 s），到点自己退出，所以流水线要能
   自愈：`latest()` 发现帧陈旧（>stale_after）就返回 None，由调用方 `ensure()` 重启。

`ffmpeg` 不在 PATH 是常态：优先环境变量 `DARKZONE_FFMPEG`，其次 PATH，最后候选表。
找不到就等于「没有 stream 这条路」，调用方应回退到 screencap。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image

#: 本机已知可用的 ffmpeg（剪映自带；Folia 那个是纯音频构建，没有 mjpeg，别用）
FFMPEG_CANDIDATES = (
    r"D:\JianyingPro\11.5.0.14471\ffmpeg.exe",
    r"D:\JianyingPro\10.3.0.13901\ffmpeg.exe",
)


def find_ffmpeg() -> str | None:
    """环境变量 → PATH → 候选表。找不到返回 None（= 这条路不可用）。"""
    env = os.environ.get("DARKZONE_FFMPEG")
    if env and Path(env).is_file():
        return str(env)
    which = shutil.which("ffmpeg")
    if which:
        return which
    for cand in FFMPEG_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


class StreamUnavailable(RuntimeError):
    """这条路起不来（多半是没 ffmpeg）——调用方应当永久放弃 stream。"""


class FrameStream:
    """常驻取帧：一个 adb 进程 + 一个 ffmpeg 进程，帧文件被 ffmpeg 反复覆盖。"""

    def __init__(self, adb, frame_path, fps=4, bit_rate="8M", time_limit=170, quality=5,
                 ffmpeg=None, stale_after=1.0):
        self.adb = adb
        self.frame = Path(frame_path)
        self.fps = int(fps)
        self.bit_rate = str(bit_rate)
        self.time_limit = int(time_limit)
        self.quality = int(quality)
        self.ffmpeg = ffmpeg or find_ffmpeg()
        self.stale_after = float(stale_after)
        self._adb_proc = None
        self._ff_proc = None
        self.started_at = 0.0
        self.stats = {"starts": 0, "frames": 0, "misses": 0, "restarts": 0,
                      "stale": 0, "last_frame_age": None}

    # ---------------------------------------------------------------- 生命周期
    def available(self) -> bool:
        return bool(self.ffmpeg)

    def _cmd(self):
        adb_path = getattr(self.adb, "adb", "adb")
        serial = getattr(self.adb, "serial", None)
        adb_cmd = [adb_path] + (["-s", serial] if serial else []) + [
            "exec-out",
            f"screenrecord --output-format=h264 --bit-rate {self.bit_rate} "
            f"--time-limit {self.time_limit} -",
        ]
        ff_cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "h264", "-i", "-",                     # 管道必须显式给格式，见模块 docstring
            "-vf", f"fps={self.fps}", "-q:v", str(self.quality),
            "-f", "image2", "-update", "1", "-y", str(self.frame),
        ]
        return adb_cmd, ff_cmd

    def start(self, timeout=8.0, warmup=1.5) -> bool:
        """起流水线，等到第一帧出现（实测 2.5 s），并等过开头那 ~1.5 s「坏帧窗口」。返回是否拿到帧。

        为什么要等过窗口：screenrecord 刚起流那一下的关键帧量化特别狠。真机实测
        （`logs\\_stream_warm_sweep_out.txt`，5 条新流在第一帧出现后每 0.25 s 取一帧判三行）：
        第一帧就有一行被读坏的流 **5/5**（`KN复防弹衣`、`AIJG突击步枪`、`钢新星战木头盔`…），
        0.25 s 时 3/5、0.50 s 2/5、1.00 s 1/5、**1.50 s 0/5**。坏读法会让那一行 `matched=None`
        → 工具误以为「池外」→ 去刷**本来就对**的行（17:52 的只读预演就是这么把槽2/槽3 判成池外的，
        反方向则是 17:05 那种「旧帧判成池内」）。多花 ~1.3 s（只付一次，2.5 s 起流已付）换开局读准。
        """
        if not self.ffmpeg:
            raise StreamUnavailable("找不到 ffmpeg（设 DARKZONE_FFMPEG 或放进候选路径）")
        self.stop()
        self.frame.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.frame.unlink()
        except FileNotFoundError:
            pass
        adb_cmd, ff_cmd = self._cmd()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._adb_proc = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, creationflags=flags)
        self._ff_proc = subprocess.Popen(ff_cmd, stdin=self._adb_proc.stdout,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=flags)
        self._adb_proc.stdout.close()          # 这条管道归 ffmpeg，父进程别再攥着
        self.started_at = time.time()
        self.stats["starts"] += 1

        def _mtime():
            try:
                return self.frame.stat().st_mtime
            except FileNotFoundError:
                return None

        deadline = time.time() + timeout
        first = _mtime()
        while first is None and time.time() < deadline:
            if self._ff_proc.poll() is not None:        # ffmpeg 先死了：不用再等
                break
            time.sleep(0.05)
            first = _mtime()
        if first is None:
            return False
        # 等满坏帧窗口（warmup）且期间至少换过一帧；timeout 当绝对上限，别卡死。
        first_seen = time.time()
        warmup_deadline = first_seen + max(0.0, warmup)
        hard_deadline = first_seen + timeout
        newer = False
        while time.time() < hard_deadline:
            cur = _mtime()
            if cur is not None and cur > first:
                newer = True
            if newer and time.time() >= warmup_deadline:
                break
            if self._ff_proc.poll() is not None:        # ffmpeg 死了就别等了（回退别的取屏路）
                break
            time.sleep(0.03)
        return self.frame.exists() and self.frame.stat().st_size > 1000

    def alive(self) -> bool:
        return bool(self._adb_proc and self._adb_proc.poll() is None
                    and self._ff_proc and self._ff_proc.poll() is None)

    def ensure(self) -> bool:
        """活着就 True；死了（含 screenrecord 到点退出）就重启。"""
        if self.alive():
            return True
        self.stats["restarts"] += 1
        return self.start()

    def stop(self):
        for proc in (self._ff_proc, self._adb_proc):
            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except Exception:                                   # noqa: BLE001
                pass
        self._ff_proc = None
        self._adb_proc = None
        # 本地 adb 客户端断了不代表设备端 screenrecord 没了：显式收一下（尽力而为）
        try:
            self.adb.shell("pkill -f screenrecord; killall screenrecord")
        except Exception:                                       # noqa: BLE001
            pass

    # ------------------------------------------------------------------- 取帧
    def latest(self, copy_to=None, torn_retries=4, torn_delay=0.015):
        """返回最新一帧的路径；`copy_to` 给了就先复制过去（留证据，约 30 ms）。

        拿不到（流水线死了 / 帧陈旧 / 撕裂缝读不开）一律返回 None，让调用方
        这一轮退到 screencap——不要在这里等，等就等于把加速又还回去了。
        """
        if not self.alive():
            self.stats["misses"] += 1
            return None
        try:
            age = time.time() - self.frame.stat().st_mtime
        except FileNotFoundError:
            self.stats["misses"] += 1
            return None
        self.stats["last_frame_age"] = round(age, 3)
        if age > self.stale_after:
            self.stats["stale"] += 1
            self.stats["misses"] += 1
            return None
        dest = Path(copy_to) if copy_to else self.frame
        for _ in range(max(1, torn_retries)):
            try:
                if copy_to:
                    shutil.copyfile(self.frame, dest)
                with Image.open(dest) as im:     # 写了一半的 JPEG 会在这里露馅
                    im.verify()
                self.stats["frames"] += 1
                return dest
            except Exception:                                   # noqa: BLE001 撕裂缝：隔一会再看
                time.sleep(torn_delay)
        self.stats["misses"] += 1
        return None

    def summary(self) -> dict:
        return dict(self.stats, alive=self.alive(), ffmpeg=self.ffmpeg,
                    frame=str(self.frame), fps=self.fps)
