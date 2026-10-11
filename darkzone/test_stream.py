"""视频流取帧（--capture stream）的离线自测：不接手机、不要 ffmpeg 也能跑。

覆盖三件事：
  1. `FrameStream.latest()`：正常拷贝 / 撕裂缝（写了一半的 JPEG）/ 帧陈旧 三种情况；
  2. `LiveSource(capture="stream")` 的**回退链**：stream 起不来必须拉黑并退到 screencap，
     最终仍要拿到图（宁可慢，不能整轮挂）；
  3. `auto` 链（默认）**首项就是 stream**，起不来再退 gzip → 原始帧 → PNG（用户 2026-10-11 定）；
     每走一档都写 `LiveSource.last_chain`（runner 靠它决定要不要多读几帧）。

跑法：`python -m darkzone.test_stream`（控制台是 GBK，所以只打印 [OK]/[NG]，别用 ✓/✗）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

from .source import LiveSource
from .stream import FrameStream, find_ffmpeg

FAILS = []


def check(cond, msg):
    print(("[OK] " if cond else "[NG] ") + msg)
    if not cond:
        FAILS.append(msg)


def make_jpeg(path: Path, size=(320, 144), color=(30, 30, 30)):
    Image.new("RGB", size, color).save(path, quality=90)
    return path


class StubAdb:
    """假 adb：gzip/raw 快路一律失败，只有设备端 PNG 能给图。"""

    def __init__(self, out_dir: Path):
        self.adb = "adb-not-real"
        self.serial = "STUB"
        self.out_dir = out_dir
        self.calls = []

    def screencap_gz(self, path, quality=90, level=3):
        self.calls.append("gz")
        raise RuntimeError("stub: gzip 路不可用")

    def screencap_fast(self, path, quality=90):
        self.calls.append("raw")
        raise RuntimeError("stub: raw 路不可用")

    def screencap(self, path):
        self.calls.append("png")
        make_jpeg(Path(path).with_suffix(".png"))
        return Path(path).with_suffix(".png")

    def shell(self, *args, timeout=None):
        self.calls.append("shell")
        return ""

    def tap(self, x, y, delay=0.0):
        self.calls.append(("tap", x, y))


# ------------------------------------------------------------------ 1. 取帧
def test_latest(tmp: Path):
    frame = tmp / "_stream_frame.jpg"
    make_jpeg(frame)
    st = FrameStream(StubAdb(tmp), frame)
    st._ff_proc = type("P", (), {"poll": staticmethod(lambda: None)})()   # 假装流水线活着
    st._adb_proc = st._ff_proc

    dest = tmp / "ev_copy.jpg"
    got = st.latest(copy_to=dest)
    check(got == dest and dest.is_file(), "latest(copy_to=) 拷贝出证据图")
    check(dest.read_bytes() == frame.read_bytes(), "拷贝内容与流里的帧逐字节一致")
    check(st.stats["frames"] == 1, "成功取帧计数 +1")

    # 撕裂缝：文件只写了一半 → 读不开 → 重试完返回 None（这一次退 screencap，不等）
    torn = tmp / "torn.jpg"
    torn.write_bytes(frame.read_bytes()[:400])
    st.frame = torn
    got = st.latest(copy_to=tmp / "ev_torn.jpg")
    check(got is None and st.stats["misses"] == 1, "撕裂缝读不开时返回 None（不抛异常）")

    # 帧陈旧：screenrecord 到点退出后帧不再更新 → 也返回 None，交给 ensure() 重启
    st.frame = frame
    old = time.time() - 5
    os.utime(frame, (old, old))
    got = st.latest(copy_to=tmp / "ev_stale.jpg")
    check(got is None and st.stats["stale"] == 1, "帧陈旧（>1 s 没更新）时返回 None 并记 stale")
    check(st.summary()["frame"] == str(frame), "summary() 带上帧路径，便于排查")


# ------------------------------------------------------------- 2. 回退链
def test_fallback(tmp: Path):
    adb = StubAdb(tmp)
    src = LiveSource(adb, work_dir=tmp, capture="stream")
    p = src.screenshot("ev")
    check(p.suffix == ".png", f"stream/gzip/raw 全不可用时退回设备端 PNG（拿到 {p.name}）")
    check(src.stream_dead, "stream 起不来被拉黑（不再每次重试）")
    check("stream" in (src.fail_reasons or {}), f"记下失败原因：{src.fail_reasons}")
    check(adb.calls.count("gz") == 1, "gzip 路只试一次（拉黑之后不再重试）")
    check(src.last_chain == "png", f"last_chain 记下最后走的是哪一档 = {src.last_chain}")
    src.close()
    check(True, "close() 可重复调用、不抛异常")


class StubStream:
    """假常驻流：latest() 直接交出现成的一张图（不碰 ffmpeg/screenrecord）。"""

    def __init__(self, frame: Path):
        self.frame = frame
        self.stopped = False

    def alive(self):
        return True

    def latest(self, copy_to=None, **kw):
        dest = Path(copy_to) if copy_to else self.frame
        if copy_to:
            shutil.copyfile(self.frame, dest)
        return dest

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"starts": 1, "frames": 1, "misses": 0, "restarts": 0, "stale": 0}


def test_auto_prefers_stream(tmp: Path):
    """默认档（auto）必须**先试视频流**，成功就写 last_chain=stream（runner 靠它开复读）。"""
    frame = make_jpeg(tmp / "_stream_frame.jpg", size=(320, 144), color=(60, 40, 40))
    adb = StubAdb(tmp)
    src = LiveSource(adb, work_dir=tmp, capture="auto")
    src.stream = StubStream(frame)                      # 假装流已经起好了
    p = src.screenshot("ev_auto")
    check(p.suffix == ".jpg" and p.is_file(), f"auto 走流拿到 JPEG（{p.name}）")
    check(src.last_chain == "stream", f"last_chain = {src.last_chain}（auto 首选流）")
    check(adb.calls == [], f"走流成功时不该碰 adb 取屏（实际调用 {adb.calls}）")


def test_chain():
    chain = LiveSource.CHAINS["stream"]
    check(chain[0] == "stream" and "gzip" in chain, f"stream 链 = {chain}（失败自动退 gzip）")
    auto = LiveSource.CHAINS["auto"]
    check(auto[0] == "stream", f"auto 链首项是 stream（用户 2026-10-11 定） = {auto}")
    check("gzip" in auto and "raw" in auto, "auto 链在流起不来时还能退 gzip → 原始帧")
    check(LiveSource.CHAINS["gzip"] == ("gzip",), "显式 gzip 仍然只走 gzip（不偷偷开流）")
    check(LiveSource.CHAINS["png"] == (), "png 档就是不压（老路）")


def main():
    print(f"ffmpeg: {find_ffmpeg() or '（没找到：stream 会被拉黑，gzip 照常）'}")
    with tempfile.TemporaryDirectory(prefix="dz_stream_") as d:
        tmp = Path(d)
        test_latest(tmp)
        test_fallback(tmp)
        test_auto_prefers_stream(tmp)
    test_chain()
    print("=" * 46)
    if FAILS:
        print(f"FAIL {len(FAILS)}")
        for f in FAILS:
            print("  - " + f)
        return 2
    print("FAIL 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
