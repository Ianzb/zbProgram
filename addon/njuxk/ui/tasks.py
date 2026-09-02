"""任务页（计划 todo 14）：任务卡片（状态/进度/参数/控制）+ 任务列表页。

线程约定（对齐 ``zbProgram/app/interface/widget.py`` 的 TaskCard 范式）：
- 任务状态/进度/倒计时的更新**只经信号**（``setStatusSignal`` / ``setProgressSignal`` /
  ``setCountdownSignal``）回主线程，工作线程严禁直接操作 QWidget；
- 本模块可在**无 zbProgram 宿主**的测试环境运行：``setting`` 经构造参数注入，
  默认 None 时不持久化（测试传 stub setting 验证写回）；
- 参数改动（延迟区间/重复次数）实时写回 ``state.save_scheduler_config``
  （全局默认调度配置），并发 ``paramsChanged`` 供装配层即时同步给调度器
  （``scheduler.set_task_params``）；非法输入（负数/超大/非数字）被校验拦截并回退旧值；
- 「定时开始」开关切换发 ``timedStartToggled``，由装配层直连
  ``scheduler.set_timed_start``，**切换即生效**（开→进入倒计时或立即开抢；
  关→取消倒计时回到等待中），无需重新点「开始」；
- 任务状态经 :meth:`TaskCard.apply_state` 统一驱动「开始/继续」与「停止」按钮的
  可用性与文案，避免按钮与实际状态不同步。

输入校验说明：计划引用的 ``zbWidgetLib.StrictIntValidator`` 在 zbWidgetLib
3.6.3 中**不存在**（已全量搜索 site-packages 确认，仅有 BoolValidator /
RangeValidator / OptionsValidator 等），按「换用可用组件」规则改用 PySide6 的
``QIntValidator`` / ``QDoubleValidator``。
"""
from __future__ import annotations

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QDoubleValidator, QIntValidator
from qtpy.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    ToolButton,
)

import zbWidgetLib as zbw

from ..core import settings as core_settings
from ..core import state
from ..core.scheduler import TaskState
from .layout import SPACING, apply_card_margins, apply_page_margins
from .text_select import make_selectable


