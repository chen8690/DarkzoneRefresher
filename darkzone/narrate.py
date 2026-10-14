"""把 runner 的事件流实时翻成「人能看懂的中文」旁白。

为什么需要它：`logs/events.jsonl` 与 `logs/run_*.json` 里的 `events` 是**机器可读**的，
字段名都是 `kind` / `tag` / `rows_changed` 这种；直接刷到控制台或者 GUI 日志框里，
不懂这套事件的人看不出「现在在干嘛」。这个模块把每个事件翻成一句中文，
由 `Runner.log()` 实时打印 —— 事件本身照旧原样写文件，两份互不干扰。

三条约定（改这个文件时请守住）：

1. **旁白里绝不出现事件 kind 的字面量**（`fast_confirm` / `read_retry` / `sweep_hit` …）。
   离线套件里有 `"fast_confirm" not in log`、`'"kind": "read_retry"' not in log` 这类
   反向断言，中文旁白一旦回显这些英文标识符就会误伤它们。
2. **拿不准的只描述现象，不下因果结论**。例如「框没了但名字还没变」就照实这么说，
   不能写成「没点进去」—— 到底是点空了还是词条还没画出来，代码自己也不确定。
3. 尽量把坐标说成人话（`[2937, 520]` → 「第 2 行的刷新键」）。坐标只在没有别的说法时才报。

翻不出来的事件返回 `None`（调用方就不打印）—— 这比硬凑一句好：
宁可不说话，也不说错话。
"""

from __future__ import annotations

import re

# 取屏链 → 人话。`chain` 字段是 runner 里 LiveSource 记的「这一屏实际走的档」。
_CHAIN = {
    "stream": "视频流取帧",
    "gzip": "设备端 gzip",
    "raw": "原始帧",
    "png": "设备端 PNG",
    "fast": "原始帧+本地压缩",
}

# 一轮结束的 status → 人话（与 print_summary 的口径保持一致）
_STATUS = {
    "ready": "任务完成（三行全在池内，已停手）",
    "dry_run": "预演完成（一次都没点）",
    "incomplete": "未完成（有行没刷到池内）",
    "give_up": "放弃（到了刷新上限）",
    "error": "出错停手",
}

# settle_guard 的 why → 人话（那段固定等待是花在哪一步上的）
_GUARD_WHY = {
    "blind_confirm": "盲点确认之后",
    "dialog_ok": "点完「确定」之后",
    "settle": "稳定复读之前",
}

# abort 的 phase → 人话（是跑到哪一步才停的手）
_PHASE = {
    "start": "开场检查",
    "safety": "安全阀",
    "list": "列表页",
    "select": "点「选择」",
    "row": "刷装备",
    "boss": "刷「暗区首领」",
}


def _slot_of(tag) -> int | None:
    """从 tag 里抠出槽位号：`slot2_refresh1_fastok` → 2。"""
    m = re.match(r"slot(\d+)_", str(tag or ""))
    return int(m.group(1)) if m else None


def _row_desc(tag) -> str:
    s = _slot_of(tag)
    return f"第 {s} 行" if s else "这一行"


def _tag_action(tag, prefix="") -> str | None:
    """把「点哪儿」翻成人话。内部步骤（`_fastok` / `_ok` / `_settle2` …）返回 None。

    那些内部步骤前面已经有一句更清楚的话了（盲点快路 / 读到确认框 → 点「确定」），
    再打印一行坐标只会把日志刷乱。
    """
    tag = str(tag or "")
    m = re.match(r"^slot(\d+)_refresh(\d+)$", tag)
    if m:
        return f"{prefix}点第 {m.group(1)} 行的刷新键（第 {m.group(2)} 次刷新）"
    m = re.match(r"^boss_refresh(\d+)$", tag)
    if m:
        return f"{prefix}点「暗区首领」那一栏的刷新键（第 {m.group(1)} 次）"
    if tag == "select":
        return f"{prefix}点「选择」，进详情页"
    return None


def _pool_desc(pool) -> str:
    pool = list(pool or [])
    return f"{len(pool)} 件：" + " / ".join(str(x) for x in pool) if pool else "（空）"


