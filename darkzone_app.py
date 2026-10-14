"""
暗区突围·武装派遣 自动刷新 — 桌面 GUI 启动器
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent


class _Tooltip:
    """鼠标移上去显示一段说明（tkinter 没有内置 tooltip，就手写一个）。

    绑定 <Enter>/<Leave>，延迟一小会儿再弹，免得鼠标划过就闪一堆框；
    点一下控件也会立刻收起来。
    """

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, justify=tk.LEFT, background="#ffffe0",
            relief=tk.SOLID, borderwidth=1, padx=8, pady=5, font=("Microsoft YaHei UI", 9),
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None


TIP_DIALOG_MODE = (
    "点「刷新」后那个「是否刷新特殊条件」的确认框怎么处理：\n"
    "· blind（盲点快路，默认）—— 不读弹窗那一屏，点完\n"
    "  刷新键等「等框」秒就直接点「确定」，最快；\n"
    "· read（读框再点）—— 先读一屏，确认屏上是「刷新」框\n"
    "  才点「确定」，慢一点，但每一步都看着屏幕。\n"
    "两种都不会点「派遣」；看不到变化就不记账。"
)

TIP_WAIT_DIALOG = (
    "盲点快路里，从点下刷新键到点「确定」之间等多久（默认 0.85 秒）。\n"
    "真机上那个确认框大概 1.0~1.4 秒才画出来。\n"
    "调小：容易点空 → 回落到「读框再点」那条路再点一次；\n"
    "调大：更糟 —— 框已经在屏上、还停在渐显动画里时，\n"
    "点它会被游戏吃掉，反而多等一轮。\n"
    "除非换手机或分辨率，否则别动这个值。"
)

TIP_BLIND_SETTLE = (
    "点完「确定」之后等多久再回读一屏验收（默认 2.2 秒）。\n"
    "真机实测：点完约 1.8 秒确认框才消失。\n"
    "等太短 → 会读到「券扣了、新装备还没画出来」的中间态，\n"
    "程序就不敢记账，白跑一趟兜底流程、反而更慢；\n"
    "等太长 → 每一发都白等。2.2 秒是实测最快且干净的取值。"
)


def bundle_dir() -> Path:
    """打包运行时（PyInstaller onedir）的资源根 = sys._MEIPASS（_internal）。
    开发环境直接跑源码时就是脚本所在目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else ROOT