class TaskCard(zbw.CardWidget):
    """单任务卡片：标题 / 状态 / 进度 / 参数区 / 控制按钮。

    信号：
        taskStartRequested(str) —— 点击「开始/继续」，参数为 teaching_class_id
        taskStopRequested(str)  —— 点击「停止」
        taskRemoved(str)        —— 点击「移除」
        timedStartToggled(str, bool) —— 「定时开始」开关切换（teaching_class_id, 是否启用），
            由装配层直连 ``scheduler.set_timed_start``，切换即生效（不经过 start()）
        paramsChanged(str, dict) —— 参数（延迟区间/重复次数）改动，
            由装配层直连 ``scheduler.set_task_params``，下一次尝试即生效
        setStatusSignal(str)    —— 线程安全更新状态文本
        setProgressSignal(int, int) —— 线程安全更新进度 (done, total)；total<=0 表示不限
        setCountdownSignal(str) —— 线程安全更新倒计时文本
        setFinishedSignal(bool, str) —— 线程安全更新终态 (ok, msg)
    """

    taskStartRequested = Signal(str)
    taskStopRequested = Signal(str)
    taskRemoved = Signal(str)
    timedStartToggled = Signal(str, bool)
    paramsChanged = Signal(str, dict)
    setStatusSignal = Signal(str)
    setProgressSignal = Signal(int, int)
    setCountdownSignal = Signal(str)
    setFinishedSignal = Signal(bool, str)

    def __init__(self, course, setting=None, parent=None):
        super().__init__(parent)
        self.course = course
        self.teaching_class_id = course.teaching_class_id
        self._setting = setting
        self._status = "等待中"
        self._done = 0
        self._total = 0
        self._begin_time = ""
        # 是否曾进入终态（停止/失败）：决定「开始」按钮文案是「开始」还是「继续」
        self._stopped_before = False

        cfg = self._load_config()
        self._delay_min = float(cfg.get("delay_min", 1.0))
        self._delay_max = float(cfg.get("delay_max", 2.0))
        self._repeat = int(cfg.get("repeat", 0))
        # 新建卡片「定时开始」开关初始值来自全局默认配置（设置页可改）；
        # 仅影响新建卡片的初始勾选态，创建后用户可随时用开关改（切换即生效）
        self._initial_timed = bool(cfg.get("use_timed_start", True))

        self._build_ui()
        self._connect_signals()
        self._apply_config_to_edits()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.titleLabel = make_selectable(BodyLabel(self))
        self.titleLabel.setText(
            f"{self.course.course_name}（{self.course.course_number}）"
        )
        self.statusLabel = make_selectable(BodyLabel(self._status, self))
        self.statusLabel.setTextColor("#606060", "#d2d2d2")
        self.progressLabel = make_selectable(BodyLabel("已尝试 0 次", self))
        self.progressLabel.setTextColor("#606060", "#d2d2d2")
        self.progressLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.progressBar = zbw.CustomProgressBar(self, useAni=False, indeterminate=False)

        self.timerSwitch = zbw.SwitchButton(self)
        self.timerSwitch.setText("定时开始")
        # 新建任务默认值来自设置页（core.settings.scheduler.use_timed_start，
        # 2026-08-31 需求默认 True）：此时 _connect_signals 尚未执行，
        # 不会误发 timedStartToggled 信号。
        self.timerSwitch.setChecked(getattr(self, "_initial_timed", True))
        self.beginTimeLabel = make_selectable(BodyLabel("批次开始：--", self))
        self.beginTimeLabel.setTextColor("#606060", "#d2d2d2")

        self.delayMinEdit = LineEdit(self)
        self.delayMaxEdit = LineEdit(self)
        self.repeatEdit = LineEdit(self)
        self.delayMinEdit.setValidator(QDoubleValidator(0.0, 3600.0, 2, self))
        self.delayMaxEdit.setValidator(QDoubleValidator(0.0, 3600.0, 2, self))
        self.repeatEdit.setValidator(QIntValidator(0, 999999, self))
        self.delayMinEdit.setFixedWidth(64)
        self.delayMaxEdit.setFixedWidth(64)
        self.repeatEdit.setFixedWidth(64)

        self.startButton = PrimaryPushButton(FIF.PLAY, "开始", self)
        self.stopButton = PushButton(FIF.CLOSE, "停止", self)
        self.removeButton = ToolButton(FIF.DELETE, self)
        self.removeButton.setToolTip("移除任务")

        # 第一行：标题 + 控制按钮
        row1 = QHBoxLayout()
        row1.addWidget(self.titleLabel, 1)
        row1.addWidget(self.startButton)
        row1.addWidget(self.stopButton)
        row1.addWidget(self.removeButton)

        # 第二行：状态 + 进度文本
        row2 = QHBoxLayout()
        row2.addWidget(self.statusLabel, 1)
        row2.addWidget(self.progressLabel)

        # 第三行：定时开始开关 + 批次开始时间
        row3 = QHBoxLayout()
        row3.addWidget(self.timerSwitch)
        row3.addWidget(self.beginTimeLabel, 1)

        # 第四行：随机延迟区间 + 重复次数（参数说明文字同样可选中复制）
        row4 = QHBoxLayout()
        row4.addWidget(make_selectable(BodyLabel("延迟", self)))
        row4.addWidget(self.delayMinEdit)
        row4.addWidget(make_selectable(BodyLabel("~", self)))
        row4.addWidget(self.delayMaxEdit)
        row4.addWidget(make_selectable(BodyLabel("秒", self)))
        row4.addSpacing(12)
        row4.addWidget(make_selectable(BodyLabel("重复", self)))
        row4.addWidget(self.repeatEdit)
        row4.addWidget(make_selectable(BodyLabel("次（0=不限）", self)))
        row4.addStretch(1)

        self.vBoxLayout = QVBoxLayout(self)
        # 边距/间距统一取自 ui.layout（与选课页、收藏页、设置对话框同一套常量）
        apply_card_margins(self.vBoxLayout)
        self.vBoxLayout.addLayout(row1)
        self.vBoxLayout.addLayout(row2)
        self.vBoxLayout.addWidget(self.progressBar)
        self.vBoxLayout.addLayout(row3)
        self.vBoxLayout.addLayout(row4)

    def _connect_signals(self):
        self.startButton.clicked.connect(
            lambda: self.taskStartRequested.emit(self.teaching_class_id)
        )
        self.stopButton.clicked.connect(
            lambda: self.taskStopRequested.emit(self.teaching_class_id)
        )
        self.removeButton.clicked.connect(
            lambda: self.taskRemoved.emit(self.teaching_class_id)
        )
        self.setStatusSignal.connect(self._on_status)
        self.setProgressSignal.connect(self._on_progress)
        self.setCountdownSignal.connect(self._on_countdown)
        self.setFinishedSignal.connect(self._on_finished)
        self.delayMinEdit.editingFinished.connect(self._on_delay_min_edited)
        self.delayMaxEdit.editingFinished.connect(self._on_delay_max_edited)
        self.repeatEdit.editingFinished.connect(self._on_repeat_edited)
        # 定时开始开关：切换即发信号（装配层直连 scheduler.set_timed_start）
        self.timerSwitch.checkedChanged.connect(
            lambda checked: self.timedStartToggled.emit(
                self.teaching_class_id, bool(checked)
            )
        )

    # ------------------------------------------------------------------
    # 线程安全更新（工作线程只调 set_*，内部经信号回主线程）
    # ------------------------------------------------------------------

    def set_status(self, text: str):
        """线程安全更新状态文本（等待中/第 N 次尝试/成功/失败/已停止）。"""
        self.setStatusSignal.emit(text)

    def set_progress(self, done: int, total: int):
        """线程安全更新进度：total<=0 表示不限次数，显示「已尝试 N 次」。"""
        self.setProgressSignal.emit(int(done), int(total))

    def set_countdown(self, seconds: int):
        """线程安全更新倒计时（秒）；等待中状态下状态文本显示「倒计时 Ns」。"""
        self.setCountdownSignal.emit(str(int(seconds)))

    def set_finished(self, ok: bool, msg: str):
        """线程安全更新终态：状态置「成功/失败」，进度区显示结果消息。"""
        self.setFinishedSignal.emit(bool(ok), str(msg))

    def _on_finished(self, ok: bool, msg: str):
        self._on_status("成功" if ok else "失败")
        self.progressLabel.setText(msg)

    def _on_status(self, text: str):
        self._status = text
        self.statusLabel.setText(text)

    def _on_progress(self, done: int, total: int):
        self._done = done
        self._total = total
        if total > 0:
            pct = min(100, int(done / total * 100)) if total else 0
            self.progressBar.setValue(pct)
            self.progressLabel.setText(f"已尝试 {done} / 总数 {total}")
        else:
            self.progressBar.setValue(0)
            self.progressLabel.setText(f"已尝试 {done} 次")

    def apply_state(self, state: str):
        """按调度器状态统一切换「开始/继续」与「停止」按钮的可用性与文案。

        用户实测反馈「开始/停止按钮与实际状态不同步」（运行中还能点开始、已停止
        还能点停止），这里把状态 → 按钮的映射收敛到一处：

        - ``waiting``（未开始/倒计时中）→ 「开始」可用、「停止」禁用；若曾停止过
          且已尝试过（``attempts > 0``），文案改为「继续」。倒计时期间要取消定时
          用「定时开始」开关（``timedStartToggled`` → ``set_timed_start(False)``），
          而不是「停止」；
        - ``running`` → 「开始/继续」禁用、「停止」可用；
        - ``stopped`` / ``failed`` → 「继续」可用、「停止」禁用；
        - ``success`` → 两者都禁用，「开始」按钮文案显示「已完成」。

        ``state`` 取 :class:`~njuxk.core.scheduler.TaskState` 常量；未知值按
        ``waiting`` 处理（保守：允许手动开始）。本方法**不改**状态文本
        （那是 :meth:`set_status` 的职责），避免与 ``clear_finished`` 的
        中文状态判定冲突。
        """
        state = (state or "").strip().lower()
        if state in (TaskState.STOPPED, TaskState.FAILED):
            self._stopped_before = True

        if state == TaskState.RUNNING:
            self.startButton.setEnabled(False)
            self.stopButton.setEnabled(True)
            return
        if state == TaskState.SUCCESS:
            self.startButton.setText("已完成")
            self.startButton.setEnabled(False)
            self.stopButton.setEnabled(False)
            return
        if state in (TaskState.STOPPED, TaskState.FAILED):
            self.startButton.setText("继续")
            self.startButton.setEnabled(True)
            self.stopButton.setEnabled(False)
            return
        # waiting（含未知状态）
        resume = self._stopped_before and self._done > 0
        self.startButton.setText("继续" if resume else "开始")
        self.startButton.setEnabled(True)
        self.stopButton.setEnabled(False)

    def _on_countdown(self, seconds_text: str):
        if self._begin_time:
            self.beginTimeLabel.setText(
                f"批次开始：{self._begin_time}（倒计时 {seconds_text}s）"
            )
        else:
            self.beginTimeLabel.setText(f"倒计时 {seconds_text}s")
        if self._status in ("等待中",) or self._status.startswith("倒计时"):
            self._status = f"倒计时 {seconds_text}s"
            self.statusLabel.setText(self._status)

    # ------------------------------------------------------------------
    # 参数区：校验 + 写回 state
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_float(text: str):
        try:
            value = float(text.strip())
        except (TypeError, ValueError):
            return None
        if value < 0 or value > 3600:
            return None
        return value

    @staticmethod
    def _parse_int(text: str):
        try:
            value = int(float(text.strip()))
        except (TypeError, ValueError):
            return None
        if value < 0 or value > 999999:
            return None
        return value

    def _on_delay_min_edited(self):
        value = self._parse_float(self.delayMinEdit.text())
        if value is None:
            self.delayMinEdit.setText(f"{self._delay_min:g}")
            return
        if value > self._delay_max:
            # 区间倒置：交换（与 state._normalize_scheduler 语义一致）
            self._delay_min, self._delay_max = self._delay_max, value
            self.delayMinEdit.setText(f"{self._delay_min:g}")
            self.delayMaxEdit.setText(f"{self._delay_max:g}")
        else:
            self._delay_min = value
        self._persist_config()

    def _on_delay_max_edited(self):
        value = self._parse_float(self.delayMaxEdit.text())
        if value is None:
            self.delayMaxEdit.setText(f"{self._delay_max:g}")
            return
        if value < self._delay_min:
            self._delay_min, self._delay_max = value, self._delay_min
            self.delayMinEdit.setText(f"{self._delay_min:g}")
            self.delayMaxEdit.setText(f"{self._delay_max:g}")
        else:
            self._delay_max = value
        self._persist_config()

    def _on_repeat_edited(self):
        value = self._parse_int(self.repeatEdit.text())
        if value is None:
            self.repeatEdit.setText(str(self._repeat))
            return
        self._repeat = value
        self._persist_config()

    def _persist_config(self):
        """参数改动：即时同步给调度器 + 写回全局默认调度配置。

        ``paramsChanged`` 由装配层直连 ``scheduler.set_task_params``，使改动在
        **下一次尝试**即生效（不必重新点「开始」）；无 setting 时只发信号不落盘。
        """
        self.paramsChanged.emit(self.teaching_class_id, {
            "delay_min": self._delay_min,
            "delay_max": self._delay_max,
            "repeat": self._repeat,
        })
        if self._setting is None:
            return
        state.save_scheduler_config(self._setting, {
            "delay_min": self._delay_min,
            "delay_max": self._delay_max,
            "repeat": self._repeat,
        })

    def _load_config(self) -> dict:
        if self._setting is not None:
            return state.load_scheduler_config(self._setting)
        return dict(core_settings.DEFAULTS["scheduler"])

    def _apply_config_to_edits(self):
        self.delayMinEdit.setText(f"{self._delay_min:g}")
        self.delayMaxEdit.setText(f"{self._delay_max:g}")
        self.repeatEdit.setText(str(self._repeat))

    def apply_config(self, config: dict):
        """外部注入任务级配置（延迟区间/重复次数），非法值忽略。"""
        if not isinstance(config, dict):
            return
        if "delay_min" in config:
            v = self._parse_float(str(config["delay_min"]))
            if v is not None:
                self._delay_min = v
        if "delay_max" in config:
            v = self._parse_float(str(config["delay_max"]))
            if v is not None:
                self._delay_max = v
        if "repeat" in config:
            v = self._parse_int(str(config["repeat"]))
            if v is not None:
                self._repeat = v
        if self._delay_min > self._delay_max:
            self._delay_min, self._delay_max = self._delay_max, self._delay_min
        self._apply_config_to_edits()

    def set_begin_time(self, begin_time: str):
        """设置批次开始时间（定时开始显示用）。"""
        self._begin_time = begin_time or ""
        if self._begin_time:
            self.beginTimeLabel.setText(f"批次开始：{self._begin_time}")
        else:
            self.beginTimeLabel.setText("批次开始：--")

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    def config(self) -> dict:
        """当前任务参数（供调度器读取）。"""
        return {
            "delay_min": self._delay_min,
            "delay_max": self._delay_max,
            "repeat": self._repeat,
            "begin_time": self._begin_time,
            "timed": self.timerSwitch.isChecked(),
        }


