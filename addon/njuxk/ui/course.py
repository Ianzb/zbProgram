"""选课页（计划 todo 11）：批次选择 → 类别切换 → 课程卡片列表 + 分页 + 搜索框。

流程：批次下拉（显示 name + beginTime，选中持久化）→ 类别 SegmentedWidget
（由所选批次的 ``limitMenuList`` 经 ``models.filter_selectable_menus`` 过滤生成）→
课程卡片列表（``zbw.CardGroup`` 装 ``CourseCard``）+ 底部「加载更多」分页 + 搜索框。

course_kind 补全：抓包 ``#24``（batch.do）的 ``limitMenuList`` 中 ``courseKind``
常为 null，真实取值在 ``sysparam.do`` 的 ``menuMap`` 与 ``student/{code}.do`` 的
``electiveBatchList[].limitMenuList``。因此首次加载类别时先在工作线程调用
``models.build_course_kind_map`` 取映射（结果缓存在 ``self._course_kind_map``，
切类别/切批次不再重复请求），再用它补全每个 ``Menu.course_kind``（仅补空值），
使非空值经 ``fetch_courses`` → ``Course`` → ``GrabTask`` 流入 ``volunteer.do``。

线程约定（对齐 ``ui/login.py``）：
- 所有网络请求走 ``program.THREAD_POOL``（宿主线程池），无宿主时用本地兜底池；
- 结果经信号回主线程，**严禁**工作线程直接操作 QWidget；
- 请求带自增序号，过期响应（序号不匹配）直接丢弃，避免乱序覆盖。

会话失效：``client.is_session_expired`` 命中时自动重新登录一次（凭据来自
``state.load_account``）并重试当前加载；重试仍失效或重新登录失败则发
``loginRequired`` 信号，由 MainPage 切回登录层（登录卡在 MainPage，见 main.py）。

启动自动登录（用户实测缺陷：重启宿主后课程列表加载不出来，日志报
``ValueError: 尚未登录，缺少 student_code``）：已迁移到装配层 ``XkApp`` 与
``MainPage``（登录卡在 MainPage 登录层）；本页只在**已持有完整凭据**时于构造
后加载批次 → 类别 → 课程。

分页约定：翻页条件为 ``is_last == False``（**严禁**用 ``totalCount``，抓包中恒为 0）；
翻页时追加而非清空。

收藏约定：收藏状态一律来自本地收藏集（``state``），**严禁**调用服务端收藏接口
（favorite.do / queryfavorite.do），也**严禁**读取课程响应里的 ``favorite`` 字段。

刷新按钮细化（用户要求「支持只刷新课程列表而不是刷新所有」）：
- 「刷新批次」（``refreshBatchButton``）→ 重新拉取批次列表并重建类别；
- 「刷新课程」（``refreshCoursesButton``）→ **只**重新查询当前类别的第 1 页，
  不重拉批次、不重建类别、不动收藏。两者刷新期间都禁用自身防连点。

校区筛选（``campusCombo``）：选项从**已加载课程**的 ``campus_name`` 去重生成，
加一项「全部校区」（默认）。切换只在客户端过滤 ``self._loaded_courses`` 后重新
渲染卡片，**不发任何请求**；切换类别/搜索时保留筛选值；筛选后为空显示
「当前筛选条件下暂无课程」。

固定顶栏（用户要求「选课页面的顶栏要固定，不随页面滚动」）：工具行与类别区
固定在页根布局，**只有**课程卡片列表在页内唯一的纵向滚动区（``cardScroll``）
里滚动；本页因此不再继承 ``zbw.BasicTab``（其整页即滚动区），改为普通
``QWidget``（详见 ``CoursePage`` 类注释与 ``ui/cards.py`` 的挂载标记约定）。

布局紧凑（用户要求「不要一个组件一行」）：批次下拉、刷新批次、搜索框、校区
筛选、刷新课程全部塞进**同一个工具行**；「刷新课程」放在工具行**最右**（与
搜索/校区同侧），与「刷新批次」之间隔着可伸缩的搜索框，两个刷新不会并排紧挨
（防误点）。批次下拉限宽（最大 260px）；类别 ``SegmentedWidget`` 强制最小宽度
并放进**横向滚动容器**（qfluentwidgets ``ScrollArea``，fluent 细滚动条 +
透明背景），类别再多也不撑宽窗口。
"""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtWidgets import QSizePolicy
from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    FluentIcon as FIF,
    PushButton,
    ScrollArea,
    SmoothScrollArea,
    ToolButton,
)

import zbWidgetLib as zbw

from ..api import models
from ..api.client import XkClient
from ..core import search as core_search
from ..core import state
from .cards import CourseCard
from .layout import (
    BATCH_COMBO_MAX_WIDTH,
    BATCH_COMBO_MIN_WIDTH,
    CATEGORY_MIN_WIDTH,
    SEARCH_MIN_WIDTH,
    SPACING,
    apply_page_margins,
    apply_tool_row,
)
from .login import _host_program, _host_setting
from .text_select import make_selectable

#: 加载卡在 ``cardGroup`` 里的占位 id（``_hide_loading`` 按它移除）
LOADING_WID = "__loading__"
#: 校区筛选的「全部校区」选项（userData 为空串，表示不过滤）
ALL_CAMPUSES = "全部校区"

#: 类别 Tab 显示顺序（menuCode 列表，如 ``["GG02", "GG01", "ZY"]``）。
#:
#: 默认为用户指定的 8 类顺序：专业 → 研学/探讨/通识 → 科学之光 → 美育 → 公选 →
#: 跨专业 → 体育 → 悦读。列表中的类别按此处顺序排在前面，常量**未收录**的类别
#: 按服务端顺序追加在尾部（遗漏的 code 不丢失、不崩溃，如 TX01 大学数学）。
#: 设为 ``[]`` 恢复「按服务端 limitMenuList 原顺序」。改动后重启宿主生效。
CATEGORY_ORDER: list[str] = ["ZY", "GG02", "GG06", "MY", "GG01", "KZY", "TY", "YD"]


