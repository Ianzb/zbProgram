"""课程卡片（计划 todo 11）：单门课程的信息展示 + 收藏/报名/抢课按钮。

展示结构（对齐用户期望的卡片样式）：
- 第一行：``已收藏`` 徽标（状态来自**本地收藏集**，由外部传入 ``favorited``，
  严禁读取课程响应里的 ``favorite`` 字段）+ ``已报名`` 徽标（``is_choose=="1"``，
  用 ``InfoLevel.SUCCESS`` 与「已收藏」区分）+ 课程编号 + 课程名 + 学分；
- 第二行：教师姓名；
- 第三行：时间地点（``teaching_place``）；
- 第四行：校区 + 已选/容量（``number_of_selected / class_capacity``）+ 选中概率
  （``models.selection_probability(course, tactic_name)``）；
- 第五行：课程简介（``kcjj``，超长省略为单行 + ``setToolTip`` 显示全文）；
- 右侧按钮：详细信息（``TransparentToolButton(FIF.INFO)``，弹出
  ``CourseInfoDialog`` 只读展示课程全部字段 + 介绍长文本，见类尾）、收藏/取消收藏
  （``PushButton(FIF.HEART, "收藏")``）、立即报名
  （``PrimaryPushButton``，单次提交一次报名请求）、加入抢课
  （``PushButton(FIF.PLAY, "加入抢课")``，加入任务列表持续尝试）、退选
  （``PushButton(FIF.DELETE, "退选")``，仅已报名时显示，带二次确认）。

文本可选中复制：卡片上全部 ``QLabel``（课程名/教师/学分/时间地点/容量/概率/
类别/简介等）经 ``ui.text_select.make_selectable`` 升级为可拖选 + 右键复制。

按钮语义（用户实测反馈「报名 / 开始抢课」语义重复，已拆分）：
- ``立即报名``：**单次**向服务器提交一次报名请求，结果即时弹 InfoBar；
- ``加入抢课``：加入任务列表，按「定时开始 / 随机延迟 / 重复次数」**持续**尝试，
   直到成功或次数用尽；
- ``退选``：退掉已报名的课（网页 UI 用词是「退选」，不是「退课」），强制二次确认。

已报名（``is_choose=="1"``）时：``立即报名`` 与 ``加入抢课`` **隐藏**（已报名的课
不能再报名/抢课），改显示 ``退选``；未报名但已满（``is_full=="1"``）时：两个报名
按钮**同时禁用**并给出原因 tooltip（已满无需再报名，也无需再抢）。

组件可用性说明：qfluentwidgets 1.11.3 的 ``FluentIcon`` 只有 ``HEART`` 一个心形图标
（无 HEART_FILLED），因此收藏按钮切换时图标保持 ``FIF.HEART``，仅文本在
「收藏 / 取消收藏」间切换；徽标用 ``zbw.InfoBadge``。``zbw.setNewToolTip`` 不存在，
简介全文提示用 ``setToolTip``。

收藏按钮**必须**用 ``PushButton`` 而非 ``ToolButton``：``ToolButton`` 是纯图标按钮，
对它 ``setText`` 会让文字压在图标上（用户实测「收藏」与 ❤️ 重叠）。``PushButton``
的 ``sizeHint`` 会为「图标 + 间距 + 文字」预留独立布局空间（实测：纯文字 54px →
带图标 78px），文字与图标不重叠。
"""
from __future__ import annotations

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QFontMetrics
from qtpy.QtWidgets import QHBoxLayout, QVBoxLayout

from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    InfoLevel,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TextBrowser,
    TransparentToolButton,
)

import zbWidgetLib as zbw

from ..api import models
from .text_select import make_selectable


