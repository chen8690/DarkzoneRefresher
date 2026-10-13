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
        "blind_settle": 1.8,
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
    # 盲点受理等待：点完「确定」到框消失恒定约 1.8 s（真机探针），调小会读成中间态
    try:
        defaults["blind_settle"] = min(5.0, max(0.0, float(defaults.get("blind_settle", 1.8))))
    except (TypeError, ValueError):
        defaults["blind_settle"] = 1.8
    return defaults


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ── ADB 工具 ──────────────────────────────────────────────
def run_adb(cfg: dict, *args) -> str:
    try:
        r = subprocess.run(
            [cfg["adb_path"]] + list(args),
            capture_output=True, text=True, timeout=10,
        )
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return str(e)


# ── 应用主窗口 ────────────────────────────────────────────
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

        self._build_ui()
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
        settings = ttk.LabelFrame(self, text="配置", padding=8)
        settings.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self._build_settings(settings)

        # 底部 — 日志 + 控制
        bottom = ttk.Frame(self, padding=8)
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.rowconfigure(0, weight=1)
        bottom.columnconfigure(0, weight=1)
        self._build_console(bottom)

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

    def _build_settings(self, parent):
        # 第0行: 规则文件
        ttk.Label(parent, text="规则 Excel:").grid(row=0, column=0, sticky="w")
        self.rules_var = tk.StringVar(value=self.cfg["rules_xlsx"])
        ttk.Entry(parent, textvariable=self.rules_var, width=55).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        ttk.Button(parent, text="浏览…", command=self._browse_rules).grid(row=0, column=2)

        # 第1行: OCR / 档位 / 上限
        row1 = ttk.Frame(parent)
        row1.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Label(row1, text="OCR 引擎:").pack(side=tk.LEFT)
        self.ocr_var = tk.StringVar(value=self.cfg["ocr"])
        ttk.Combobox(row1, textvariable=self.ocr_var, values=["auto", "win", "rapid"],
                     width=10, state="readonly").pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="  刷新上限:").pack(side=tk.LEFT)
        self.limit_var = tk.IntVar(value=self.cfg["limit"])
        ttk.Spinbox(row1, textvariable=self.limit_var, from_=1, to=200, width=5).pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="  档位:").pack(side=tk.LEFT)
        self.profile_var = tk.StringVar(value=self.cfg["profile"])
        ttk.Combobox(row1, textvariable=self.profile_var, values=self._list_profiles(),
                     width=10).pack(side=tk.LEFT, padx=4)

        # 确认框处置：blind=盲点快路（默认，不读弹窗那屏、按坐标点「确定」后再复核一屏）
        ttk.Label(row1, text="  确认框:").pack(side=tk.LEFT)
        self.dialog_mode_var = tk.StringVar(value=self.cfg["dialog_mode"])
        ttk.Combobox(row1, textvariable=self.dialog_mode_var, values=["blind", "read"],
                     width=6, state="readonly").pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="  等框(s):").pack(side=tk.LEFT)
        self.wait_dialog_var = tk.DoubleVar(value=self.cfg["wait_dialog"])
        ttk.Spinbox(row1, textvariable=self.wait_dialog_var, from_=0.0, to=5.0,
                    increment=0.05, width=5).pack(side=tk.LEFT, padx=4)

        # 盲点点完「确定」后的受理等待：1.8 s = 真机探针量到的「点完到框消失」
        ttk.Label(row1, text="  受理等(s):").pack(side=tk.LEFT)
        self.blind_settle_var = tk.DoubleVar(value=self.cfg["blind_settle"])
        ttk.Spinbox(row1, textvariable=self.blind_settle_var, from_=0.0, to=5.0,
                    increment=0.1, width=5).pack(side=tk.LEFT, padx=4)

        self.dry_first_var = tk.BooleanVar(value=bool(self.cfg.get("dry_run_first", True)))

        ttk.Checkbutton(row1, text="安全预演优先", variable=self.dry_first_var).pack(
            side=tk.LEFT, padx=12
        )

        # 第2行: adb / ffmpeg 路径
        row2 = ttk.Frame(parent)
        row2.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(row2, text="ADB:").pack(side=tk.LEFT)
        self.adb_var = tk.StringVar(value=self.cfg["adb_path"])
        ttk.Entry(row2, textvariable=self.adb_var, width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="FFmpeg:").pack(side=tk.LEFT, padx=(12, 0))
        self.ffmpeg_var = tk.StringVar(value=self.cfg["ffmpeg_path"])
        ttk.Entry(row2, textvariable=self.ffmpeg_var, width=18).pack(side=tk.LEFT, padx=4)

        # 第3行: Python 解释器路径（打包后必需，真机运行时用它跑 darkzone.runner）
        row3 = ttk.Frame(parent)
        row3.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(row3, text="Python:").pack(side=tk.LEFT)
        self.python_var = tk.StringVar(value=self.cfg.get("python_path", DSH_PYTHON))
        ttk.Entry(row3, textvariable=self.python_var, width=55).pack(side=tk.LEFT, padx=4)
        ttk.Button(row3, text="浏览…", command=self._browse_python).pack(side=tk.LEFT)

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

    def _browse_rules(self):
        path = filedialog.askopenfilename(
            title="选择规则 Excel 文件",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
        )
        if path:
            self.rules_var.set(path)

    def _browse_python(self):
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

        # 检查 ADB
        if mode == "live":
            dev = run_adb(self.cfg, "devices")
            if "device" not in dev:
                if not messagebox.askyesno("警告", "未检测到 ADB 设备！\n\n确定要继续吗？"):
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
        else:
            self._log(f"\n⚠️ 程序退出 (code={returncode})\n")
            self.status_var.set(f"已退出 (code={returncode})")

    def _set_running(self, running: bool):
        self._running = running
        state = tk.DISABLED if running else tk.NORMAL
        self.btn_dry.config(state=state)
        self.btn_live.config(state=state)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中…" if running else "就绪")

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