def _plan_block(ev) -> str:
    """dry-run 的 `plan` 事件 → 多行中文计划（预演本来就是要让人看清楚的）。"""
    block = ev.get("block") or {}
    lines = ["预演计划（只读屏，一次都不点）"]
    head = "／".join(str(x) for x in (block.get("title"), block.get("timing"),
                                      block.get("condition")) if x)
    if head:
        lines.append(f"  规则块：{head}")
    excluded = block.get("excluded") or []
    if excluded:
        lines.append(f"  排除名单：{'、'.join(str(x) for x in excluded)}")
    # 各行的装备池：注意 `block["columns"]` 是列号（1/2/3），不是能读的标题，
    # 真正有用的是每一行的池子，所以从 rows 里取。
    for r in ev.get("rows") or []:
        pool = r.get("pool") or []
        if pool:
            lines.append(f"  第 {r.get('slot')} 行装备池（{len(pool)} 件）："
                         + " / ".join(str(x) for x in pool))
    lines.append(f"  当前屏面：{_SCREEN_KIND.get(ev.get('screen'), ev.get('screen'))}")
    for r in ev.get("rows") or []:
        where = "在池内，不用刷" if r.get("in_pool") else f"不在池内，会点它的刷新键 {r.get('refresh_tap')}"
        lines.append(f"  第 {r.get('slot')} 行 现在「{r.get('raw')}」→ {where}")
    boss = ev.get("boss")
    if isinstance(boss, dict):
        lines.append(f"  「暗区首领」当前「{boss.get('name')}」，"
                     f"{'在排除名单里 → 会刷' if boss.get('excluded') else '不在排除名单 → 不刷'}")
    elif boss:
        lines.append(f"  暗区首领：{boss}")
    lines.append(f"  每行最多刷 {ev.get('limit')} 次；取屏 {_CHAIN.get(str(ev.get('chain')), '自动')}")
    return "\n".join(lines)


_SCREEN_KIND = {"detail": "详情页", "list": "列表页", "dialog": "一个弹窗", "unknown": "认不出来"}