class CourseCard(zbw.CardWidget):
    """单门课程卡片（教学班粒度）。"""

    favoriteToggled = Signal(str, bool)
    enrollRequested = Signal(str)
    grabRequested = Signal(str)
    dropRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.course = None
        self._tactic_name = ""
        self._favorited = False
        self._chosen = False
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.favBadge = zbw.InfoBadge("已收藏", self)
        self.favBadge.hide()
        # 已报名徽标：用 SUCCESS 变体（绿色）与「已收藏」（ATTENTION 红）区分
        self.chosenBadge = zbw.InfoBadge("已报名", self, InfoLevel.SUCCESS)
        self.chosenBadge.hide()

        self.courseNumberLabel = make_selectable(BodyLabel(self))
        self.courseNumberLabel.setTextColor("#606060", "#d2d2d2")
        self.courseNameLabel = make_selectable(StrongBodyLabel(self))
        self.creditLabel = make_selectable(BodyLabel(self))
        self.creditLabel.setTextColor("#606060", "#d2d2d2")
        self.teacherLabel = make_selectable(BodyLabel(self))
        self.placeLabel = make_selectable(BodyLabel(self))
        self.campusLabel = make_selectable(BodyLabel(self))
        self.capacityLabel = make_selectable(BodyLabel(self))
        self.probabilityLabel = make_selectable(BodyLabel(self))
        self.introLabel = make_selectable(BodyLabel(self))
        self.introLabel.setWordWrap(False)

        # PushButton（图标 + 文字正常排版），不能用 ToolButton（纯图标，文字会压图标）
        self.favButton = PushButton(FIF.HEART, "收藏", self)
        self.favButton.setToolTip("收藏到本地收藏集")
        self.enrollButton = PrimaryPushButton("立即报名", self)
        self.grabButton = PushButton(FIF.PLAY, "加入抢课", self)
        # 退选：网页 UI 用词是「退选」，仅已报名时显示，点击后由装配层弹二次确认
        self.dropButton = PushButton(FIF.DELETE, "退选", self)
        self.dropButton.hide()
        # 详细信息：纯图标按钮（弹窗展示课程全部信息 + 介绍长文本），放按钮列顶部
        self.infoButton = TransparentToolButton(FIF.INFO, self)
        self.infoButton.setToolTip("详细信息")

        # 第一行：徽标 + 编号 + 课程名 + 学分
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(self.favBadge)
        row1.addWidget(self.chosenBadge)
        row1.addWidget(self.courseNumberLabel)
        row1.addWidget(self.courseNameLabel, 1)
        row1.addWidget(self.creditLabel)

        # 第二行：教师
        row2 = QHBoxLayout()
        row2.addWidget(self.teacherLabel)

        # 第三行：时间地点
        row3 = QHBoxLayout()
        row3.addWidget(self.placeLabel)

        # 第四行：校区 + 已选/容量 + 选中概率
        row4 = QHBoxLayout()
        row4.setSpacing(16)
        row4.addWidget(self.campusLabel)
        row4.addWidget(self.capacityLabel)
        row4.addWidget(self.probabilityLabel)
        row4.addStretch(1)

        # 第五行：课程简介（单行省略）
        row5 = QHBoxLayout()
        row5.addWidget(self.introLabel, 1)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addLayout(row1)
        left.addLayout(row2)
        left.addLayout(row3)
        left.addLayout(row4)
        left.addLayout(row5)

        # 右侧按钮列（详细信息在最上，收藏/报名/抢课/退选次序不变）
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.infoButton)
        right.addWidget(self.favButton)
        right.addWidget(self.enrollButton)
        right.addWidget(self.grabButton)
        right.addWidget(self.dropButton)
        right.addStretch(1)

        self.hBoxLayout = QHBoxLayout(self)
        self.hBoxLayout.setContentsMargins(16, 12, 12, 12)
        self.hBoxLayout.setSpacing(16)
        self.hBoxLayout.addLayout(left, 1)
        self.hBoxLayout.addLayout(right)

    def _connect_signals(self):
        self.favButton.clicked.connect(self._on_fav_clicked)
        self.enrollButton.clicked.connect(self._on_enroll_clicked)
        self.grabButton.clicked.connect(self._on_grab_clicked)
        self.dropButton.clicked.connect(self._on_drop_clicked)
        self.infoButton.clicked.connect(self._on_info_clicked)

    # ------------------------------------------------------------------
    # 数据更新
    # ------------------------------------------------------------------

    @property
    def tactic_name(self) -> str:
        """当前卡片的批次策略名（``set_course`` 传入，空串表示未知）。

        选课页加入收藏时用它把策略名一并写入本地收藏记录，收藏页才能按同一
        公式算出概率（见 :func:`~njuxk.api.models.selection_probability`）。
        """
        return self._tactic_name

    def set_course(self, course, tactic_name="", favorited=False):
        """用一门课程刷新全部展示。

        ``favorited`` 来自本地收藏集（外部传入），**严禁**读取课程响应里的
        ``favorite`` 字段（服务端收藏已废弃）。
        """
        self.course = course
        self._tactic_name = tactic_name or ""
        self.courseNumberLabel.setText(course.course_number)
        self.courseNameLabel.setText(course.course_name)
        self.creditLabel.setText(f"{course.credit} 学分")
        self.teacherLabel.setText(course.teacher_name)
        self.placeLabel.setText(course.teaching_place)
        self.campusLabel.setText(course.campus_name)
        self.capacityLabel.setText(
            f"已选/容量: {course.number_of_selected}/{course.class_capacity}"
        )
        prob = models.selection_probability(course, self._tactic_name)
        self.probabilityLabel.setText(f"选中概率: {prob}")
        self._update_intro()
        self.set_favorited(favorited)
        self.set_chosen(self.course.is_choose == "1")
        self._update_action_buttons()

    def set_favorited(self, flag):
        """单独更新收藏徽标与收藏按钮（本地收藏状态）。"""
        self._favorited = bool(flag)
        self.favBadge.setVisible(self._favorited)
        self.favButton.setText("取消收藏" if self._favorited else "收藏")

    def set_chosen(self, flag):
        """单独更新「已报名」徽标（``is_choose=="1"``）。"""
        self._chosen = bool(flag)
        self.chosenBadge.setVisible(self._chosen)

    def _update_action_buttons(self):
        """已报名 → 隐藏报名/抢课并显示「退选」；已满 → 两个按钮同时禁用。

        已报名（``is_choose=="1"``）：不能再报名也不能再抢，因此**隐藏**两个按钮
        （不是禁用——禁用会留下两个点不动的按钮），改显示「退选」；
        已满（``is_full=="1"``）：既无需再报名，也无需再抢，两个按钮跟随同一套
        禁用规则并给出原因 tooltip，避免「能加入抢课却不能报名」的语义矛盾。
        """
        if self.course is None:
            return
        if self.course.is_choose == "1":
            self.enrollButton.hide()
            self.grabButton.hide()
            self.dropButton.setVisible(True)
            self.dropButton.setEnabled(True)
            self.dropButton.setToolTip("退选该课程（需二次确认）")
        else:
            self.dropButton.hide()
            self.enrollButton.setVisible(True)
            self.grabButton.setVisible(True)
            if self.course.is_full == "1":
                self._set_action_enabled(
                    False, "课程已满，无法报名", "课程已满，无法抢课"
                )
            else:
                self._set_action_enabled(
                    True,
                    "立即提交一次报名请求，结果马上弹出（只试一次）",
                    "加入抢课任务，按定时与重试参数持续抢课，成功后自动停止",
                )

    def _set_action_enabled(self, enabled, enroll_tip, grab_tip):
        """统一设置两个动作按钮的可用性与 tooltip。"""
        self.enrollButton.setEnabled(enabled)
        self.enrollButton.setToolTip(enroll_tip)
        self.grabButton.setEnabled(enabled)
        self.grabButton.setToolTip(grab_tip)

    def _update_intro(self):
        """课程简介：超长省略为单行，全文放 tooltip。"""
        text = self.course.kcjj if self.course is not None else ""
        self.introLabel.setToolTip(text)
        if not text:
            self.introLabel.setText("")
            return
        fm = QFontMetrics(self.introLabel.font())
        width = self.introLabel.width()
        if width <= 0:
            width = 600
        self.introLabel.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, width))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.course is not None:
            self._update_intro()

    # ------------------------------------------------------------------
    # 信号
    # ------------------------------------------------------------------

    def _on_fav_clicked(self):
        if self.course is None:
            return
        self.favoriteToggled.emit(self.course.teaching_class_id, not self._favorited)

    def _on_enroll_clicked(self):
        if self.course is None:
            return
        self.enrollRequested.emit(self.course.teaching_class_id)

    def _on_grab_clicked(self):
        if self.course is None:
            return
        self.grabRequested.emit(self.course.teaching_class_id)

    def _on_drop_clicked(self):
        if self.course is None:
            return
        self.dropRequested.emit(self.course.teaching_class_id)

    def _dialog_parent(self):
        """详细信息弹窗的父级：最近的插件页面（``zbw.BasicTab``）。

        契约（test_main_page_ui 的 grep 规则）：提示/对话框一律挂**插件页面**，
        绝不提升到宿主主窗口 —— 卡片沿 parent 链上溯找所属页签页，遮罩即可覆盖
        整个插件页区域。独立构造的卡片（测试环境、无页面祖先）退回卡片自身。
        """
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, zbw.BasicTab):
                return widget
            widget = widget.parentWidget()
        return self

    def _on_info_clicked(self):
        """点「详细信息」：主线程直接弹课程信息弹窗（无网络、无 worker 交互）。

        ``exec`` 是模态调用（正常用户路径），测试用替身替换该方法避免阻塞。
        """
        if self.course is None:
            return
        self._info_dialog = CourseInfoDialog(
            self.course, parent=self._dialog_parent()
        )
        self._info_dialog.exec()


