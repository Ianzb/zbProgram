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
- 标题行：**详细信息**按钮（``TransparentToolButton(FIF.INFO)``，弹出
  ``CourseInfoDialog`` 富文本展示课程大纲 + 教学周历，见类尾）——用户要求把
  详细信息按钮放到**课程标题文字右边紧贴**（同一行、小间距 4px，不再占用
  独立的按钮列）；
- 右侧按钮列：收藏/取消收藏（``PushButton(FIF.HEART, "收藏")``）、立即报名
  （``PrimaryPushButton``，单次提交一次报名请求）、加入抢课
  （``PushButton(FIF.PLAY, "加入抢课")``，加入任务列表持续尝试）、退选
  （``PushButton(FIF.DELETE, "退选")``，仅已报名时显示，带二次确认）。

详细信息弹窗（``CourseInfoDialog``，2026-09 大改）：
- **第一部分 课程大纲**：本地课程既有展示项（课程名/代码/教师/学分/校区/时间地点/
  容量/概率，打开即显示，不依赖网络）+ 详情接口（抓包 [12] querykcxx.do）三节
  ——「课程基本信息」「课程学时信息」「课程详细信息」，长文本字段（育人目标/
  教学目标/契合关系/简介/教材/参考资料/成绩构成）用**自动换行的富文本 QLabel**
  展示，右侧配 ``zbw.CopyTextButton`` 一键复制原文；
- **第二部分 教学周历**：周历接口（抓包 [14] courseSchedule.do）逐条渲染到
  ``_ScheduleTable``（qfw ``TableWidget`` 子类：周次/教学内容/教学方式/教师，
  ``ElideNone`` + ``wordWrap`` + 行高自适应让长文本全文可见，只读，Ctrl+C /
  右键复制选中单元格）；
- **异步**：弹窗立即打开，先显示 ``IndeterminateProgressRing`` +「正在获取课程
  详情…」，工作线程调 ``XkClient.fetch_course_info`` / ``fetch_course_schedule``
  （走 zbToolLib，享受日志与会话失效自动重登），结果经信号回主线程渲染；
  **严禁工作线程触碰 QWidget**；失败一个不影响另一个，对应节显示错误文案 +
  「重试」按钮；加载行在该节首次渲染（成功或失败）时**移出布局并销毁**
  （用户实测：只 ``hide()`` 会遗留「正在获取课程详情…」在弹窗左上角）；
  弹窗关闭时置 ``_aborted`` 并断开信号，迟到信号被安全忽略。

**禁用 TextEdit 家族**：弹窗内不使用 TextEdit/TextBrowser/QPlainTextEdit，全部
QLabel 富文本 + ``zbw.CopyTextButton`` + ``ui/text_select.make_selectable``。

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

import html
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtGui import QFontMetrics, QKeySequence
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentIcon as FIF,
    IconWidget,
    IndeterminateProgressRing,
    InfoLevel,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.table_view import TableItemDelegate

import zbWidgetLib as zbw

from ..api import models
from .text_select import make_selectable

#: 课程大纲各节 key 标签统一列宽（覆盖全部 KV 键文案），值列因此跨节左对齐
KEY_LABEL_WIDTH = 130
#: 大纲各节 KV 网格 / 长文本行共用的水平与垂直间距（同一组值，值列起始 x 一致）
KV_H_SPACING = 16
KV_V_SPACING = 4

#: 弹窗备注行文字色（照 ui/results.py ResultCard 备注行已验证色值：
#: 浅色深橙、深色亮橙，明暗主题都可读）
_REMARK_LIGHT = "#9a5b00"
_REMARK_DARK = "#ffb02e"


def _selected_table_text(table) -> str:
    """选中单元格文本：行内 tab 分隔、行间换行；单单元格 = 纯文本。"""
    items = sorted(
        table.selectedItems(), key=lambda it: (it.row(), it.column())
    )
    if not items:
        return ""
    lines = []
    row = None
    cells: list[str] = []
    for it in items:
        if row is not None and it.row() != row:
            lines.append("\t".join(cells))
            cells = []
        row = it.row()
        cells.append(it.text())
    lines.append("\t".join(cells))
    return "\n".join(lines)


def build_table_copy_menu(table, parent=None) -> RoundMenu:
    """周历表右键菜单（与 ui/text_select.build_copy_menu 同范式：只构建不弹出）。

    - 有选中 → 「复制」（多单元格行内 tab 分隔、行间换行）；
    - 无选中但有当前格 → 「复制当前格」兜底（不强制用户先拖选）；
    - 表为空 → 空菜单（调用方跳过弹出）。
    """
    menu = RoundMenu(parent=parent or table)
    if table.selectedItems():
        copy_act = Action(FIF.COPY, "复制")
        copy_act.triggered.connect(
            lambda: QApplication.clipboard().setText(_selected_table_text(table))
        )
        menu.addAction(copy_act)
        return menu
    item = table.currentItem()
    if item is not None:
        copy_cell_act = Action(FIF.COPY, "复制当前格")
        copy_cell_act.triggered.connect(
            lambda: QApplication.clipboard().setText(item.text())
        )
        menu.addAction(copy_cell_act)
    return menu


class _ScheduleItemDelegate(TableItemDelegate):
    """周历表委托：按列宽换行计算单元格 sizeHint 高度。

    qfw ``TableItemDelegate.sizeHint`` 只返回单行高度——QTableView 的
    ``resizeRowsToContents`` 因此拿不到换行后的真实高度，长教学内容换行后
    行高不足被竖向裁剪（ElideNone 全文可见的前提）。这里用 ``option.rect``
    的列宽对文本换行计高（QTableView::sizeHintForRow 会把 option.rect 宽度
    设为列宽），取与单行高度的较大值。
    """

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text or option.rect.width() <= 0:
            return size
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        fm = QFontMetrics(opt.font)
        # 减去 delegate margin 与少量内边距，与实际绘制文本区域对齐
        width = option.rect.width() - 2 * self.margin - 8
        if width <= 0:
            return size
        wrapped = fm.boundingRect(
            0, 0, width, 0, Qt.TextFlag.TextWordWrap, text
        )
        size.setHeight(max(size.height(), wrapped.height() + 2 * self.margin + 2))
        return size


