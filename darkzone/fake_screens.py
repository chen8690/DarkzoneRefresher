"""离线假手机：合成与真机同版式的截图，并按坐标判断「点到哪个热区」。

用途：没有手机也能端到端测试主循环。关键是热区坐标**与解析层用同一套常量**导出来，
所以如果主循环点歪了、热区没命中，状态就不会变，主循环的「点了没变化」检测会立刻报错。

版式常量取自真机截图 samples/screen1_refresh_boss.jpg / screen2_refresh_entries.jpg (2844x1280)。
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

IMG_W, IMG_H = 2844, 1280
FONT_CANDIDATES = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                   r"C:\Windows\Fonts\Deng.ttf"]
BG = (16, 16, 18)
FG = (235, 235, 235)

# 版式锚点（图片坐标）
LIST_TITLE_Y = 148
LIST_COST_Y = 151
LIST_PANEL_X = {"普通队员": 239, "精锐队员": 1065, "暗区首领": 1891}
LIST_SELECT_X = {"普通队员": 803, "精锐队员": 1629, "暗区首领": 2454}
SELECT_Y = 1187
DETAIL_ROW_Y = [251, 451, 649]          # 词条行文字左上角 y
DETAIL_ROW_X = 1978
DETAIL_EQUIP_Y = [339, 538, 737]        # 「点击装备」按钮
DETAIL_EQUIP_X = 2233
DETAIL_DISPATCH = (2385, 1187)
LIST_REFRESH_Y = 166
# 与 profiles/default.json 保持一致（数值来自真机 3200x1440 截图的像素测量）
REFRESH_X_FRAC = {"普通队员": 0.3314, "精锐队员": 0.6217, "暗区首领": 0.9120}
DETAIL_REFRESH_X_FRAC = 0.9178
# 刷新确认弹窗（真机实测：点 ↻ 先弹「提示 / 是否刷新特殊条件：xxx / 确定」，点确定才真刷新）
DIALOG_CONFIRM_FRAC = (1592 / 3200, 1032 / 1440)     # 「确定」按钮中心
DIALOG_CANCEL_FRAC = (2237 / 3200, 381 / 1440)       # 右上角 ✕
HOTSPOT_R = 60


def font(size=30):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _center_text(d, cx, cy, text, f, fill=FG):
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=f)
    d.text((cx - (x1 - x0) / 2, cy - (y1 - y0) / 2), text, font=f, fill=fill)


def render_list(boss_name, boss_cost="15000", names=None, equipment=(), status="0/3 ? 1 1827.0K"):
    names = names or {"普通队员": "卓成·科英", "精锐队员": "昆廷·福克斯", "暗区首领": boss_name}
    img = Image.new("RGB", (IMG_W, IMG_H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_small = font(34), font(28)
    d.text((198, 40), "加文·麦康奈尔", font=font(44), fill=FG)   # 真机列表页左上角是区域名
    d.text((2016, 40), status, font=f_small, fill=FG)
    for kind, x in LIST_PANEL_X.items():
        d.text((x, LIST_TITLE_Y), f"{kind}——{names[kind]}", font=f_title, fill=FG)
    d.text((2484, LIST_COST_Y), boss_cost, font=f_small, fill=FG)
    for i, line in enumerate(equipment):
        d.text((1893, 1082 + i * 34), line, font=f_small, fill=(210, 120, 110))
    for kind, x in LIST_SELECT_X.items():
        _center_text(d, x, SELECT_Y, "选择", font(36))
    return img


def render_detail(boss_name, items, status="0/3 ? 1 1827.0K"):
    img = Image.new("RGB", (IMG_W, IMG_H), BG)
    d = ImageDraw.Draw(img)
    f_small, f_btn = font(26), font(32)
    d.text((198, 40), "武装派遣", font=font(44), fill=FG)
    d.text((2016, 40), status, font=f_small, fill=FG)
    d.text((214, 172), f"暗区首领——{boss_name}", font=font(34), fill=FG)
    d.text((1978, 172), "特殊习惯", font=f_small, fill=FG)
    d.text((320, 254), "预计收益", font=f_small, fill=FG)
    d.text((662, 254), "派遣时长", font=f_small, fill=FG)
    for i, item in enumerate(items[:3]):
        d.text((DETAIL_ROW_X, DETAIL_ROW_Y[i]), item, font=f_small, fill=(210, 120, 110))
        d.text((2525, DETAIL_ROW_Y[i] + 4), "500", font=f_small, fill=FG)
        _center_text(d, DETAIL_EQUIP_X + 65, DETAIL_EQUIP_Y[i] + 16, "点击装备", f_btn)
    _center_text(d, *DETAIL_DISPATCH, "消耗4 派遣", font(38))
    return img


def render_dialog(base: Image.Image, prompt: str) -> Image.Image:
    """弹窗视图：背景被压暗（真机 OCR 就读不出背景了），上面画提示框 + 确定 + ✕。"""
    img = Image.blend(base.convert("RGB"), Image.new("RGB", base.size, (0, 0, 0)), 0.82)
    d = ImageDraw.Draw(img)
    W, H = img.size
    d.rectangle([0.275 * W, 0.23 * H, 0.78 * W, 0.78 * H],
                fill=(28, 30, 34), outline=(90, 95, 105))
    d.text((0.31 * W, 0.29 * H), "提示", font=font(46), fill=FG)
    d.text((0.34 * W, 0.45 * H), prompt, font=font(38), fill=FG)
    cx, cy = DIALOG_CANCEL_FRAC[0] * W, DIALOG_CANCEL_FRAC[1] * H
    d.line([cx - 20, cy - 20, cx + 20, cy + 20], fill=(235, 80, 50), width=7)
    d.line([cx - 20, cy + 20, cx + 20, cy - 20], fill=(235, 80, 50), width=7)
    bx, by = DIALOG_CONFIRM_FRAC[0] * W, DIALOG_CONFIRM_FRAC[1] * H
    d.rectangle([bx - 0.045 * W, by - 0.032 * H, bx + 0.045 * W, by + 0.032 * H],
                fill=(170, 60, 25))
    _center_text(d, bx, by, "确定", font(38))
    return img


class FakePhone:
    """状态机式假手机：点了热区才改状态，点歪了什么也不变。"""

    def __init__(self, work_dir="logs_fake", image_size=(IMG_W, IMG_H), device_size=(3200, 1440),
                 boss_queue=("阿贾克斯", "雷诺伊尔", "德尔文"), row_queues=None,
                 hot_shift=(0, 0), screen="list", resource=1_827_000, slow_reads=0, anim_reads=0,
                 flaky_rows=None, anim_seconds=0.0, dialog_delay_reads=0,
                 noise_when_pending=None):
        # slow_reads：点击生效后，接下来 N 次读屏**仍返回旧画面**——模拟真机上
        # 「等太短了，游戏还没把新词条画出来」。用来验证 runner 的重读安全网。
        # anim_reads：点击生效后，接下来 N 次读屏返回**「顶栏券数已扣、词条还是旧的」**
        # 这一帧——真机的刷新顺序就是先扣券再画词条。用来验证 settle 稳定性复读。
        # anim_seconds：同上，但按**时间**而不是按读屏次数——真机的动画窗口是秒级的，跟这一帧
        # 被读了几次无关。用来验证 settle_guard 时间闸门（按次数的 anim_reads 骗不过 sleep）。
        # flaky_rows={槽位: (假名字, 次数)}：接下来 N 次渲染里该行故意画成另一个名字，
        # 模拟 h264 流帧把短字母核心糊掉（真机 `AUG突击步枪` → `AIJG突击步枪`）。用来验证
        # runner「花券前再确认」的 confirm_reads。
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.iw, self.ih = image_size
        self.dw, self.dh = device_size
        self.sx, self.sy = self.dw / self.iw, self.dh / self.ih
        self.boss_queue = list(boss_queue)
        self.row_queues = {int(k): list(v) for k, v in (row_queues or {}).items()}
        self.hot_shift = hot_shift              # 故意把热区挪走，用来测「点歪了」的检测
        self.screen = screen
        self.resource = resource
        self.boss_i = 0
        self.row_i = {k: 0 for k in self.row_queues}
        self.shots = 0
        self.refresh_count = 0                  # 成功刷新次数（顶栏 0/3 与资源都随它变）
        self.taps = []                          # [(设备坐标, 命中热区名)]
        self.dispatch_tapped = False
        self.back_count = 0
        self.dialog = None                      # 刷新确认弹窗（模态）
        self.dialog_count = 0                   # 弹出过多少次确认框
        self.slow_reads = int(slow_reads)       # 点击后「旧画面」还要持续几次读屏
        self._freeze = 0                        # 还剩几次读屏返回旧画面
        self._last_img = None                   # 上一次真正渲染出来的画面
        self.anim_reads = int(anim_reads)       # 点击后「顶栏新、词条旧」还要持续几次读屏
        self._anim = 0                          # 还剩几次读屏返回动画中间态
        self._anim_rows = None                  # 动画中间态里的旧词条
        self._anim_boss = None                  # 动画中间态里的旧首领名
        self.anim_seconds = float(anim_seconds)  # 点击后「顶栏新、词条旧」持续多少秒（按时间）
        self._anim_until = 0.0                  # 动画中间态到什么时候结束（time.time() 比较）
        self.flaky_rows = {int(k): [str(v[0]), int(v[1])]
                           for k, v in (flaky_rows or {}).items()}   # 槽位 → [假名字, 剩余次数]
        # noise_when_pending={槽位: 噪声名字}：只要「确认框已请求、还没画出来」这几帧，该行
        # 就故意画成另一个写法 —— 模拟真机 h264 流帧把同一件装备糊成另一个写法（`SEK复合防弹衣`
        # → `SEK合防弹衣`）。它跟 flaky_rows 的区别是**窗口自动对齐那次点击**：点之前是真名、
        # 点之后（弹窗未画出的那几帧）是噪声名，而 `rows()` 里那件装备一动没动。
        # 这正是 2026-10-11 真机 4/30 幽灵记账的来路，场景 W 就是钉它。
        self.noise_when_pending = {int(k): str(v)
                                   for k, v in (noise_when_pending or {}).items()}
        # dialog_delay_reads：点 ↻ 之后弹窗要过几帧才画出来（真机 ~2 s）。这几帧里顶栏已经变了
        # （status 带「刷新中」），屏面却不是弹窗 → 逼 runner 走「先在 settle 里发现弹窗、
        # 下一轮 _ensure_kind 里才确认」这条路（场景 S；真机 logs\live_guard_1.txt 就是它）。
        self.dialog_delay_reads = int(dialog_delay_reads)
        self._pending_dialog = None             # 还没画出来的弹窗
        self._dialog_in = 0                     # 还差几帧画出来

    # ---------------------------------------------------------------- 状态
    @property
    def boss(self):
        return self.boss_queue[min(self.boss_i, len(self.boss_queue) - 1)]

    def rows(self):
        return [self.row_queues[k][min(self.row_i[k], len(self.row_queues[k]) - 1)]
                for k in sorted(self.row_queues)]

    def render_rows(self):
        """这一帧要画的三行词条：带 flaky_rows 的槽位这次故意画成「像池外」的名字。"""
        rows = self.rows()
        for i in range(len(rows)):
            f = self.flaky_rows.get(i + 1)
            if f and f[1] > 0:
                f[1] -= 1
                rows[i] = f[0]
            n = self.noise_when_pending.get(i + 1)
            if n is not None and self._pending_dialog is not None:
                rows[i] = n
        return rows

    def size(self):
        return (self.dw, self.dh)

    def status(self):
        """顶栏文案：刷新成功次数 + 资源都会变 → 主循环能用它判断「点了有没有变化」。

        还在等弹窗弹出来的那几帧故意加个后缀：真机就是这样 —— 点完 ↻ 顶栏先变（券数/计数），
        弹窗要 ~2 s 才画出来，于是「签名和点之前不一样」为真、而屏面又不是弹窗。
        这正是 `logs\\live_guard_1.txt` 里闸门被绕过的那条路（见 test 场景 S）。
        """
        base = f"{self.refresh_count}/3 ? 1 {self.resource / 1000:.1f}K"
        return base + " 刷新中" if self._pending_dialog is not None else base

    # ---------------------------------------------------------------- 渲染
    def render(self):
        if self._pending_dialog is not None:
            # 弹窗还没画出来：再等几帧，到点了这一帧就把弹窗画上（真机 ~2 s 才弹）。
            self._dialog_in -= 1
            if self._dialog_in <= 0:
                self.dialog, self._pending_dialog = self._pending_dialog, None
        if self._anim > 0 or time.time() < self._anim_until:
            # 动画中间态：顶栏（刷新计数/资源）已经是新的，三行词条还是旧的。
            if self._anim > 0:
                self._anim -= 1
            if self.screen == "detail":
                img = render_detail(self._anim_boss or self.boss,
                                    self._anim_rows or self.rows(), status=self.status())
            else:
                img = render_list(self._anim_boss or self.boss, status=self.status())
            if (self.iw, self.ih) != (IMG_W, IMG_H):
                img = img.resize((self.iw, self.ih))
            self._last_img = img
            return img
        if self._freeze > 0 and self._last_img is not None:
            self._freeze -= 1
            return self._last_img               # 画面还没刷新：仍给旧图
        if self.screen == "detail":
            img = render_detail(self.boss, self.render_rows(), status=self.status())
        else:
            img = render_list(self.boss, status=self.status())
        if self.dialog:
            img = render_dialog(img, self.dialog["prompt"])
        if (self.iw, self.ih) != (IMG_W, IMG_H):
            img = img.resize((self.iw, self.ih))
        self._last_img = img
        return img

    def screenshot(self, tag="shot") -> Path:
        self.shots += 1
        p = self.work_dir / f"{tag}_{self.shots:03d}.png"
        self.render().save(p)
        return p

    # ---------------------------------------------------------------- 热区
    def hotspots(self):
        dx, dy = self.hot_shift
        if self.dialog:                      # 模态弹窗：背景一概点不动
            # 注意：hot_shift 模拟的是「配置里的坐标偏了」，弹窗没有配置坐标——主循环是照
            # OCR 量到的「确定」位置点的，所以弹窗热区**不跟着偏**（否则就变成测坐标错位了）。
            return {
                "dialog_confirm": (self.iw * DIALOG_CONFIRM_FRAC[0],
                                   self.ih * DIALOG_CONFIRM_FRAC[1]),
                "dialog_cancel": (self.iw * DIALOG_CANCEL_FRAC[0],
                                  self.ih * DIALOG_CANCEL_FRAC[1]),
            }
        hs = {}
        if self.screen == "list":
            for kind, frac in REFRESH_X_FRAC.items():
                hs[f"list_refresh:{kind}"] = (self.iw * frac + dx, LIST_REFRESH_Y + dy)
            for kind, x in LIST_SELECT_X.items():
                hs[f"list_select:{kind}"] = (x + dx, SELECT_Y + dy)
        else:
            for i in range(3):
                hs[f"row{i + 1}"] = (self.iw * DETAIL_REFRESH_X_FRAC + dx, DETAIL_ROW_Y[i] + 12 + dy)
            hs["dispatch"] = (DETAIL_DISPATCH[0] + dx, DETAIL_DISPATCH[1] + dy)
        return hs

    def _ask_dialog(self, prompt, target):
        """真机行为：点 ↻ **不会直接刷新**，而是先弹模态框，点「确定」才真的花券刷新。

        dialog_delay_reads > 0 时弹窗要过几帧才画出来（真机 ~2 s）：这几帧里顶栏已经变了、
        屏面还不是弹窗 —— runner 必须能从这条路里把弹窗找出来并确认（场景 S）。
        """
        d = {"prompt": prompt, "target": target, "applied": False}
        if self.dialog_delay_reads > 0:
            self._pending_dialog = d
            self._dialog_in = self.dialog_delay_reads
        else:
            self.dialog = d
        self.dialog_count += 1

    def _apply_dialog(self):
        d = self.dialog
        if not d or d.get("applied"):
            return
        d["applied"] = True
        old_rows, old_boss = self.rows(), self.boss      # 扣券前的词条：动画中间态要用
        if d["target"].startswith("list_refresh"):
            self.boss_i += 1
            self.resource -= 15000
            self.refresh_count += 1
        elif d["target"].startswith("row"):
            idx = int(d["target"][3:])
            self.row_i[idx] = self.row_i.get(idx, 0) + 1
            self.resource -= 500
            self.refresh_count += 1
        if self.anim_reads > 0 or self.anim_seconds > 0:
            # 真机顺序：先扣券（顶栏立刻变）→ 新词条才慢慢画出来
            self._anim = self.anim_reads
            self._anim_until = time.time() + self.anim_seconds
            self._anim_rows, self._anim_boss = old_rows, old_boss
        self.dialog = None

    def tap(self, xy):
        ix, iy = xy[0] / self.sx, xy[1] / self.sy
        best, bestd = None, 1e9
        for name, (hx, hy) in self.hotspots().items():
            dist = ((ix - hx) ** 2 + (iy - hy) ** 2) ** 0.5
            if dist < bestd:
                best, bestd = name, dist
        hit = best if bestd <= HOTSPOT_R else None
        self.taps.append(((int(xy[0]), int(xy[1])), hit))
        if hit is None:
            return
        self._freeze = self.slow_reads          # 命中热区（状态要变了）：接下来几次读屏还是旧画面
        if hit == "dialog_confirm":
            self._apply_dialog()
            return
        if hit == "dialog_cancel":
            self.dialog = None
            return
        if hit == "dispatch":
            self.dispatch_tapped = True
        elif hit.startswith("list_refresh"):
            self._ask_dialog(f"是否刷新特殊条件：{self.boss}", hit)
        elif hit.startswith("list_select"):
            self.screen = "detail"
        elif hit.startswith("row"):
            idx = int(hit[3:])
            self._ask_dialog(f"是否刷新特殊条件：{self.rows()[idx - 1]}", hit)

    def back(self):
        self.back_count += 1
        self.screen = "list"
