"""设置对话框（需求「加入设置页面，入口位于账号按钮右侧」）。

入口：``MainPage`` 顶栏账号按钮右侧的「设置」工具按钮（未登录也可见 —— 设置是
全局默认值，不依赖登录态）；点击发 ``settingsRequested``，由装配层
``XkApp._open_settings`` 构造本对话框并 ``exec()``。

结构（照 ``ui.cards.CourseInfoDialog`` 的 MessageBoxBase 范式：整体内容放进
``SmoothScrollArea`` 可滚动 + ``enableTransparentBackground`` 透明背景，
**禁用 TextEdit 家族**，一律 LineEdit / SwitchButton）：

- 分组一「新建任务默认值」：新建任务卡片的初始参数
  （默认开启定时开始 / 随机延迟区间 / 每轮最大尝试次数）；
- 分组二「调度器」：调度器运行参数
  （并发线程数 / 最小请求间隔 / QoS 退避基数与上限）。

生效时机（写入 MANUAL_TEST.md 的口径）：
- 「新建任务默认值」在**下一张新建任务卡片**上生效（既有卡片不受影响）；
- ``min_interval`` / ``max_workers`` / ``qos_backoff_*`` 在**下次任务启动**
  （调度器重新装配/读配置）时生效。

行为约定：
- 打开时从 ``state.load_scheduler_config`` 读取现值填充，**不做任何网络请求**；
- 「保存」（yesButton，经 ``validate()`` 钩子）：全部输入校验通过 →
  ``state.save_scheduler_config`` 落盘（含新键 use_timed_start / min_interval）→
  InfoBar.success → 关闭；非法输入 → InfoBar.warning 指明字段，**不关窗**；
- delay_min > delay_max 时按 ``get_scheduler_config`` 现有「区间交换」语义
  直接交换后保存；
- 「取消」（cancelButton）与右上角关闭 / Esc（reject）等同取消，不写任何值。
"""
from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtGui import QDoubleValidator, QIntValidator
from qtpy.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    SmoothScrollArea,
    StrongBodyLabel,
    SwitchButton,
)

import zbWidgetLib as zbw

from ..core import state
from ..core.settings import DEFAULTS
from .layout import apply_card_margins


def _parse_float(text, lo: float, hi: float):
    """宽松 float 解析（对齐 TaskCard._parse_float），越界 / 非数字返回 None。"""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value < lo or value > hi:
        return None
    return value


def _parse_int(text, lo: int, hi: int):
    """宽松 int 解析（对齐 TaskCard._parse_int），越界 / 非数字返回 None。"""
    try:
        value = int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None
    if value < lo or value > hi:
        return None
    return value


