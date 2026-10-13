"""真机探针：量「点 ↻ 之后那个确认框到底是几点几秒画出来的、画出来之后点它还有没有用」。

背景（2026-10-13 四发 live 的间接证据）：0.85 s 盲点点的进去、1.0/1.3/1.9 全都落到
`blind_missed`（复核那一屏重新 OCR 判成 dialog）。当时只是**假设**「框已经在屏幕上时点它
会被游戏吃掉」。这个脚本第一次直接量：

  A. 耗时表：点 ↻ 之后连拍（默认 15 fps 视频流），帧与参考帧做**像素差** → 框出现的时刻 t_a
  B. 生效窗口：在「相对 t_a」的指定 offset 处点一次「确定」，点完再连拍 + OCR，看框什么时候
     消失（判成非 dialog）、以及这一行的名字有没有真的换掉
  C. 对照组：offset 依次从负到正扫，就能看出「早点」和「晚点」的区别

设计要点（第一版写错过一次，记下来）：**检测循环里绝不跑 OCR**。Windows OCR 一屏
0.25~0.35 s，塞进连拍循环会把帧率压到 ~3 fps、时间分辨率全毁。所以帧差只用像素，
OCR 只出现在三个关键点：进入前判详情页、每次「点完之后」判框消没消失（这才是真正
的 ground truth）、以及确认时读那一行词条。

每次试验点完都会**补一发**确认真点（能一眼看出框还在就再点，最多 4 发）直到框消失，
不然模态框会挡住下一发的 ↻。

用法（手机停在「武装派遣」详情页，至少一行不在池内）：

  python tools\\dialog_timing_probe.py                    # 默认 6 发，offset 从 -0.6 起每发 +0.3
  python tools\\dialog_timing_probe.py --trials 4 --fps 15
  python tools\\dialog_timing_probe.py --no-ocr           # 只量像素（更快，但判不了框消失）

产出：`logs\\dialog_probe_<stamp>\\`
  frames\\*.jpg     每次试验的连拍帧
  timeline.txt     每帧的「相对点 ↻ 时刻 / 全屏差 / 中心区差」
  summary.txt      每发试验一行 JSON（t_a、offset、点击后多久框消失、行变没变）

只做「点 ↻ / 点确定」两种点击，**绝不碰派遣**。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from PIL import Image, ImageChops, ImageStat        # noqa: E402

from darkzone import runner as R                    # noqa: E402
from darkzone.adb import Adb                        # noqa: E402
from darkzone.stream import FrameStream             # noqa: E402
from darkzone.ui import load_profile, parse_screen  # noqa: E402

DEV_W, DEV_H = 3200, 1440
# 弹窗面板大概占的中间一带（横屏工作坐标）。检测框出现就看这一带的像素差，
# 免得顶栏券数、时钟这些一直在变的像素把信号淹了。
DIALOG_BOX = (1300, 340, 1900, 1100)
# 三行词条的窄条（横屏坐标）：判「这一行名字变了没有」。
ROW_BOX = {
    1: (2520, 220, 3200, 380),
    2: (2520, 445, 3200, 605),
    3: (2520, 670, 3200, 830),
}
# 详情页三行 ↻ 的竖条（真机 297/521/745，2026-10-11 量的）
ROW_Y_FRAC = {1: 297 / DEV_H, 2: 521 / DEV_H, 3: 745 / DEV_H}


def crop_sig(im: Image.Image, box, size=(64, 24)):
    return im.crop(box).convert("L").resize(size)


def diff_score(a: Image.Image, b: Image.Image) -> float:
    return statistics.fmean(ImageStat.Stat(ImageChops.difference(a, b)).mean)


class TapTimer:
    """测「adb tap 真正落下去」的时刻。

    `adb.tap` 是同步阻塞的（`adb shell input tap`），开销约 0.1~0.2 s，所以
    `t_after - adb.tap(...)` 根本不是点击生效的时刻。这里把 tap 丢进线程跑，
    落点时刻取「发出前」与「返回后」的中点，误差缩小到几十毫秒。
    """

    def __init__(self, adb: Adb):
        self.adb = adb
        self.hist: list[tuple[str, float]] = []

    def tap(self, x, y, label="") -> float:
        import threading
        t0 = time.perf_counter()
        box = {}

        def work():
            self.adb.tap(x, y)
            box["t1"] = time.perf_counter()

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join()
        mid = (t0 + box.get("t1", time.perf_counter())) / 2
        self.hist.append((label or f"({x},{y})", round(mid, 3)))
        return mid


class Shooter:
    """连拍器：优先常驻视频流（帧率可调），流不可用就退回 adb screencap。"""

    def __init__(self, adb: Adb, frame_dir: Path, fps: int):
        self.adb = adb
        self.frame_dir = frame_dir
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.direct = 0
        self.stream = None
        self.frames: list[tuple[float, Path, float, float, float]] = []   # t, path, full, center, row
        frame_path = frame_dir / "_stream_frame.jpg"
        try:
            st = FrameStream(adb, frame_path, fps=fps, quality=2, time_limit=300)
            if st.available() and st.start():
                self.stream = st
                print(f"[连拍] 视频流就绪 fps={fps} → {frame_path}")
            else:
                print("[连拍] 视频流起不来，退回 adb screencap（每帧 ~1.2 s，时刻会粗）")
        except Exception as e:                                  # noqa: BLE001
            print(f"[连拍] 视频流异常（{e}），退回 adb screencap")

    def grab(self, tag: str):
        """返回 (时刻, 落盘路径)。时刻 = 我们拿到这一帧的那一瞬间。"""
        dst = self.frame_dir / f"{tag}.jpg"
        if self.stream is not None:
            got = self.stream.latest(copy_to=dst)
            if got:
                return time.perf_counter(), dst
        self.direct += 1
        t = time.perf_counter()
        self.adb.screencap(dst)
        return t, dst

    def stop(self):
        if self.stream is not None:
            self.stream.stop()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=6, help="试验发数（每发 = 一次 ↻）")
    ap.add_argument("--fps", type=int, default=15, help="连拍帧率（视频流）")
    ap.add_argument("--offset-start", type=float, default=-0.6, help="首发 offset（相对 t_a，秒）")
    ap.add_argument("--offset-step", type=float, default=0.3, help="每发 offset 递增")
    ap.add_argument("--slot", type=int, default=0, help="只刷这一行（0 = 自动挑第一行不在池内的）")
    ap.add_argument("--anchor", choices=("dialog", "tap"), default="dialog",
                    help="offset 相对什么：dialog=框出现（连拍测得）；tap=点 ↻ 那一刻"
                         "（不连拍，只等固定秒数，更贴近 runner 的盲点路）")
    ap.add_argument("--settle-window", type=float, default=3.5,
                    help="点完之后盯多久判「框消没消失」")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--ocr", default="win")
    ap.add_argument("--no-ocr", action="store_true", help="点完之后不 OCR（快，但判不了框消失）")
    a = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "logs" / f"dialog_probe_{stamp}"
    frame_dir = out_dir / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    adb = Adb(serial=a.serial)
    adb.require()
    prof = load_profile(ROOT / "profiles" / "default.json")
    cfg = R.Config()
    cfg.ocr = a.ocr
    book = R.parse_workbook(R.default_rules_path())
    ocr_name, ocr_lines = R.Runner._pick_ocr(a.ocr)
    print(f"[环境] serial={adb.serial} ocr={ocr_name} 输出={out_dir}")

    confirm_frac = (prof.get("dialog_confirm_xy_frac") or [0.4975, 0.7167])
    confirm = (int(confirm_frac[0] * DEV_W), int(confirm_frac[1] * DEV_H))
    refresh_x = int(prof["screen_detail"]["refresh_x_frac"] * DEV_W)
    print(f"[坐标] 「确定」= {confirm}；派遣 = ({0.808*DEV_W:.0f},{0.926*DEV_H:.0f})，不碰")

    shot = Shooter(adb, frame_dir, a.fps)
    taps = TapTimer(adb)
    timeline = (out_dir / "timeline.txt").open("w", encoding="utf-8")

    def read_screen(tag: str):
        """抓一帧 + OCR 判页面类型（慢，只在关键点用）。"""
        t, path = shot.grab(tag)
        info = {"kind": "?"}
        if not a.no_ocr:
            try:
                rows, _res = ocr_lines(path, lang=cfg.lang)
                kind, scr = parse_screen(rows, (DEV_W, DEV_H), prof,
                                         vocab=book.vocab, min_score=cfg.min_score)
                info["kind"] = kind
                if kind == "dialog":
                    info["prompt"] = getattr(scr, "prompt", None)
                    info["img"] = path.name
                    return t, info
                info["boss"] = getattr(scr, "boss", None)
                for r in getattr(scr, "rows", []) or []:
                    info[r.index] = r.raw
                info["img"] = path.name
            except Exception as e:                              # noqa: BLE001
                info = {"kind": f"ocr_error:{e}", "img": path.name}
        return t, info

    def capture_for(t_ref, tag_prefix, max_s, stop_after_s=None, thresh=None):
        """连拍纯像素帧，最多 max_s 秒。

        `thresh` 给了就「早退」：全屏差第一次越过它之后，再多拍 `stop_after_s` 秒即停。
        早退很关键——不早退的话采集会跑满整个窗口，等我们再回头算 t_a，目标时刻早就
        过去了，点击就全挤在同一时刻（第一版就是这么废掉的）。
        """
        frames = []
        t_first_hit = None
        t_end = t_ref + max_s
        while True:
            t, path = shot.grab(f"{tag_prefix}{len(frames):02d}")
            sg = _sig(path)
            fd = diff_score(ref_full, sg[0])
            cd = diff_score(ref_center, sg[1])
            rd = diff_score(ref_row, sg[2]) if ref_row is not None else 0.0
            frames.append((t - t_ref, path, fd, cd, rd))
            if thresh is not None and t_first_hit is None and fd > thresh:
                t_first_hit = t - t_ref
            now = time.perf_counter()
            if t_first_hit is not None and stop_after_s is not None \
                    and now - t_ref - t_first_hit >= stop_after_s:
                break
            if now >= t_end:
                break
        for dt, path, fd, cd, rd in frames:
            timeline.write(json.dumps({"trial": tag_prefix, "dt": round(dt, 3),
                                       "full": round(fd, 2), "center": round(cd, 2),
                                       "row": round(rd, 2), "img": path.name},
                                      ensure_ascii=False) + "\n")
        timeline.flush()
        return frames

    ref_full = ref_center = ref_row = None
    sig_cache: dict[str, tuple] = {}

    def _sig(path: Path) -> tuple:
        """一帧的三个签名。**每帧只做一次 crop+resize**（先把整屏缩到 320x144 再切），
        否则 3200x1440 的 JPEG 每帧要缩放三次，实测把连拍压到 7 fps。"""
        key = str(path)
        got = sig_cache.get(key)
        if got is None:
            with Image.open(path) as im:
                small = im.convert("RGB").resize((320, 144), Image.BILINEAR)
                full = small.resize((96, 40), Image.BILINEAR)
                center = small.crop((130, 34, 190, 110))
                row = small.crop((252, int(ROW_Y_FRAC[row_slot] * 144) - 8,
                                  320, int(ROW_Y_FRAC[row_slot] * 144) + 8))
                got = (full, center, row)
            sig_cache[key] = got
            if len(sig_cache) > 240:
                for k in list(sig_cache)[:120]:
                    sig_cache.pop(k, None)
        return got

    # ------------------------------------------------------------ 前置检查
    _t0, info = read_screen("ref")
    if info.get("kind") != "detail":
        print(f"[中止] 当前不在详情页（判成 {info.get('kind')}）：{info.get('img')}")
        shot.stop()
        return 3
    pool = book.blocks[cfg.block_index].pools
    row_slot = a.slot
    if not row_slot:
        for i in (1, 2, 3):
            raw = info.get(i)
            if raw and raw not in pool.get(i, []):
                row_slot = i
                break
    if not row_slot:
        print(f"[中止] 三行都在池内，没有可刷新的行：{info}")
        shot.stop()
        return 0
    print(f"[目标] 第 {row_slot} 行 = {info.get(row_slot)!r}（不在池内：{pool.get(row_slot)}）")
    refresh = (refresh_x, int(ROW_Y_FRAC[row_slot] * DEV_H))
    print(f"[坐标] 第 {row_slot} 行 ↻ = {refresh}")

    summary = []
    for k in range(a.trials):
        offset = round(a.offset_start + k * a.offset_step, 3)
        print(f"\n===== 试验 {k+1}/{a.trials}：目标 offset = {offset:+.2f} s（相对框出现） =====")

        # 1) 参考帧：等页面安静（连续两帧中心差 < 1.0），再 OCR 一次当基线
        prev = None
        for _ in range(40):
            _t_r, p_r = shot.grab(f"t{k}_ref")
            cur = _sig(p_r)[1]
            if prev is not None and diff_score(prev, cur) < 1.0:
                break
            prev = cur
            time.sleep(0.06)
        _t_pre, info_pre = read_screen(f"t{k}_pre")
        if info_pre.get("kind") != "detail":
            print(f"  !! 进试验前不是详情页（{info_pre.get('kind')}），本轮中止")
            break
        before_raw = info_pre.get(row_slot)
        ref_full, ref_center, ref_row = _sig(frame_dir / info_pre["img"])
        print(f"  起点：第 {row_slot} 行 = {before_raw!r}")

        # 2) 点 ↻。anchor=dialog 时连拍找框出现（先拍 0.7 s 量基线噪声定阈值，
        #    再按阈值早退——这样 t_a 一出现就能停，才谈得上「在 t_a + offset 处点击」）。
        #    anchor=tap 时不连拍：offset 就是「点完 ↻ 之后等几秒」，直接睡。
        #    点击时刻用 TapTimer 量（adb tap 是阻塞的，直接读表会把点击时刻推迟 0.1~0.2 s）。
        t_tap = taps.tap(*refresh, label=f"t{k}:refresh")
        frames: list = []
        base = rate = 0.0
        thresh = 0.0
        t_onset = None
        if a.anchor == "dialog":
            warm = capture_for(t_tap, f"t{k}_w", 0.7)
            base = statistics.fmean([f[2] for f in warm]) if warm else 0.0
            thresh = max(3.0 * base, 1.5)
            frames = warm + capture_for(t_tap, f"t{k}_a", 3.6,
                                        stop_after_s=0.12, thresh=thresh)
            t_a = None
            for i in range(1, len(frames) - 1):
                if frames[i][2] > thresh and frames[i + 1][2] > thresh:
                    t_a = frames[i][0]
                    break
            # 「框开始画出来」的乐观时刻：全屏差第一次抬头（真机是渐显，早于阈值那一下）
            t_onset = next((f[0] for f in frames if f[2] > 1.6), None)
            span = frames[-1][0] if frames else 0.0
            rate = len(frames) / max(1e-6, span)
            print(f"  连拍 {len(frames)} 帧 / {span:.2f} s（≈{rate:.1f} fps，"
                  f"基线差 {base:.2f}，阈值 {thresh:.2f}）")
            print(f"  全屏差曲线：{[round(f[2], 1) for f in frames]}")
            print(f"  中心差曲线：{[round(f[3], 1) for f in frames]}")
            if t_a is None:
                print("  !! 没检测到框出现（曲线没越过阈值），跳过这一发")
                summary.append({"trial": k + 1, "slot": row_slot, "offset": offset,
                                "t_a": None, "note": "no_dialog_detected",
                                "before": before_raw,
                                "curve": [round(f[2], 1) for f in frames]})
                continue
            print(f"  → 框出现于 t_a = {t_a:.3f} s（相对点 ↻）"
                  + (f"；渐显起点 t_onset = {t_onset:.3f} s" if t_onset else ""))
        else:
            # anchor=tap：offset 直接就是「点完 ↻ 等多久再点确定」。把 t_a 记 0，
            # 这样下面统一算 target = t_tap + offset，且日志里「相对框出现」那一列无意义。
            t_a = 0.0
            print(f"  → 按固定等待：目标 = 点 ↻ 之后 {offset:.3f} s 点「确定」（不连拍判框）")

        # 3) 等到目标时刻点「确定」
        target = t_tap + t_a + offset
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        t_click = taps.tap(*confirm, label=f"t{k}:confirm")
        print(f"  → 点「确定」于 {t_click - t_tap:.3f} s（相对点 ↻）"
              f" / {t_click - t_tap - t_a:+.3f} s（相对框出现）"
              + (f" / {t_click - t_tap - t_onset:+.3f} s（相对渐显起点）" if t_onset else ""))

        # 4) 点完之后：先纯像素连拍 1.2 s 留证据，再每隔一会儿 OCR 一次判框消没消失
        #    （「框还在不在」只有 OCR 判页面类型才算数，像素差会被动画骗）
        capture_for(t_click, f"t{k}_b", 1.2)
        closed_dt = None
        after_raw = None
        last_kind = None
        # 相对点击时刻的 OCR 采样点：密一点，才能把「框消失于点击后多久」量准
        samples = [0.3, 0.55, 0.8, 1.05, 1.3, 1.55, 1.8, 2.1, 2.4, 2.7, 3.0, 3.3, 3.6]
        samples = [w for w in samples if a.anchor == "dialog" or w <= a.settle_window]
        for w in samples:
            wait = t_click + w - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            if a.no_ocr:
                break
            t_c, info_now = read_screen(f"t{k}_c{w:.2f}")
            kind = info_now.get("kind")
            last_kind = kind
            print(f"     +{t_c - t_click:.2f} s → {kind}"
                  + (f" prompt={info_now.get('prompt')!r}" if kind == "dialog" else ""))
            if kind != "dialog":
                if closed_dt is None:
                    closed_dt = t_c - t_click
                if kind == "detail" and info_now.get(row_slot) \
                        and info_now.get(row_slot) != before_raw:
                    after_raw = info_now.get(row_slot)
                    break
            elif closed_dt is None and t_c - t_click >= a.settle_window:
                break

        print(f"  → 框消失于点击后 {closed_dt if closed_dt is None else round(closed_dt, 3)} s"
              f"；第 {row_slot} 行 {before_raw!r} → {after_raw!r}")
        summary.append({"trial": k + 1, "slot": row_slot, "offset": offset,
                        "t_a": round(t_a, 3),
                        "t_onset": None if t_onset is None else round(t_onset, 3),
                        "frames": len(frames),
                        "fps": round(rate, 1),
                        "baseline": round(base, 2), "thresh": round(thresh, 2),
                        "t_click_rel_tap": round(t_click - t_tap, 3),
                        "t_click_rel_dialog": round(t_click - t_tap - t_a, 3),
                        "t_click_rel_onset": None if t_onset is None
                        else round(t_click - t_tap - t_onset, 3),
                        "closed_after_click": settled(closed_dt),
                        "closed_ok": closed_dt is not None,
                        "last_kind": last_kind,
                        "before": before_raw, "after": after_raw,
                        "curve_full": [round(f[2], 1) for f in frames],
                        "curve_center": [round(f[3], 1) for f in frames],
                        "clicked_at": round(t_click, 3)})
        with (out_dir / "summary.txt").open("w", encoding="utf-8") as f:
            f.write(f"# 试验日志  {stamp}  流fps={a.fps} slot={row_slot}\n")
            for rec in summary:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 5) 清场：框还挂着就补点，最多 4 发（间隔递增）
        for j in range(4):
            _t, info_c = read_screen(f"t{k}_clean{j}")
            if a.no_ocr or info_c.get("kind") != "dialog":
                break
            print(f"  [清场] 框还在，补点「确定」第 {j+1} 次")
            taps.tap(*confirm, label=f"t{k}:clean{j}")
            time.sleep(0.7 + 0.4 * j)

    timeline.close()
    shot.stop()

    print("\n================ 汇总 ================")
    print(f"{'发':>2} {'offset':>7} {'t_a(s)':>7} {'渐显':>6} {'帧数':>4} {'fps':>5} "
          f"{'点@↻后':>8} {'点@框后':>8} {'点@渐显后':>9} {'框消失@点后':>11} {'行变化':>6}")
    for rec in summary:
        if rec.get("t_a") is None:
            print(f"{rec['trial']:>2} {rec['offset']:>+7.2f}   ——  {rec.get('note')}")
            continue
        print(f"{rec['trial']:>2} {rec['offset']:>+7.2f} {rec['t_a']:>7.3f} "
              f"{(rec['t_onset'] if rec['t_onset'] is not None else float('nan')):>6.2f} "
              f"{rec['frames']:>4} {rec['fps']:>5.1f} {rec['t_click_rel_tap']:>8.3f} "
              f"{rec['t_click_rel_dialog']:>+8.3f} "
              f"{(rec['t_click_rel_onset'] if rec['t_click_rel_onset'] is not None else float('nan')):>+9.3f} "
              f"{str(rec['closed_after_click']):>11} "
              f"{str(rec['before'] != rec['after']):>6}")
    print(f"\n结果落盘：{out_dir}")
    return 0


def settled(v):
    return None if v is None else round(v, 3)


if __name__ == "__main__":
    raise SystemExit(main())