def _kind_display_name(course) -> str:
    """课程类别展示名：menu_code 静态中文名表 → code 本身（不编造）。"""
    code = course.teaching_class_type
    return models.MENU_CODE_NAMES.get(code) or code


class CourseInfoDialog(MessageBoxBase):
    """课程详细信息弹窗：只读展示 ``Course`` 全部字段 + 课程介绍长文本。

    - 标题 = 课程名（``SubtitleLabel``，同样支持选中复制）；
    - 正文用 ``TextBrowser``（readOnly：天然可选中复制、可滚动、自带换行），
      长介绍不会截断，超出高度滚动查看；
    - 宽度 560px；只有「关闭」一个按钮（只读展示，不做任何编辑）；
    - 数据只来自当前 ``Course`` 模型（课程列表接口下发字段），不额外请求
      ``courseInfoElective.do``（网页详情弹窗的独立详情接口，本插件不引入）。
    """

    def __init__(self, course, parent=None):
        super().__init__(parent=parent)
        self.course = course
        self.titleLabel = SubtitleLabel(course.course_name, self.widget)
        make_selectable(self.titleLabel)
        self.textBrowser = TextBrowser(self.widget)
        self.textBrowser.setReadOnly(True)
        self.textBrowser.setPlainText(self._build_text(course))
        self.textBrowser.setMinimumHeight(300)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.textBrowser, 1)
        self.widget.setFixedWidth(560)
        self.yesButton.setText("关闭")
        self.hideCancelButton()

    @staticmethod
    def _build_text(course) -> str:
        """把 ``Course`` 全部展示字段排成「字段名: 值」逐行文本（含介绍长文本）。"""
        prob = models.selection_probability(course)
        is_full = "已满" if course.is_full == "1" else "未满"
        is_choose = (
            "已报名" if course.is_choose == "1" else "未报名"
        )
        kind_name = _kind_display_name(course)
        lines = [
            f"课程名称: {course.course_name}",
            f"课程代码: {course.course_number}",
            f"教学班号: {course.teaching_class_id}",
            f"教师团队: {course.teacher_name}",
            f"学分: {course.credit}",
            f"学时: {course.hours}",
            f"校区: {course.campus_name}",
            f"上课时间/地点: {course.teaching_place}",
            f"课程类别: {kind_name}（courseKind {course.course_kind}）",
            f"选课类型: {course.elective_type}",
            f"选课限制说明: {is_full}，{is_choose}",
            f"已选/容量: {course.number_of_selected}/{course.class_capacity}",
            f"录取概率: {prob}",
        ]
        lines.append("课程介绍:")
        lines.append(course.kcjj if course.kcjj else "（暂无介绍）")
        return "\n".join(lines)
