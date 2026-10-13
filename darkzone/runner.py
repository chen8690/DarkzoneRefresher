"""刷新主循环 v2：两段式。

    界面1（列表）：**默认完全不碰**（detail_only=True，用户在手机上自己点「选择」进详情页）。
                   只有显式加 --allow-list 才走旧流程：检查「暗区首领」是否命中排除名单 → 是就点
                   该栏 ↻ → 直到出现可刷的首领 → 点该栏「选择」进入详情。
    界面2（详情）：规则块的每个槽位对应一行装备 → 该行已在池内就跳过 → 不在就点该行 ↻
                   → 命中即停该行、转下一行；三行都命中 → 停手，**不点派遣**（用户自己点）

（用户 2026-10-11 的决定：「暗区首领那一栏彻底不管」「这条刷完就停手，我手动点派遣」——
 所以默认模式下不刷首领、不判排除名单、不点「选择」，也不去找「下一条刷新项」。）

安全阀：
  1. dry_run 默认 True：只读屏、算坐标、打印计划，一次都不点；
  2. 每次点击前后都落截图（logs/ev_*.png）+ logs/events.jsonl 事件流；
  3. 「点了之后全屏文本没任何变化」连续 stale_limit 次 → 立即报错停止（不硬刷）；
  4. tap_sweep：点 ↻ 没反应时，按 profiles 里的偏移量扫描附近坐标重试（真机比例未核实时兜底）；
  5. auto_confirm：点 ↻ 后游戏会先弹「提示 / 是否刷新特殊条件：xxx / 确定」模态框，只有文本含
     「刷新」的弹窗才自动点「确定」（派遣/购买/删除这类一律拒绝并报错）；这种「点了确定」算生效，
     不会因为词条没变被误判成 no_change 去扫描重试（那会白花券）；
  6. 每行刷新次数上限可调，默认 40（refresh_limit_min/max 夹紧）；
  7. detail_only：当前不是详情页（或屏上有弹窗）就一步都不走，直接报错让用户手动进页面；
  8. 行锚定 + 行数守卫：详情页三行按「点击装备」反推锚定，行数不等于槽位数就停手，绝不猜着点。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 中文 Windows 控制台默认 GBK，而本文件的帮助文本里有 ↻（U+21BB）：
# `--help` 会在 argparse 打印时抛 UnicodeEncodeError，日志/摘要同理。
# errors="replace" 让打不出的字符退化成 ?，绝不因为输出编码把整次运行打断。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

from dataclasses import dataclass
from pathlib import Path

from .ocr_match import canon, similarity
from .rules import default_rules_path, parse_workbook, validate
from .ui import (DIALOG_FORBID_RE, CoordMapper, boss_excluded, dialog_is_refresh,
                 load_profile, parse_screen)
from .win_ocr import ocr_lines as win_ocr_lines
from .win_ocr import worker_status as win_ocr_worker_status

try:                                        # RapidOCR 装在项目 .venv 里，可能没装
    from .rapid_ocr import available as rapid_ocr_available
    from .rapid_ocr import ocr_lines as rapid_ocr_lines
except Exception:                           # noqa: BLE001
    rapid_ocr_lines = None

    def rapid_ocr_available():
        return False

# 本工具版本号；发布 tag 用 v0.1.0，命令行 --version 读它
__version__ = "0.1.0"

# 默认 OCR 后端；设 DARKZONE_OCR=rapid 可以让整套（含离线套件）跑在 RapidOCR 下
DEFAULT_OCR = os.environ.get("DARKZONE_OCR", "auto")

CONFIRM_WORDS = ("确认", "确定", "是")

# 「还是同一件装备吗」的相似度门槛（canon 归一再比）。真机 30 次刷新的实测分布
# （`logs\_timeline_round.txt` / README 事实 22）：同一件装备的两种噪声写法 ≥0.842
# （0.933/0.917/0.875/0.875/0.875/0.842），真换件 ≤0.462 → 0.72 落在两簇中间的空档里。
ROW_SAME_MIN = 0.72


def parse_cost(cost_raw):
    """把界面上的刷新花费文本变成券数：「免费」/空 → 0，含数字就取数字（"15000"→15000）。"""
    if not cost_raw:
        return 0
    s = str(cost_raw)
    if "免费" in s:
        return 0
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


class SafetyError(RuntimeError):
    """安全阀触发：绝不允许因为坐标算错而点到「派遣」。"""


@dataclass
class Config:
    dry_run: bool = True
    block_index: int = 0
    max_refresh_per_row: int = 40
    refresh_limit_min: int = 1
    refresh_limit_max: int = 200
    wait_after_tap: float = 0.25    # 点击后固定等的部分（2026-10-11 从 0.6/1.2 降下来加速，
    wait_after_screen: float = 0.55  # 降速有风险：读早了会多读几次重试，见 read_retries）
    read_retries: int = 8           # 读到的画面和点之前一样时，先多读几次（游戏/服务器要时间），
    read_retry_delay: float = 0.7   # 再判定「点了没变化」——否则会误判去扫描重试、白花券。
                                    # 上限 8×0.7≈5.6 s，画面一变就立刻停，所以平均不慢。
    capture: str = "auto"           # auto=先视频流、起不来再 gzip、再 raw、最后 PNG
                                    # （用户 2026-10-11 定：流路读一帧 31 ms、gzip 1.15 s）；
                                    # stream=只要流（同样会退 gzip）；gzip=设备端压完再传；png=老路
    settle_reads: int = 0           # 画面变了之后，再连读几次确认「签名稳定」才算数：
                                    # 视频流取帧有 ~0.35 s 延迟，不设这层会读到「券已扣、
                                    # 词条还是旧的」动画中间帧 → 误判点空了 → 再点一次 ↻ 白花 500 券。
                                    # 0 = 自动（实际走流路取 2 次，gzip/raw/png 路不额外读）。
    settle_delay: float = 0.15      # 每次稳定性复读之间等多久
    settle_guard: float = 0.9       # 点完「确定」之后、**信任任何一帧之前**至少等这么久。
                                    # 为什么必须等：真机顺序是「顶栏先扣券 → 新词条过一会儿才画
                                    # 出来」，而流路读一帧只要 0.01 s，于是点完确定立刻读到的还是
                                    # **刷新前的旧画面**。旧画面是噪声读法、和点前的文本不一样，
                                    # 所以「签名变了」和「settle 两帧稳定」都会被骗过 →
                                    # 把旧词条当成刷新结果（事故 live_stream_1：槽1 把已经在池内的
                                    # `AUG突击步枪` 读成 `AIJG突击步枪` 判成池外、白刷 500 券，
                                    # 刷完又把旧帧读回 `AUG突击步枪` 误报命中；而真正抽到的是
                                    # `AN94突击步枪`，直到下一次预演才露出来）。
                                    # 最坏的后果不是白花券，而是「池外的行被旧帧判成池内 → 报可以
                                    # 派遣」。0 = 关（老行为，留给离线测试与 gzip 慢路）。
                                    # 实测噪声读法与真换件的相似度不可分离（间隙 0.000），
                                    # 所以只能用时间，不能用文本比对 —— 见 README。
    settle_wait_change: float = 3.0  # 刷完继续重读，直到**这一行的名字真的变了**（0 = 关，老行为）。
                                    # 为什么：真机跑（logs\run_20261011_182111.json）83 次刷新里
                                    # 有 **21 次 = 25.3%**「刷完读到的名字」和「刷卡前」一模一样
                                    # （槽2 护甲真机出现过 62 种，随机重名只该 ~1.6%）。
                                    # 2026-10-11 18:30 的定向实测（logs\land_probe3_out.txt）把
                                    # 时序量清楚了：点 ↻ → **确认框 0.8~2.6 s 后才画出来** →
                                    # 点「确定」→ 新词条 **0.7 s 就落屏**。所以那 25.3% 的大头不是
                                    # 「游戏画得慢」，而是**确认框晚到**被整屏签名噪声误判成「已生效」、
                                    # 于是「记了 500 券而游戏没抽」（行级判据见 _tap_progressed）。
                                    # 这一层留着兜「确认框已点、词条还没重画」的那一帧。
                                    # 旧→旧只是白刷（本来就要刷），**真正危险的是「真抽中了却读到旧
                                    # 名字」→ 当成没中再刷一次 → 把命中刷没了**（每次 500 券）。
                                    # 流路读一帧 0.01 s，只有那 25% 会真的等到这个上限。
    confirm_reads: int = 0          # 花券之前再确认：连续读到几次「这一行不在池内」才允许点 ↻。
                                    # 为什么需要：视频流帧会把短字母核心糊掉（真机实测
                                    # `AUG突击步枪` → `AIJG突击步枪`），模糊匹配对 3 个字母的核心
                                    # 容不下这种错，于是「本来就在池内」被读成「不在池内」→
                                    # 白点一次 ↻ 花 500 券（live_stream_1 槽1 就这么被刷掉的）。
                                    # 多读一帧在流路上只要 0.01 s，所以 0 = 自动（实际走流路取 2，
                                    # 其余路取 1 = 只看一次，老行为）。
                                    # 注意这跟 settle_reads 不是一回事：settle 管「画面稳没稳」，
                                    # confirm 管「结论可不可信」。
    dialog_mode: str = "blind"      # 点完 ↻ 之后那个「是否刷新特殊条件」确认框怎么点：
                                    #   "blind"（默认，= 旧的 --fast-confirm 盲点快路）：不读那一屏
                                    #     弹窗，先等 `wait_dialog` 让框画出来，再按 profile 里量好的
                                    #     「确定」坐标点一下，然后才读屏看结果。一次刷新少读一屏。
                                    #     「确定」是照像素位置点的，不是按框文字点的；位置错了最多
                                    #     白点一次空（读数落回重读网），不会去确认别的按钮。
                                    #   "read"：老行为——先读屏、只放行含「刷新」的框再点它的热区；
                                    #     只读一屏、且拒绝任何非刷新弹窗（对照/保守用）。
    wait_dialog: float = 0.85       # 盲点「确定」前等确认框画出来的秒数。
                                    # ⚠️ 2026-10-13 用真机探针（`logs\_dialog_timing_probe.py`，15 fps
                                    # 连拍 + 点击时刻用「发出/返回中点」量）把这件事量清楚了，
                                    # 推翻了之前「框在屏幕上时点会被吃掉」的结论：
                                    #  · 框**从不吞点击**。offset（相对框出现）从 −0.45 s 一路扫到
                                    #    +2.50 s，再加一组「点 ↻ 后固定等 0.85/1.30/1.75/2.20 s」，
                                    #    共 23 次点击**全部生效**（每次 ↻ 只花 1 笔 500 券、行确实换件）。
                                    #  · 点完到框消失是**恒定约 1.8 s**（23 次里 1.54~2.06 s，均值
                                    #    ≈1.82 s，σ≈0.13 s），跟「什么时候点」无关 —— 这是游戏自己的
                                    #    受理延迟，runner 只能等。
                                    #  · 连拍里框是**渐显**：点 ↻ 后约 0.7~1.5 s 就开始有像素变化，
                                    #    明显跳变在 1.5~1.8 s。
                                    # 意思是：之前的 `blind_missed` 不是「点丢了」，而是点完之后
                                    # **1.8 s 之内复核那一屏必然还看得见框**（它本来就要那么久才消失）。
                                    # 所以这个值调大调小都不影响点击是否生效，只影响盲点那一下
                                    # 落在动画的哪个阶段；0.85 仍在实测窗口内，保持默认。
                                    # 历史数据（四发 live、每发 2 行）blind 的 charge.sec =
                                    # 6.64 / 6.17 / 6.34 / 5.63 / 5.92 / 7.16 / 5.37 / 6.0，
                                    # 对照 read 老路 8.04 / 6.64。
    blind_settle: float = 1.8       # 盲点点完「确定」之后、去读结果之前额外等的秒数。
                                    # 依据：2026-10-13 探针量出「点了确定 → 框消失」恒定约
                                    # 1.8 s（23 次 1.54~2.06 s，均值 1.82 s、σ0.13 s），这是
                                    # 游戏自己的受理延迟；1.8 s 一到，框没了、新词条也落屏了。
                                    # 2026-10-13 的 5 次真机实测（10 发刷新）里，旧写法（只等
                                    # 0.8 s 就读）**10 发全部**落到 `blind_unclear` → 再等一次
                                    # `wait_change` → 再走 1~2 次 `read_retry`（每次 +0.7 s），
                                    # 其中 6 发根本不需要再点「确定」（框早自己没了）。等满
                                    # 1.8 s 再读，这一屏就该是「框没了 + 新词条已在」的最终态。
                                    # 记账不变量不变：名字真变了才算这一下抽了（`_row_changed`，
                                    # 噪声写法 ≥0.842 不算换件），预算用完仍是旧名 → 不记账。
    flush_pending: bool = True      # 收尾补确认（默认开）：一轮的**最后一次** ↻，确认框常在跑完之后
                                    # 才画出来。真机实测（logs\live_guard_slot2_1.txt 之后的启动
                                    # abort）：那轮结束后手机上还挂着「是否刷新特殊条件：SEK堡垒
                                    # 防弹衣」—— charge 已经记了、行却没提交、下一轮启动直接被模态框
                                    # 挡住（要人来点）。所以出报告之前多读一屏：挂着刷新确认框就补点
                                    # 「确定」（这笔刷新本来就点过、钱也记了）并重判那一行；
                                    # 不是刷新框就只记一笔，交给人工。关掉 = 老行为（离线对照用）。
    stale_limit: int = 3
    tap_sweep: bool = True
    auto_confirm: bool = True
    stop_before_dispatch: bool = True
    row_refresh_cost: int = 500   # 真机实测：详情页每点一次某行的 ↻ 花 500 券
    detail_only: bool = False     # True：只认「当前打开的详情页」，列表页/暗区首领栏一概不读不点
                                  # （CLI 默认就是 True，要跑旧的列表流程得显式 --allow-list）
    evidence: bool = True
    log_dir: str = "logs"
    work_dir: str = "logs"
    min_score: float = 0.62
    profile: str | None = None
    device_size: tuple | None = None
    lang: str = "zh-Hans-CN"
    ocr: str = DEFAULT_OCR          # auto/win=Windows 自带 OCR（快）；rapid=项目 .venv 里的 RapidOCR
    fast_confirm: bool | None = None   # 【旧字段，别再用】2026-10-11 深夜确认框改成盲点快路之后，
                                    # 开关换成 `dialog_mode`（"blind"/"read"）。这个字段只为兼容
                                    # 还传 `Config(fast_confirm=True)` 的旧脚本/旧套件而保留：
                                    # True → dialog_mode="blind"，None/False → 保持 dialog_mode 原值
                                    # （默认本来就是 blind，要 read 请显式写 dialog_mode="read"）。

    def limit(self) -> int:
        return max(self.refresh_limit_min, min(self.refresh_limit_max, int(self.max_refresh_per_row)))

    def __post_init__(self):
        """把旧字段折到新开关上，并校验 `dialog_mode`。

        2026-10-11 深夜：确认框默认改成盲点快路，开关从 `fast_confirm: bool` 换成
        `dialog_mode: str`。为不打断还传 `fast_confirm=True` 的旧脚本/旧套件，这里把它
        折成 `dialog_mode="blind"`；`False`/`None` **不**把模式改回 "read"（默认就是 blind，
        要 read 就显式写 `dialog_mode="read"` 或 `--dialog-mode read`）。
        """
        legacy = getattr(self, "fast_confirm", None)
        if legacy:                      # 只认 True：False/None 不改 dialog_mode（默认就是 blind）
            self.dialog_mode = "blind"
        self.fast_confirm = bool(legacy)
        if (self.dialog_mode or "").lower() not in ("blind", "read"):
            raise ValueError(f"dialog_mode 只能是 blind/read，收到 {self.dialog_mode!r}")
        self.dialog_mode = (self.dialog_mode or "blind").lower()


class Runner:
    def __init__(self, book, source, cfg: Config | None = None):
        self.book = book
        self.source = source
        self.cfg = cfg or Config()
        self.profile = load_profile(self.cfg.profile)
        self.limit = self.cfg.limit()
        self.log_dir = Path(self.cfg.log_dir)
        self.work_dir = Path(self.cfg.work_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.last = None
        self.mapper = None
        self.boss_name = None
        self.boss_refreshes = None
        self.dispatch_xy = None
        self.dispatch_box = None
        self.spent = 0        # 本次跑掉多少券（只有真刷成功的那次才记）
        self._last_refresh_slot = None   # 最近一次点过 ↻ 的槽位（收尾补确认要知道补的是哪一行，
                                         # 见 Config.flush_pending / _flush_pending_dialog）
        # 收尾补确认要能把这笔券补记上：行级判据之下，若那一下 ↻ 的确认框跑完才画出来，
        # `detail_phase` 是看不到「这一行变了」的 → 它不会记账，可钱在点 ↻ 时就花了。
        self._last_refresh_charged = False   # 这一下 ↻ 的券记过没有
        self._last_refresh_item = None       # 刷卡前那一行读到的词条（记账用，和原来一致）
        self._last_refresh_attempt = None
        self.ledger = []      # 每一笔消费的流水，便于事后核对
        self.run_id = time.strftime("%Y%m%d_%H%M%S")
        self.ledger_path = self.log_dir / "task_ledger.jsonl"   # 跨次累计的消费台账
        self.spent_before = self._ledger_total()
        ds = self.cfg.device_size
        self.device_size = tuple(ds) if ds else tuple(source.size())
        self.ocr_name, self.ocr_lines = self._pick_ocr(self.cfg.ocr)

    # ------------------------------------------------- 按实际取屏路自动定「多读几次」
    def _on_stream(self) -> bool:
        """这一次实际是不是走视频流取帧。"""
        return getattr(self.source, "last_chain", None) == "stream"

    def eff_settle_reads(self) -> int:
        """稳定性复读次数：显式配了就用配置；没配（0）时按**实际取屏路**自动定。

        为什么按实际路而不是按 `--capture`：`auto` 现在优先视频流，但没 ffmpeg 的机器
        （或流起不来）会自动退到 gzip——那时读一帧 1.4 秒，再复读几帧纯属浪费；
        流路读一帧 0.01 秒，多读几乎免费，所以只有真在流上才自动开。
        """
        if self.cfg.settle_reads > 0:
            return int(self.cfg.settle_reads)
        return 2 if self._on_stream() else 0

    def eff_confirm_reads(self) -> int:
        """花券前再确认几次：同上，显式配置优先；没配时只有流路自动取 2（见 Config.confirm_reads）。"""
        if self.cfg.confirm_reads > 0:
            return int(self.cfg.confirm_reads)
        return 2 if self._on_stream() else 1

    # ------------------------------------------------------------------ OCR 后端
    @staticmethod
    def _pick_ocr(name: str):
        """挑 OCR 后端。

        **默认（auto）= Windows 自带 OCR**：真机实测每屏 1.2–1.7 秒，三次假阳性修完之后
        精度够用（用户 2026-10-11 的决定）。RapidOCR 更准（不插空格、`BT101` 这类字母数字
        直接读对）但慢 7 倍（7–11 秒/屏），所以只在**显式** `--ocr rapid` 时才用。
        """
        want = (name or "auto").lower()
        if want == "rapid":
            if rapid_ocr_lines is None or not rapid_ocr_available():
                raise RuntimeError(
                    "指定了 --ocr rapid，但没有可用的 RapidOCR。装法（国内镜像）：\n"
                    "  py -m venv --system-site-packages .venv\n"
                    "  .venv\\Scripts\\python.exe -m pip install rapidocr-onnxruntime "
                    "-i https://pypi.tuna.tsinghua.edu.cn/simple/\n"
                    "然后用 .venv\\Scripts\\python.exe 跑本脚本。"
                )
            return "rapidocr", rapid_ocr_lines
        return "win_ocr", win_ocr_lines

    # ------------------------------------------------------------------ 日志
    def log(self, event, **kw):
        ev = {"t": time.strftime("%H:%M:%S"), "kind": event}
        for k, v in kw.items():
            if isinstance(v, Path):
                v = str(v)
            elif isinstance(v, tuple):
                v = [round(x, 1) for x in v]
            ev[k] = v
        self.events.append(ev)
        line = json.dumps(ev, ensure_ascii=False)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode(), flush=True)
        with open(self.log_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return ev

    # ------------------------------------------------------------------ 花销台账
    def _ledger_total(self):
        """历史累计消费：把 task_ledger.jsonl 里每一笔的金额加起来（坏行忽略）。"""
        total = 0
        if self.ledger_path.exists():
            for ln in self.ledger_path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    total += int(json.loads(ln).get("amount") or 0)
                except Exception:
                    continue
        return total

    def _charge(self, kind, amount, **kw):
        """记一笔券。只有「这次刷新真的生效了」（changed=True，含弹窗点了确定）才调用。

        同时追加到 logs/task_ledger.jsonl：这样下一次跑也能报「累计消费」。
        """
        amount = int(amount or 0)
        self.spent += amount
        item = {"kind": kind, "amount": amount, "total": self.spent}
        item.update(kw)
        self.ledger.append(item)
        # 注意：日志事件的 kind 是 "charge" 本身，所以把券种放到 charge_kind 里，
        # 否则 ev.update(kw) 会把 kind 覆盖成 "row"/"boss"，事件流里就认不出这是记账了。
        self.log("charge", charge_kind=kind, amount=amount, total=self.spent, **kw)
        rec = dict(item, run=self.run_id, t=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:            # 台账写不了也不能影响刷任务
            self.log("warn", phase="cost", msg=f"台账写入失败：{e}")
        return amount

    def cost_summary(self, rows=None):
        """刷新次数 + 消费汇总（给结尾的「任务完成」摘要用）。"""
        rows = list(rows or [])
        spent_row = sum(e["amount"] for e in self.ledger if e["kind"] == "row")
        spent_boss = sum(e["amount"] for e in self.ledger if e["kind"] == "boss")
        return {
            "row_attempts": sum(int(r.get("refreshes") or 0) for r in rows),
            "row_charged": sum(1 for e in self.ledger if e["kind"] == "row"),
            "boss_attempts": self.boss_refreshes,
            "boss_charged": sum(1 for e in self.ledger if e["kind"] == "boss"),
            "unit_row_cost": self.cfg.row_refresh_cost,
            "spent_row": spent_row,
            "spent_boss": spent_boss,
            "spent": self.spent,
            "spent_before": self.spent_before,
            "spent_cumulative": self.spent_before + self.spent,
            "ledger": list(self.ledger),
        }

    # ------------------------------------------------------------------ 读屏
    def _read(self, tag):
        img = self.source.screenshot(tag)
        rows, res = self.ocr_lines(img, lang=self.cfg.lang)
        size = (int(res["width"]), int(res["height"]))
        self.mapper = CoordMapper(size, self._tap_space(size))
        kind, scr = parse_screen(rows, size, self.profile, vocab=self.book.vocab,
                                 min_score=self.cfg.min_score)
        self.dispatch_xy = scr.dispatch if kind == "detail" else None
        self.dispatch_box = getattr(scr, "dispatch_box", None) if kind == "detail" else None
        return {"kind": kind, "screen": scr, "rows": rows, "image": str(img), "size": size,
                "ocr": res.get("engine", self.ocr_name),
                # 这一屏实际是怎么弄到的（stream/gzip/raw/png）：流帧噪声大，
                # 排查「为什么这一行被读错」时先看它是哪条路取来的。
                "chain": getattr(self.source, "last_chain", None)}

    def _tap_space(self, img_size):
        """「真机点击坐标系」用哪个方向。

        坑：`wm size` 报物理竖屏尺寸（1440x3200），但游戏横屏时 framebuffer 和 `input tap`
        的坐标系都是横屏 3200x1440。实测不交换方向时 ↻ 被算成 x=1310（正确 2912），会点到界面外。
        """
        iw, ih = img_size
        dw, dh = self.device_size
        if not dw or not dh:
            return img_size
        if (iw > ih) != (dw > dh):
            return (dh, dw)
        return (dw, dh)

    @staticmethod
    def _sig(st):
        """全屏文本签名：含顶栏资源/计数，所以「真刷新了」和「点空了」能区分开。"""
        return tuple(sorted(r[0] for r in st["rows"]))

    @staticmethod
    def _rows_sig(st):
        """只盯「词条/首领栏」文本（顶栏券数不算）——判断刷新结果到底画出来了没有。

        为什么要单独一个：真机刷新的顺序是「先扣券（顶栏立刻变）→ 再画新词条」，
        视频流取帧还有 ~0.35 s 延迟，所以很容易读到「券已扣、词条还是旧的」中间帧。
        那时**整屏签名已经变了**，但结果并没画出来；拿旧词条去判池，万一新词条其实
        在池内就当没中，白刷 500 券（settle_reads 就是为这个加的）。
        """
        scr = st.get("screen")
        rows = getattr(scr, "rows", None) or []
        if rows and hasattr(rows[0], "raw"):
            return tuple((r.index, r.raw) for r in rows)
        panels = getattr(scr, "panels", None) or []
        if panels:
            return tuple((getattr(p, "kind", None), getattr(p, "name", None)) for p in panels)
        return tuple(sorted(r[0] for r in st["rows"]))

    @staticmethod
    def _row_raw(st, index):
        """取当前这一屏某一行（槽位）的原文；拿不到就 None。见 `Config.settle_wait_change`。"""
        if index is None:
            return None
        scr = st.get("screen")
        row = scr.row(index) if hasattr(scr, "row") else None
        return getattr(row, "raw", None)

    @staticmethod
    def _row_raw_in(rows_sig, index):
        """从 `_rows_sig()` 的结果（`((index, raw), …)`）里取某一行的原文。"""
        for idx, raw in rows_sig or ():
            if idx == index:
                return raw
        return None

    @classmethod
    def _row_changed(cls, st, before_rows, index):
        """这一行的词条**真的换了一件**没有 —— 用相似度而不是原文相等（见 `ROW_SAME_MIN`）。

        原文相等太严（噪声写法会被当成「换件了」→ 幽灵记账），所以按相似度取反：
        `similarity(canon(now), canon(old)) < ROW_SAME_MIN` 才算换件。真机 30 次刷新实测，
        同一件装备的噪声写法 ≥0.842、真换件 ≤0.462，0.72 落在两簇之间的空档里。
        """
        if index is None or before_rows is None:
            return False
        now = cls._row_raw(st, index)
        old = cls._row_raw_in(before_rows, index)
        if now is None or old is None:
            return False
        return similarity(canon(now), canon(old)) < ROW_SAME_MIN

    @classmethod
    def _still_old(cls, st, before_rows, index):
        """这一屏读到的还是**刷卡前那一件**吗（弹窗屏不算旧词条 —— 那屏还没刷卡）。

        `settle_wait_change`（等名字变）与盲点路的回落判据共用这一条。
        """
        if st["kind"] == "dialog":
            return False
        if before_rows is None:
            return False
        if index is None:
            return st and cls._rows_sig(st) == before_rows
        return not cls._row_changed(st, before_rows, index)

    # ------------------------------------------------------------------ 点击
    def _dispatch_keepout(self):
        """「派遣」按钮的禁点矩形（图像坐标）：以 OCR 到的「派遣」文字框为中心向外扩一圈。

        实测「派遣」文字框只有 94x45px，而整个橙色按钮 765x106px —— 光靠「离中心 <0.03W」
        挡不住按钮左半边，所以按按钮宽度经验倍数外扩，保证整颗按钮都落在禁点区里。
        """
        if not self.dispatch_box or not self.mapper:
            return None
        x0, y0, x1, y1 = self.dispatch_box
        iw, ih = self.mapper.iw, self.mapper.ih
        return (x0 - 0.16 * iw, y0 - 0.06 * ih, x1 + 0.10 * iw, y1 + 0.06 * ih)

    def _tap(self, xy_img, tag):
        box = self._dispatch_keepout()
        if box and box[0] <= xy_img[0] <= box[2] and box[1] <= xy_img[1] <= box[3]:
            self.log("refuse_tap", tag=tag, at=[round(v) for v in xy_img],
                     reason="目标落在「派遣」按钮禁点区内，安全阀拒绝点击")
            raise SafetyError(f"拒绝点击 {tag}：目标 {tuple(round(v) for v in xy_img)} "
                              f"落在「派遣」禁点区 {tuple(round(v) for v in box)} 内")
        if self.dispatch_xy:
            dist = ((xy_img[0] - self.dispatch_xy[0]) ** 2
                    + (xy_img[1] - self.dispatch_xy[1]) ** 2) ** 0.5
            if dist < 0.03 * self.mapper.iw:
                self.log("refuse_tap", tag=tag, at=[round(v) for v in xy_img],
                         reason="目标离「派遣」按钮太近，安全阀拒绝点击")
                raise SafetyError(f"拒绝点击 {tag}：目标 {xy_img} 距「派遣」{self.dispatch_xy} 仅 {dist:.0f}px")
        xy = self.mapper.pt(xy_img)
        if self.cfg.dry_run:
            self.log("would_tap", tag=tag, at=list(xy))
            return False
        self.log("tap", tag=tag, at=list(xy))
        self.source.tap(xy)
        return True

    def _sweep(self, xy_img):
        dxs = self.profile.get("tap_sweep_dx_frac", [0.0])
        dys = self.profile.get("tap_sweep_dy_frac", [0.0])
        out, seen = [], set()
        for dy in dys:
            for dx in dxs:
                p = (xy_img[0] + dx * self.mapper.iw, xy_img[1] + dy * self.mapper.ih)
                key = (round(p[0]), round(p[1]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(p)
        return out

    def _confirm_targets(self, st):
        """兜底：屏上出现短的「确定/确认」行。只有确认是刷新框才允许点（见 _dialog_ok）。"""
        if not self._dialog_ok(st):
            return []
        out = []
        for r in st["rows"]:
            text = r[0]
            if len(text) <= 4 and any(w in text for w in CONFIRM_WORDS):
                out.append((r[1] + r[3] / 2, r[2] + r[4] / 2))
        return out

    @staticmethod
    def _dialog_ok(st):
        """这个弹窗允许自动点「确定」吗？只放行「刷新」确认框；派遣/购买/删除…一律拒绝。"""
        txt = " ".join(r[0] for r in st["rows"])
        return ("刷新" in txt) and not DIALOG_FORBID_RE.search(txt)

    def _settle_guard_wait(self, tag, why=""):
        """时间闸门：点完「确定」之后先睡过动画窗口，再信任任何一帧。见 `Config.settle_guard`。

        放在「点「确定」」这个动作里面（而不是调用点），是因为「确定」可能从两条路被点到：
        `_tap_wait_read` 里第一屏就读到弹窗，或者先点空了、下一轮 `_ensure_kind` 才发现弹窗
        （真机 `logs\\live_guard_1.txt` 就是后者 —— 闸门写在调用点时会整条漏掉）。
        """
        if self.cfg.dry_run or self.cfg.settle_guard <= 0 or self.eff_settle_reads() <= 0:
            return                                  # gzip/慢路自带 ~1.4 s 取屏延迟，不必再等
        self.log("settle_guard", tag=tag, wait=self.cfg.settle_guard, why=why)
        time.sleep(self.cfg.settle_guard)

    def _handle_dialog(self, st, tag, depth=0):
        """点击后弹出的模态框。

        真机实测：点列表页/详情页的 ↻ 都不会直接刷新，而是先弹
        「提示 / 是否刷新特殊条件：xxx / 确定」——必须再点一次「确定」才真的刷新（花券）。
        为防手滑确认不可逆操作（派遣等），这里只放行文本含「刷新」的弹窗。

        返回 (st, confirmed)。confirmed=True 表示确实点了「确定」：**即使词条没变，
        这次点击也算生效**，不能当 no_change 去扫描重试（那会白花券）。
        """
        scr = st["screen"]
        prompt = str(getattr(scr, "prompt", None) or getattr(scr, "text", ""))
        if not (self.cfg.auto_confirm and dialog_is_refresh(scr)):
            # 等待压短后，读到的可能是「弹窗刚弹出、文字还没画完」的中间态：先重读几次再决定。
            # 一次误拒会把整轮 abort 掉，比多读几次贵得多。
            for i in range(0 if self.cfg.dry_run else self.cfg.read_retries):
                time.sleep(self.cfg.read_retry_delay)
                st = self._read(f"ev_{tag}_dlgretry{i + 1}")
                if st["kind"] != "dialog":
                    self.log("dialog_gone", tag=tag, kind=st["kind"], image=st["image"])
                    return st, False                  # 弹窗自己没了：没点过「确定」
                scr = st["screen"]
                prompt = str(getattr(scr, "prompt", None) or getattr(scr, "text", ""))
                if self.cfg.auto_confirm and dialog_is_refresh(scr):
                    self.log("dialog_ready", tag=tag, tries=i + 1, prompt=prompt[:60])
                    break
            else:
                self.log("refuse_dialog", tag=tag, prompt=prompt[:60], image=st["image"])
                raise SafetyError(
                    f"弹窗不是刷新确认框（或 auto_confirm 已关），拒绝点「确定」：{prompt[:60]}")
        at = scr.confirm
        if self.cfg.dry_run:
            self.log("would_confirm", tag=tag, at=[round(v) for v in at], prompt=prompt[:60])
            return st, True
        self.log("auto_confirm", tag=tag, at=[round(v) for v in at], prompt=prompt[:60])
        self._tap(at, f"{tag}_ok")
        time.sleep(self.cfg.wait_after_tap + self.cfg.wait_after_screen)
        # 时间闸门：真正花券的是这一下，新词条要过一会儿才画出来。见 Config.settle_guard。
        self._settle_guard_wait(tag, why="dialog_ok")
        st2 = self._read(f"ev_{tag}_ok")
        # 同上：等待压短后可能读到「确定还没生效」的旧弹窗画面。若直接按弹窗处理，
        # 会再点一次「确定」甚至误报 dialog_stuck（整轮 abort）——先重读几次。
        tried = 0
        while (not self.cfg.dry_run and st2["kind"] == "dialog"
               and tried < self.cfg.read_retries):
            tried += 1
            self.log("read_retry", tag=f"{tag}_ok", tries=tried, wait=self.cfg.read_retry_delay,
                     screen_kind=st2["kind"], phase="after_confirm")
            time.sleep(self.cfg.read_retry_delay)
            st2 = self._read(f"ev_{tag}_ok_retry{tried}")
        if st2["kind"] == "dialog" and depth < 2:
            st2, _ = self._handle_dialog(st2, f"{tag}_ok", depth + 1)
        if st2["kind"] == "dialog":
            prompt = str(getattr(st2["screen"], "prompt", None) or "")[:60]
            self.log("dialog_stuck", tag=tag, prompt=prompt, image=st2["image"])
            raise SafetyError(f"点了「确定」弹窗还在（{prompt}）：坐标可能不准，停手不继续点")
        return st2, True

    def _blind_confirm(self, tag, before_rows=None, row_index=None):
        """盲点「确定」：不读那一屏弹窗，直接点 profile 里量好的确认框坐标（真机 1592,1032）。

        只有 `dialog_mode="blind"`（默认）才走这里。点完等 `blind_settle`（默认 1.8 s = 探针量到的
        「点完到框消失」受理延迟）再读一屏，三种结果各有处置：

        - **框没了，而且这一行的名字真的换了**：盲点命中，这一下的「抽签」已经发生 →
          `confirmed=True`，睡掉时间闸门再补读一屏，好让结果落到最新。
        - **框没了、名字还是旧的**：说不清是「点空了」还是「真抽了但新词条还没画出来」。
          这一屏**绝不记账**，交给 `_wait_row_changed` 按 `settle_wait_change` 的预算继续读：
          等的过程中读到弹窗就把框的处置权交回 `_handle_dialog`；预算用完名字还是旧的 →
          也返回 `confirmed=False`，当「没抽」处理，绝不当成刷新生效。
        - **框还在**（等满受理延迟还读到弹窗那一屏）：那一下确实没点掉框，**不重复点**，
          返回 `confirmed=False` 交调用点，由 `_handle_dialog` 重读框文字、只放行含「刷新」的框再点。

        为什么等 1.8 s 再读（2026-10-13 5 次真机实测，10 发刷新）：旧写法只等 0.8 s 就读，
        **10 发全部**落到「框没了但名字还是旧的」→ 再等一次 `wait_change` → 再走 1~2 次
        `read_retry`（每次 +0.7 s），10 发里有 6 发其实框早自己没了、根本不需要再点「确定」。
        等满受理延迟再读，那一屏直接就是最终态，省掉这些回落读屏。

        为什么点空也没有额外风险：「确定」是照像素位置点的，位置不对最多白点一下空屏，
        游戏只会忽略它；**不是**按框里文字决定点不点，所以不存在「弹出别的框也被确认」。

        `confirmed=False` 的含义是**结论已经交给调用方**（框还在 → `_handle_dialog` 去点；
        等不到变化 → 重读网继续看）：这两种情况都**不许**再往下走「补点确认框 / 扫描候选点」
        ——那些补点会再点一次「确定」，每一次都是一个 500 券的幽灵记账（场景 V2/W 钉的就是它）。
        """
        frac = self.profile.get("dialog_confirm_xy_frac") or [0.4975, 0.7167]
        at = (frac[0] * self.mapper.iw, frac[1] * self.mapper.ih)
        self.log("fast_confirm", tag=tag, at=[round(v) for v in at], mode="blind")
        self._tap(at, f"{tag}_fastok")
        time.sleep(self.cfg.blind_settle)     # 等游戏受理（点完到框消失恒定约 1.8 s，见 Config.blind_settle）
        st2 = self._read(f"ev_{tag}_fastok")
        if st2["kind"] == "dialog":
            self.log("blind_missed", tag=tag, image=st2["image"],
                     note="盲点后等满受理延迟确认框还在：那一下确实没点掉框，交回读框那条路")
            return st2, False
        changed = (before_rows is None or row_index is None
                   or self._row_changed(st2, before_rows, row_index))
        if not changed:
            # 框没了、可名字还是旧的：说不清是「点空了」还是「真抽了但新词条还没画出来」
            # （动画窗口里新词条要过几帧才落屏）。按 `settle_wait_change` 的预算继续读。
            self.log("blind_unclear", tag=tag, image=st2["image"],
                     note="盲点后框没了、这一行名字还没变：等新词条落屏，等不到不记账")
            st3 = self._wait_row_changed(tag, st2, before_rows, row_index)
            if st3 is None:                     # 等出了弹窗（说明刚才那下没点掉框）
                return st2, False
            st2 = st3
            changed = self._row_changed(st2, before_rows, row_index)
        # 命中：真机是「顶栏先扣券 → 新词条过一会儿才画出来」，所以要睡过动画窗口再补读一屏，
        # 否则会把刷新前的旧画面当结果（见 Config.settle_guard）。闸门只在这里跑一次：
        # 盲点已经自己确认过弹窗了，稳定复读那层就不该再跑（否则 `why` 两次、每次刷新多 0.9 s）。
        time.sleep(self.cfg.settle_delay)
        if changed:
            self._settle_guard_wait(tag, why="blind_confirm")
        # 名字真的换了才算「这一下抽了」；预算用完还是旧名字 → `changed=False`，
        # 由调用点的重读网继续（它退出时按行级判据决定记不记账），绝不变成幽灵 500 券。
        return self._read(f"ev_{tag}_blindok"), changed

    def _wait_row_changed(self, tag, st, before_rows, row_index):
        """等这一行的名字真的变了（盲点路回落专用，预算同 `settle_wait_change`）。

        返回：等到了 → 最后那一屏；预算用完还是旧的 → 还是那一屏；
        等的过程中读到确认框 → `None`（调用方按「框还在」处理，把球交给 `_handle_dialog`）。
        """
        budget = self.cfg.settle_wait_change
        if budget <= 0:
            self.log("wait_change", tag=tag, slot=row_index, reads=0, waited=0.0,
                     changed=False, screen_kind=st["kind"], raw=self._row_raw(st, row_index))
            return st
        t0, k = time.time(), 0
        cap = int(budget / 0.1) + 4
        while time.time() - t0 < budget and k < cap:
            k += 1
            time.sleep(max(self.cfg.settle_delay, 0.1))
            nxt = self._read(f"ev_{tag}_blindwait{k}")
            if nxt["kind"] == "dialog":
                self.log("wait_change", tag=tag, slot=row_index, reads=k,
                         waited=round(time.time() - t0, 2), changed=False,
                         screen_kind="dialog", raw=None)
                return None
            st = nxt
            if not self._still_old(st, before_rows, row_index):
                break
        changed = not self._still_old(st, before_rows, row_index)
        self.log("wait_change", tag=tag, slot=row_index, reads=k,
                 waited=round(time.time() - t0, 2), changed=changed,
                 screen_kind=st["kind"], raw=self._row_raw(st, row_index))
        return st

    def _tap_progressed(self, st, before_sig, before_rows, row_index, blind=False):
        """这一下点击**真的生效了**没有（返回 False = 现在这一屏还不能算「点了有效果」）。

        判据按有没有 `row_index` 分两种：
        - 刷某一行的 ↻（`row_index` 给了）：**只认「读到弹窗」**。真机每一次刷新的必经之路就是
          那个确认框（README 事实 1），它就是「服务器收下了这一下」的唯一可靠凭据。
          行文字不能当凭据：真机 30 次刷新里 6 次「刷卡前/刷卡后」读到的是**同一件装备的两种
          OCR 噪声写法**（`SEK复合防弹衣`/`SEK合防弹衣` 相似度 0.933、`RUSH战木背包`/`RUSH战术背包`
          0.875、`指库官A战木耳机`/`指库官A战术耳机` 0.875 …），真换件最高才 0.462 —— 旧代码用
          「原始字符串不相等」当判据，正是这 4 次（30 次里）**没弹窗也记了 500 券**的来路。
        - 其它点击（列表页 ↻、选择）：沿用整屏签名（那边没有「逐行文字」可看）。

        `blind=True`（`dialog_mode="blind"` 的盲点快路，默认）：那一屏弹窗**被盲点掉了**、
        没读到，所以拿不到上面那条凭据，只能退回相似度判据（`_row_changed`：噪声写法不算换件）。
        这个弱保证只在**盲点确认成功之后**用：那一下要是点空了（框还在），
        `_blind_confirm` 会返回 `confirmed=False`，这里就不会被当成「已生效」。
        """
        if st["kind"] == "dialog":
            return True
        if row_index is not None:
            # 盲点路：点 ↻ 那一下的「抽签」凭据（弹窗）被盲点掉了，所以这一下只在
            # **盲点已有定论**（框被点掉、或已交给 `_handle_dialog`）时，才允许用行级判据
            # 兜底；没走盲点又没读到弹窗的行级点击 → 不算生效。
            return bool(blind) and self._row_changed(st, before_rows, row_index)
        return self._sig(st) != before_sig

    def _tap_wait_read(self, xy_img, tag, before_sig, expect_dialog=False, before_rows=None,
                       row_index=None):
        """点一下、等一会儿、读屏。

        等待刻意压短（默认 0.25+0.55 s）以加速，所以这里加了一层**重读**安全网：
        还没「真的生效」时（判据见 `_tap_progressed`：刷某一行**只认读到弹窗**，其它点击看整屏签名）
        先多读 `read_retries` 次（每读一次就多等 `read_retry_delay`，默认共 5.6 s ——
        真机确认框是渐显：点 ↻ 后约 0.7~1.5 s 开始有像素变化，明显跳变 1.5~1.8 s，这段就是等它）。
        没有这层的话「读早了」会被当成「点空了」→ 扫描再点一次 → 白花 500 券。

        `expect_dialog=True` 且 `dialog_mode="blind"`（默认）时走盲点快路：点 ↻ 后不等
        读到弹窗文字，先 `wait_dialog` 让框画出来、直接点它的「确定」坐标，再等 `blind_settle`
        （默认 1.8 s，探针量到的受理延迟）读一屏结果 —— 一次刷新从 2 次读屏降到 1 次
        （省掉的那屏大约 2.4 s 里的一屏）。

        ⚠️ `wait_dialog` 的实测事实（2026-10-13 真机探针，23 次点击）：框**从不吞点击**，
        offset 从 −0.45 s 到 +2.50 s 全部生效；「框出现在屏幕上时点它会被吃掉」这个旧结论
        是错的。点完到框消失恒定约 **1.8 s**（游戏受理延迟），所以盲点点完之后必须等满
        `blind_settle` 再读，否则读到的还是「框刚没了、新词条没落屏」的中间态
        （2026-10-13 实测：只等 0.8 s 时 10 发刷新全部落到 `blind_unclear`）。
        理由与数据见 `Config.wait_dialog` / `Config.blind_settle`。

        before_rows 给了且**实际取屏路**需要稳定性复读时（流路自动开，见 `eff_settle_reads`）：
        见 `_rows_sig` 的注释。

        返回 `(st, changed, blind_done)`：`blind_done=True` 表示盲点那一下**已经有定论**
        （框被点掉、这一行的名字也真的换了），调用点就**不许**再补点确认框 / 扫描候选点
        ——那些补点会再点一次「确定」，每一次都是一个 500 券的幽灵记账（场景 V2 钉的就是它）。
        `blind_done=False`（盲点没定论：框还在、或名字还没变）时反过来要靠调用点接着处理。
        """
        blind = bool(expect_dialog and (self.cfg.dialog_mode or "blind").lower() == "blind"
                     and not self.cfg.dry_run)
        blind_done = False
        self._tap(xy_img, tag)
        confirmed = False
        if blind:
            time.sleep(self.cfg.wait_dialog)      # 等确认框画出来（真机是渐显，见 Config.wait_dialog）
            st, confirmed = self._blind_confirm(tag, before_rows=before_rows, row_index=row_index)
            blind_done = confirmed
        else:
            time.sleep(self.cfg.wait_after_tap + self.cfg.wait_after_screen)
            st = self._read(f"ev_{tag}")
        tried = 0
        while (not self.cfg.dry_run and tried < self.cfg.read_retries
               and not confirmed
               and not self._tap_progressed(st, before_sig, before_rows, row_index,
                                            blind=blind)):
            tried += 1
            self.log("read_retry", tag=tag, tries=tried, wait=self.cfg.read_retry_delay,
                     screen_kind=st["kind"])
            time.sleep(self.cfg.read_retry_delay)
            st = self._read(f"ev_{tag}_retry{tried}")
        if st["kind"] == "dialog":
            st, confirmed = self._handle_dialog(st, tag)
        # 稳定性复读。必须放在弹窗处理**之后**：点 ↻ 读到的第一屏就是确认框（kind=dialog），
        # 真正要等的「新词条画出来」发生在点了「确定」之后。见 _rows_sig 的注释。
        # 次数按**这一次实际走的取屏路**定（见 eff_settle_reads）：流路 2 次、gzip 路 0 次。
        settle_reads = self.eff_settle_reads()
        prev, settled, extra = None, 0, 0
        while (not self.cfg.dry_run and settle_reads > 0 and before_rows is not None
               and st["kind"] != "dialog"
               and (confirmed or self._tap_progressed(st, before_sig, before_rows, row_index,
                                                      blind=blind))
               and extra < settle_reads + 6):
            rs = self._rows_sig(st)
            if rs == before_rows:
                settled = 0                     # 券可能已经扣了，但新词条还没画出来：继续等
            elif rs == prev:
                settled += 1                    # 和上一次读到的词条一模一样：稳定候选 +1
            else:
                settled = 1
            prev = rs
            if settled >= settle_reads:
                break
            extra += 1
            time.sleep(self.cfg.settle_delay)
            nxt = self._read(f"ev_{tag}_settle{extra}")
            if nxt["kind"] == "dialog":
                # 等到这里才画出来的确认框**必须点掉**：它就是这一下的「抽签」，
                # 不点就等于「记了 500 券而游戏没抽」（见 _tap_progressed 的注释）。
                st, confirmed = self._handle_dialog(nxt, f"{tag}_settle_dlg")
                break
            st = nxt
        if extra:
            self.log("settle", tag=tag, reads=extra, settled=settled, screen_kind=st["kind"],
                     rows_changed=self._rows_sig(st) != before_rows,
                     sig=str(self._sig(st))[:80])
        # 「刷完等名字真的变了」。见 Config.settle_wait_change。判据与盲点回落共用
        # `_still_old`：同一件装备的噪声写法相似度实测 ≥0.842、真换件 ≤0.462
        # （`ROW_SAME_MIN = 0.72` 落在中间），所以噪声不会让我们误以为「已经换好了」。
        if (not self.cfg.dry_run and confirmed and settle_reads > 0 and before_rows is not None
                and self.cfg.settle_wait_change > 0 and self._still_old(st, before_rows, row_index)):
            t0, k = time.time(), 0
            cap = int(self.cfg.settle_wait_change / 0.1) + 4
            while time.time() - t0 < self.cfg.settle_wait_change and k < cap:
                k += 1
                time.sleep(max(self.cfg.settle_delay, 0.1))
                st = self._read(f"ev_{tag}_waitchg{k}")
                if not self._still_old(st, before_rows, row_index):
                    break
            self.log("wait_change", tag=tag, slot=row_index, reads=k,
                     waited=round(time.time() - t0, 2),
                     changed=not self._still_old(st, before_rows, row_index),
                     screen_kind=st["kind"], raw=self._row_raw(st, row_index))
        return st, (confirmed or self._tap_progressed(st, before_sig, before_rows, row_index,
                                                     blind=blind)), blind_done

    def _tap_until_change(self, xy_img, tag, before_sig, expect_dialog=False, before_rows=None,
                          row_index=None):
        """点 xy（不生效就按 profile 扫描候选点），直到全屏文本发生变化。

        扫描最多再试 profile 里的前 2 个偏移点：坐标已经过真机像素核对，
        扫描只是兜底；不设上限的话一次「点了没变化」会连点十几次，
        每次都要花刷新券（词条 500/次），预算会失控。

        **盲点快路那一下有定论就不再补点**（`blind_done`）：那些补点（`confirm_dialog` /
        `_sweep` 候选点）会再点一次「确定」，每一次都是一个 500 券的幽灵记账。
        """
        cands = self._sweep(xy_img) if self.cfg.tap_sweep else [xy_img]
        st, changed, blind_done = self._tap_wait_read(cands[0], tag, before_sig,
                                                      expect_dialog=expect_dialog,
                                                      before_rows=before_rows, row_index=row_index)
        if changed or self.cfg.dry_run or blind_done:
            # 盲点那一下已有定论就到此为止：下面的 `confirm_dialog` 补点与 `_sweep` 候选点
            # 都会**再点一次「确定」**，每一次都是一个 500 券的幽灵记账（场景 V2 钉的就是它）。
            return st, changed
        for cxy in self._confirm_targets(st):
            self.log("confirm_dialog", tag=tag, at=[round(v) for v in cxy])
            st, changed, _ = self._tap_wait_read(cxy, f"{tag}_ok", before_sig,
                                                 before_rows=before_rows, row_index=row_index)
            if changed:
                return st, True
        for i, cxy in enumerate(cands[1:3], start=1):
            st, changed, _ = self._tap_wait_read(cxy, f"{tag}_sweep{i}", before_sig,
                                                 expect_dialog=expect_dialog,
                                                 before_rows=before_rows, row_index=row_index)
            if changed:
                self.log("sweep_hit", tag=tag, tries=i + 1,
                         offset=[round(cxy[0] - xy_img[0]), round(cxy[1] - xy_img[1])])
                return st, True
        return st, False

    # ------------------------------------------------------------------ 计划
    def plan(self, st=None):
        st = st or self._read("dry")
        block = self.book.blocks[self.cfg.block_index]
        out = {
            "status": "dry_run",
            "block": {"title": block.title, "condition": block.condition, "timing": block.timing,
                      "columns": list(block.columns), "excluded": list(block.excluded)},
            "limit": self.limit,
            "screen": st["kind"],
            "ocr": st.get("ocr", self.ocr_name),
            "image": st["image"],
            "device_size": list(self.device_size),
            "tap_size": [self.mapper.dw, self.mapper.dh],
            "cost": self.cost_summary([]),
        }
        if st["kind"] == "dialog":
            scr = st["screen"]
            out["dialog"] = {
                "title": scr.title,
                "prompt": scr.prompt,
                "is_refresh": dialog_is_refresh(scr),
                "confirm_tap": list(self.mapper.pt(scr.confirm)) if scr.confirm else None,
            }
            return out
        if st["kind"] == "list":
            boss = st["screen"].by_kind("暗区首领")
            out["boss"] = None if boss is None else {
                "name": boss.name, "cost": boss.cost_raw,
                "excluded": boss_excluded(boss.name, block.excluded),
                "refresh_tap": list(self.mapper.pt(boss.refresh)),
                "select_tap": list(self.mapper.pt(boss.select)) if boss.select else None,
            }
        else:
            rows = []
            for r in st["screen"].rows:
                pool = block.pools.get(r.index, [])
                rows.append({
                    "slot": r.index, "raw": r.raw, "matched": r.matched,
                    "in_pool": r.matched in pool, "pool": list(pool),
                    "refresh_tap": list(self.mapper.pt(r.refresh)),
                })
            out["boss"] = st["screen"].boss
            out["rows"] = rows
        return out

    # ------------------------------------------------------------------ 主流程
    def run(self):
        """外壳：跑完一定把常驻资源收掉（视频流会留 ffmpeg/screenrecord 在后台）。"""
        try:
            return self._run_once()
        finally:
            close = getattr(self.source, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:                               # noqa: BLE001
                    pass

    def _run_once(self):
        self.t_start = time.time()
        self.log("start", dry_run=self.cfg.dry_run, limit=self.limit, ocr=self.ocr_name,
                 device_size=list(self.device_size), work_dir=str(self.work_dir),
                 wait_after_tap=self.cfg.wait_after_tap, wait_after_screen=self.cfg.wait_after_screen,
                 read_retries=self.cfg.read_retries, read_retry_delay=self.cfg.read_retry_delay,
                 capture=self.cfg.capture, dialog_mode=self.cfg.dialog_mode,
                 settle_reads=self.cfg.settle_reads, settle_delay=self.cfg.settle_delay,
                 settle_guard=self.cfg.settle_guard,
                 settle_wait_change=self.cfg.settle_wait_change,
                 confirm_reads=self.cfg.confirm_reads, reads_auto="按实际取屏路（0=自动）",
                 wait_dialog=self.cfg.wait_dialog, blind_settle=self.cfg.blind_settle,
                 win_ocr_worker=win_ocr_worker_status())
        block = self.book.blocks[self.cfg.block_index]
        self.last = self._read("start")
        w, h = self.last["size"]
        self.log("screen", kind=self.last["kind"], image=self.last["image"], size=[w, h],
                 device_size=list(self.device_size),
                 tap_size=[self.mapper.dw, self.mapper.dh],
                 chain=self.last.get("chain"),
                 win_ocr_worker=win_ocr_worker_status())
        if h > w:
            self.log("abort", phase="start",
                     reason=f"截到的是竖屏 {w}x{h}：游戏没横过来或不在前台，请先手动切到武装派遣界面")
            return {"status": "error", "phase": "start", "reason": "portrait_screen", "size": [w, h]}
        try:
            if self.cfg.dry_run:
                plan = self.plan(self.last)
                self.log("plan", **{k: v for k, v in plan.items() if k != "image"})
                return plan
            if self.last["kind"] == "dialog":
                scr = self.last["screen"]
                prompt = str(getattr(scr, "prompt", None) or getattr(scr, "text", ""))[:60]
                self.log("abort", phase="start", reason="界面上有个模态弹窗，先手动关掉再跑",
                         prompt=prompt)
                return {"status": "error", "phase": "start", "reason": "dialog_open",
                        "prompt": prompt}
            if self.last["kind"] == "detail":
                self.boss_name = self.last["screen"].boss
                detail_st = self.last
            elif self.cfg.detail_only:
                # 用户要求：「暗区首领」那一栏彻底不管——不刷首领、不判排除名单、不点「选择」。
                # 所以只认手动打开的详情页；当前不是详情页就一步都不走。
                self.log("abort", phase="start", reason="detail_only", got=self.last["kind"],
                         msg="本工具只管详情页：请在手机上点「选择」进入武装派遣详情页再跑")
                return {"status": "error", "phase": "start", "reason": "detail_only",
                        "got": self.last["kind"]}
            else:
                lr = self.list_phase(block)
                if lr.get("status") != "ok":
                    return lr
                self.boss_refreshes = lr.get("boss_refreshes")
                detail_st = lr.pop("_st")
            out = self.detail_phase(block, detail_st)
        except SafetyError as e:
            self.log("abort", phase="safety", reason=str(e))
            return {"status": "error", "phase": "safety", "reason": "safety_guard", "detail": str(e)}
        out["boss"] = self.boss_name
        out["boss_refreshes"] = self.boss_refreshes
        out["cost"] = self.cost_summary(out.get("rows"))
        return self._finish(out)

    def _ensure_kind(self, st, want, tag, phase):
        """把屏面弄成期望的种类：有弹窗先处理掉，否则重读一次。

        返回 (st, err)；err 非 None 表示拿不到 want 界面（调用方要当成结构化错误返回，
        **不能**直接对屏面对象调它没有的方法——真机踩过 AttributeError: ListScreen has no row）。
        """
        if st["kind"] == "dialog":
            st, _ = self._handle_dialog(st, f"{tag}_dlg")
        if st["kind"] != want:
            st = self._read(f"{tag}_resync")
            if st["kind"] == "dialog":
                st, _ = self._handle_dialog(st, f"{tag}_dlg2")
        if st["kind"] != want:
            self.log("unexpected_screen", phase=phase, tag=tag, got=st["kind"], image=st["image"])
            return st, {"status": "error", "phase": phase,
                        "reason": f"unexpected_screen:{st['kind']}"}
        return st, None

    def list_phase(self, block):
        st, err = self._ensure_kind(self.last, "list", "list", "list")
        if err:
            return err
        if st["screen"].by_kind("暗区首领") is None:
            self.log("abort", phase="list", reason="没在界面上找到「暗区首领」这一栏")
            return {"status": "error", "phase": "list", "reason": "boss_panel_not_found"}
        attempts, stale = 0, 0
        while True:
            st, err = self._ensure_kind(st, "list", "list_loop", "list")
            if err:
                return err
            boss = st["screen"].by_kind("暗区首领")
            name = boss.name if boss else ""
            excluded = boss_excluded(name, block.excluded)
            self.log("boss_check", attempt=attempts, name=name, excluded=excluded,
                     cost=(boss.cost_raw if boss else None), image=st["image"])
            if not name:
                self.log("warn", phase="list", msg="首领名没识别出来，无法用排除名单判定，按「可刷」继续")
            if not excluded:
                break
            if attempts >= self.limit:
                self.log("give_up", phase="list", attempts=attempts, name=name)
                return {"status": "give_up", "phase": "list", "attempts": attempts, "boss": name}
            attempts += 1
            before = self._sig(st)
            st, changed = self._tap_until_change(boss.refresh, f"boss_refresh{attempts}", before)
            if changed:
                self._charge("boss", parse_cost(boss.cost_raw), attempt=attempts, boss=name)
            if not changed:
                stale += 1
                self.log("no_change", phase="list", attempt=attempts, stale=stale, image=st["image"])
                if stale >= self.cfg.stale_limit:
                    self.log("abort", phase="list",
                             reason="连续点击后界面毫无变化：坐标可能不准 / 有弹窗 / 资源不足")
                    return {"status": "error", "phase": "list", "reason": "no_change", "stale": stale}
            else:
                stale = 0
        boss = st["screen"].by_kind("暗区首领")
        self.boss_name = boss.name
        if not boss.select:
            self.log("abort", phase="list", reason="没找到「选择」按钮")
            return {"status": "error", "phase": "list", "reason": "select_not_found"}
        before = self._sig(st)
        st2, changed = self._tap_until_change(boss.select, "select", before)
        if not changed or st2["kind"] != "detail":
            self.log("abort", phase="select", reason="点了「选择」但没进详情页",
                     kind=st2["kind"], image=st2["image"])
            return {"status": "error", "phase": "select", "reason": "detail_not_entered"}
        self.log("entered_detail", boss=boss.name, image=st2["image"])
        return {"status": "ok", "boss": boss.name, "boss_refreshes": attempts, "_st": st2}

    def _confirm_out_of_pool(self, block, slot, st, tag):
        """花券之前再确认一次「这一行确实不在池内」。

        真机教训（`logs\\live_stream_1.txt`，2026-10-11 深夜）：视频流帧把槽1 的
        `AUG突击步枪` 读成 `AIJG突击步枪`（h264 把小字的笔画糊了），模糊匹配对 3 个字母的
        核心容不下 2 个字符的错 → 判「不在池内」→ 白点一次 ↻ 花 500 券，而那一行本来是好的。
        多读一帧的代价在流路上只有 0.01 s，所以**实际走流路**时默认要求连续 2 次都读到
        「不在池内」才动手（`eff_confirm_reads` / `Config.confirm_reads`）。

        返回 `(st, can_spend, why)`：`why` = `ok`（可以点）/ `in_pool`（再读发现在池内 → 跳过）/
        `unclear`（读不到详情页或行数不对 → 这一槽停手，绝不瞎点）。
        """
        need = self.eff_confirm_reads()
        if need <= 1:
            return st, True, "ok"
        for i in range(need - 1):
            st2 = self._read(f"{tag}_confirm{i + 1}")
            if st2["kind"] == "dialog":
                st2, _ = self._handle_dialog(st2, f"{tag}_confirm{i + 1}_dlg")
            scr2 = st2["screen"] if st2["kind"] == "detail" else None
            if scr2 is None or len(scr2.rows) != len(block.pools):
                self.log("confirm_unclear", slot=slot, got=st2["kind"],
                         rows=(len(scr2.rows) if scr2 is not None else None), image=st2.get("image"))
                return st2, False, "unclear"
            row2 = scr2.row(slot)
            if row2 is None:
                self.log("confirm_unclear", slot=slot, got="row_missing", image=st2["image"])
                return st2, False, "unclear"
            if row2.matched is not None and row2.matched in block.pools[slot]:
                self.log("confirm_in_pool", slot=slot, raw=row2.raw, matched=row2.matched,
                         image=st2["image"], note="再读一次发现在池内 → 不点 ↻（省 500 券）")
                return st2, False, "in_pool"
            self.log("confirm_out", slot=slot, raw=row2.raw, agree=i + 1, need=need,
                     image=st2["image"])
            st = st2
        return st, True, "ok"

    def detail_phase(self, block, st):
        """详情页逐槽刷新。

        每一轮点击**之前**，先把所有还没判定的槽位在同一张截图上重新体检一遍：已经在规则池里
        的槽位一律记 ok、refreshes=0 直接跳过，绝不会去点它的 ↻。（真机 live_full_4：
        槽2、槽3 本来就在池内 → 一次都没点，只刷了槽1。）
        """
        resolved = {}
        slots = sorted(block.pools)
        for slot in slots:
            if slot in resolved:
                continue   # 体检时已经判定过（在池内 → ok）：绝不回头再点它的 ↻
            attempts, stale = 0, 0
            while True:
                st, err = self._ensure_kind(st, "detail", f"slot{slot}", "row")
                if err:
                    resolved[slot] = {"slot": slot, "item": None, "refreshes": attempts,
                                      "status": err["reason"]}
                    break
                scr = st["screen"]
                if len(scr.rows) != len(slots):
                    # 行数不对 = OCR 多读/漏读了行（真机实测：右侧面板表头『特殊习惯』被当成
                    # 第 1 行 → 三行整体错位）→ 点 ↻ 会刷错行、白花券（live_full_3 槽2 连刷
                    # 30 次一次没命中就是这个）。重读一次，仍不对就停手，绝不猜着点。
                    self.log("row_count_unexpected", slot=slot, got=len(scr.rows),
                             want=len(slots), image=st["image"])
                    st = self._read(f"slot{slot}_rowcount")
                    if st["kind"] == "dialog":
                        st, _ = self._handle_dialog(st, f"slot{slot}_rowcount_dlg")
                    scr = st["screen"] if st["kind"] == "detail" else None
                    if scr is None or len(scr.rows) != len(slots):
                        got = len(scr.rows) if scr is not None else st["kind"]
                        self.log("abort", phase="row", slot=slot,
                                 reason="row_count_unexpected", got=got, image=st.get("image"))
                        resolved[slot] = {"slot": slot, "item": None, "refreshes": attempts,
                                          "status": "row_count_unexpected"}
                        break
                # —— 体检：本轮点击前把所有未判定槽位过一遍，符合的跳过 ——
                row, ok = None, False
                for s in slots:
                    if s in resolved:
                        continue
                    r_s = scr.row(s)
                    ok_s = bool(r_s) and r_s.matched is not None and r_s.matched in block.pools[s]
                    if s == slot:
                        row, ok = r_s, ok_s
                        continue
                    if ok_s:
                        self.log("row_check", slot=s, attempt=0, raw=r_s.raw, matched=r_s.matched,
                                 in_pool=True, skip=True, precheck=True, image=st["image"])
                        resolved[s] = {"slot": s, "item": r_s.matched, "refreshes": 0,
                                       "status": "ok"}
                if row is None:
                    self.log("row_missing", slot=slot, image=st["image"])
                    resolved[slot] = {"slot": slot, "item": None, "refreshes": attempts,
                                      "status": "row_missing"}
                    break
                self.log("row_check", slot=slot, attempt=attempts, raw=row.raw,
                         matched=row.matched, in_pool=ok, image=st["image"],
                         chain=st.get("chain"))
                if ok:
                    resolved[slot] = {"slot": slot, "item": row.matched, "refreshes": attempts,
                                      "status": "ok"}
                    break
                if attempts >= self.limit:
                    self.log("give_up", phase="row", slot=slot, attempts=attempts,
                             item=row.matched or row.raw)
                    resolved[slot] = {"slot": slot, "item": row.matched or row.raw,
                                      "refreshes": attempts, "status": "give_up"}
                    break
                # 每次花钱之前都再确认一遍：只有连续 confirm_reads 次都读到「不在池内」才点 ↻
                st, can_spend, why = self._confirm_out_of_pool(
                    block, slot, st, f"slot{slot}_refresh{attempts + 1}")
                if why == "in_pool":
                    r_ok = st["screen"].row(slot) if st.get("kind") == "detail" else None
                    item_ok = (r_ok.matched or r_ok.raw) if r_ok is not None \
                        else (row.matched or row.raw)
                    resolved[slot] = {"slot": slot, "item": item_ok, "refreshes": attempts,
                                      "status": "ok"}
                    break
                if not can_spend:
                    resolved[slot] = {"slot": slot, "item": row.matched or row.raw,
                                      "refreshes": attempts, "status": "confirm_unclear"}
                    break
                # 确认用的那几屏可能已经换了画面 → 重新取一下当前行与签名
                st, err = self._ensure_kind(st, "detail", f"slot{slot}", "row")
                if err:
                    resolved[slot] = {"slot": slot, "item": row.matched or row.raw,
                                      "refreshes": attempts, "status": err["reason"]}
                    break
                row = st["screen"].row(slot) or row
                attempts += 1
                before = self._sig(st)
                before_rows = self._rows_sig(st)
                t_cycle = time.time()
                self._last_refresh_slot = slot      # 收尾补确认要知道补的是哪一行
                self._last_refresh_item = row.matched or row.raw   # 记账记的是「刷前那件」
                self._last_refresh_attempt = attempts
                self._last_refresh_charged = False  # 这一下的券还没记（changed 才记）
                st, changed = self._tap_until_change(row.refresh, f"slot{slot}_refresh{attempts}",
                                                     before, expect_dialog=True,
                                                     before_rows=before_rows, row_index=slot)
                sec = round(time.time() - t_cycle, 2)
                if changed:
                    self._charge("row", self.cfg.row_refresh_cost, slot=slot, attempt=attempts,
                                 item=(row.matched or row.raw), sec=sec)
                    self._last_refresh_charged = True
                if not changed:
                    stale += 1
                    self.log("no_change", phase="row", slot=slot, attempt=attempts,
                             stale=stale, image=st["image"], sec=sec)
                    if stale >= self.cfg.stale_limit:
                        self.log("abort", phase="row", slot=slot, reason="no_change")
                        resolved[slot] = {"slot": slot, "item": row.matched or row.raw,
                                          "refreshes": attempts, "status": "error_no_change"}
                        break
                else:
                    stale = 0
        # 收尾补确认：最后一次 ↻ 的确认框往往在跑完之后才画出来（见 _flush_pending_dialog）
        st, pending = self._flush_pending_dialog(st, resolved)
        rows_out = [resolved[s] for s in slots if s in resolved]
        items = {r["slot"]: r["item"] for r in rows_out if r.get("item")}
        ready = len(rows_out) == len(slots) and all(r["status"] == "ok" for r in rows_out)
        # 只管详情页时（CLI 默认）不把首领名交给校验：排除名单是「暗区首领栏」的规矩，
        # 用户已明确那一栏彻底不管，所以这里只按三行是否落在规则池里判。
        boss_arg = None if self.cfg.detail_only else self.boss_name
        reports = validate(self.book, items, boss=boss_arg)
        out = {"status": "ready" if ready else "incomplete", "ready": ready, "rows": rows_out,
               "items": items, "reports": [rep.summary() for rep in reports],
               "boss_display": self.boss_name, "excluded_enforced": not self.cfg.detail_only}
        if pending:
            out["pending_dialog"] = pending
        for rep in reports:
            self.log("validate", summary=rep.summary())
        if self.cfg.stop_before_dispatch:
            self.log("stop_before_dispatch", ready=ready, msg="三行就绪后停手，派遣按钮由人来点")
        self.last = st
        return out

    def _flush_pending_dialog(self, st, resolved):
        """收尾补确认：一轮的**最后一次** ↻，确认框常在跑完之后才画出来。

        真机证据（`logs\\live_guard_slot2_1.txt` 那一轮之后的启动 abort）：`--limit 5` 跑完时手机上
        还挂着「是否刷新特殊条件：SEK堡垒防弹衣」—— `charge` 已经记了、行却没提交、下一轮启动
        直接被模态框挡住（`abort phase=start reason=界面上有个模态弹窗`）。原因：点完 ↻ 弹窗要
        ~2 s 才画出来，而那一轮的复读循环已经按「详情页」收尾了。

        所以出报告之前多读一屏：若挂着**刷新确认框**就补点「确定」（这笔刷新本来就点过、钱也记了），
        再按补确认之后的真实词条重判那一行（原来那次读到的可能是刷新前的旧行）；不是刷新框就只记一笔，
        交给人工。返回 `(st, note)`，`note` 会写进报告并在结尾摘要里提示。
        """
        slot = self._last_refresh_slot
        if slot is None or not self.cfg.flush_pending or self.cfg.dry_run:
            return st, None
        peek = self._read("final_peek")
        if peek["kind"] != "dialog":
            return peek, None
        scr = peek["screen"]
        prompt = str(getattr(scr, "prompt", None) or getattr(scr, "text", ""))[:60]
        if not (self.cfg.auto_confirm and dialog_is_refresh(scr)):
            self.log("pending_dialog_left", slot=slot, prompt=prompt, image=peek["image"],
                     note="跑完还挂着弹窗，但不是刷新确认框（或 auto_confirm 关了）→ 交给人工")
            return peek, {"kind": "left", "slot": slot, "prompt": prompt}
        self.log("pending_dialog", slot=slot, prompt=prompt, image=peek["image"],
                 note="这一轮的确认框跑完才画出来：补点「确定」并重判这一行")
        # 钱要记上：点 ↻ 那一下券就花了，只是确认框跑完才画出来 → `detail_phase` 当时没看到
        # 「这一行变了」，按行级判据它不会记账。不补这一笔，台账就会**少记**真花掉的券
        # （和 V2「没抽就记账」正好是反方向的两个错，两个都得堵）。
        if not self._last_refresh_charged:
            self._charge("row", self.cfg.row_refresh_cost, slot=slot,
                         attempt=self._last_refresh_attempt, item=self._last_refresh_item,
                         note="收尾补确认：这一下 ↻ 的券在点击时就花了，跑完才看到确认框")
            self._last_refresh_charged = True
        try:
            st2, confirmed = self._handle_dialog(peek, f"slot{slot}_final")
        except SafetyError as e:        # 拒绝点/弹窗卡住：不能因为收尾这一下把整轮报告丢掉
            self.log("pending_dialog_left", slot=slot, prompt=prompt, reason=str(e)[:80])
            return peek, {"kind": "left", "slot": slot, "prompt": prompt, "reason": str(e)[:80]}
        note = {"kind": "confirmed", "slot": slot, "prompt": prompt,
                "auto_confirmed": prompt[:60]}
        if not confirmed or st2["kind"] != "detail":
            return st2, note
        row = st2["screen"].row(slot)
        if row is None:
            self.log("final_recheck", slot=slot, got="row_missing", image=st2["image"])
            return st2, note
        block = self.book.blocks[self.cfg.block_index]
        ok = row.matched is not None and row.matched in block.pools.get(slot, [])
        prev = resolved.get(slot) or {}
        self.log("final_recheck", slot=slot, raw=row.raw, matched=row.matched, in_pool=ok,
                 image=st2["image"],
                 note="补确认之后的真实词条（原来那次可能读的是刷新前的旧行）")
        resolved[slot] = {"slot": slot, "item": row.matched or row.raw,
                          "refreshes": prev.get("refreshes", 0),
                          "status": "ok" if ok else "pool_miss"}
        note["item"] = row.matched or row.raw
        note["in_pool"] = ok
        return st2, note

    def _finish(self, out):
        path = self.log_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}.json"
        data = dict(out)
        data["events"] = self.events
        # 视频流是 opt-in 的加速路：到底走没走成、有没有偷偷退回 gzip，日志里要能查。
        summary = getattr(self.source, "stream_summary", None)
        stream = summary() if callable(summary) else None
        if self.cfg.capture == "stream" and stream:
            data["stream"] = stream
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log("done", status=out.get("status"), ready=out.get("ready"), report=str(path),
                 elapsed_s=round(time.time() - getattr(self, "t_start", time.time()), 1),
                 **({"stream": stream} if self.cfg.capture == "stream" and stream else {}))
        return out


def print_summary(out):
    """结尾的「任务完成 + 刷新次数 + 总消费」摘要（人看的，纯 ASCII/GBK 安全字符）。"""
    labels = {"ready": "任务完成：三行全部命中规则池，停止刷新",
              "dry_run": "预演完成（dry-run：一次都没点）",
              "incomplete": "未完成：有行没刷到规则池",
              "give_up": "放弃：达到刷新上限",
              "error": "出错停手"}
    st = out.get("status")
    cost = out.get("cost") or {}
    line = "=" * 46
    print(line)
    if st == "error" and out.get("reason") == "detail_only":
        print("结果: 当前画面不是「武装派遣」详情页——本工具只管详情页，一步都没走")
        print(f"      现在读到的是: {out.get('got')}")
        print("      请在手机上点「选择」进入详情页，再重新跑一次（暗区首领那栏不碰）。")
        print(line)
        return
    print(f"结果: {labels.get(st, st)}")
    if out.get("boss"):
        note = "" if out.get("excluded_enforced", True) else "（仅供参考：本次不判定排除名单）"
        print(f"首领: {out['boss']}{note}")
    rows = out.get("rows") or []
    if rows and "status" not in rows[0]:
        # dry-run 的计划：只报「现在是什么、在不在池、刷新键在哪」
        # 注意别在这里用 ↻ 这种字符：控制台是 GBK，会 UnicodeEncodeError 把整段摘要打断
        for r in rows:
            mark = "OK" if r.get("in_pool") else "NG:不在池内"
            print(f"  第{r.get('slot')}行 当前: {r.get('raw')}  [{mark}]  "
                  f"刷新键 {r.get('refresh_tap')}")
    else:
        for r in rows:
            mark = "OK" if r.get("status") == "ok" else f"NG:{r.get('status')}"
            print(f"  第{r.get('slot')}行 刷新 {r.get('refreshes')} 次  最终: {r.get('item')}  [{mark}]")
    print(f"刷新次数: 行 {cost.get('row_charged', 0)} 次，首领 {cost.get('boss_charged', 0)} 次")
    print(f"总消费: 本次 {cost.get('spent', 0)} 券"
          f"（行 {cost.get('spent_row', 0)} + 首领 {cost.get('spent_boss', 0)}，"
          f"行刷新单价 {cost.get('unit_row_cost', 500)}/次）")
    print(f"        累计 {cost.get('spent_cumulative', cost.get('spent', 0))} 券"
          f"（台账之前 {cost.get('spent_before', 0)}）")
    for rep in out.get("reports") or []:
        print(f"规则校验: {rep}")
    pd = out.get("pending_dialog") or {}
    if pd.get("kind") == "confirmed":
        extra = f" -> {pd.get('item')}" if pd.get("item") else ""
        print(f"收尾补确认: 跑完才发现第{pd.get('slot')}行的刷新确认框（画得比读屏晚），"
              f"已补点「确定」并按新词条重判{extra}")
    elif pd.get("kind") == "left":
        print(f"注意: 屏上还挂着一个弹窗（{pd.get('prompt')}）——不是刷新确认框，"
              f"请手动关掉再跑下一轮。")
    if out.get("status") == "ready":
        print("派遣按钮未点：按配置停手，由人来点「派遣」。")
    print(line)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="暗区突围·武装派遣自动刷新（默认 dry-run，不点任何东西）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--rules", default=None,
                    help="规则表 xlsx；默认先找项目根 规则表.xlsx，再找桌面 工作簿1.xlsx")
    ap.add_argument("--block", type=int, default=0)
    ap.add_argument("--live", action="store_true", help="真正点击（默认只 dry-run）")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--wait-device", type=float, default=60.0,
                    help="最多等多少秒让手机出现在 adb devices 里，默认60")
    ap.add_argument("--limit", type=int, default=40, help="每行/首领刷新次数上限，默认40")
    ap.add_argument("--image", default=None,
                    help="离线预演：直接读一张现成截图算坐标计划（不发任何 adb 命令、不点击）")
    ap.add_argument("--device", default="3200x1440", help="搭配 --image：真机分辨率，默认 3200x1440")
    ap.add_argument("--no-sweep", action="store_true",
                    help="关掉坐标兜底扫描：每个目标只点 1 次，点了没变化就报 no_change（省刷新券）")
    ap.add_argument("--allow-list", action="store_true",
                    help="允许从列表页开始跑（会点「选择」、必要时刷暗区首领栏）。"
                         "默认关闭：只管手机上手打开的详情页")
    ap.add_argument("--ocr", choices=("auto", "rapid", "win"), default=DEFAULT_OCR,
                    help="OCR 后端：auto/win=Windows 自带 OCR（快，每屏约 1.5 秒）；"
                         "rapid=项目 .venv 里的 RapidOCR（更准，但每屏约 10 秒）。"
                         "也可用环境变量 DARKZONE_OCR 改默认值")
    ap.add_argument("--wait-tap", type=float, default=Config.wait_after_tap,
                    help=f"点击后立刻等的秒数，默认 {Config.wait_after_tap}")
    ap.add_argument("--wait-screen", type=float, default=Config.wait_after_screen,
                    help=f"截图前再等的秒数，默认 {Config.wait_after_screen}（等太短会触发重读重试）")
    ap.add_argument("--read-retries", type=int, default=Config.read_retries,
                    help=f"点完画面没变时先多读几次，默认 {Config.read_retries}")
    ap.add_argument("--capture", choices=("auto", "gzip", "stream", "fast", "png"),
                    default=Config.capture,
                    help="取屏方式：auto=先视频流、起不来再设备端 gzip、再原始帧、最后 PNG（默认；"
                         "流路要有 ffmpeg：环境变量 DARKZONE_FFMPEG 或放候选路径）；"
                         "stream=只要视频流（起不来同样退 gzip）；gzip=设备端 gzip 压完再传（1.15 秒/屏）；"
                         "fast=原始帧+本地 JPEG；png=设备端 PNG")
    ap.add_argument("--settle-reads", type=int, default=Config.settle_reads,
                    help=f"画面变了之后再连读几次确认稳定，默认 {Config.settle_reads}"
                         f"（0 = 自动：实际走视频流取帧时取 2，其余路不额外读；防读到动画中间帧）")
    ap.add_argument("--settle-delay", type=float, default=Config.settle_delay,
                    help=f"稳定性复读之间等多久，默认 {Config.settle_delay}")
    ap.add_argument("--settle-guard", type=float, default=Config.settle_guard,
                    help=f"点完「确定」之后、信任任何一帧之前至少等多久，默认 {Config.settle_guard}"
                         f"（0 = 关；这段是「券已扣、新词条还没画出来」的动画窗口，"
                         f"不等就会把刷新前的旧词条当成刷新结果 —— 见 README）")
    ap.add_argument("--settle-wait-change", type=float, default=Config.settle_wait_change,
                    help=f"刷完继续重读、直到这一行的名字真的变了，最多等这么久，"
                         f"默认 {Config.settle_wait_change} 秒（0 = 关）。真机实测 25.3%% 的刷新"
                         f"刷完读到的还是刷卡前那一件（大头是确认框 0.8~2.6 s 才画出来，"
                         f"不是游戏画得慢）——不等就分不清「真抽中了」和「还没抽」")
    ap.add_argument("--confirm-reads", type=int, default=Config.confirm_reads,
                    help=f"花券前再确认：连续读到几次「这一行不在池内」才允许点 ↻，"
                         f"默认 {Config.confirm_reads}（0 = 自动：实际走视频流取帧时取 2、"
                         f"gzip/原始帧路取 1；防流帧把小字读糊 → 白花 500 券）")
    ap.add_argument("--dialog-mode", choices=("blind", "read"), default=Config.dialog_mode,
                    help=f"点完 ↻ 那个「是否刷新特殊条件」确认框怎么点，默认 {Config.dialog_mode}。"
                         f"blind = 盲点快路：不读那一屏弹窗，等 --wait-dialog 后按量好的坐标直接点"
                         f"「确定」（点空就交回读框那条路，只放行含「刷新」的框）；"
                         f"read = 老行为：先读框、只放行含「刷新」的框再点它的热区")
    ap.add_argument("--fast-confirm", dest="dialog_mode", action="store_const", const="blind",
                    help="兼容旧写法，等价 --dialog-mode blind（现在本来就是默认，写了不改变行为）")
    ap.add_argument("--wait-dialog", type=float, default=Config.wait_dialog,
                    help=f"盲点「确定」前等确认框画出来的秒数，默认 {Config.wait_dialog}。"
                         f"真机探针实测：框是渐显（点 ↻ 后约 0.7~1.5 s 开始有变化），"
                         f"而点击**从不被吞**——offset 从 −0.45 s 到 +2.50 s 共 23 次点击全部生效。"
                         f"所以这个值不影响成败，只影响盲点那一下落在动画的哪个阶段；"
                         f"0.85 在实测窗口内，除非换了设备/分辨率否则别动")
    ap.add_argument("--blind-settle", type=float, default=Config.blind_settle,
                    help=f"盲点点完「确定」后、去读结果之前额外等的秒数，默认 {Config.blind_settle}。"
                         f"探针实测「点完到框消失」恒定约 1.8 s（23 次 1.54~2.06 s）—— 等满它，"
                         f"读到的那一屏就是最终态；调小会读成「框刚没了、新词条没落屏」的中间态，"
                         f"回落走读框路、每次多花 0.7~1.4 s")
    ap.add_argument("--no-flush-pending", dest="flush_pending", action="store_false",
                    default=Config.flush_pending,
                    help="关掉「收尾补确认」：默认开会在一轮结束时多读一屏，"
                         "若还挂着刷新确认框就补点「确定」并重判那一行"
                         "（最后一次 ↻ 的框常在跑完之后才画出来；关掉会把它留给人工，见 README）")
    a = ap.parse_args(argv)

    book = parse_workbook(a.rules or default_rules_path())
    sweep = not a.no_sweep
    detail_only = not a.allow_list
    speed = dict(wait_after_tap=a.wait_tap, wait_after_screen=a.wait_screen,
                 read_retries=a.read_retries, capture=a.capture,
                 settle_reads=a.settle_reads, settle_delay=a.settle_delay,
                 settle_guard=a.settle_guard,
                 settle_wait_change=a.settle_wait_change,
                 confirm_reads=a.confirm_reads,
                 dialog_mode=a.dialog_mode, wait_dialog=a.wait_dialog,
                 blind_settle=a.blind_settle,
                 flush_pending=a.flush_pending)
    if a.image:
        from .source import ImageSource
        dw, dh = (int(v) for v in a.device.split("x"))
        cfg = Config(dry_run=True, block_index=a.block, max_refresh_per_row=a.limit,
                     tap_sweep=sweep, detail_only=detail_only, ocr=a.ocr, **speed)
        runner = Runner(book, ImageSource(a.image, (dw, dh)), cfg)
        print(f"[OK] OCR 后端: {runner.ocr_name}")
        out = runner.run()
    else:
        from .adb import Adb, find_adb
        from .source import LiveSource
        cfg = Config(dry_run=not a.live, block_index=a.block, max_refresh_per_row=a.limit,
                     tap_sweep=sweep, detail_only=detail_only, ocr=a.ocr, **speed)
        adb = Adb(a.serial, find_adb())
        d = adb.wait_for_device(timeout=a.wait_device)
        if not d:
            print(f"[NG] {a.wait_device:.0f} 秒内 adb 里没有可用设备；"
                  f"请插 USB、手机上点「允许 USB 调试」。当前: {Adb.devices(adb.adb)}")
            return 3
        print(f"[OK] 设备 {d['serial']} {d['state']} {d['info']}")
        runner = Runner(book, LiveSource(adb, work_dir=cfg.work_dir, capture=cfg.capture), cfg)
        print(f"[OK] OCR 后端: {runner.ocr_name}")
        out = runner.run()
    print_summary(out)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:6000])
    return 0 if out.get("status") in ("ready", "dry_run") else 2


if __name__ == "__main__":
    raise SystemExit(main())
