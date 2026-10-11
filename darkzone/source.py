"""屏幕来源抽象：真机走 adb，离线测试走假手机。

把这些副作用收在一个接口后面，主循环就变成纯逻辑（可离线测试）：
    screenshot(tag) -> 图片路径
    tap(xy)         -> 点一下（真机坐标）
    back()          -> 返回键
    size()          -> 真机分辨率 (w,h)
"""

from __future__ import annotations

import time
from pathlib import Path

from .stream import FrameStream, StreamUnavailable


class LiveSource:
    """真机：adb 截图 + input tap。

    capture（取屏路径，实测见 logs\\cap_speed2_out.txt、logs\\stream_probe_out.txt）：
      `auto`（默认）= 先试 `gzip`（设备端压完再传，1.15 s），不行退 `raw`，再不行退 `png`；
      `stream`      = 视频流取帧（screenrecord | ffmpeg 常驻，读一帧 31 ms）—— **opt-in**，
                      因为要多一个 ffmpeg 依赖；起不来或被拉黑就自动退回 gzip；
      `gzip`/`gz`   = 设备端 `screencap | gzip -3` + 本地解压 → JPEG（1.15 s / 5.05 MB）；
      `fast`/`raw`  = 抓原始帧在本地转 JPEG（1.68 s / 18.43 MB）；
      `png`         = 旧的设备端 PNG（2.13 s / 3.41 MB）。
    某条快路一旦抛异常就被拉黑（不再重试），最终一定有 PNG 兜底，不让整轮挂掉。
    """

    # capture 模式 → 依次尝试的快路（都失败就走设备端 PNG）
    # auto 现在也把视频流放第一位（用户 2026-10-11 定：读一帧 0.01 s vs gzip 1.4 s），
    # 没有 ffmpeg / 起不来就自动退回 gzip，再退原始帧，最后设备端 PNG。
    CHAINS = {
        "auto": ("stream", "gzip", "raw"),
        "gzip": ("gzip",),
        "gz": ("gzip",),
        "fast": ("raw",),
        "raw": ("raw",),
        "stream": ("stream", "gzip", "raw"),
        "png": (),
    }

    def __init__(self, adb, work_dir="logs", device_size=None, capture="auto"):
        self.adb = adb
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._size = tuple(device_size) if device_size else None
        self.taps = []
        self.capture = capture
        self.fast_failed = None
        self.fail_reasons = {}     # 每条快路最后一次的失败原因（stream 挂了要能看见）
        self.dead = set()          # 已经失败过的快路
        self.stream = None         # FrameStream：常驻视频流（只有 capture=stream 才建）
        self.stream_dead = False   # 拉黑：没有 ffmpeg / 起不来
        self.stream_misses = 0     # 单次没帧（不拉黑，这一次退 gzip）
        self.last_chain = None     # 最近一次取屏实际走的是哪一档（stream/gzip/raw/png）
                                   # runner 用它决定要不要开「稳定性复读 / 花券前再确认」：
                                   # 流路读一帧 0.01 s，多读几次几乎免费；gzip 路一帧 1.4 s，就不多读。

    # --------------------------------------------------------------- 视频流
    def _stream_shot(self, dest: Path):
        """从常驻视频流拷一帧到 dest；这次拿不到就返回 None（不抛，交给链上的下一档）。"""
        if self.stream_dead:
            return None
        if self.stream is None:
            try:
                self.stream = FrameStream(self.adb, self.work_dir / "_stream_frame.jpg")
                if not self.stream.start():
                    raise StreamUnavailable("起流水线 8 s 内没出第一帧")
            except Exception as e:                       # noqa: BLE001 拉黑 stream，退回 gzip
                self.stream_dead = True
                self.fast_failed = self.fail_reasons["stream"] = f"stream: {type(e).__name__}: {e}"
                self.stream = None
                return None
        elif not self.stream.alive():
            if not self.stream.ensure():                 # screenrecord 到点退出 → 重启
                self.stream_misses += 1
                return None
        got = self.stream.latest(copy_to=dest)
        if got is None:
            self.stream_misses += 1
        return got

    def close(self):
        """收工：别把 ffmpeg / screenrecord 留在后台。"""
        if self.stream is not None:
            self.stream.stop()

    def stream_summary(self):
        if self.stream is None:
            return {"enabled": self.capture == "stream", "dead": self.stream_dead,
                    "misses": self.stream_misses}
        return dict(self.stream.summary(), dead=self.stream_dead, misses=self.stream_misses)

    # ---------------------------------------------------------------- 取屏
    def screenshot(self, tag="shot") -> Path:
        stem = f"{tag}_{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
        for step in self.CHAINS.get(self.capture, ("gzip", "raw")):
            if step in self.dead:
                continue
            p = self.work_dir / f"{stem}.jpg"      # 快路存的是 JPEG，就别骗人写成 .png
            try:
                if step == "stream":
                    if self._stream_shot(p) is None:
                        continue                        # 只让这一次读退下去，不拉黑
                elif step == "gzip":
                    self.adb.screencap_gz(p)
                else:
                    self.adb.screencap_fast(p)
                self.last_chain = step
                return p
            except Exception as e:                   # noqa: BLE001 拉黑这条路，继续往下退
                self.dead.add(step)
                self.fast_failed = self.fail_reasons[step] = f"{step}: {type(e).__name__}: {e}"
        p = self.work_dir / f"{stem}.png"
        self.adb.screencap(p)
        self.last_chain = "png"
        return p

    def tap(self, xy):
        x, y = int(round(xy[0])), int(round(xy[1]))
        self.taps.append((x, y))
        self.adb.tap(x, y)

    def back(self):
        self.adb.back()

    def size(self):
        if self._size is None:
            self._size = tuple(self.adb.screen_size())
        return self._size


class ImageSource:
    """离线：只喂一张现成截图，只读不点（用来在没有手机时预演坐标计划）。"""

    def __init__(self, path, device_size=None):
        self.path = Path(path)
        self._size = tuple(device_size) if device_size else None
        self.taps = []

    def screenshot(self, tag="shot") -> Path:
        return self.path

    def tap(self, xy):
        self.taps.append(xy)
        raise RuntimeError("ImageSource 是离线只读来源，不允许点击（请用 --live 接上真机）")

    def back(self):
        pass

    def size(self):
        return self._size or (0, 0)
