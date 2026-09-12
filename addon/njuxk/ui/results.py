"""我的课程 / 我的报名页（courseResult.do，抓包 [2]/[3]）。

数据源：``client.fetch_course_results(other, batch_code)``，``other`` 由构造参数
决定——``"99"``=我的课程（已选中，``selectStatus`` 全 ``"99"``）、``"01"``=我的报名
（报名/抽签队列，``selectStatus`` 全 ``"01"``）。两页行 ``canDelete=="1"`` 均有
删除按钮：我的课程页文案「退选」、我的报名页文案「取消报名」，后端同一
``delete_volunteer`` 流程。

展示（用户需求「以课程列表的形式在单独的标签页展示结果」+「我的报名/我的课程
的课程卡片改成与选课/收藏页一样的布局」）：每门课一张 :class:`ResultCard`
（继承选课/收藏页的 :class:`~njuxk.ui.cards.CourseCard`，**布局完全一致**——
编号/课程名/学分、教师、时间地点、校区 + 已选容量 + 选中概率、简介、备注 +
右侧按钮列），结果页只保留删除按钮（``canDelete=="1"`` 才显示，文案按页面：
我的课程「退选」/ 我的报名「取消报名」），隐藏收藏/报名/抢课。

批次码来源：``state.load_selection(setting)`` 的 ``selected_batch``（与选课页
持久化的同一份）；无批次时空态提示「请先在选课页选择批次」，不发请求。

数据新鲜度（``mark_stale`` 机制）：报名/退选成功后服务端结果会变——装配层在
``enrollFinished`` / ``dropFinished`` 成功分支调 :meth:`mark_stale` 置脏；
MainPage 页签切到本页时调 :meth:`on_shown`，置脏或从未加载过才真正 ``load_results()``
（主线程发起，网络在工作线程，信号回主线程渲染——线程约定对齐 ``ui/course.py``）。

固定顶栏（结构冻结，照抄 ``ui/course.py`` / ``ui/favorites.py`` 三页既定范式）：
工具行固定在页根，只有卡片列表在本页唯一一个纵向 ``SmoothScrollArea``
（``resultScroll``）里滚动；``enableTransparentBackground()`` 在 ``setWidget``
**之后**调用 + inner 显式透明样式（``ui/course.py:362`` 最新修法，两道保险）。
"""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    ToolButton,
)

import zbWidgetLib as zbw

from ..api import models
from ..api.client import XkClient
from ..core import state
from .cards import CourseCard
from .layout import SPACING, apply_page_margins, apply_tool_row
from .login import _host_program, _host_setting
from .text_select import make_selectable

#: 加载卡在 ``cardGroup`` 里的占位 id（``_hide_loading`` 按它移除）
LOADING_WID = "__result_loading__"


def _format_credits(total: float) -> str:
    """学分汇总展示：整数不带小数点，非整数最多保留 2 位并去掉末尾 0。"""
    if abs(total - round(total)) < 1e-9:
        return str(int(round(total)))
    return f"{total:.2f}".rstrip("0").rstrip(".")


class ResultCard(CourseCard):
    """选课结果卡片（我的课程 / 我的报名共用）。

    布局与选课/收藏页的 :class:`~njuxk.ui.cards.CourseCard` **完全一致**：
    编号 + 课程名 + 学分 / 教师 / 时间地点 / 校区 + 已选容量 + 选中概率 /
    简介 / 备注 + 右侧按钮列。结果页只保留删除按钮（``can_delete=="1"``
    才显示），隐藏收藏/报名/抢课。

    ``allow_drop`` 兼容旧调用（结果页恒为 ``True``）；``drop_label`` 决定按钮
    文案（我的课程「退选」/ 我的报名「取消报名」）。
    """

    def __init__(self, parent=None, allow_drop=False, drop_label="退选"):
        super().__init__(parent, result_mode=True, drop_label=drop_label)
        self._allow_drop = bool(allow_drop)