def app_dir() -> Path:
    """可写目录（存 app_config.json / logs）：打包后取 exe 所在目录，源码运行取项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


BUNDLE = bundle_dir()

# 打包成 exe 后 sys.executable 变成 exe 本身，无法再 `-m darkzone.runner`，
# 所以真机运行需要一个真实的 Python 解释器。优先用配置里的，其次 DSH 自带运行时
# （按当前用户家目录拼，不写死用户名），最后退到 PATH 里的 python。
DSH_PYTHON = str(Path(os.path.expanduser("~")) / ".dsh" / "dsh-runtimes"
                 / "dsh-primary-runtime" / "dependencies" / "python" / "python.exe")


def resolve_python(cfg: dict) -> str:
    """返回能跑 `-m darkzone.runner` 的真实 python。源码运行用自己；冻结后按配置找。"""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for cand in (cfg.get("python_path"), DSH_PYTHON):
        if cand and Path(cand).is_file():
            return cand
    which = shutil.which("python")
    return which or sys.executable


def resolve_adb(cfg: dict) -> str:
    """返回真能用的 adb：用户填的路径 → PATH → runner 自己用的候选位置（scrcpy/Escrcpy 自带）。

    打包版双击启动时 PATH 里常常没有 adb（它不是系统自带的），只写 "adb" 三个字母会在
    subprocess 里直接失败 —— 于是「明明插着手机却报未连接」。这里与 darkzone/adb.py 的
    ADB_CANDIDATES 保持一致地兜底。
    """
    cand = str(cfg.get("adb_path") or "adb").strip() or "adb"
    if cand.lower() != "adb" and Path(cand).is_file():
        return cand
    hit = shutil.which(cand)
    if hit:
        return hit
    home = Path(os.path.expanduser("~"))
    for p in (
        home / "Apps" / "scrcpy" / "adb.exe",
        home / "AppData" / "Local" / "Programs" / "Escrcpy"
        / "resources" / "extra" / "win" / "scrcpy" / "adb.exe",
        Path("C:/platform-tools/adb.exe"),
    ):
        if p.is_file():
            return str(p)
    return cand


def resolve_project_dir(cfg: dict) -> Path:
    """返回包含 darkzone 包的项目目录（subprocess 用它当 cwd，`-m darkzone.runner` 才找得到包）。
    源码运行时 ROOT 就是项目根；冻结后 exe 目录里没有 darkzone，退回配置或用 ROOT。"""
    cands = [Path(cfg.get("project_dir", "")) if cfg.get("project_dir") else None,
             ROOT]
    for c in cands:
        if c and (c / "darkzone").is_dir():
            return c
    return ROOT


# ── 配置持久化 ────────────────────────────────────────────
CONFIG_FILE = app_dir() / "app_config.json"


def default_rules_path() -> str:
    """优先用打包进应用的规则表；没有就找项目根，最后才退到桌面原始文件。"""
    desktop = Path.home() / "Desktop"
    for cand in (BUNDLE / "规则表.xlsx", BUNDLE / "工作簿1.xlsx",
                 ROOT / "规则表.xlsx",
                 desktop / "工作簿1.xlsx", desktop / "规则表.xlsx"):
        if cand.is_file():
            return str(cand)
    return str(BUNDLE / "规则表.xlsx")


def load_config() -> dict:
    defaults = {
        "rules_xlsx": default_rules_path(),
        "profile": "default",
        "ocr": "win",
        "limit": 40,
        "dialog_mode": "blind",
        "wait_dialog": 0.85,
        "blind_settle": 2.2,
        "adb_path": "adb",
        "ffmpeg_path": "ffmpeg",
        "python_path": DSH_PYTHON,
        "project_dir": str(ROOT),
        "dry_run_first": True,
    }
    try:
        if CONFIG_FILE.exists():
            d = json.loads(CONFIG_FILE.read_text("utf-8"))
            defaults.update(d)
    except Exception:
        pass
    # 归一化：老版本配置可能存过 runner 不认的值（如 win_ocr），强制回退合法值
    if defaults.get("ocr") not in ("auto", "win", "rapid"):
        defaults["ocr"] = "win"
    # 确认框处置方式：blind=盲点快路（2026-10-13 起的默认），read=读框文字再点（老路）
    if defaults.get("dialog_mode") not in ("blind", "read"):
        defaults["dialog_mode"] = "blind"
    try:
        defaults["wait_dialog"] = min(5.0, max(0.0, float(defaults.get("wait_dialog", 0.85))))
    except (TypeError, ValueError):
        defaults["wait_dialog"] = 0.85
    # 盲点受理等待：2.2 s 与 settle_reads=1 配对实测中位 4.9 s/发（2.5 s 更保守、每发多 0.3 s）
    try:
        defaults["blind_settle"] = min(5.0, max(0.0, float(defaults.get("blind_settle", 2.2))))
    except (TypeError, ValueError):
        defaults["blind_settle"] = 2.2
    return defaults


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ── ADB 工具 ──────────────────────────────────────────────
def run_adb(cfg: dict, *args) -> str:
    try:
        r = subprocess.run(
            [resolve_adb(cfg)] + list(args),
            capture_output=True, text=True, timeout=10,
        )
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return str(e)


# ── 应用主窗口 ────────────────────────────────────────────
# ── 打开即弹的使用声明 ────────────────────────────────────
# 正文（更完整的一版）见项目根 DISCLAIMER.md；这里是与它保持一致的摘要，
# 直接内嵌成常量：打包后 exe 里没有 md 文件，弹窗不能依赖外部文件。
DISCLAIMER_SECONDS = 3
DISCLAIMER_TITLE = "使用声明与免责条款"
DISCLAIMER_TEXT = """【使用声明与免责条款】   v0.2.1 及以后 · 全文见仓库 DISCLAIMER.md