def _sort_menus_by_category_order(menus):
    """按 ``CATEGORY_ORDER`` 常量重排类别菜单（常量为空 = 服务端原顺序）。

    常量收录的 menuCode 按常量出现顺序排前面；未收录的按服务端顺序追加在
    尾部——遗漏的 code 不丢失、不崩溃。
    """
    if not CATEGORY_ORDER:
        return menus
    rank = {code: i for i, code in enumerate(CATEGORY_ORDER)}
    front = sorted(
        (m for m in menus if m.menu_code in rank),
        key=lambda m: rank[m.menu_code],
    )
    tail = [m for m in menus if m.menu_code not in rank]
    return front + tail


class CoursePage(QWidget):
    """选课页：批次 → 类别 → 课程列表（分页 + 搜索）。

    登录卡与账号按钮不在本页：登录层在 ``MainPage``（main.py），账号菜单在
    ``MainPage`` 顶栏。本页只负责已登录后的浏览（批次/类别/课程）。

    布局（用户要求「顶栏要固定，不随页面滚动」）：本页**不再**继承
    ``zbw.BasicTab``（BasicTab 整页就是一个纵向滚动区，工具行/类别区会跟着
    课程卡片一起滚走），改为普通 ``QWidget`` 页根布局 + 内部唯一一个纵向
    ``SmoothScrollArea``（``cardScroll``）—— 工具行（``toolRow``）与类别区
    （``categoryScroll``）固定在页根，只有课程卡片列表（cardGroup /
    emptyLabel / loadMoreButton）在 ``cardScroll`` 里滚动。
    ``BasicTabPage.addPage`` 只要求 widget 本身，不要求 BasicTab 类型，页签
    行为不变。

    信号：
        enrollRequested(str) —— 立即报名请求（teaching_class_id），转发自卡片
        grabRequested(str)   —— 加入抢课请求（teaching_class_id），转发自卡片
        dropRequested(str)   —— 退选请求（teaching_class_id），转发自卡片
            （仅 ``is_choose=="1"`` 的卡片显示「退选」按钮）
        favoriteToggled(str, bool) —— 收藏切换（teaching_class_id, 是否收藏），
            由 ``_on_favorite_toggled`` 在写完本地收藏集后发出，供装配层刷新收藏页
        loginRequired()      —— 会话失效且重新登录失败，请求切回登录层
            （由 MainPage 响应；本页不再内嵌登录卡）
    """

    # InfoBar / 对话框挂载标记：``ui/cards.py`` 的 ``_dialog_parent`` 沿 parent
    # 链上溯找插件页面（原来只认 ``zbw.BasicTab``），本页改基类后靠此标记继续
    # 被识别为插件页面 —— 弹窗遮罩仍覆盖整个选课页，绝不提升到宿主主窗口
    _info_parent_flag = True

    enrollRequested = Signal(str)
    grabRequested = Signal(str)
    dropRequested = Signal(str)
    favoriteToggled = Signal(str, bool)
    loginRequired = Signal()

    # 内部线程信号（工作线程 emit，主线程槽接收）
    _batchesReady = Signal(list)
    _coursesReady = Signal(object)
    _loadError = Signal(object)
    _reloginReady = Signal(bool, str)
    _courseKindMapReady = Signal(object)

    def __init__(self, parent=None, client=None, setting=None, program=None):
        super().__init__(parent)
        self.client = client if client is not None else XkClient()
        self._setting = setting
        self._program = program
        self._fallback_pool = None

        self._batches = []
        self._menus = []
        self._current_batch_code = ""
        self._current_menu = None
        self._page_number = 0
        self._page_size = 50
        self._query_content = ""
        self._is_last = True
        self._load_seq = 0
        self._course_kind_seq = 0
        self._relogin_attempted = False
        self._loading_card = None
        self._loading_container = None
        # menu_code → course_kind 映射；None = 尚未拉取（首次需在工作线程取）
        self._course_kind_map = None
        # 当前类别/搜索下已加载的全部课程（校区筛选在这份列表上做**客户端过滤**，
        # 不发请求）；翻页追加，reset 时清空
        self._loaded_courses = []
        # 当前校区筛选值（空串 = 全部校区）
        self._campus_filter = ""

        self._build_ui()
        self._connect_signals()

        # 未登录的分支（自动登录 / 登录卡）由 MainPage 与装配层负责（登录层在
        # MainPage）；本页只在已持有完整凭据时直接进入浏览并加载批次。
        if self.is_logged_in():
            self.load_batches()

    # ------------------------------------------------------------------
    # program / setting 兜底（对齐 ui/login.py）
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
        # 页根布局：工具行 + 类别区 + 卡片滚动区（前三者/前者固定，最后者滚动）
        self.vBoxLayout = QVBoxLayout(self)
        apply_page_margins(self.vBoxLayout)

        # ---- 工具行：批次下拉 + 刷新批次 + 搜索 + 校区 + 刷新课程（同一行）----
        # 需求「不要一个组件一行」：此前批次下拉、刷新、搜索各占一行，纵向空间被
        # 吃掉大半。这里全部塞进一个 QHBoxLayout，只有搜索框可伸缩（stretch=1），
        # 其余控件都是固定/最小宽度，窗口变窄时先压缩搜索框。
        # 「刷新课程」放最右（与搜索/校区同侧），与「刷新批次」之间隔着可伸缩的
        # 搜索框 —— 两个刷新不会并排紧挨，防止误点（用户实测反馈）。
        toolRow = apply_tool_row(QHBoxLayout())

        self.batchCombo = ComboBox(self)
        # 限宽：长批次名（「【老生】2026年秋季课程补选」）不能把整行撑满
        self.batchCombo.setMinimumWidth(BATCH_COMBO_MIN_WIDTH)
        self.batchCombo.setMaximumWidth(BATCH_COMBO_MAX_WIDTH)
        self.batchCombo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )

        self.refreshBatchButton = ToolButton(FIF.SYNC, self)
        self.refreshBatchButton.setToolTip(
            "刷新批次：重新拉取选课活动（批次）列表，并重建课程类别"
        )
        self.refreshCoursesButton = ToolButton(FIF.UPDATE, self)
        self.refreshCoursesButton.setToolTip(
            "刷新课程：只重新查询当前类别的第 1 页课程，"
            "不重新拉取批次、不重建类别、不影响收藏"
        )

        self.searchEdit = zbw.SearchLineEdit(self)
        self.searchEdit.setPlaceholderText("搜索课程名称 / 课程号 / 教师")
        self.searchEdit.setMinimumWidth(SEARCH_MIN_WIDTH)

        self.campusCombo = ComboBox(self)
        self.campusCombo.setToolTip("按校区筛选当前已加载的课程（不发请求）")
        self.campusCombo.setMinimumWidth(110)
        self.campusCombo.addItem(ALL_CAMPUSES, userData="")

        toolRow.addWidget(self.batchCombo)
        toolRow.addWidget(self.refreshBatchButton)
        toolRow.addWidget(self.searchEdit, 1)
        toolRow.addWidget(self.campusCombo)
        toolRow.addWidget(self.refreshCoursesButton)
        self.vBoxLayout.addLayout(toolRow)

        # ---- 类别区：SegmentedWidget（最小宽度 + 横向滚动容器）----
        # 类别数量不定（夹具 6 个，真实批次可达 10+），自然宽度会远超窗口宽度。
        # 方案：强制最小宽度 + 放进横向滚动容器 —— 超出时横向滚动，不撑宽窗口。
        # 注意：Pivot 内部布局是 SetMinimumSize 约束，会把 widget 的 minimumSize
        # 重置为布局的自然宽度，因此最小宽度要设在**外层容器**上才稳定。
        self.categoryPivot = zbw.SegmentedWidget(self)
        self.categoryPivot.setMinimumWidth(CATEGORY_MIN_WIDTH)
        self.categoryPivot.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum
        )

        self.categoryContainer = QWidget(self)
        categoryLayout = QHBoxLayout(self.categoryContainer)
        categoryLayout.setContentsMargins(0, 0, 0, 0)
        categoryLayout.setSpacing(0)
        categoryLayout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        categoryLayout.addWidget(self.categoryPivot)
        self.categoryContainer.setMinimumWidth(CATEGORY_MIN_WIDTH)

        # **必须用 qfluentwidgets ScrollArea + setWidgetResizable(True)**（用户
        # 实测回归：类别点不动）。此前用的 SingleDirectionScrollArea 配
        # setWidgetResizable(False)：setWidget 时内容 widget 被按**当时**的
        # sizeHint 定尺寸（布局尚空 → 0 高），之后无人再调整 —— 类别项全部被
        # 0 高容器裁剪（visibleRegion 为空），真实点击永远落在容器上而非类别项，
        # 程序化 setCurrentItem 却正常（既有测试因此全绿而用户点不动）。
        # widgetResizable(True) 会把内容 widget 持续撑到
        # max(viewport, minimumSize)，不吞任何鼠标事件。
        # ScrollArea = QScrollArea + SmoothScrollDelegate（fluent 自绘细滚动条）
        # + enableTransparentBackground()：原生滚动条被 delegate 置为 AlwaysOff
        # （不再压缩 viewport），fluent 滚动条是覆盖在滚动区底边的自绘 overlay，
        # 显隐只影响覆盖条自身，不改变 viewport 高度。
        self.categoryScroll = ScrollArea(self)
        self.categoryScroll.setWidgetResizable(True)
        self.categoryScroll.setWidget(self.categoryContainer)
        self.categoryScroll.setMinimumWidth(CATEGORY_MIN_WIDTH)
        self.categoryScroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        # delegate 接管了 setXxxScrollBarPolicy：原生滚动条一律 AlwaysOff，
        # fluent 覆盖条按 AsNeeded 语义显隐（范围 maximum > 0 时可见）
        self.categoryScroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.categoryScroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # 透明背景（滚动区与内容 widget 各设一条样式，等价于此前手写的两条）
        self.categoryScroll.enableTransparentBackground()
        # 横向滚动范围变化 → 重算行高：fluent 覆盖条出现时行高要预留它的
        # 覆盖带高度，否则覆盖条压在类别项底部（点击热区被盖掉一条）。
        # 原生 QScrollBar 仍是范围模型（delegate 的覆盖条只是它的自绘视图），
        # rangeChanged 照常发出；尺寸不变时 setFixedHeight 是空操作，不会循环。
        self.categoryScroll.horizontalScrollBar().rangeChanged.connect(
            lambda *_: self._sync_category_size()
        )
        # QScrollArea 的 minimumSizeHint 恒为 (68, 68)，会把行高撑到 68px；
        # 按类别控件实际高度锁死，避免类别区占掉半屏
        self._sync_category_size()
        self.vBoxLayout.addWidget(self.categoryScroll)

        # ---- 卡片区：唯一纵向滚动区（顶栏/类别区固定在滚动内容之外）----
        # 用户需求「选课页面的顶栏要固定，不随页面滚动」：此前整页继承
        # zbw.BasicTab（BetterScrollArea），工具行/类别区在滚动内容里跟着卡片
        # 滚走。现在只有课程列表在 cardScroll 里滚动，范式对齐 ui/settings.py
        # （SmoothScrollArea + widgetResizable + NoFrame + 透明背景）。
        self.cardScroll = SmoothScrollArea(self)
        self.cardScroll.setWidgetResizable(True)
        self.cardScroll.setFrameShape(QFrame.NoFrame)
        self.cardScroll.enableTransparentBackground()

        # 滚动内容容器：页面边距已由 apply_page_margins 提供，这里只留
        # 少量上下边距（顶部与固定顶栏拉开一点、底部给加载更多按钮留呼吸），
        # 左右为 0 —— 不再叠一层大边距
        inner = QWidget(self.cardScroll)
        self.cardScroll.setWidget(inner)
        # inner 必须显式透明：qfw enableTransparentBackground() 只给「当时已
        # setWidget 的内容 widget」设透明样式（本页在其之前调用 → inner 拿不到）；
        # 而宿主 FluentWindow 的 FLUENT_WINDOW qss 会层叠进页面，使 inner
        # autoFillBackground=True 并涂上系统窗口色（亮 #efefef，暗色主题下与
        # 页面背景形成大色块）。写法对齐旧版 zbw.BasicTab（BetterScrollArea.view）。
        inner.setStyleSheet("QWidget {background-color: rgba(0,0,0,0); border: none}")
        innerLayout = QVBoxLayout(inner)
        innerLayout.setContentsMargins(0, 4, 0, 8)
        innerLayout.setSpacing(SPACING)

        # 列表区：CardGroup + 空结果 + 加载更多（全在滚动内容里）
        self.cardGroup = zbw.CardGroup(inner, show_title=False, is_v=True)
        innerLayout.addWidget(self.cardGroup)

        self.emptyLabel = make_selectable(BodyLabel("暂无课程", inner))
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setTextColor("#606060", "#d2d2d2")
        self.emptyLabel.hide()
        innerLayout.addWidget(self.emptyLabel)

        self.loadMoreButton = PushButton("加载更多", inner)
        self.loadMoreButton.hide()
        innerLayout.addWidget(self.loadMoreButton)

        # 卡片滚动区独占剩余纵向空间（stretch=1），工具行/类别区保持固有高度
        self.vBoxLayout.addWidget(self.cardScroll, 1)

    def _sync_category_size(self):
        """按类别项 sizeHint 同步滚动内容最小尺寸与滚动容器行高。

        三处实测过的坑：
        1. ``QScrollArea.minimumSizeHint()`` 恒为 ``(68, 68)``，布局会把类别行
           撑到 68px 高（白白占掉半屏），必须 ``setFixedHeight`` 锁死；
        2. 类别项刚插入时处于隐藏态（``WA_WState_Hidden``，show 之前 Qt 布局
           把它们当 empty 跳过），pivot / container 的 sizeHint 恒为 ``(0, 0)``
           —— 因此尺寸直接由**各类别项**的 sizeHint 累加/取最大，不依赖布局
           时机；
        3. ``QScrollArea`` 只在自身 resize / setWidget 时重算内容尺寸，错过
           时机（内容 sizeHint 事后才变有效）就永远不出横向滚动条，末尾类别
           不可达 —— 所以内容最小尺寸要**显式**设到 container 上（实测
           widgetResizable 会尊重 ``widget.minimumSize()``）。
        横向 fluent 覆盖条可见时把它的覆盖带高度一并计入行高（覆盖条贴滚动区
        底边，不预留会压住类别项底部），类别增减后重建时再调一次即可。
        """
        items = list(self.categoryPivot.items.values())
        if not items:
            return
        width = sum(item.sizeHint().width() for item in items)
        height = max(item.sizeHint().height() for item in items)
        if width <= 0 or height <= 0:
            return
        self.categoryContainer.setMinimumSize(width, height)
        # 横向滚动条可见性由 QAbstractScrollArea 的布局过程**事后**应用，
        # rangeChanged 回调里 isVisible() 还是旧值；AsNeeded 策略下
        # 「maximum > 0 ⟺ 可见」且 maximum 已同步更新，用它判断才可靠
        if self.categoryScroll.horizontalScrollBar().maximum() > 0:
            # fluent 覆盖条（delegate 自绘）高 12px、贴滚动区底边
            # （y = H-13），预留 13px 让类别项完整露在覆盖带之上
            height += self.categoryScroll.scrollDelagate.hScrollBar.height() + 1
        self.categoryScroll.setFixedHeight(
            height + 2 * self.categoryScroll.frameWidth()
        )

    def _connect_signals(self):
        self.batchCombo.currentIndexChanged.connect(self._on_batch_changed)
        self.refreshBatchButton.clicked.connect(self.load_batches)
        self.refreshCoursesButton.clicked.connect(self.refresh_courses)
        self.campusCombo.currentIndexChanged.connect(self._on_campus_changed)
        self.categoryPivot.currentItemChanged.connect(self._on_category_changed)
        self.searchEdit.searchSignal.connect(self._on_search)
        self.searchEdit.returnPressed.connect(
            lambda: self._on_search(self.searchEdit.text())
        )
        self.searchEdit.clearSignal.connect(lambda: self._on_search(""))
        self.loadMoreButton.clicked.connect(self._on_load_more)
        self._batchesReady.connect(self._on_batches_ready)
        self._coursesReady.connect(self._on_courses_ready)
        self._loadError.connect(self._on_load_error)
        self._reloginReady.connect(self._on_relogin_ready)
        self._courseKindMapReady.connect(self._on_course_kind_map_ready)

    # ------------------------------------------------------------------
    # 登录态
    # ------------------------------------------------------------------

    def _has_credentials(self) -> bool:
        """client 是否同时持有 token 与 student_code（缺一不可）。

        只恢复 token 不算已登录：``fetch_courses`` 要用 ``student_code`` 构造请求
        体，缺它会抛 ``ValueError: 尚未登录，缺少 student_code``（用户实测：重启
        宿主后课程列表加载不出来）。stub client 可能没有 ``student_code`` 属性
        （老夹具），此时只校验 token。
        """
        if not getattr(self.client, "token", ""):
            return False
        if not hasattr(self.client, "student_code"):
            return True
        return bool(self.client.student_code)

    def is_logged_in(self) -> bool:
        """是否已登录：client 持有完整凭据，或可从本地会话恢复出完整凭据。"""
        if self._has_credentials():
            return True
        if self._setting is not None:
            cookies, token, _ts = state.load_session(self._setting)
            if token and hasattr(self.client, "import_session"):
                try:
                    self.client.import_session(cookies, token)
                except Exception as e:
                    logging.debug("恢复本地会话失败：%s", e)
                    return False
                # 会话恢复只带回 token，student_code 仍为空 → 走自动登录补全
                return self._has_credentials()
        return False

    def has_saved_account(self) -> bool:
        """本地是否保存了账号密码（自动登录的前提）。"""
        if self._setting is None:
            return False
        user, pwd = state.load_account(self._setting)
        return bool(user and pwd)

    def reset_to_login(self):
        """退出登录：清空客户端会话与课程列表。

        登录层的切换由 ``MainPage`` 负责（本页不再内嵌登录卡）。客户端会话清空
        做防御式处理：有 ``clear_session`` 方法则调用，否则直接清 token /
        student_code / cookies，兼容测试注入的 stub client。
        """
        client = self.client
        if hasattr(client, "clear_session"):
            client.clear_session()
        else:
            client.token = ""
            client.student_code = ""
            session = getattr(client, "session", None)
            if session is not None and hasattr(session, "cookies"):
                session.cookies.clear()
        self.cardGroup.clearCard()
        self._loaded_courses = []
        # 会话已清空：course_kind 映射作废，下次加载批次时重新拉取
        self._course_kind_map = None

    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------

    def load_batches(self):
        """拉取批次列表（登录成功 / 已登录启动 / 手动「刷新批次」共用入口）。"""
        # 刷新期间禁用自身防连点（_on_batches_ready / _on_load_error 里恢复）
        self.refreshBatchButton.setEnabled(False)
        self._thread_pool().submit(self._fetch_batches_worker)

    def _fetch_batches_worker(self):
        """工作线程：拉取批次列表，经信号回主线程。"""
        try:
            raw = self.client.fetch_batches()
            self._batchesReady.emit(raw)
        except Exception as e:
            logging.error(f"获取批次失败：{traceback.format_exc()}")
            self._loadError.emit((None, f"获取批次失败：{e}"))

    def _on_batches_ready(self, raw_batches):
        self.refreshBatchButton.setEnabled(True)
        self._batches = [
            models.parse_batch(b) for b in raw_batches if isinstance(b, dict)
        ]
        # 填充期间屏蔽信号：QComboBox 加第一项会自动把 currentIndex 置 0 并发
        # currentIndexChanged，若不屏蔽会先按「第 1 个批次」建一次类别、等下面
        # setCurrentIndex 再建一次（记忆的批次非首个时重复拉一次课程）
        self.batchCombo.blockSignals(True)
        self.batchCombo.clear()
        for b in self._batches:
            self.batchCombo.addItem(f"{b.name}（{b.begin_time}）", userData=b.code)
        self.batchCombo.blockSignals(False)
        if not self._batches:
            self.cardGroup.clearCard()
            self._loaded_courses = []
            self.emptyLabel.setText("暂无可用批次")
            self.emptyLabel.setVisible(True)
            self.loadMoreButton.hide()
            return
        # 记忆上次选择的选课活动：命中则选中它（进而加载对应类别/课程）
        saved_batch, _ = self._saved_selection()
        index = self._find_batch_index(saved_batch)
        if index < 0:
            # 批次会变（补选/初选轮换），保存的批次可能已不存在 → 回退到第一个，
            # 只记 debug 不报错，绝不能卡在这里不加载
            logging.debug(
                "记忆的批次 %r 不在新拉取的 %d 个批次中，回退到第一个批次 %r",
                saved_batch,
                len(self._batches),
                self._batches[0].code,
            )
            index = 0
        if self.batchCombo.currentIndex() == index:
            # 填充后 currentIndex 已是 0，index 也是 0 → setCurrentIndex 不会再发
            # 信号，这里手动触发一次，保证类别/课程一定被加载
            self._on_batch_changed(index)
        else:
            self.batchCombo.setCurrentIndex(index)

    def _find_batch_index(self, code: str) -> int:
        for i, b in enumerate(self._batches):
            if b.code == code:
                return i
        return -1

    def _on_batch_changed(self, index):
        if index < 0 or index >= len(self._batches):
            return
        batch = self._batches[index]
        self._current_batch_code = batch.code
        self._persist_selection()
        self._rebuild_categories(batch)

    # ------------------------------------------------------------------
    # 类别
    # ------------------------------------------------------------------

    def _rebuild_categories(self, batch):
        menus = [
            models.parse_menu(m)
            for m in batch.limit_menu_list
            if isinstance(m, dict)
        ]
        menus = models.filter_selectable_menus(menus)
        # 类别 Tab 显示顺序：按 CATEGORY_ORDER 常量重排（常量为空 = 服务端原顺序）
        menus = _sort_menus_by_category_order(menus)
        self._menus = menus
        self._current_menu = None
        self.categoryPivot.clear()
        for m in menus:
            # 抓包 #24 的 menuName 多为 null → 用静态中文名表，未收录的回退 code
            self.categoryPivot.addItem(
                routeKey=m.menu_code, text=models.menu_display_name(m)
            )
        # 类别数量变了 → 重算滚动内容尺寸与容器行高（否则新增类别后行高不对、
        # 横向滚动范围过期）
        self._sync_category_size()
        if not menus:
            self.cardGroup.clearCard()
            self.emptyLabel.setText("暂无可用类别")
            self.emptyLabel.setVisible(True)
            self.loadMoreButton.hide()
            return
        if self._course_kind_map is None:
            # 首次：先在工作线程取 course_kind 映射（涉及网络，严禁在 UI 线程发），
            # 取到后再选类别，避免用空 course_kind 发查询/报名请求
            self._course_kind_seq += 1
            seq = self._course_kind_seq
            self._thread_pool().submit(
                self._fetch_course_kind_map_worker, seq, batch.limit_menu_list
            )
            return
        self._apply_course_kind_map()
        self._select_initial_category()

    def _fetch_course_kind_map_worker(self, seq, batch_limit_menu_list):
        """工作线程：构建 menu_code → course_kind 映射，经信号回主线程。

        抓包 ``#24`` 的 ``limitMenuList`` 中 ``courseKind`` 常为 null，真实取值在
        ``sysparam.do`` 的 ``menuMap`` 与 ``student/{code}.do`` 的
        ``electiveBatchList[].limitMenuList``（见 ``models.build_course_kind_map``）。
        严禁在此操作 QWidget。
        """
        try:
            mapping = models.build_course_kind_map(
                self.client, batch_limit_menu_list
            )
        except Exception as e:
            logging.debug(f"构建 course_kind 映射失败：{traceback.format_exc()}")
            mapping = {}
        self._courseKindMapReady.emit((seq, mapping))

    def _on_course_kind_map_ready(self, payload):
        """主线程：缓存映射并补全菜单的 course_kind（过期响应直接丢弃）。"""
        seq, mapping = payload
        if seq != self._course_kind_seq:
            return
        self._course_kind_map = mapping or {}
        self._apply_course_kind_map()
        if self._current_menu is None:
            self._select_initial_category()
        elif self._current_menu.course_kind:
            # 类别已选中且补全后 course_kind 非空 → 重新加载，使非空值流入 Course
            self._load_courses(reset=True)

    def _apply_course_kind_map(self):
        """用缓存映射补全菜单的 course_kind：仅补空值，已有值不覆盖。"""
        mapping = self._course_kind_map or {}
        if not mapping:
            return
        for m in self._menus:
            if not m.course_kind:
                m.course_kind = mapping.get(m.menu_code) or ""

    def _select_initial_category(self):
        """选中持久化/默认的类别（触发 currentItemChanged → 加载课程）。"""
        saved_batch, saved_category = self._saved_selection()
        target = ""
        if saved_batch == self._current_batch_code and saved_category:
            target = saved_category
        if target not in [m.menu_code for m in self._menus]:
            target = self._menus[0].menu_code if self._menus else ""
        if target:
            # clear() 后 currentRouteKey 为 None，setCurrentItem 必然触发
            # currentItemChanged → _on_category_changed → 加载课程
            self.categoryPivot.setCurrentItem(target)

    def _menu_by_code(self, code):
        for m in self._menus:
            if m.menu_code == code:
                return m
        return None

    def _on_category_changed(self, route_key):
        menu = self._menu_by_code(route_key)
        if menu is None:
            return
        self._current_menu = menu
        self._persist_selection()
        self.searchEdit.clear()
        self._query_content = ""
        self._load_courses(reset=True)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def _on_search(self, keyword):
        self._query_content = core_search.build_query_content(keyword)
        self._load_courses(reset=True)

    # ------------------------------------------------------------------
    # 校区筛选（纯客户端过滤，不发请求）
    # ------------------------------------------------------------------

    def _current_campus(self) -> str:
        """当前校区筛选值；空串表示「全部校区」（不过滤）。"""
        return self._campus_filter or ""

    def _filtered_courses(self) -> list:
        """按当前校区筛选已加载的课程（不发请求，只在内存里过滤）。"""
        campus = self._current_campus()
        if not campus:
            return list(self._loaded_courses)
        return [
            c for c in self._loaded_courses
            if (c.campus_name or "").strip() == campus
        ]

    def _rebuild_campus_options(self):
        """按已加载课程重建校区选项（去重 + 「全部校区」）。

        切换类别/搜索时**保留**当前筛选值：即使新课程列表里没有该校区，也把它
        留在选项里，这样「筛选后为空」能正确显示「当前筛选条件下暂无课程」，
        而不是悄悄把用户的筛选条件吞掉。
        """
        names = []
        for course in self._loaded_courses:
            name = (course.campus_name or "").strip()
            if name and name not in names:
                names.append(name)
        current = self._current_campus()
        if current and current not in names:
            names.insert(0, current)
        # 重建期间屏蔽信号，避免 setCurrentIndex 触发一次多余的重新渲染
        self.campusCombo.blockSignals(True)
        self.campusCombo.clear()
        self.campusCombo.addItem(ALL_CAMPUSES, userData="")
        for name in names:
            self.campusCombo.addItem(name, userData=name)
        index = self.campusCombo.findData(current)
        self.campusCombo.setCurrentIndex(index if index >= 0 else 0)
        self.campusCombo.blockSignals(False)

    def _on_campus_changed(self, index):
        """切换校区：只按新条件重新渲染已加载的卡片，不发任何请求。"""
        self._campus_filter = str(self.campusCombo.itemData(index) or "")
        self._render_courses()

    # ------------------------------------------------------------------
    # 课程加载（线程池 + 序号防乱序）
    # ------------------------------------------------------------------

    def _load_courses(self, reset=True):
        if self._current_menu is None:
            return
        if reset:
            self._page_number = 0
            self._is_last = False
            self._loaded_courses = []
        self._load_seq += 1
        seq = self._load_seq
        self._show_loading(reset)
        self._thread_pool().submit(
            self._fetch_courses_worker,
            seq,
            self._current_batch_code,
            self._current_menu.menu_code,
            self._current_menu.course_kind,
            self._query_content,
            self._page_number,
            self._page_size,
        )

    def _fetch_courses_worker(
            self,
            seq,
            batch_code,
            teaching_class_type,
            course_kind,
            query_content,
            page_number,
            page_size,
    ):
        """工作线程：查询课程，经信号回主线程。严禁在此操作 QWidget。"""
        try:
            rows, is_last = self.client.fetch_courses(
                batch_code=batch_code,
                teaching_class_type=teaching_class_type,
                course_kind=course_kind,
                query_content=query_content,
                page_number=page_number,
                page_size=page_size,
            )
            self._coursesReady.emit((seq, rows, is_last, page_number, query_content))
        except Exception as e:
            logging.error(f"获取课程失败：{traceback.format_exc()}")
            self._loadError.emit((seq, f"获取课程失败：{e}"))

    def _on_courses_ready(self, payload):
        seq, rows, is_last, page_number, query_content = payload
        if seq != self._load_seq:
            return  # 过期响应，丢弃
        self._hide_loading()
        # 会话失效：自动重新登录一次并重试当前加载
        if self.client.is_session_expired(rows):
            self._handle_session_expired()
            return
        self._relogin_attempted = False

        courses = self._parse_course_rows(rows)
        courses = core_search.rank_courses(courses, query_content)
        if page_number == 0:
            self._loaded_courses = []
        known = {c.teaching_class_id for c in self._loaded_courses}
        for course in courses:
            if course.teaching_class_id in known:
                continue  # 翻页时服务端可能重复返回同一教学班
            known.add(course.teaching_class_id)
            self._loaded_courses.append(course)
        self._is_last = is_last
        self._page_number = page_number + 1
        self._rebuild_campus_options()
        self._render_courses()

    def _render_courses(self):
        """按「已加载课程 + 当前校区筛选」重建卡片列表（不发请求）。

        卡片一律重建而不是增量追加：校区筛选需要在同一份数据上反复切换，增量
        追加无法表达「移除不符合条件的卡片」。收藏状态仍来自本地收藏集。
        """
        self.cardGroup.clearCard()
        tactic = self._current_batch_tactic_name()
        for course in self._filtered_courses():
            card = CourseCard(self)
            card.client = self.client
            card.set_course(
                course,
                tactic,
                self._is_favorited(course.teaching_class_id),
            )
            card.favoriteToggled.connect(self._on_favorite_toggled)
            card.enrollRequested.connect(self.enrollRequested)
            card.grabRequested.connect(self.grabRequested)
            card.dropRequested.connect(self.dropRequested)
            self.cardGroup.addCard(card, wid=course.teaching_class_id)
        self._update_empty_and_loadmore()

    def _parse_course_rows(self, rows):
        """按类别路由解析课程行：ZY → programCourse.do 父子结构，其余 → 扁平行。"""
        menu = self._current_menu
        batch_code = self._current_batch_code
        courses = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if menu.menu_code == "ZY":
                for tc in row.get("tcList") or []:
                    if isinstance(tc, dict):
                        courses.append(
                            models.from_program_course(
                                row, tc, menu.course_kind, menu.menu_code, batch_code
                            )
                        )
            else:
                courses.append(
                    models.from_public_course(
                        row, menu.course_kind, menu.menu_code, batch_code
                    )
                )
        return courses

    def _on_load_error(self, payload):
        seq, msg = payload
        if seq is not None and seq != self._load_seq:
            return
        # 批次加载失败（seq 为 None）也要恢复刷新按钮，否则按钮永久禁用
        self.refreshBatchButton.setEnabled(True)
        self._hide_loading()
        self.emptyLabel.setText(msg)
        self.emptyLabel.setVisible(True)
        self.loadMoreButton.hide()

    # ------------------------------------------------------------------
    # 会话失效 → 重新登录一次 → 重试
    # ------------------------------------------------------------------

    def _handle_session_expired(self):
        if self._relogin_attempted:
            # 已重登过一次仍失效 → 请求切回登录层（登录卡在 MainPage）
            self._relogin_attempted = False
            self.loginRequired.emit()
            return
        self._relogin_attempted = True
        self._thread_pool().submit(self._relogin_worker)

    def _relogin_worker(self):
        """工作线程：用本地保存的账号重新登录。严禁在此操作 QWidget。"""
        try:
            user, pwd = state.load_account(self._setting) if self._setting else ("", "")
            if not user or not pwd:
                self._reloginReady.emit(False, "无保存的账号，请重新登录")
                return
            self.client.init_session()
            ok, msg = self.client.login(user, pwd)
            if ok:
                session = self.client.export_session()
                if self._setting is not None:
                    state.save_session(
                        self._setting,
                        session.get("cookies", {}),
                        session.get("token", ""),
                    )
            self._reloginReady.emit(ok, msg)
        except Exception as e:
            logging.error(f"重新登录失败：{traceback.format_exc()}")
            self._reloginReady.emit(False, f"重新登录失败：{e}")

    def _on_relogin_ready(self, ok, msg):
        if ok:
            # 重试当前加载（_relogin_attempted 保持 True，防止再次失效时死循环）
            self._load_courses(reset=False)
        else:
            self._relogin_attempted = False
            self.loginRequired.emit()

    # ------------------------------------------------------------------
    # 分页 / 加载态 / 空态
    # ------------------------------------------------------------------

    def _on_load_more(self):
        if self._is_last:
            return
        self._load_courses(reset=False)

    def reload_current_category(self):
        """重新查询当前类别（退选成功后刷新列表，使徽标与按钮回到未报名态）。

        当前没有选中类别时（未登录 / 尚未加载）直接返回，不发起请求。
        """
        if self._current_menu is None:
            return
        self._load_courses(reset=True)

    def refresh_courses(self):
        """「刷新课程」：只重新查询当前类别的第 1 页。

        与「刷新批次」的区别（用户明确要求拆开）：
        - **不**重新拉取批次列表（不发 ``fetch_batches``）；
        - **不**重建课程类别（``categoryPivot`` 保持原样，当前类别不变）；
        - **不**动本地收藏（收藏状态仍来自 ``state``，不重新计算）；
        - 只把当前类别的查询重置到第 1 页重发一次 ``fetch_courses``。
        """
        if self._current_menu is None:
            return
        self.refreshCoursesButton.setEnabled(False)
        self._load_courses(reset=True)

    def _show_loading(self, reset):
        self.emptyLabel.hide()
        self.loadMoreButton.setEnabled(False)
        self.loadMoreButton.hide()
        if reset:
            # 重置加载：清空旧卡片并显示加载卡；翻页加载保留已有卡片
            self.cardGroup.clearCard()
            # 加载指示器**水平居中**：CardGroup 的 addCard 用 AlignTop 插入，卡片
            # 会靠左；这里套一层左右 stretch 的容器，让加载卡居中显示
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
        # 刷新课程期间禁用了自身，加载结束（成功或失败）一律恢复
        self.refreshCoursesButton.setEnabled(True)

    def _update_empty_and_loadmore(self):
        count = self.cardGroup.count()
        total = len(self._loaded_courses)
        if count == 0 and total > 0:
            # 有课程但被校区筛选过滤光了 → 明确告诉用户是筛选导致的
            self.emptyLabel.setText("当前筛选条件下暂无课程")
        else:
            self.emptyLabel.setText("暂无课程")
        self.emptyLabel.setVisible(count == 0)
        self.loadMoreButton.setEnabled(True)
        # 翻页按钮只看「还有没有下一页」+「有没有加载到课程」，不受校区筛选影响
        self.loadMoreButton.setVisible(not self._is_last and total > 0)

    # ------------------------------------------------------------------
    # 收藏（本地）
    # ------------------------------------------------------------------

    def _is_favorited(self, teaching_class_id) -> bool:
        if self._setting is None:
            return False
        return state.is_favorited(self._setting, teaching_class_id)

    def _on_favorite_toggled(self, teaching_class_id, favorited):
        """收藏切换：写本地收藏集 + 更新卡片徽标。严禁调用服务端收藏接口。"""
        if self._setting is None:
            return
        card = self.cardGroup.getCard(teaching_class_id)
        if favorited:
            if card is not None and card.course is not None:
                # 连同当前批次的 tactic_name 一起写入：收藏页没有批次上下文，
                # 只能靠记录里的策略名算选中概率（缺失时按空串兜底）
                state.add_favorite(
                    self._setting,
                    self._course_to_dict(card.course, card.tactic_name),
                )
        else:
            state.remove_favorite(self._setting, teaching_class_id)
        if card is not None:
            card.set_favorited(favorited)
        self.favoriteToggled.emit(teaching_class_id, favorited)

    def refresh_favorite_badges(self):
        """按本地收藏集重刷**所有**课程卡片的收藏徽标与按钮文案。

        卡片建卡时只读一次收藏状态，之后两处会失同步（用户实测反馈）：
        1. 在选课页点收藏后，其他同课程卡片不刷新；
        2. 在设置里「清空收藏」后，选课页卡片徽标仍显示已收藏。
        本方法遍历当前所有 ``CourseCard``，按 ``state.is_favorited`` 重新
        ``set_favorited()``，供装配层在收藏变化后调用。
        """
        for card in self.cardGroup.getCards():
            if getattr(card, "course", None) is None:
                continue  # 跳过 LoadingCard 等非课程卡片
            card.set_favorited(self._is_favorited(card.course.teaching_class_id))

    @staticmethod
    def _course_to_dict(course, tactic_name="") -> dict:
        """课程 → 收藏记录 dict。

        额外写入 ``tactic_name``（**不是** ``Course`` 字段）：收藏页渲染时没有
        批次上下文，只能从记录里取策略名算选中概率；老记录缺该键时按空串处理。
        """
        from dataclasses import asdict

        record = asdict(course)
        record["tactic_name"] = tactic_name or ""
        return record

    # ------------------------------------------------------------------
    # 持久化 / 工具
    # ------------------------------------------------------------------

    def _saved_selection(self):
        if self._setting is None:
            return "", ""
        return state.load_selection(self._setting)

    def _persist_selection(self):
        if self._setting is None:
            return
        if self._current_menu is None:
            # 批次已切换但类别尚未选中（启动加载流程）：只更新批次，**保留**
            # 已持久化的类别，否则「恢复上次类别」会在这里先被空串抹掉
            # （实测缺陷：_select_initial_category 读到的永远是空 → 每次启动
            # 都回退到第一个类别）。批次真的换了则类别随之作废。
            saved_batch, saved_category = self._saved_selection()
            keep = saved_category if saved_batch == self._current_batch_code else ""
            state.save_selection(self._setting, self._current_batch_code, keep)
            return
        state.save_selection(
            self._setting,
            self._current_batch_code,
            self._current_menu.menu_code,
        )

    def _current_batch_tactic_name(self) -> str:
        """当前批次的策略名（如「先选先得」「可选可退」）；未匹配到时返回空串。

        卡片只在 ``_current_menu`` 非空时才渲染，而它只在批次列表加载完成后才被
        赋值，因此正常流程下批次列表一定已加载。返回空串的两种情况：
        1. 批次列表尚未加载（时序异常）——留 ``logging.debug`` 便于排查；
        2. 当前批次不在列表里（批次被刷新掉）。
        两种情况都由 :func:`~njuxk.api.models.selection_probability` 的兜底分支
        处理（未满即显示 100%），卡片不会显示横线。
        """
        for b in self._batches:
            if b.code == self._current_batch_code:
                return b.tactic_name
        if not self._batches:
            logging.debug(
                "取当前批次策略名为空：批次列表尚未加载（batch_code=%r）",
                self._current_batch_code,
            )
        else:
            logging.debug(
                "取当前批次策略名为空：批次 %r 不在已加载的 %d 个批次中",
                self._current_batch_code,
                len(self._batches),
            )
        return ""