class _ScheduleTable(TableWidget):
    """教学周历表（用户需求：长文本全文可见 + 可选中复制但不可编辑）。

    - 不截断：``ElideNone`` + ``wordWrap`` + 行高自适应（长教学内容换行显示，
      不再被省略号截断）；
    - 只读：``NoEditTriggers``（双击 / F2 不能改内容）；
    - 可选中复制：``ExtendedSelection`` 跨行选，Ctrl+C / 右键菜单把选中单元格
      文本写入剪贴板（多单元格行内 tab 分隔、行间换行；单单元格纯文本）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 换行计高的委托（见 _ScheduleItemDelegate：ElideNone 全文可见的前提）
        self.delegate = _ScheduleItemDelegate(self)
        self.setItemDelegate(self.delegate)
        #: 宽度变化回调（弹窗注入 ``_adjust_schedule_height``）：换行点随宽度
        #: 变化，行高必须重算，否则加宽弹窗后行高残留旧值
        self._width_callback = None

    def keyPressEvent(self, e):
        # Ctrl+C：复制选中单元格文本（无选中时走默认行为）
        if e.matches(QKeySequence.StandardKey.Copy) or (
                e.modifiers() & Qt.KeyboardModifier.ControlModifier
                and e.key() == Qt.Key.Key_C
        ):
            text = _selected_table_text(self)
            if text:
                QApplication.clipboard().setText(text)
                e.accept()
                return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        menu = build_table_copy_menu(self)
        if menu.actions():
            menu.exec(e.globalPos())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        old_w = e.oldSize().width()
        if self._width_callback is not None and old_w != e.size().width():
            self._width_callback()


class CourseCard(zbw.CardWidget):
    """单门课程卡片（教学班粒度）。"""

    favoriteToggled = Signal(str, bool)
    enrollRequested = Signal(str)
    grabRequested = Signal(str)
    dropRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.course = None
        # 详情接口客户端：由装配页（CoursePage / FavoritesPage）注入；
        # None 时详情弹窗只显示本地课程字段，网络节显示错误 + 重试
        self.client = None
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
        # 详细信息：纯图标按钮（弹窗展示课程大纲 + 教学周历），
        # 用户要求放在课程标题文字右边紧贴（同一行、小间距，不占独立按钮列）
        self.infoButton = TransparentToolButton(FIF.INFO, self)
        self.infoButton.setToolTip("详细信息")

        # 第一行：徽标 + 编号 + [课程标题 + 紧贴其右的详细信息按钮] + 学分
        # 标题与按钮包进同一子布局（间距 4px，比行距 8px 更「贴」）；尾部
        # stretch 把学分推回最右，标题不拉伸 → 按钮紧贴标题文字右侧
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(self.favBadge)
        row1.addWidget(self.chosenBadge)
        row1.addWidget(self.courseNumberLabel)
        titleRow = QHBoxLayout()
        titleRow.setContentsMargins(0, 0, 0, 0)
        titleRow.setSpacing(4)
        titleRow.addWidget(self.courseNameLabel)
        titleRow.addWidget(self.infoButton)
        row1.addLayout(titleRow)
        row1.addStretch(1)
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

        # 右侧按钮列（收藏/报名/抢课/退选，次序与点击区域不变；详细信息按钮
        # 已上移到标题行，见 row1 的 titleRow）
        right = QVBoxLayout()
        right.setSpacing(8)
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
        """详细信息弹窗的父级：最近的插件页面（``zbw.BasicTab`` 或带标记的页面）。

        契约（test_main_page_ui 的 grep 规则）：提示/对话框一律挂**插件页面**，
        绝不提升到宿主主窗口 —— 卡片沿 parent 链上溯找所属页签页，遮罩即可覆盖
        整个插件页区域。独立构造的卡片（测试环境、无页面祖先）退回卡片自身。

        插件页面识别：``zbw.BasicTab`` 实例（favorites/tasks 页），或带
        ``_info_parent_flag`` 类标记的页面 —— ``CoursePage`` 改基类为 QWidget
        （顶栏固定，只有卡片列表滚动，见 ``ui/course.py``）后无法再被
        ``isinstance(BasicTab)`` 识别，靠该标记继续命中。
        """
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, zbw.BasicTab) or getattr(
                    widget, "_info_parent_flag", False
            ):
                return widget
            widget = widget.parentWidget()
        return self

    def _on_info_clicked(self):
        """点「详细信息」：主线程直接弹课程详情弹窗（弹窗自己异步拉详情）。

        ``exec`` 是模态调用（正常用户路径），测试用替身替换该方法避免阻塞。
        """
        if self.course is None:
            return
        self._info_dialog = CourseInfoDialog(
            self.course, client=self.client, parent=self._dialog_parent()
        )
        self._info_dialog.exec()


def _kind_display_name(course) -> str:
    """课程类别展示名：menu_code 静态中文名表 → code 本身（不编造）。"""
    code = course.teaching_class_type
    return models.MENU_CODE_NAMES.get(code) or code


def _clean_kind_text(course_kind) -> str:
    """展示层兜底：courseKind 脏值（整坨 dict 文本）→ 空串（后缀省略）。

    用户实测：「课程类别」行曾渲染出 ``专业（courseKind {'grade': None, ...}）``。
    解析层（``models._kind_str``）已堵住泄漏，这里再挡一道历史脏数据：
    值含 ``{`` / ``}`` 或超过 12 个字符即视为脏值，按空串处理——后缀整体省略，
    绝不把 dict 文本渲染给用户。
    """
    text = (course_kind or "").strip()
    if not text or "{" in text or "}" in text or len(text) > 12:
        return ""
    return text


class CourseInfoDialog(MessageBoxBase):
    """课程详情弹窗：富文本展示课程大纲（详情接口）+ 教学周历（周历接口）。

    结构（内容长，整体放进 ``SmoothScrollArea`` 可滚动；滚动区与内容容器
    ``enableTransparentBackground`` 全透明，跟随 MessageBoxBase 内容区背景，
    富文本 label 不再出现异色色块）：
    - 第一部分「课程大纲」：
      * 「课程基本信息」= 本地 ``Course`` 既有展示项（课程名/代码/教学班号/教师/
        学分/校区/时间地点/容量/概率，打开即显示，**不依赖网络**）+ 详情接口的
        服务端字段（key-value 紧凑网格，两列键值对）；其中「课程类别」行在详情
        到达后用服务端 ``kclb``（+ ``publicCourseTypeName``）覆盖页签推导值
        （``teaching_class_type`` 只是浏览页签的 menu_code，ZY 页签下会误显示
        「专业」），见 :meth:`_override_local_kind`；
      * **备注行**（本地网格之后）：``course.remark``（extInfo，多行长通知）
        与详情节点 ``remark``/``extMsg`` 去重拼接展示，MESSAGE 图标 + 橙色 +
        wordWrap + 可选中复制，空则整行隐藏（照 ResultCard 备注行范式，
        见 :meth:`_apply_server_remark`）；
      * 「课程学时信息」「课程详细信息」= 详情接口其余字段；长文本字段为
        自动换行的富文本 ``QLabel``（``wordWrap=True``），右侧配
        ``zbw.CopyTextButton`` 复制原文；
    - **第二部分「教学周历」**：``_ScheduleTable``（qfw ``TableWidget`` 子类，放进
        带 layout 的容器再入 ``contentLayout``，宽度随弹窗伸展、高度按行内容
        自适应），列 = 周次/教学内容/教学方式/教师；``ElideNone`` + ``wordWrap``
        长文本全文可见（不省略号截断）、只读、选中单元格可 Ctrl+C / 右键复制；
    - **禁用 TextEdit 家族**（TextEdit/TextBrowser/QPlainTextEdit 一律不用）。

    异步（对齐 ui/course.py / ui/favorites.py 信号范式）：
    - 弹窗立即打开，两个网络节先显示 ``IndeterminateProgressRing`` +
      「正在获取课程详情…」；工作线程分别调 ``fetch_course_info`` /
      ``fetch_course_schedule``，结果经 ``_infoReady`` / ``_scheduleReady``
      信号回主线程渲染；**严禁工作线程触碰 QWidget**；
    - 失败一个不影响另一个：对应节显示错误文案 + 「重试」按钮，点重试只重新
      发起该节请求；
    - 弹窗关闭（``done``）置 ``_aborted`` 并断开信号，工作线程迟到信号被安全
      忽略（防野指针崩溃，参照项目 Loading 处理）。

    详情接口（抓包 [12] querykcxx.do 响应 data 节点）编码键 → 页面字段映射表：
      departmentName→开课单位  publicCourseTypeName→通识公选类别
      txkclb→通修课程类别      ynkcfl→院内课程分类（服务端下发的是代码）
      courselevel→课程层次     kcfl1→理论/实践（「理论类课程」等）
      kslx→考试类型            courseNumber→课程号   courseName→课程名
      engCourseName→英文课程名 ybzsxm→大纲填写人姓名 kclb→课程类别
      kczt→课程状态            kcfzrxm→课程负责人姓名 kxqkc→跨学期课程
      credit→学分              hours→总学时  zxs→周学时（周学时 zxs 与
                                        总学时 zong 同缩写，按取值合理性区分：
                                        hours=32 是总学时，zxs=3 是周学时）
      syxs→实验学时（shiyan）  sjxs→实践学时（shijian）
      llxs→理论学时            jzsjzs→集中实践周数
      sfqywsk→是否全英文授课   sfsysk→是否双语授课
      kcyrmb→课程育人目标      kcjxmb→课程教学目标
      rcpymb→与学校本科人才培养目标的契合关系
      kcjj→课程简介            jc→教材（「1、不使用教材#」井号分隔条目）
      ckzl→参考资料            cjgc→成绩构成

    周历接口（抓包 [14] courseSchedule.do 响应 dataList，服务端把周历数据塞在
    复用的编码键里）：credit→周次（该接口此字段是周次不是学分）
      conflictDesc→教学内容（「N. 标题\\n内容」原文展示）
      courseTypeName→教学方式  teacherName→教师（抓包中为 null → 显示空）
    """

    # 工作线程信号（工作线程 emit，主线程槽接收）
    _infoReady = Signal(object)
    _infoError = Signal(str)
    _scheduleReady = Signal(object)
    _scheduleError = Signal(str)

    #: 弹窗固定宽度（key-value 双列网格 + 换行长文本 + 复制按钮需要比旧版更宽）
    WIDTH = 680

    # 服务端详情 → 「课程基本信息」节（键名, data 里的编码键）
    _COURSE_INFO_FIELDS = [
        ("课程号", "courseNumber"),
        ("课程名", "courseName"),
        ("英文课程名", "engCourseName"),
        ("开课单位", "departmentName"),
        ("通识公选类别", "publicCourseTypeName"),
        ("通修课程类别", "txkclb"),
        ("院内课程分类", "ynkcfl"),
        ("课程层次", "courselevel"),
        ("理论/实践", "kcfl1"),
        ("考试类型", "kslx"),
        ("大纲填写人姓名", "ybzsxm"),
        ("课程类别", "kclb"),
        ("课程状态", "kczt"),
        ("课程负责人姓名", "kcfzrxm"),
        ("跨学期课程", "kxqkc"),
    ]

    # 服务端详情 → 「课程学时信息」节
    _COURSE_HOURS_FIELDS = [
        ("学分", "credit"),
        ("总学时", "hours"),
        ("周学时", "zxs"),
        ("实验学时", "syxs"),
        ("实践学时", "sjxs"),
        ("理论学时", "llxs"),
        ("集中实践周数", "jzsjzs"),
    ]

    # 服务端详情 → 「课程详细信息」节：前两个是短字段（进 KV 网格），
    # 其余为长文本字段（自动换行富文本 + CopyTextButton）
    _COURSE_DETAIL_KV = [
        ("是否全英文授课", "sfqywsk"),
        ("是否双语授课", "sfsysk"),
    ]
    _COURSE_DETAIL_LONG = [
        ("课程育人目标", "kcyrmb"),
        ("课程教学目标", "kcjxmb"),
        ("与学校本科人才培养目标的契合关系", "rcpymb"),
        ("课程简介", "kcjj"),
        ("教材", "jc"),
        ("参考资料", "ckzl"),
        ("成绩构成", "cjgc"),
    ]

    # 周历表列头与 dataList 编码键
    _SCHEDULE_HEADERS = ["周次", "教学内容", "教学方式", "教师"]
    _SCHEDULE_FIELDS = ["credit", "conflictDesc", "courseTypeName", "teacherName"]

    def __init__(self, course, client=None, parent=None):
        super().__init__(parent=parent)
        self.course = course
        self.client = client
        # 弹窗关闭后置 True：工作线程迟到信号直接忽略（防野指针崩溃）
        self._aborted = False
        self._fallback_pool = None

        self._build_ui()
        self._connect_signals()
        self._start_fetches()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.widget.setFixedWidth(self.WIDTH)
        self.yesButton.setText("关闭")
        self.hideCancelButton()

        self.titleLabel = SubtitleLabel(self.course.course_name, self.widget)
        make_selectable(self.titleLabel)

        # 内容整体放进平滑滚动区（内容长，必须可滚动）
        self.scrollArea = SmoothScrollArea(self.widget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setFrameShape(QFrame.NoFrame)
        inner = QWidget(self.scrollArea)
        self.scrollArea.setWidget(inner)
        self.contentLayout = QVBoxLayout(inner)
        self.contentLayout.setContentsMargins(24, 4, 24, 8)
        self.contentLayout.setSpacing(8)
        # 滚动区 + 内容容器全部透明（qfw enableTransparentBackground）：跟随
        # MessageBoxBase 内容区背景（#centerWidget，深浅主题由 dialog.qss 统一
        # 决定）。不开启的话滚动视口保持 palette 底色，与白色内容区割裂，
        # 富文本 label 一块一块「浮」在异色底上（用户实测很丑）。
        self.scrollArea.enableTransparentBackground()

        self.contentLayout.addWidget(self.titleLabel)

        # ============ 第一部分：课程大纲 ============
        self._add_section_title("课程大纲")

        self.outlineSubtitle = self._add_subtitle("课程基本信息")
        # 本地课程字段：打开即显示，不依赖网络（key-value 紧凑网格，两列键值对）
        self.localGrid = self._make_grid_layout()
        self._fill_local_grid()
        localHolder = QWidget(self.widget)
        localHolder.setLayout(self.localGrid)
        self.contentLayout.addWidget(localHolder)

        # 备注行（用户需求「在课程详细信息里面也要显示备注信息」）：照
        # ResultCard 备注行范式（ui/results.py，MESSAGE 图标 + 橙色多行文本 +
        # 可选中复制，明暗主题可读）。本地 ``course.remark``（来自 extInfo，
        # 「我的报名/我的课程」页课程带值）非空时打开弹窗立即可见（不依赖
        # 网络）；详情接口节点 remark / extMsg 非空时补充显示（见
        # _apply_server_remark）；空则整行隐藏不留空隙。独立 holder 挂进
        # contentLayout（游离即左上角堆叠，见 serverGridWidget 处注释），
        # 不参与加载行销毁/重建，重试路径不受影响。
        self._remark_parts: list[str] = []
        self.remarkIcon = IconWidget(FIF.MESSAGE, self.widget)
        self.remarkIcon.setFixedSize(16, 16)
        self.remarkLabel = make_selectable(BodyLabel(self.widget))
        self.remarkLabel.setWordWrap(True)
        self.remarkLabel.setTextColor(_REMARK_LIGHT, _REMARK_DARK)
        # 横向 Ignored：多行备注换行显示而非把最小宽度撑大（对齐 ResultCard）
        self.remarkLabel.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        remarkRow = QHBoxLayout()
        remarkRow.setContentsMargins(0, 0, 0, 0)
        remarkRow.setSpacing(6)
        # 图标顶对齐：多行备注时图标不悬在中间
        remarkRow.addWidget(self.remarkIcon, 0, Qt.AlignmentFlag.AlignTop)
        remarkRow.addWidget(self.remarkLabel, 1)
        self.remarkHolder = QWidget(self.widget)
        self.remarkHolder.setLayout(remarkRow)
        self.contentLayout.addWidget(self.remarkHolder)
        self.remarkHolder.hide()
        self._add_local_remark()

        # 服务端字段网格：详情就绪后填充（初始隐藏）。**必须挂进 contentLayout**
        # （与周历 scheduleHolder 同病同治）：只建 widget + setLayout 不入布局，
        # show() 后整节堆在弹窗内容区左上角 (0,0)，多行「是/否」键值格子互相
        # 重叠成一坨（用户实测）
        self.serverGrid = self._make_grid_layout()
        self.serverGridWidget = QWidget(self.widget)
        self.serverGridWidget.setLayout(self.serverGrid)
        self.contentLayout.addWidget(self.serverGridWidget)
        self.serverGridWidget.hide()

        # 服务端三节共用的加载行（ProgressRing + 文案）
        self.infoLoadingRow = self._make_loading_row()
        # 服务端详情错误行（错误文案 + 重试，初始隐藏）
        self.infoErrorLabel, self.infoRetryButton, self.infoErrorRow = (
            self._make_error_row()
        )

        # 「课程学时信息」节（服务端）
        self.hoursSubtitle = self._add_subtitle("课程学时信息")
        self.hoursGrid = self._make_grid_layout()
        self.hoursGridWidget = QWidget(self.widget)
        self.hoursGridWidget.setLayout(self.hoursGrid)
        self.contentLayout.addWidget(self.hoursGridWidget)
        self.hoursGridWidget.hide()

        # 「课程详细信息」节（服务端）
        self.detailSubtitle = self._add_subtitle("课程详细信息")
        self.detailKvGrid = self._make_grid_layout()
        self.detailKvWidget = QWidget(self.widget)
        self.detailKvWidget.setLayout(self.detailKvGrid)
        self.contentLayout.addWidget(self.detailKvWidget)
        self.detailKvWidget.hide()
        # 长文本字段容器（富文本 QLabel + CopyTextButton，就绪后填充）；
        # 同样必须挂进 contentLayout（游离即左上角堆叠，见 serverGridWidget 处注释）
        self.detailLongLayout = QVBoxLayout()
        self.detailLongLayout.setContentsMargins(0, 0, 0, 0)
        self.detailLongLayout.setSpacing(8)
        self.detailLongWidget = QWidget(self.widget)
        self.detailLongWidget.setLayout(self.detailLongLayout)
        self.contentLayout.addWidget(self.detailLongWidget)
        self.detailLongWidget.hide()

        # ============ 第二部分：教学周历 ============
        self._add_section_title("教学周历")
        # 周历表（就绪后填充；初始隐藏）。长文本全文可见（ElideNone + wordWrap +
        # 行高自适应，不被省略号截断）、只读、可选中复制（见 _ScheduleTable）
        self.scheduleTable = _ScheduleTable(self.widget)
        self.scheduleTable.setColumnCount(len(self._SCHEDULE_HEADERS))
        self.scheduleTable.setHorizontalHeaderLabels(self._SCHEDULE_HEADERS)
        self.scheduleTable.setBorderVisible(False)
        self.scheduleTable.setBorderRadius(8)
        self.scheduleTable.setWordWrap(True)
        # 不截断：省略模式关闭（qfw delegate 按 option 绘制，ElideNone 出全文）
        self.scheduleTable.setTextElideMode(Qt.TextElideMode.ElideNone)
        # 只读：双击 / F2 不能改内容
        self.scheduleTable.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        # 可选中复制：跨行选（Ctrl/Shift 多选），按单元格选择（Ctrl+C 复制选中格）
        self.scheduleTable.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.scheduleTable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self.scheduleTable.verticalHeader().hide()
        header = self.scheduleTable.horizontalHeader()
        # 周次/教学方式窄列固定，教学内容占满剩余宽度
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 宽度随弹窗伸展、高度按内容自适应（填充行后 setFixedHeight）
        self.scheduleTable.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.scheduleTable.hide()
        # 必须放进带 layout 的容器（margin 0）再入 contentLayout：表格直接挂在
        # content 上会游离在布局之外——不随滚动内容排布、宽度不随弹窗伸展
        # （用户实测的周历布局异常，即漏了这一步）
        self.scheduleHolder = QWidget(self.widget)
        scheduleHolderLayout = QVBoxLayout(self.scheduleHolder)
        scheduleHolderLayout.setContentsMargins(0, 0, 0, 0)
        scheduleHolderLayout.addWidget(self.scheduleTable)
        self.contentLayout.addWidget(self.scheduleHolder)
        # 宽度变化 → 换行点变化 → 行高重算（表高显式设定，见 _adjust_schedule_height）
        self.scheduleTable._width_callback = self._adjust_schedule_height

        self.scheduleLoadingRow = self._make_loading_row()
        self.scheduleErrorLabel, self.scheduleRetryButton, self.scheduleErrorRow = (
            self._make_error_row()
        )

        self.viewLayout.addWidget(self.scrollArea, 1)

    def _add_section_title(self, text):
        """分节大标题：醒目 SubtitleLabel + 分隔线。"""
        title = SubtitleLabel(text, self.widget)
        self.contentLayout.addSpacing(8)
        self.contentLayout.addWidget(title)
        line = QFrame(self.widget)
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("QFrame{background:rgba(125,125,125,0.5)}")
        line.setFixedHeight(1)
        self.contentLayout.addWidget(line)
        return title

    def _add_subtitle(self, text):
        """小节标题（灰色小标签，比大标题低调一级）。"""
        label = BodyLabel(text, self.widget)
        label.setTextColor("#808080", "#a0a0a0")
        self.contentLayout.addWidget(label)
        return label

    def _make_grid_layout(self):
        """key-value 紧凑网格：两列键值对（4 列：键|值|键|值）。

        间距用全弹窗统一常量（KV_H_SPACING / KV_V_SPACING）：配合
        :meth:`_kv_cell` 的统一键宽，各节值列起始 x 相同（跨节左右对齐）。
        """
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(KV_H_SPACING)
        grid.setVerticalSpacing(KV_V_SPACING)
        return grid

    def _kv_cell(self, key, value, row, col, grid):
        """往网格写入一对键值：标签灰色、值可选中富文本；返回值标签。

        键标签统一固定宽（KEY_LABEL_WIDTH）：各节键列等宽 → 值列在同一 x 起始
        （用户需求「课程大纲各节右侧一列跨节左右对齐」）。wordWrap 兜底防极窄
        字体下键文案被截断。
        """
        keyLabel = BodyLabel(f"{key}：", self.widget)
        keyLabel.setTextColor("#606060", "#d2d2d2")
        keyLabel.setFixedWidth(KEY_LABEL_WIDTH)
        keyLabel.setWordWrap(True)
        valLabel = make_selectable(BodyLabel(self.widget))
        valLabel.setTextFormat(Qt.TextFormat.RichText)
        valLabel.setWordWrap(True)
        # 水平 Ignored：值标签不把「最长不可断行 token」灌进网格最小宽度（如
        # 教学班号长数字串），否则内容区最小宽超过弹窗宽度、滚动区横向溢出，
        # 周历表也无法跟随弹窗宽度伸展
        valLabel.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        valLabel.setText(self._rich(value) if value else "—")
        grid.addWidget(keyLabel, row, col * 2)
        grid.addWidget(valLabel, row, col * 2 + 1)
        grid.setColumnStretch(col * 2 + 1, 1)
        return valLabel

    def _fill_local_grid(self):
        """本地 Course 既有展示项（打开即显示，不依赖网络）。

        「课程类别」行的值标签要单独记下（``_local_kind_label``）：详情接口
        数据到达后用服务端字段覆盖（见 :meth:`_override_local_kind`）。
        """
        course = self.course
        prob = models.selection_probability(course)
        is_full = "已满" if course.is_full == "1" else "未满"
        is_choose = "已报名" if course.is_choose == "1" else "未报名"
        kind_name = _kind_display_name(course)
        # courseKind 脏值（dict 文本）→ 空串 → 后缀整体省略（不渲染坨文本）
        kind_text = _clean_kind_text(course.course_kind)
        kind_value = f"{kind_name}（courseKind {kind_text}）" if kind_text else kind_name
        pairs = [
            ("课程名", course.course_name),
            ("课程代码", course.course_number),
            ("教学班号", course.teaching_class_id),
            ("教师团队", course.teacher_name),
            ("学分", course.credit),
            ("学时", course.hours),
            ("校区", course.campus_name),
            ("上课时间/地点", course.teaching_place),
            ("课程类别", kind_value),
            ("选课类型", course.elective_type),
            ("选课状态", f"{is_full}，{is_choose}"),
            ("已选/容量", f"{course.number_of_selected}/{course.class_capacity}"),
            ("录取概率", prob),
        ]
        self._local_kind_label = None
        for i, (key, value) in enumerate(pairs):
            val = self._kv_cell(key, value, i // 2, i % 2, self.localGrid)
            if key == "课程类别":
                self._local_kind_label = val

    def _add_local_remark(self):
        """本地 ``course.remark`` 非空 → 备注行立即显示（不依赖网络）。"""
        remark = ""
        if self.course is not None:
            remark = (self.course.remark or "").strip()
        if remark:
            self._remark_parts.append(remark)
            self._render_remark()

    def _apply_server_remark(self, node):
        """详情节点 remark / extMsg 非空 → 补充显示（与本地备注去重拼接）。

        服务端优先取节点 ``remark``，为空回落 ``extMsg``（本抓包恰好为空，
        但两者都是服务端字段，任何课程都可能带值）。与已显示文本不同且非空
        时**拼接**在本地备注之后（各占一行）；相同则只显示一份。
        detail 请求失败或未返回时不调用本方法。
        """
        remark = str(node.get("remark") or "").strip()
        if not remark:
            remark = str(node.get("extMsg") or "").strip()
        if not remark or remark in self._remark_parts:
            return
        self._remark_parts.append(remark)
        self._render_remark()

    def _render_remark(self):
        """渲染备注行：无内容整行隐藏（不留空隙），有内容「备注：」前缀多行显示。"""
        if not self._remark_parts:
            self.remarkHolder.hide()
            return
        self.remarkLabel.setText("备注：" + "\n".join(self._remark_parts))
        self.remarkHolder.show()

    def _override_local_kind(self, node):
        """详情到达后用服务端字段覆盖本地网格的「课程类别」行。

        页签推导值只是兜底：``teaching_class_type`` 是**浏览页签的 menu_code**
        （fetch 时注入），用户在 ZY 页签下看课就会显示「专业」；服务端真相是
        ``kclb``（课程类别）为主、``publicCourseTypeName`` 非空时追加展示，
        如「通识课程 · 新生研讨课」。两者都空则保留原页签推导值；
        「（courseKind X）」后缀保留（对用户排查有用，脏值时整体省略）。
        detail 请求失败或未返回时不调用本方法（不覆盖、不报错）。
        """
        if self._local_kind_label is None:
            return
        kclb = str(node.get("kclb") or "").strip()
        pctn = str(node.get("publicCourseTypeName") or "").strip()
        combined = " · ".join(part for part in (kclb, pctn) if part)
        if not combined:
            return
        # courseKind 脏值（dict 文本）→ 空串 → 后缀整体省略（不渲染坨文本）
        kind_text = _clean_kind_text(self.course.course_kind)
        suffix = f"（courseKind {kind_text}）" if kind_text else ""
        self._local_kind_label.setText(self._rich(f"{combined}{suffix}"))

    def _make_loading_row(self, index=-1):
        """加载行：IndeterminateProgressRing +「正在获取课程详情…」。

        ``index >= 0`` 时插回 ``contentLayout`` 的原位置（「重试」时按
        :meth:`_dismiss_loading_row` 记录的位置重建），否则追加到内容末尾。
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        ring = IndeterminateProgressRing(self.widget)
        ring.setFixedWidth(24)
        text = BodyLabel("正在获取课程详情…", self.widget)
        text.setTextColor("#606060", "#d2d2d2")
        row.addWidget(ring)
        row.addWidget(text)
        row.addStretch(1)
        holder = QWidget(self.widget)
        holder.setLayout(row)
        if index < 0:
            self.contentLayout.addWidget(holder)
        else:
            self.contentLayout.insertWidget(index, holder)
        return holder

    def _dismiss_loading_row(self, attr):
        """把某节的加载行移出布局并销毁（该节成功/失败首次渲染时调用）。

        用户实测：数据加载完成后弹窗左上角仍残留「正在获取课程详情…」+ 加载圈
        ——只 ``hide()`` 不可靠。这里彻底移出 ``contentLayout`` + ``deleteLater``，
        保证该节首次渲染（成功或失败）后弹窗内不留任何加载指示；并记住原位置
        （``_<attr>_index``），点「重试」时按原位置重建（见 :meth:`_show_loading_row`）。
        """
        holder = getattr(self, attr, None)
        if holder is None:
            return
        index = self.contentLayout.indexOf(holder)
        holder.hide()
        self.contentLayout.removeWidget(holder)
        holder.deleteLater()
        setattr(self, f"_{attr}_index", index)
        setattr(self, attr, None)

    def _show_loading_row(self, attr):
        """发起（或重试）某节请求时确保加载行可见；已销毁则按原位置重建。"""
        holder = getattr(self, attr, None)
        if holder is None:
            holder = self._make_loading_row(getattr(self, f"_{attr}_index", -1))
            setattr(self, attr, holder)
        holder.show()

    def _make_error_row(self):
        """错误行：红色错误文案（可选中）+「重试」按钮，初始隐藏。"""
        label = make_selectable(BodyLabel(self.widget))
        label.setWordWrap(True)
        label.setTextColor("#c42b1c", "#ff99a4")
        retry = PushButton(FIF.SYNC, "重试", self.widget)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(label, 1)
        row.addWidget(retry)
        holder = QWidget(self.widget)
        holder.setLayout(row)
        holder.hide()
        self.contentLayout.addWidget(holder)
        return label, retry, holder

    @staticmethod
    def _rich(text):
        """原文 → 富文本：HTML 转义 + 换行转 <br> + 教材井号分隔条目转 <br>。"""
        escaped = html.escape(str(text))
        escaped = escaped.replace("\r\n", "<br>").replace("\n", "<br>")
        escaped = escaped.replace("#", "<br>")
        return escaped.strip("<br>")

    # ------------------------------------------------------------------
    # 异步获取（工作线程只调 client + emit 信号，严禁触碰 QWidget）
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self._infoReady.connect(self._on_info_ready)
        self._infoError.connect(self._on_info_error)
        self._scheduleReady.connect(self._on_schedule_ready)
        self._scheduleError.connect(self._on_schedule_error)
        self.infoRetryButton.clicked.connect(self._start_fetch_info)
        self.scheduleRetryButton.clicked.connect(self._start_fetch_schedule)

    def _pool(self):
        """线程池：宿主 program.THREAD_POOL（与项目其他页面一致），无宿主兜底本地池。"""
        program = None
        try:
            from .login import _host_program

            program = _host_program()
        except Exception:
            program = None
        if program is not None and hasattr(program, "THREAD_POOL"):
            return program.THREAD_POOL
        if self._fallback_pool is None:
            self._fallback_pool = ThreadPoolExecutor(max_workers=2)
        return self._fallback_pool

    def _start_fetches(self):
        """打开弹窗即并行发起两个详情请求（互不影响）。"""
        self._start_fetch_info()
        self._start_fetch_schedule()

    def _start_fetch_info(self):
        """发起课程大纲请求（首次或点重试）。"""
        if self._aborted:
            return
        self._show_loading_row("infoLoadingRow")
        self.infoErrorRow.hide()
        self._pool().submit(self._info_worker)

    def _start_fetch_schedule(self):
        """发起周历请求（首次或点重试）。"""
        if self._aborted:
            return
        self._show_loading_row("scheduleLoadingRow")
        self.scheduleErrorRow.hide()
        self._pool().submit(self._schedule_worker)

    def _info_worker(self):
        """工作线程：课程大纲请求（抓包 [12]），失败只影响本节。"""
        try:
            if self.client is None:
                raise RuntimeError("未连接选课系统，无法获取详情")
            data = self.client.fetch_course_info(
                self.course.teaching_class_id,
                self.course.course_number,
                self.course.batch_code,
            )
            self._infoReady.emit(data)
        except Exception as e:
            logging.warning("获取课程详情失败：%s", e)
            self._infoError.emit(f"课程大纲获取失败：{e}")

    def _schedule_worker(self):
        """工作线程：教学周历请求（抓包 [14]），失败只影响本节。"""
        try:
            if self.client is None:
                raise RuntimeError("未连接选课系统，无法获取详情")
            data = self.client.fetch_course_schedule(
                self.course.teaching_class_id,
                self.course.batch_code,
            )
            self._scheduleReady.emit(data)
        except Exception as e:
            logging.warning("获取教学周历失败：%s", e)
            self._scheduleError.emit(f"教学周历获取失败：{e}")

    # ------------------------------------------------------------------
    # 主线程槽：渲染
    # ------------------------------------------------------------------

    def _on_info_ready(self, data):
        if self._aborted:
            return
        node = (data or {}).get("data") or {}
        # 该节首次渲染（成功）→ 加载行移出布局并销毁，弹窗内不留加载指示
        self._dismiss_loading_row("infoLoadingRow")
        self.serverGridWidget.show()
        self._fill_kv_grid(self.serverGrid, self._COURSE_INFO_FIELDS, node)
        self.hoursGridWidget.show()
        self._fill_kv_grid(self.hoursGrid, self._COURSE_HOURS_FIELDS, node)
        self.detailKvWidget.show()
        self._fill_kv_grid(self.detailKvGrid, self._COURSE_DETAIL_KV, node)
        self._fill_detail_long(node)
        # 服务端类别真相覆盖本地页签推导值（kclb/publicCourseTypeName 为空时不动）
        self._override_local_kind(node)
        # 服务端备注（remark 优先，extMsg 回落）补充显示（去重拼接，见实现）
        self._apply_server_remark(node)

    def _fill_kv_grid(self, grid, fields, node):
        """把 (标题, 编码键) 字段表填进 key-value 网格（两列键值对）。"""
        row = col = 0
        for key, code in fields:
            self._kv_cell(key, str(node.get(code) or ""), row, col, grid)
            if col == 0:
                col = 1
            else:
                col = 0
                row += 1

    def _fill_detail_long(self, node):
        """长文本字段：自动换行富文本 QLabel + zbw.CopyTextButton（复制原文）。

        键标签与 KV 网格同宽（KEY_LABEL_WIDTH）、行间距同值（KV_H_SPACING），
        值列起始 x 与各 KV 节一致；超长键（如「与学校本科人才培养目标的契合
        关系：」）在同宽内自动换行，不截断。
        """
        while self.detailLongLayout.count():
            item = self.detailLongLayout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for key, code in self._COURSE_DETAIL_LONG:
            raw = str(node.get(code) or "").strip()
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(KV_H_SPACING)
            keyLabel = BodyLabel(f"{key}：", self.widget)
            keyLabel.setTextColor("#606060", "#d2d2d2")
            keyLabel.setFixedWidth(KEY_LABEL_WIDTH)
            keyLabel.setWordWrap(True)
            keyLabel.setAlignment(Qt.AlignmentFlag.AlignTop)
            valLabel = make_selectable(BodyLabel(self.widget))
            # 标签上挂一份纯文本（测试与调试用；富文本里是 <br> 版本）
            valLabel._plain_text = raw
            valLabel.setTextFormat(Qt.TextFormat.RichText)
            valLabel.setWordWrap(True)
            valLabel.setText(self._rich(raw) if raw else "—")
            copyBtn = zbw.CopyTextButton(raw, key, self.widget)
            if not raw:
                copyBtn.setEnabled(False)
            row.addWidget(keyLabel)
            row.addWidget(valLabel, 1)
            row.addWidget(copyBtn, 0, Qt.AlignmentFlag.AlignTop)
            holder = QWidget(self.widget)
            holder.setLayout(row)
            self.detailLongLayout.addWidget(holder)
        self.detailLongWidget.show()

    def _on_info_error(self, msg):
        if self._aborted:
            return
        # 该节首次渲染（失败）→ 加载行同样移出布局并销毁
        self._dismiss_loading_row("infoLoadingRow")
        self.infoErrorLabel.setText(msg)
        self.infoErrorRow.show()

    def _on_schedule_ready(self, data):
        if self._aborted:
            return
        rows = (data or {}).get("dataList") or []
        # 该节首次渲染（成功）→ 加载行移出布局并销毁，弹窗内不留加载指示
        self._dismiss_loading_row("scheduleLoadingRow")
        self.scheduleTable.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, code in enumerate(self._SCHEDULE_FIELDS):
                text = str((row or {}).get(code) or "")
                self.scheduleTable.setItem(r, c, QTableWidgetItem(text))
        self.scheduleTable.show()
        # 行高自适应内容（长教学内容换行显示），需在布局完成后调用；
        # 列宽在布局激活后才稳定，故再排队校正一次行高与表高
        self.scheduleTable.resizeRowsToContents()
        QTimer.singleShot(0, self.scheduleTable, self._adjust_schedule_height)

    def _adjust_schedule_height(self):
        """周历表高度按内容自适应：重排行高后 = 表头 + 各行高 + 边框。

        宽度交给 layout 伸展（Expanding），垂直 Fixed 且高度显式设定——
        否则 QAbstractScrollArea 的 sizeHint 不随行数增长，行会被截断成
        内部滚动（「没有加入 layout」观感的一部分）。
        """
        table = self.scheduleTable
        if self._aborted or table.isHidden():
            return
        table.resizeRowsToContents()
        rows_h = sum(table.rowHeight(r) for r in range(table.rowCount()))
        table.setFixedHeight(
            table.horizontalHeader().height() + rows_h + 2 * table.frameWidth() + 2
        )

    def _on_schedule_error(self, msg):
        if self._aborted:
            return
        # 该节首次渲染（失败）→ 加载行同样移出布局并销毁
        self._dismiss_loading_row("scheduleLoadingRow")
        self.scheduleErrorLabel.setText(msg)
        self.scheduleErrorRow.show()

    # ------------------------------------------------------------------
    # 关闭安全
    # ------------------------------------------------------------------

    def done(self, code):
        """关闭弹窗：置 aborted 并断开工作线程信号，迟到信号安全忽略。

        工作线程仍会跑完并 emit，但断开后不再有槽被调用；线程池任务本身
        幂等（只读 client 与本地字段），无需强杀。
        """
        self._aborted = True
        for sig in (self._infoReady, self._infoError,
                    self._scheduleReady, self._scheduleError):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        super().done(code)