一、使用风险（自行承担）
本工具用 adb 截屏 + OCR 识别「武装派遣」详情页三行装备名，只在某一行不在你的装备池里时
点该行的刷新键；三行都在池内就停手，绝不点「派遣」。自动化操作在线游戏有风险：
  · 可能被判定违规，导致账号警告、封禁、道具被回收；
  · 每次刷新都会真实消耗游戏内货币（默认按 500 券/次记账），识别误差或页面卡顿都可能
    造成并非你本意的刷新与消耗；
  · OCR 在特效、遮挡、动画中途可能读错；坐标按 3200×1440 标定，换机型/分辨率会点偏；
  · 长时间亮屏连点会让手机发热耗电，USB 调试常开也有被滥用的风险。
你是否使用、在哪个账号上使用，完全由你自己决定，并自行承担后果。

二、仅供学习研究，禁止商用
源代码按 MIT License 发布，仅限个人学习、研究与自用；不得出售、出租、收费代练或代刷、
付费分发、捆绑销售，或以本工具引流、广告变现。禁止任何商业用途。
《暗区突围》及其名称、界面、图标、装备名称与素材的一切权利，归其开发商/发行商
（腾讯及其关联公司）所有；本项目与该厂商没有任何关系，未获其授权、认可或赞助。

三、后果自负，作者不担责
本工具按「现状」（AS IS）提供，不附带任何明示或默示担保。使用者使用本工具所产生的
一切后果，由使用者自行承担；作者与贡献者不承担任何责任，包括但不限于账号封禁或限制、
游戏内道具与货币损失、数据丢失、设备损坏、时间损失及任何直接、间接、附带或后果性损害。

请遵守游戏用户协议、平台规则与所在地法律法规。不同意以上条款，请立即关闭并删除本工具。