class CourseResultPage(QWidget):
    """选课结果页（我的课程 other="99" / 我的报名 other="01"）。

    布局（三页既定范式，结构冻结）：工具行（标题 + 计数 + 刷新）固定在页根，
    只有卡片列表在页内唯一一个纵向 ``SmoothScrollArea``（``resultScroll``）里
    滚动；通知/弹窗由 ``ui/info.info_parent`` 统一提升到插件最高层级页面（MainPage）。

    信号：
        dropRequested(str) —— 删除请求（teaching_class_id），转发自卡片
            （``canDelete=="1"`` 的卡片有按钮：我的课程页「退选」/ 我的报名页
            「取消报名」），由装配层接二次确认 + ``delete_volunteer`` 流程
    """

    # InfoBar / 对话框挂载标记（对齐 ui/course.py / ui/favorites.py）
    _info_parent_flag = True

    dropRequested = Signal(str)

    # 内部线程信号（工作线程 emit，主线程槽接收）
    _resultsReady = Signal(object)
    _loadError = Signal(object)

    def __init__(
            self,
            parent=None,
            client=None,
            setting=None,
            program=None,
            other="01",
            title="",
    ):
        super().__init__(parent)
        self.client = client if client is not None else XkClient()
        self._setting = setting
        self._program = program
        self._fallback_pool = None
        if other not in ("01", "99"):
            raise ValueError(f"other 只允许 '01'/'99'：{other!r}")
        self._other = other
        self._title = title or ("我的课程" if other == "99" else "我的报名")
        # 数据新鲜度：True = 置脏或从未加载过（on_shown 时需要刷新）
        self._stale = True
        self._load_seq = 0
        self._loaded_courses = []
        self._loading_card = None
        self._loading_container = None

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # program / setting 兜底（对齐 ui/course.py）
    # ------------------------------------------------------------------

    @property
    def program(self):
        """宿主 program（注入优先，其次 main 模块，最后 None）。"""
        if self._program is not None:
            return self._program
        return _host_program()

    @property
    def setting(self):
        """宿主 setting（注入优先，其次 main 模块，最后 None）。"""
        if self._setting is not None:
            return self._setting
        return _host_setting()

    def _thread_pool(self):
        """返回线程池：宿主 ``program.THREAD_POOL``，无宿主时用本地兜底池。"""
        program = self.program
        if program is not None and hasattr(program, "THREAD_POOL"):
            return program.THREAD_POOL
        if self._fallback_pool is None:
            self._fallback_pool = ThreadPoolExecutor(max_workers=1)
        return self._fallback_pool

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        # 页根布局：工具行（固定）+ 卡片滚动区（滚动）
        self.vBoxLayout = QVBoxLayout(self)
        apply_page_margins(self.vBoxLayout, bottom=0)

        # ---- 工具行：标题 + 计数 + 刷新 ----
        toolRow = apply_tool_row(QHBoxLayout())
        self.titleLabel = StrongBodyLabel(self._title, self)
        self.countLabel = BodyLabel("", self)
        self.countLabel.setTextColor("#606060", "#d2d2d2")
        self.refreshButton = ToolButton(FIF.SYNC, self)
        self.refreshButton.setToolTip(
            f"刷新{self._title}：重新拉取选课结果（选课/退选后数据会变）"
        )
        toolRow.addWidget(self.titleLabel)
        toolRow.addWidget(self.countLabel)
        toolRow.addStretch(1)
        toolRow.addWidget(self.refreshButton)
        self.vBoxLayout.addLayout(toolRow)

        # ---- 卡片区：唯一纵向滚动区（工具行固定在滚动内容之外）----
        self.resultScroll = SmoothScrollArea(self)
        self.resultScroll.setWidgetResizable(True)
        self.resultScroll.setFrameShape(QFrame.NoFrame)
        # 滚动内容容器：**先 setWidget 再 enableTransparentBackground**（qfw
        # 只给「当时已 setWidget 的内容 widget」设透明样式），inner 再显式透明
        # 一道保险（照 ui/course.py:362 最新修法，见 test_scroll_background）
        inner = QWidget(self.resultScroll)
        self.resultScroll.setWidget(inner)
        self.resultScroll.enableTransparentBackground()
        inner.setStyleSheet("QWidget {background-color: rgba(0,0,0,0); border: none}")
        innerLayout = QVBoxLayout(inner)
        innerLayout.setContentsMargins(0, 4, 0, 0)
        innerLayout.setSpacing(SPACING)

        # 列表区：CardGroup + 空态 + 错误行（全在滚动内容里）
        self.cardGroup = zbw.CardGroup(inner, show_title=False, is_v=True)
        innerLayout.addWidget(self.cardGroup)

        self.emptyLabel = make_selectable(BodyLabel("", inner))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setTextColor("#606060", "#d2d2d2")
        self.emptyLabel.hide()
        innerLayout.addWidget(self.emptyLabel)

        # 错误行：错误文案（红，可选中）+ 重试按钮，初始隐藏
        self.errorLabel = make_selectable(BodyLabel("", inner))
        self.errorLabel.setWordWrap(True)
        self.errorLabel.setTextColor("#c42b1c", "#ff99a4")
        self.retryButton = PushButton(FIF.SYNC, "重试", inner)
        self.errorRow = QWidget(inner)
        errorLayout = QHBoxLayout(self.errorRow)
        errorLayout.setContentsMargins(0, 0, 0, 0)
        errorLayout.setSpacing(SPACING)
        errorLayout.addWidget(self.errorLabel, 1)
        errorLayout.addWidget(self.retryButton)
        self.errorRow.hide()
        innerLayout.addWidget(self.errorRow)

        # 末尾弹簧吸收多余纵向空间：CardGroup 纵向策略是 Preferred，滚动内容又被
        # widgetResizable(True) 撑到整个视口高，没有弹簧时列表区会被拉得比卡片总高
        # 出一截（用户反馈各页多出的空白还不一样）。弹簧让列表按内容自然高度贴顶。
        innerLayout.addStretch(1)

        # 卡片滚动区独占剩余纵向空间（stretch=1），工具行保持固有高度
        self.vBoxLayout.addWidget(self.resultScroll, 1)

    def _connect_signals(self):
        self.refreshButton.clicked.connect(self.load_results)
        self.retryButton.clicked.connect(self.load_results)
        self._resultsReady.connect(self._on_results_ready)
        self._loadError.connect(self._on_load_error)

    # ------------------------------------------------------------------
    # 数据加载（线程池 + 序号防乱序，范式对齐 ui/course.py）
    # ------------------------------------------------------------------

    def _current_batch_code(self) -> str:
        """当前选课批次码（与选课页持久化同一份 ``selected_batch``）。"""
        if self._setting is None:
            return ""
        batch, _category = state.load_selection(self._setting)
        return batch or ""

    def load_results(self):
        """拉取选课结果并渲染（刷新按钮 / 重试按钮 / 页签切换共用入口）。

        无批次码（用户还没在选课页选过批次）时只显示提示，**不发请求**。
        """
        batch_code = self._current_batch_code()
        if not batch_code:
            self._show_empty("请先在选课页选择批次")
            return
        self._stale = False
        self._load_seq += 1
        seq = self._load_seq
        self.refreshButton.setEnabled(False)
        self._show_loading()
        self._thread_pool().submit(self._fetch_worker, seq, batch_code)

    def _fetch_worker(self, seq, batch_code):
        """工作线程：查询选课结果，经信号回主线程。严禁在此操作 QWidget。"""
        try:
            rows = self.client.fetch_course_results(self._other, batch_code)
            self._resultsReady.emit((seq, rows, batch_code))
        except Exception as e:
            logging.error(f"获取{self._title}失败：{traceback.format_exc()}")
            self._loadError.emit((seq, f"获取{self._title}失败：{e}"))

    def _on_results_ready(self, payload):
        """主线程：解析行 → 建卡渲染（过期响应按序号丢弃）。"""
        seq, rows, batch_code = payload
        if seq != self._load_seq:
            return  # 过期响应，丢弃
        self.refreshButton.setEnabled(True)
        self._hide_loading()
        self.errorRow.hide()
        courses = [
            models.from_result_row(row, batch_code)
            for row in rows or []
            if isinstance(row, dict)
        ]
        self._loaded_courses = courses
        self._render()

    def _render(self):
        """按已加载课程重建卡片列表与计数标签（不发请求）。"""
        self.cardGroup.clearCard()
        # 两页均显示删除按钮（canDelete=="1" 才可见），文案按页面类型：
        # 我的课程页（99）「退选」、我的报名页（01）「取消报名」
        drop_label = "退选" if self._other == "99" else "取消报名"
        for course in self._loaded_courses:
            card = ResultCard(self, allow_drop=True, drop_label=drop_label)
            card.client = self.client
            card.set_course(course)
            card.dropRequested.connect(self.dropRequested)
            self.cardGroup.addCard(card, wid=course.teaching_class_id)
        total = len(self._loaded_courses)
        credits = self._total_credits()
        self.countLabel.setText(
            f"共 {total} 门 · 总学分 {_format_credits(credits)}"
        )
        self.emptyLabel.setText(
            "暂无已选课程" if self._other == "99" else "暂无报名记录"
        )
        self.emptyLabel.setVisible(total == 0)

    def _total_credits(self) -> float:
        """当前列表内可解析学分之和（credit 缺失/非数字的课程跳过）。"""
        total = 0.0
        for course in self._loaded_courses:
            try:
                total += float(course.credit)
            except (TypeError, ValueError):
                continue
        return total

    def _on_load_error(self, payload):
        """主线程：显示错误行 + 重试按钮（过期响应按序号丢弃）。"""
        seq, msg = payload
        if seq is not None and seq != self._load_seq:
            return
        self.refreshButton.setEnabled(True)
        self._hide_loading()
        self.errorLabel.setText(msg)
        self.errorRow.setVisible(True)
        self.emptyLabel.setVisible(False)

    def find_course(self, teaching_class_id):
        """按 teaching_class_id 在已加载列表里找 Course（找不到返回 None）。

        装配层退选时用它定位课程对象（结果页没有卡片级 course 之外的来源）。
        """
        for course in self._loaded_courses:
            if course.teaching_class_id == teaching_class_id:
                return course
        return None

    # ------------------------------------------------------------------
    # 新鲜度 / 页签切换
    # ------------------------------------------------------------------

    def mark_stale(self):
        """数据置脏（报名/退选成功后服务端结果会变）：页签再次进入时刷新。"""
        self._stale = True

    def on_shown(self):
        """页签切到本页时由装配层（MainPage）调用：置脏或从未加载过则刷新。"""
        if self._stale:
            self.load_results()

    def clear_results(self):
        """清空列表与状态（登出时由装配层调用）：下次进入重新加载。"""
        self._stale = True
        # 序号自增使在途响应过期，避免登出后迟到响应重新渲染卡片
        self._load_seq += 1
        self._hide_loading()
        self.cardGroup.clearCard()
        self._loaded_courses = []
        self.countLabel.setText("")
        self.errorRow.hide()
        self.emptyLabel.hide()

    # ------------------------------------------------------------------
    # 加载态
    # ------------------------------------------------------------------

    def _show_loading(self):
        """清空旧卡片并显示居中加载卡（照 course 页 _loading_card 范式）。"""
        self.emptyLabel.hide()
        self.errorRow.hide()
        self.cardGroup.clearCard()
        # 加载指示器水平居中：CardGroup 的 addCard 靠左插入，套一层
        # 左右 stretch 的容器居中显示
        self._loading_container = QWidget(self)
        loadingLayout = QHBoxLayout(self._loading_container)
        loadingLayout.setContentsMargins(0, 0, 0, 0)
        loadingLayout.setSpacing(0)
        loadingLayout.addStretch(1)
        self._loading_card = zbw.LoadingCard(self._loading_container)
        loadingLayout.addWidget(self._loading_card)
        loadingLayout.addStretch(1)
        loadingLayout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.cardGroup.addCard(self._loading_container, wid=LOADING_WID)

    def _hide_loading(self):
        if self._loading_card is not None:
            self.cardGroup.removeCard(LOADING_WID)
            self._loading_card = None
            self._loading_container = None

    def _show_empty(self, text):
        """显示空态提示（如「请先在选课页选择批次」），不发请求。"""
        self._hide_loading()
        self.errorRow.hide()
        self.cardGroup.clearCard()
        self._loaded_courses = []
        self.countLabel.setText("")
        self.emptyLabel.setText(text)
        self.emptyLabel.setVisible(True)