class TaskPage(QWidget):
    """任务列表页：滚动区里的 CardGroup 装 TaskCard，wid = teaching_class_id。

    布局（用户要求「收藏和任务界面的顶栏也要固定」，对齐 ``ui/course.py``）：
    本页**不再**继承 ``zbw.CardGroup``（其整页即列表，清空按钮行会跟着任务卡片
    一起滚走），改为普通 ``QWidget`` 页根布局 + 内部唯一一个纵向
    ``SmoothScrollArea``（``taskScroll``）——「一键清空」工具行固定在页根
    （保持原位置：列表上方），只有任务卡片列表（innerGroup / emptyLabel）在
    ``taskScroll`` 里滚动。``BasicTabPage.addPage`` 只要求 widget 本身，页签
    行为不变。

    **CardGroup 兼容**：本页对外仍被装配层/测试当作 CardGroup 使用（addCard /
    clearCard / removeCard / count / getCards 等），这些 API 一律转发给滚动
    内容里的 ``innerGroup``；``cardCountChanged`` 由 innerGroup 转发重发。

    信号：
        taskStartRequested(str) —— 转发自卡片
        taskStopRequested(str)
        taskRemoved(str)
        timedStartToggled(str, bool) —— 转发自卡片（定时开始开关切换）
        paramsChanged(str, dict)     —— 转发自卡片（延迟/重复次数改动）
        cardCountChanged(int)        —— 转发自 innerGroup（卡片数量变化）
        clearAllRequested()          —— 「一键清空」二次确认后发出，
            由装配层执行（停止全部任务 + 调度器任务表清空 + 移除全部卡片）

    设置入口在 MainPage 顶栏的账号菜单里（原任务页「设置」按钮已移除）。
    """

    # InfoBar / 对话框挂载标记：``ui/cards.py`` 的 ``_dialog_parent`` 沿 parent
    # 链上溯找插件页面（原来只认 ``zbw.BasicTab``），本页改基类后靠此标记继续
    # 被识别为插件页面 —— 弹窗遮罩仍覆盖整个任务页，绝不提升到宿主主窗口
    _info_parent_flag = True

    taskStartRequested = Signal(str)
    taskStopRequested = Signal(str)
    taskRemoved = Signal(str)
    timedStartToggled = Signal(str, bool)
    paramsChanged = Signal(str, dict)
    cardCountChanged = Signal(int)
    clearAllRequested = Signal()

    def __init__(self, setting=None, parent=None):
        super().__init__(parent)
        self._setting = setting
        self._clear_confirm_btn = None
        self._clear_cancel_btn = None
        self._build_ui()
        # innerGroup 的计数变化 → 重发本页信号（驱动空态显隐 + 外部订阅者）
        self.innerGroup.cardCountChanged.connect(self.cardCountChanged.emit)
        self.innerGroup.cardCountChanged.connect(self._update_empty_visibility)

    def _build_ui(self):
        # 页根布局：工具行 + 任务列表滚动区（前者固定，后者滚动）。
        # 页面级边距/间距统一取自 ui.layout（与选课页、收藏页同一套常量）
        self.vBoxLayout = QVBoxLayout(self)
        apply_page_margins(self.vBoxLayout)

        # 顶部工具行：「一键清空」（设置对话框已删除，这是清空任务的唯一入口）
        # —— 固定在页根（保持原位置：列表上方），不随列表滚动
        toolRow = QHBoxLayout()
        toolRow.addStretch(1)
        self.clearAllButton = PushButton(FIF.DELETE, "一键清空", self)
        self.clearAllButton.setToolTip("停止并移除全部任务（含运行中）")
        self.clearAllButton.clicked.connect(self._on_clear_all_clicked)
        toolRow.addWidget(self.clearAllButton)
        self.vBoxLayout.addLayout(toolRow)

        # ---- 列表区：唯一纵向滚动区（顶栏固定在滚动内容之外）----
        # 用户需求「收藏和任务界面的顶栏也要固定」：范式对齐 ui/course.py /
        # ui/settings.py（SmoothScrollArea + widgetResizable + NoFrame + 透明背景）
        self.taskScroll = SmoothScrollArea(self)
        self.taskScroll.setWidgetResizable(True)
        self.taskScroll.setFrameShape(QFrame.NoFrame)
        self.taskScroll.enableTransparentBackground()

        # 滚动内容容器：页面边距已由 apply_page_margins 提供，这里只留少量
        # 上下边距（顶部与固定工具行拉开一点、底部留呼吸），左右为 0
        inner = QWidget(self.taskScroll)
        self.taskScroll.setWidget(inner)
        # inner 必须显式透明：qfw enableTransparentBackground() 只给「当时已
        # setWidget 的内容 widget」设透明样式（本页在其之前调用 → inner 拿不到）；
        # 而宿主 FluentWindow 的 FLUENT_WINDOW qss 会层叠进页面，使 inner
        # autoFillBackground=True 并涂上系统窗口色（亮 #efefef，暗色主题下与
        # 页面背景形成大色块）。写法对齐旧版 zbw.BasicTab（BetterScrollArea.view）。
        inner.setStyleSheet("QWidget {background-color: rgba(0,0,0,0); border: none}")
        innerLayout = QVBoxLayout(inner)
        innerLayout.setContentsMargins(0, 4, 0, 8)
        innerLayout.setSpacing(SPACING)

        # 任务卡片列表（在滚动内容里，自然高度不占 stretch）
        self.innerGroup = zbw.CardGroup(inner, show_title=False, is_v=True)
        innerLayout.addWidget(self.innerGroup)

        self.emptyLabel = make_selectable(BodyLabel("暂无任务", inner))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setTextColor("#606060", "#d2d2d2")
        innerLayout.addWidget(self.emptyLabel)

        # 任务列表滚动区独占剩余纵向空间（stretch=1），工具行保持固有高度
        self.vBoxLayout.addWidget(self.taskScroll, 1)

    # ------------------------------------------------------------------
    # CardGroup 兼容转发（外部仍把 TaskPage 当 CardGroup 使用）
    # ------------------------------------------------------------------

    @property
    def boxLayout(self):
        """兼容别名：历史上 TaskPage 是 CardGroup，``boxLayout`` 指列表布局；
        现指向滚动内容里 innerGroup 的布局（无外部写引用，只读兜底）。"""
        return self.innerGroup.boxLayout

    def addCard(self, card, wid: str | int = None, pos: int = -1):
        return self.innerGroup.addCard(card, wid, pos)

    def addWidget(self, card, wid: str | int = None, pos: int = -1):
        return self.innerGroup.addWidget(card, wid, pos)

    def removeCard(self, wid: str | int):
        return self.innerGroup.removeCard(wid)

    def removeWidget(self, wid: int | str):
        return self.innerGroup.removeWidget(wid)

    def getCard(self, wid: str | int):
        return self.innerGroup.getCard(wid)

    def getWidget(self, wid: str | int):
        return self.innerGroup.getWidget(wid)

    def getCards(self):
        return self.innerGroup.getCards()

    def getWidgets(self):
        return self.innerGroup.getWidgets()

    def getWids(self):
        return self.innerGroup.getWids()

    def getCardMap(self):
        return self.innerGroup.getCardMap()

    def getWidgetMap(self):
        return self.innerGroup.getWidgetMap()

    def count(self):
        return self.innerGroup.count()

    def clearCard(self):
        self.innerGroup.clearCard()

    def clearWidget(self):
        self.innerGroup.clearWidget()

    # ------------------------------------------------------------------
    # 任务增删
    # ------------------------------------------------------------------

    def add_task(self, course, begin_time: str = "", config: dict = None) -> TaskCard:
        """新增任务卡片；同一 teaching_class_id 已存在时提示并返回已有卡片。"""
        tid = course.teaching_class_id
        existing = self.getCard(tid)
        if existing is not None:
            InfoBar.warning(
                title="任务已存在",
                content=f"「{course.course_name}」已在任务列表中",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                # 提示挂任务页自身，随页面显示（不挂宿主主窗口）
                parent=self,
            )
            return existing
        card = TaskCard(course, setting=self._setting, parent=self)
        if begin_time:
            card.set_begin_time(begin_time)
        if config:
            card.apply_config(config)
        card.taskStartRequested.connect(self.taskStartRequested)
        card.taskStopRequested.connect(self.taskStopRequested)
        card.taskRemoved.connect(self._on_card_removed)
        card.timedStartToggled.connect(self.timedStartToggled)
        card.paramsChanged.connect(self.paramsChanged)
        self.addCard(card, wid=tid)
        return card

    def remove_task(self, teaching_class_id) -> TaskCard | None:
        """按 teaching_class_id 移除任务卡片；不存在返回 None。"""
        card = self.getCard(teaching_class_id)
        if card is not None:
            self.removeCard(teaching_class_id)
        return card

    def clear_finished(self):
        """移除所有已结束（成功/失败/已停止）的任务卡片。"""
        for card in list(self.getCards()):
            if card.status in ("成功", "失败", "已停止"):
                self.removeCard(card.teaching_class_id)

    def clear_all(self):
        """移除全部任务卡片（「一键清空」的页面侧动作）。

        调度器侧（停止 + 任务表清除）由装配层在 ``clearAllRequested`` 处理；
        这里只清卡片，不逐卡发 ``taskRemoved``（避免与装配层的批量移除重复）。
        """
        self.clearCard()

    # ------------------------------------------------------------------
    # 一键清空（二次确认后交装配层执行）
    # ------------------------------------------------------------------

    def _on_clear_all_clicked(self):
        """「一键清空」：二次确认（InfoBar 确定/取消，沿用清空收藏范式）。

        确认 → 发 ``clearAllRequested``（执行在装配层）；取消 → 无任何变化。
        没有任务时不弹确认框直接返回。
        """
        if self.count() == 0:
            return
        infoBar = InfoBar(
            InfoBarIcon.WARNING,
            "一键清空",
            "确定清空全部任务？（正在运行的任务也会停止，该操作无法撤销！）",
            isClosable=False,
            duration=-1,
            # 提示挂任务页自身，随页面显示（不挂宿主主窗口）
            parent=self,
        )

        def confirm():
            infoBar.close()
            self.clearAllRequested.emit()

        def cancel():
            infoBar.close()

        self._clear_confirm_btn = PushButton(text="确定")
        self._clear_confirm_btn.clicked.connect(confirm)
        infoBar.addWidget(self._clear_confirm_btn)

        self._clear_cancel_btn = PushButton(text="取消")
        self._clear_cancel_btn.clicked.connect(cancel)
        infoBar.addWidget(self._clear_cancel_btn)
        infoBar.show()

    def get_task(self, teaching_class_id) -> TaskCard | None:
        return self.getCard(teaching_class_id)

    def _on_card_removed(self, tid: str):
        self.removeCard(tid)
        self.taskRemoved.emit(tid)

    def _update_empty_visibility(self, count: int):
        self.emptyLabel.setHidden(count > 0)