（本窗口停留 3 秒后才允许关闭。）"""


class DarkzoneApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("暗区突围·武装派遣 — 自动刷新")
        self.geometry("900x700")
        self.minsize(700, 550)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cfg = load_config()
        self.runner_process: subprocess.Popen | None = None
        self._monitor_job: threading.Thread | None = None
        self._running = False
        # 运行中要锁住的设置控件：(控件, 空闲时的 state)，见 _lock / _set_running
        self._setting_widgets: list[tuple[tk.Widget, str]] = []

        self._build_ui()
        self._show_disclaimer()          # 打开即弹：3 秒内关不掉
        self._refresh_device_status()

    # ── UI 构建 ──────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # 顶部 — 设备状态
        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        self._build_top_bar(top)

        # 中部 — 设置面板
        self.settings_frame = ttk.LabelFrame(self, text="配置", padding=8)
        self.settings_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self._build_settings(self.settings_frame)

        # 底部 — 日志 + 控制
        bottom = ttk.Frame(self, padding=8)
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.rowconfigure(0, weight=1)
        bottom.columnconfigure(0, weight=1)
        self._build_console(bottom)

    # ── 使用声明弹窗 ─────────────────────────────────────
    def _show_disclaimer(self):
        """打开程序先弹声明：前 3 秒关不掉（按钮禁用，右上角 × 与 Esc 都无效）。"""
        dlg = tk.Toplevel(self)
        dlg.title(DISCLAIMER_TITLE)
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._disclaimer_try_close())
        dlg.bind("<Escape>", lambda _e: self._disclaimer_try_close())

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=DISCLAIMER_TITLE,
                  font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")

        text = tk.Text(body, width=78, height=21, wrap=tk.WORD,
                       font=("Microsoft YaHei", 9), bg="#1e1e1e", fg="#d4d4d4",
                       relief=tk.FLAT, padx=8, pady=8)
        text.insert("1.0", DISCLAIMER_TEXT)
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, pady=(8, 8))

        self._disc_hint = ttk.Label(body, text="", foreground="#c04000")
        self._disc_hint.pack(anchor="w")
        self._disc_btn = ttk.Button(body, text="", state=tk.DISABLED,
                                    command=self._disclaimer_try_close)
        self._disc_btn.pack(anchor="e", pady=(8, 0))

        self._disc_dlg = dlg
        self._disc_deadline = time.monotonic() + DISCLAIMER_SECONDS
        self._disc_closable = False
        self._disclaimer_tick()
        dlg.grab_set()          # 模态：主窗口在声明关掉之前点不动
        dlg.focus_force()
        self.wait_window(dlg)

    def _disclaimer_tick(self):
        """每 200 ms 刷一次倒计时；到点才把按钮放出来、才允许关窗。"""
        if not getattr(self, "_disc_dlg", None) or not self._disc_dlg.winfo_exists():
            return
        left = self._disc_deadline - time.monotonic()
        if left > 0:
            self._disc_btn.config(text=f"请阅读声明（{int(left) + 1} 秒后可关闭）")
            self._disc_hint.config(
                text="声明需要停留 3 秒：这期间「关闭」按钮与右上角 ×、Esc 都不生效。",
                foreground="#c04000")
            self.after(200, self._disclaimer_tick)
        else:
            self._disc_closable = True
            self._disc_btn.config(state=tk.NORMAL, text="我已阅读并同意，继续使用")
            self._disc_hint.config(
                text="点击上面的按钮（或关闭本窗口）即表示你已阅读并接受以上条款。",
                foreground="#2e7d32")

    def _disclaimer_try_close(self):
        """未满 3 秒不放行；满了就关窗继续。"""
        if not self._disc_closable:
            self._disc_hint.config(text="还不能关闭：请先阅读满 3 秒。", foreground="#c04000")
            return
        dlg = getattr(self, "_disc_dlg", None)
        if dlg and dlg.winfo_exists():
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

    def _build_top_bar(self, parent):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="设备状态:").grid(row=0, column=0, sticky="w")
        self.device_label = ttk.Label(parent, text="检测中…", foreground="gray")
        self.device_label.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(parent, text="🔄 刷新设备", command=self._refresh_device_status).grid(
            row=0, column=2, sticky="e", padx=4
        )
        ttk.Label(parent, text="💡 连接手机 → 打开武装派遣界面 → 启动").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

    def _lock(self, widget: tk.Widget, normal: str = tk.NORMAL):
        """登记一个「运行中要禁用」的设置控件；normal=空闲时恢复成什么 state。"""
        self._setting_widgets.append((widget, normal))
        return widget

    def _build_settings(self, parent):
        # 第0行: 规则文件
        ttk.Label(parent, text="规则 Excel:").grid(row=0, column=0, sticky="w")
        self.rules_var = tk.StringVar(value=self.cfg["rules_xlsx"])
        self._lock(ttk.Entry(parent, textvariable=self.rules_var, width=55)).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        self._lock(ttk.Button(parent, text="浏览…", command=self._browse_rules)).grid(
            row=0, column=2
        )

        # 第1行: OCR / 档位 / 上限
        row1 = ttk.Frame(parent)
        row1.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Label(row1, text="OCR 引擎:").pack(side=tk.LEFT)
        self.ocr_var = tk.StringVar(value=self.cfg["ocr"])
        self._lock(ttk.Combobox(row1, textvariable=self.ocr_var, values=["auto", "win", "rapid"],
                                width=10, state="readonly"), "readonly").pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="  刷新上限:").pack(side=tk.LEFT)
        self.limit_var = tk.IntVar(value=self.cfg["limit"])
        self._lock(ttk.Spinbox(row1, textvariable=self.limit_var, from_=1, to=200, width=5)).pack(
            side=tk.LEFT, padx=4
        )

        # 档位：界面上隐藏了。它从来没有传进 runner（命令行里没有 --profile，
        # 由 runner 自己按 profiles/ 里的默认档位加载），留着这个变量只是让
        # app_config.json 里的键还在、值不被清掉。
        self.profile_var = tk.StringVar(value=self.cfg["profile"])

        # 确认框处置：blind=盲点快路（默认，不读弹窗那屏、按坐标点「确定」后再复核一屏）
        lbl_dialog = ttk.Label(row1, text="  确认框:")
        lbl_dialog.pack(side=tk.LEFT)
        self.dialog_mode_var = tk.StringVar(value=self.cfg["dialog_mode"])
        cbo_dialog = ttk.Combobox(row1, textvariable=self.dialog_mode_var, values=["blind", "read"],
                                  width=6, state="readonly")
        self._lock(cbo_dialog, "readonly")
        cbo_dialog.pack(side=tk.LEFT, padx=4)

        lbl_wait = ttk.Label(row1, text="  等框(s):")
        lbl_wait.pack(side=tk.LEFT)
        self.wait_dialog_var = tk.DoubleVar(value=self.cfg["wait_dialog"])
        spn_wait = ttk.Spinbox(row1, textvariable=self.wait_dialog_var, from_=0.0, to=5.0,
                               increment=0.05, width=5)
        self._lock(spn_wait)
        spn_wait.pack(side=tk.LEFT, padx=4)

        # 盲点点完「确定」后的受理等待：2.2 s = 真机实测「点完到框消失 1.8 s」+ 余量
        lbl_settle = ttk.Label(row1, text="  受理等(s):")
        lbl_settle.pack(side=tk.LEFT)
        self.blind_settle_var = tk.DoubleVar(value=self.cfg["blind_settle"])
        spn_settle = ttk.Spinbox(row1, textvariable=self.blind_settle_var, from_=0.0, to=5.0,
                                 increment=0.1, width=5)
        self._lock(spn_settle)
        spn_settle.pack(side=tk.LEFT, padx=4)

        # 鼠标移上去给这三项讲清楚它到底在等什么（label 和控件都挂）
        for widget, tip in (
            (lbl_dialog, TIP_DIALOG_MODE), (cbo_dialog, TIP_DIALOG_MODE),
            (lbl_wait, TIP_WAIT_DIALOG), (spn_wait, TIP_WAIT_DIALOG),
            (lbl_settle, TIP_BLIND_SETTLE), (spn_settle, TIP_BLIND_SETTLE),
        ):
            _Tooltip(widget, tip)

        self.dry_first_var = tk.BooleanVar(value=bool(self.cfg.get("dry_run_first", True)))

        self._lock(ttk.Checkbutton(row1, text="安全预演优先", variable=self.dry_first_var)).pack(
            side=tk.LEFT, padx=12
        )

        # 第2/3行原本是 ADB / FFmpeg / Python 解释器路径输入框，2026-10-14 起界面上隐藏。
        # 理由：这三项都不需要日常改动 ——
        #   · ADB：GUI 从不把它传给 runner（runner 自己按 ADB_CANDIDATES → PATH 找），本地只用于
        #     设备状态和启动前预检，而 resolve_adb() 已经会自己找到（PATH → scrcpy/Escrcpy → platform-tools）；
        #   · FFmpeg：GUI 也从不传给 runner（runner 按 DARKZONE_FFMPEG → PATH → 剪映自带找）；
        #   · Python：resolve_python() 会自动用 DSH_PYTHON 或 PATH 里的 python。
        # 变量留着，_persist_config() 照旧把它们写回 app_config.json —— 键还在，
        # 需要手改路径时直接编辑 app_config.json 仍然生效。
        self.adb_var = tk.StringVar(value=self.cfg["adb_path"])
        self.ffmpeg_var = tk.StringVar(value=self.cfg["ffmpeg_path"])
        self.python_var = tk.StringVar(value=self.cfg.get("python_path", DSH_PYTHON))

    def _build_console(self, parent):
        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="运行日志", padding=4)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        # 控制按钮
        ctrl = ttk.Frame(parent, padding=(0, 6, 0, 0))
        ctrl.grid(row=1, column=0, sticky="ew")

        self.btn_dry = ttk.Button(ctrl, text="🔍 安全预演 (Dry Run)", command=lambda: self._start("dry"))
        self.btn_dry.pack(side=tk.LEFT, padx=2)
        self.btn_live = ttk.Button(ctrl, text="▶️ 真机运行 (Live)", command=lambda: self._start("live"))
        self.btn_live.pack(side=tk.LEFT, padx=2)
        self.btn_stop = ttk.Button(ctrl, text="⏹ 停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        self.btn_clear = ttk.Button(ctrl, text="清空日志", command=lambda: self.log.delete("1.0", tk.END))
        self.btn_clear.pack(side=tk.RIGHT, padx=2)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(parent, textvariable=self.status_var, foreground="gray").grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )

    # ── 设备 ─────────────────────────────────────────────
    def _list_profiles(self) -> list[str]:
        profiles_dir = BUNDLE / "profiles"
        if profiles_dir.is_dir():
            return [p.stem for p in profiles_dir.glob("*.json")]
        return ["default"]

    def _refresh_device_status(self):
        out = run_adb(self.cfg, "devices")
        lines = [l for l in out.splitlines() if l.strip() and "List of devices" not in l]
        if lines:
            self.device_label.config(text="✅ " + ", ".join(lines), foreground="green")
        else:
            self.device_label.config(text="❌ 未检测到设备", foreground="red")

    def _device_online(self) -> bool:
        """adb 里有没有处于 device 状态的设备（unauthorized / offline / 没插线都不算）。"""
        for line in run_adb(self.cfg, "devices").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
        return False

    def _browse_rules(self):
        path = filedialog.askopenfilename(
            title="选择规则 Excel 文件",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
        )
        if path:
            self.rules_var.set(path)

    def _browse_python(self):
        """界面上的「浏览…」按钮已随 Python 输入框一起隐藏，这里留着以备将来做「高级设置」面板。"""
        path = filedialog.askopenfilename(
            title="选择 Python 解释器",
            filetypes=[("Python", "python.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.python_var.set(path)

    # ── 保存配置 ─────────────────────────────────────────
    def _persist_config(self):
        self.cfg.update(
            rules_xlsx=self.rules_var.get(),
            profile=self.profile_var.get(),
            ocr=self.ocr_var.get(),
            limit=self.limit_var.get(),
            dialog_mode=self.dialog_mode_var.get(),
            wait_dialog=float(self.wait_dialog_var.get()),
            blind_settle=float(self.blind_settle_var.get()),
            dry_run_first=bool(self.dry_first_var.get()),
            adb_path=self.adb_var.get(),
            ffmpeg_path=self.ffmpeg_var.get(),
            python_path=self.python_var.get(),
        )
        save_config(self.cfg)

    # ── 启动 / 停止 ──────────────────────────────────────
    def _start(self, mode: str):
        self._persist_config()
        xlsx = self.rules_var.get()
        if not os.path.isfile(xlsx):
            messagebox.showerror("错误", f"规则文件不存在:\n{xlsx}")
            return

        # 没手机就别启动：日志里说清原因，然后自己退出（不占着界面空等 60 秒）
        if not self._device_online():
            self.log.delete("1.0", tk.END)
            self._log(f"══════ {datetime.now():%H:%M:%S} 未启动 mode={mode} ══════\n")
            self._log("❌ 未连接手机：adb 里没有处于 device 状态的设备，本次已自动停止。\n")
            self._log("   插好数据线 → 手机上点「允许 USB 调试」→ 回这里点「🔄 刷新设备」看到变绿，再启动。\n")
            self._refresh_device_status()
            self.status_var.set("未连接手机，已自动停止")
            return

        # 命令：用真实 python 跑 darkzone.runner（打成 exe 后 sys.executable 是 exe 本身，不能跑 -m）
        py = resolve_python(self.cfg)
        proj = resolve_project_dir(self.cfg)
        cmd = [
            py, "-m", "darkzone.runner",
            "--rules", xlsx,
            "--ocr", self.ocr_var.get(),
            "--limit", str(self.limit_var.get()),
            "--dialog-mode", self.dialog_mode_var.get(),
            "--wait-dialog", str(float(self.wait_dialog_var.get())),
            "--blind-settle", str(float(self.blind_settle_var.get())),
            # 启动瞬间没设备就别让它自己等 60 秒：5 秒等不到就退出（退出码 3），界面立刻报「未连接手机」
            "--wait-device", "5",
        ]
        if mode == "live":
            cmd.append("--live")

        # 让子进程找到 darkzone 包：源码运行在 ROOT，冻结运行时在 _internal(BUNDLE)。
        # PYTHONPATH 同时带上这两个候选，谁有 darkzone 谁生效。
        env = dict(os.environ)
        pythonpath = os.pathsep.join(
            p for p in (str(proj), str(BUNDLE)) if p and p not in env.get("PYTHONPATH", "")
        )
        if pythonpath:
            env["PYTHONPATH"] = (env.get("PYTHONPATH", "") + os.pathsep + pythonpath).strip(os.pathsep)
        # 子进程的 stdout 走管道，不是控制台：不指定的话 Windows 上按 ANSI 代码页(936/GBK)编码，
        # 而这里用 encoding="utf-8" 读回来 → 中文全是乱码（mojibake）。只固定流编码，不动文件默认编码。
        env["PYTHONIOENCODING"] = "utf-8"

        self._set_running(True)
        self.log.delete("1.0", tk.END)
        self._log(f"══════ 启动 {datetime.now():%H:%M:%S} mode={mode} ══════\n")
        self._log(f"命令: {' '.join(cmd)}\n\n")

        try:
            self.runner_process = subprocess.Popen(
                cmd, cwd=str(app_dir()),
                env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
        except Exception as e:
            self._log(f"❌ 启动失败: {e}\n")
            self._set_running(False)
            return

        self._monitor_job = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_job.start()

    def _stop(self):
        if self.runner_process and self.runner_process.poll() is None:
            self.runner_process.terminate()
            self._log("\n⏹ 用户手动停止\n")
        self._set_running(False)

    def _monitor(self):
        proc = self.runner_process
        for line in proc.stdout:
            self._log(line)
        rc = proc.wait()
        self.after(0, lambda: self._on_finish(rc))

    def _on_finish(self, returncode: int):
        self._set_running(False)
        if returncode == 0:
            self._log("\n✅ 程序正常结束\n")
            self.status_var.set("完成")
        elif returncode == 3:
            # runner 的约定：3 = 等不到设备
            self._log("\n❌ 未连接手机：等不到设备，程序已自动停止（没花券、没点「派遣」）。\n")
            self._log("   插好数据线、在手机上允许 USB 调试，然后点「🔄 刷新设备」确认变绿再启动。\n")
            self._refresh_device_status()
            self.status_var.set("未连接手机，已自动停止")
        else:
            self._log(f"\n⚠️ 程序退出 (code={returncode})\n")
            self.status_var.set(f"已退出 (code={returncode})")

    def _set_running(self, running: bool):
        self._running = running
        state = tk.DISABLED if running else tk.NORMAL
        self.btn_dry.config(state=state)
        self.btn_live.config(state=state)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
        # 运行中锁住配置：命令行在启动那一刻就固化了，中途改这些值不会影响正在跑的那一次，
        # 只会让人误以为改了就生效 —— 所以锁掉，并把面板标题和状态栏写清楚。
        for widget, normal in self._setting_widgets:
            try:
                widget.config(state=tk.DISABLED if running else normal)
            except tk.TclError:
                pass
        if hasattr(self, "settings_frame"):
            self.settings_frame.config(text="配置（运行中已锁定）" if running else "配置")
        self.status_var.set("运行中…（设置已锁定）" if running else "就绪")

    def _log(self, text: str):
        def _append():
            self.log.insert(tk.END, text)
            self.log.see(tk.END)
        self.after(0, _append)

    def _on_close(self):
        if self._running:
            if messagebox.askyesno("确认", "程序正在运行中，确定要退出吗？"):
                self._stop()
                time.sleep(0.3)
        self.destroy()


# ── 入口 ─────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(str(app_dir()))
    app = DarkzoneApp()
    app.mainloop()