class SettingsDialog(MessageBoxBase):
    """全局默认调度配置设置弹窗（两个分组，键名行 + 右侧控件）。"""

    #: 弹窗固定宽度（照 CourseInfoDialog.WIDTH 范式；两列行布局 560 足够）
    WIDTH = 560

    # 分组一「新建任务默认值」：key = scheduler 配置键
    _DEFAULTS_GROUP = [
        # (控件类型, key, 键名, validator 参数 / 说明)
        ("switch", "use_timed_start", "默认开启定时开始", None),
        ("float", "delay_min", "随机延迟下限（秒）", (0.0, 3600.0, 2)),
        ("float", "delay_max", "随机延迟上限（秒）", (0.0, 3600.0, 2)),
        ("int", "repeat", "每轮最大尝试次数（0=不限）", (0, 999999)),
    ]

    # 分组二「调度器」
    _SCHEDULER_GROUP = [
        ("int", "max_workers", "并发线程数", (1, 16)),
        ("float", "min_interval", "最小请求间隔（秒）", (0.05, 10.0, 2)),
        ("float", "qos_backoff_base", "QoS 退避基数（秒）", (0.0, 120.0, 2)),
        ("float", "qos_backoff_max", "QoS 退避上限（秒）", (0.0, 600.0, 2)),
    ]

    def __init__(self, setting=None, parent=None):
        super().__init__(parent=parent)
        self._setting = setting
        # (key, 控件, 类型, 校验下界, 校验上界, 键名) —— validate 用
        self._float_rows = []
        self._int_rows = []
        self._rows_by_key = {}

        self._build_ui()
        self._load()

    # ------------------------------------------------------------------
    # UI 构建（照 CourseInfoDialog：滚动区 + 透明背景 + contentLayout）
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.widget.setFixedWidth(self.WIDTH)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")

        self.scrollArea = SmoothScrollArea(self.widget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setFrameShape(QFrame.NoFrame)
        inner = QWidget(self.scrollArea)
        self.scrollArea.setWidget(inner)
        self.contentLayout = QVBoxLayout(inner)
        self.contentLayout.setContentsMargins(24, 4, 24, 8)
        self.contentLayout.setSpacing(8)
        # 滚动区 + 内容容器全透明（跟随 MessageBoxBase 内容区背景）
        self.scrollArea.enableTransparentBackground()

        self.viewLayout.addWidget(self.scrollArea)

        # ---- 分组一：新建任务默认值 ----
        self.defaultsSubtitle = StrongBodyLabel("新建任务默认值", self.widget)
        self.contentLayout.addWidget(self.defaultsSubtitle)
        self.defaultsCard = self._build_group_card(self._DEFAULTS_GROUP)
        self.contentLayout.addWidget(self.defaultsCard)

        # ---- 分组二：调度器 ----
        self.schedulerSubtitle = StrongBodyLabel("调度器", self.widget)
        self.contentLayout.addWidget(self.schedulerSubtitle)
        self.schedulerCard = self._build_group_card(self._SCHEDULER_GROUP)
        self.contentLayout.addWidget(self.schedulerCard)

    def _build_group_card(self, fields) -> QWidget:
        """一个分组 = CardWidget 容器 + 逐行（键名 BodyLabel 左 / 控件右）。"""
        card = zbw.CardWidget(self.widget)
        layout = QVBoxLayout(card)
        apply_card_margins(layout)
        for kind, key, label, bounds in fields:
            row = QHBoxLayout()
            keyLabel = BodyLabel(label, card)
            row.addWidget(keyLabel, 1)
            if kind == "switch":
                control = SwitchButton(card)
                control.setText("")
                self._switch = control
                self._rows_by_key[key] = (control, kind, None, None, label)
            else:
                control = LineEdit(card)
                control.setFixedWidth(140)
                if kind == "float":
                    lo, hi, decimals = bounds
                    control.setValidator(QDoubleValidator(lo, hi, decimals, control))
                    self._float_rows.append((key, control, lo, hi, label))
                else:
                    lo, hi = bounds
                    control.setValidator(QIntValidator(lo, hi, control))
                    self._int_rows.append((key, control, lo, hi, label))
                self._rows_by_key[key] = (control, kind, lo, hi, label)
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(row)
        return card

    # ------------------------------------------------------------------
    # 现值加载 / 收集校验
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        """打开时读现值（无 setting 用 DEFAULTS；不做任何网络请求）。"""
        if self._setting is not None:
            return state.load_scheduler_config(self._setting)
        return dict(DEFAULTS["scheduler"])

    def _load(self):
        cfg = self._load_config()
        for key, (control, kind, lo, hi, label) in self._rows_by_key.items():
            if kind == "switch":
                control.setChecked(bool(cfg.get(key, True)))
            elif kind == "float":
                control.setText(f"{float(cfg.get(key, 0)):g}")
            else:
                control.setText(str(int(cfg.get(key, 0))))

    def _collect(self):
        """校验并收集全部输入 -> (cfg dict, 错误文案)；全部合法时错误为 None。"""
        cfg = {}
        for key, control, lo, hi, label in self._float_rows:
            value = _parse_float(control.text(), lo, hi)
            if value is None:
                return None, f"「{label}」输入无效（合法范围 {lo:g} ~ {hi:g}）"
            cfg[key] = value
        for key, control, lo, hi, label in self._int_rows:
            value = _parse_int(control.text(), lo, hi)
            if value is None:
                return None, f"「{label}」输入无效（合法范围 {lo} ~ {hi} 的整数）"
            cfg[key] = value
        cfg["use_timed_start"] = bool(self._switch.isChecked())
        # 区间倒置：按 get_scheduler_config 的「区间交换」语义直接交换
        if cfg["delay_min"] > cfg["delay_max"]:
            cfg["delay_min"], cfg["delay_max"] = cfg["delay_max"], cfg["delay_min"]
            self._rows_by_key["delay_min"][0].setText(f"{cfg['delay_min']:g}")
            self._rows_by_key["delay_max"][0].setText(f"{cfg['delay_max']:g}")
        return cfg, None

    # ------------------------------------------------------------------
    # 保存（yesButton → validate() → True 才 accept 关闭）
    # ------------------------------------------------------------------

    def _info_parent(self):
        """保存结果提示挂弹窗的父窗口（MainPage），随插件页面显示；
        无父窗口（纯测试）时挂弹窗自身，绝不挂宿主主窗口。"""
        return self.parent() if self.parent() is not None else self

    def _popup(self, icon: InfoBarIcon, title: str, content: str):
        InfoBar(
            icon,
            title,
            content,
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self._info_parent(),
        )

    def validate(self) -> bool:
        """保存前校验（qfw MessageBoxBase 钩子）：合法 → 落盘并关窗，否则不关窗。"""
        cfg, error = self._collect()
        if error is not None:
            self._popup(InfoBarIcon.WARNING, "设置未保存", error)
            return False
        if self._setting is not None:
            state.save_scheduler_config(self._setting, cfg)
        self._popup(
            InfoBarIcon.SUCCESS,
            "设置已保存",
            "新建任务默认值对下一张新任务卡生效；调度器参数在下次任务启动时生效",
        )
        return True