def narrate(ev: dict) -> str | None:
    """一个事件 → 一句中文；翻不出来返回 None。"""
    if not isinstance(ev, dict):
        return None
    k = ev.get("kind")

    # ---- 开场与屏面 --------------------------------------------------------
    if k == "start":
        mode = "安全预演（只读屏，一次都不点）" if ev.get("dry_run") else "真机运行"
        dlg = "盲点快路" if str(ev.get("dialog_mode")) == "blind" else "读屏确认"
        bits = [f"开始：{mode}", f"确认框走「{dlg}」"]
        if str(ev.get("dialog_mode")) == "blind":
            bits.append(f"点完「确定」等 {ev.get('blind_settle')} 秒再复核")
        bits.append(f"每行最多刷 {ev.get('limit')} 次")
        bits.append(f"OCR={ev.get('ocr')}")
        worker = ev.get("win_ocr_worker")
        if worker and worker != "ok":
            bits.append(f"常驻 OCR worker：{worker}")
        return "；".join(bits) + "。"

    if k in _SCREEN_KIND:
        # 注意：这一条事件的 kind 会被屏面种类覆盖（`log("screen", kind=...)`），
        # 所以 detail / list / dialog 三种 kind 都可能是「刚读了一屏」。
        name = _SCREEN_KIND[k]
        chain = _CHAIN.get(str(ev.get("chain")))
        tail = f"（{chain}）" if chain else ""
        if k == "detail":
            return f"读到「武装派遣」详情页{tail}"
        if k == "list":
            return f"读到「武装派遣」列表页{tail}"
        return f"屏上是{name}"

    if k == "plan":
        return _plan_block(ev)

    # ---- 点哪儿 ------------------------------------------------------------
    if k == "tap":
        return _tag_action(ev.get("tag"))

    if k == "would_tap":
        return _tag_action(ev.get("tag"), prefix="【预演】")

    if k == "refuse_tap":
        return f"⛔ 安全阀拒绝这次点击：{ev.get('reason')}"

    if k == "sweep_hit":
        return (f"{_row_desc(ev.get('tag'))}：第一次点没有生效 → 换候选点再点"
                f"（第 {ev.get('tries')} 个，偏移 {ev.get('offset')}）")

    if k == "confirm_dialog":
        return f"{_row_desc(ev.get('tag'))}：补点一次「确定」"

    # ---- 确认弹窗 ----------------------------------------------------------
    if k == "fast_confirm":
        return "盲点快路：不读弹窗那一屏，直接点「确定」"

    if k == "would_confirm":
        return f"【预演】会在确认框上点「确定」（{ev.get('prompt')}）"

    if k == "auto_confirm":
        return f"读到刷新确认框「{ev.get('prompt')}」→ 点「确定」"

    if k == "dialog_ready":
        return f"重读第 {ev.get('tries')} 次，确认是刷新确认框 → 点「确定」"

    if k == "dialog_gone":
        return "弹窗自己消失了（不必点「确定」）"

    if k == "dialog_stuck":
        return f"⚠️ 点了「确定」弹窗还在（{ev.get('prompt')}）→ 停手，不接着点"

    if k == "refuse_dialog":
        return (f"⚠️ 屏上那个弹窗不是「刷新」确认框（{ev.get('prompt')}）"
                f"→ 拒绝点「确定」，停手")

    if k == "blind_missed":
        return "点完发现确认框还在（这一下点早了）→ 交回「读框再点」那条路补点一次"

    if k == "blind_unclear":
        return "点完那一屏：框已经没了，但这一行的名字还没变 → 继续等新词条落屏（这一屏不记账）"

    # ---- 等待与稳定 --------------------------------------------------------
    if k == "settle_guard":
        why = _GUARD_WHY.get(str(ev.get("why")), "这一步")
        return f"等 {ev.get('wait')} 秒动画窗口（券已经扣了、新词条还没画出来）——{why}"

    if k == "read_retry":
        return (f"这一屏还没稳定，再读一次（第 {ev.get('tries')} 次，"
                f"等 {ev.get('wait')} 秒）")

    if k == "wait_change":
        slot = ev.get("slot")
        where = f"第 {slot} 行" if slot else "这一行"
        if str(ev.get("screen_kind")) == "dialog":
            return f"等{where}的新词条时等出了一个确认框 → 交回读框那条路处理"
        raw = f"（现在是「{ev.get('raw')}」）" if ev.get("raw") else ""
        if not ev.get("reads"):
            return f"{where}名字没有变{raw}" if not ev.get("changed") else f"{where}名字已经变了{raw}"
        if ev.get("changed"):
            return f"{where}的新词条落屏了：读了 {ev.get('reads')} 屏、等了 {ev.get('waited')} 秒{raw}"
        return (f"等{where}的新词条：读了 {ev.get('reads')} 屏、等了 {ev.get('waited')} 秒，"
                f"名字还是旧的{raw} → 不记账")

    if k == "settle":
        slot = ev.get("slot")
        where = f"第 {slot} 行" if slot else "画面"
        changed = "名字已经换了" if ev.get("rows_changed") else "名字还没换"
        return f"{where}画面稳定了：稳定复读 {ev.get('reads')} 屏（{changed}）"

    # ---- 词条与判定 --------------------------------------------------------
    if k == "row_check":
        slot = ev.get("slot")
        raw = ev.get("raw")
        if ev.get("precheck"):
            return f"第 {slot} 行「{raw}」本来就在池内 → 不动它（顺手体检）"
        if ev.get("in_pool"):
            done = "✅ 刷到了池内的装备" if ev.get("attempt") else "在池内"
            return f"第 {slot} 行「{raw}」{done} → 不用再刷"
        if ev.get("attempt"):
            return f"第 {slot} 行刷新后是「{raw}」→ 还不在池内"
        return f"第 {slot} 行现在「{raw}」→ 不在池内"

    if k == "confirm_out":
        return (f"刷卡之前再确认一次：第 {ev.get('slot')} 行「{ev.get('raw')}」"
                f"（{ev.get('agree')}/{ev.get('need')} 次一致）")

    if k == "confirm_in_pool":
        return (f"再读一次发现第 {ev.get('slot')} 行「{ev.get('raw')}」其实在池内"
                f" → 不点刷新，省下 500 券")

    if k == "confirm_unclear":
        rows = ev.get("rows")
        extra = f"，读到 {rows} 行" if rows is not None else ""
        return (f"⚠️ 刷卡之前的复核看不清楚（这屏是 {_SCREEN_KIND.get(str(ev.get('got')), ev.get('got'))}"
                f"{extra}）→ 这一行停手，绝不瞎点")

    if k == "row_missing":
        return f"⚠️ 读不到第 {ev.get('slot')} 行 → 这一行停手"

    if k == "row_count_unexpected":
        return f"⚠️ 详情页读到 {ev.get('got')} 行、期望 {ev.get('want')} 行 → 停手"

    if k == "unexpected_screen":
        return (f"⚠️ 画面不是这一步要的了（读到 "
                f"{_SCREEN_KIND.get(str(ev.get('got')), ev.get('got'))}）→ 停手")

    if k == "no_change":
        return f"⚠️ 点了之后画面毫无变化（连续第 {ev.get('stale')} 次）"

    if k == "boss_check":
        name = ev.get("name")
        if ev.get("excluded"):
            return f"「暗区首领」当前是「{name}」，在排除名单里 → 继续刷"
        return f"「暗区首领」当前是「{name}」，不在排除名单里 → 留着"

    if k == "entered_detail":
        return f"进到详情页了（首领「{ev.get('boss')}」）"

    # ---- 记账 --------------------------------------------------------------
    if k in ("charge", "row"):
        ck = ev.get("charge_kind") or ("boss" if k == "row" and ev.get("boss") else "row")
        if ck == "boss":
            desc = f"「暗区首领」那一栏第 {ev.get('attempt')} 次刷新成功"
        else:
            desc = f"第 {ev.get('slot')} 行第 {ev.get('attempt')} 次刷新成功"
        spent = f"💰 {desc} → 扣 {ev.get('amount')} 券（本次累计 {ev.get('total')}）"
        if ev.get("item"):
            spent += f"，刷掉的是「{ev.get('item')}」"
        if ev.get("sec") is not None:
            spent += f"，本发用时 {ev.get('sec')} 秒"
        return spent

    if k == "give_up":
        if ev.get("phase") == "list":
            return (f"暗区首领刷了 {ev.get('attempts')} 次还是排除名单里的人"
                    f"（当前「{ev.get('name')}」）→ 停手")
        return (f"第 {ev.get('slot')} 行刷到上限（{ev.get('attempts')} 次）停手，"
                f"现在是「{ev.get('item')}」")

    # ---- 收尾 --------------------------------------------------------------
    if k == "validate":
        return f"规则校验：{ev.get('summary')}"

    if k == "stop_before_dispatch":
        head = "✅ 三行都在池内了" if ev.get("ready") else "停手了（还有行没进池）"
        return f"{head} →「派遣」按钮没有点，由你来点"

    if k == "pending_dialog":
        return (f"跑完才发现第 {ev.get('slot')} 行的确认框（画得比读屏晚）"
                f"→ 补点「确定」并按新词条重判这一行")

    if k == "pending_dialog_left":
        return (f"⚠️ 屏上还挂着一个弹窗（{ev.get('prompt')}）——不是刷新确认框，"
                f"请手动关掉再跑")

    if k == "final_recheck":
        raw = ev.get("raw") or "（没读到）"
        where = "在池内" if ev.get("in_pool") else "不在池内"
        return f"补确认之后第 {ev.get('slot')} 行是「{raw}」→ {where}"

    if k == "done":
        status = _STATUS.get(str(ev.get("status")), ev.get("status"))
        tail = f"，用时 {ev.get('elapsed_s')} 秒" if ev.get("elapsed_s") is not None else ""
        return f"本轮结束：{status}{tail}。详细报告：{ev.get('report')}"

    # ---- 停手 / 提示 -------------------------------------------------------
    if k == "abort":
        # 注意：这个分支里**不能**去读 `ev["kind"]` —— 那就是 "abort" 本身
        # （`log()` 里 kw 会覆盖 kind，好在没有哪个 abort 调用传 kind=…）。
        # 想知道「当前读到的是哪一屏」，字段名是 `got`。
        phase = _PHASE.get(str(ev.get("phase")), "")
        reason = ev.get("reason")
        bits = [f"⛔ {'在' + phase + '时' if phase else ''}停手：{reason}"]
        if ev.get("msg"):
            bits.append(str(ev["msg"]))
        if ev.get("prompt"):
            bits.append(f"屏上那个弹窗是「{ev['prompt']}」")
        if ev.get("got"):
            bits.append(f"当前读到的是{_SCREEN_KIND.get(str(ev['got']), ev['got'])}")
        if ev.get("item"):
            bits.append(f"现在是「{ev['item']}」")
        return "；".join(bits)

    if k == "warn":
        return f"⚠️ {ev.get('msg')}"

    # 认不出来的事件：不硬凑（见文件头第 3 条约定）。
    return None