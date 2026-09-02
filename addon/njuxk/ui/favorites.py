"""收藏页（计划 todo 13）：纯本地收藏列表 + 移除 + 刷新占用 + 一键开始全部抢课。

收藏完全本地实现（``core.state`` 持久化，以 ``teaching_class_id`` 为唯一键），
**严禁**调用任何服务端收藏接口（favorite.do / queryfavorite.do），也**严禁**读取
课程响应里的 ``favorite`` 字段。

刷新占用：按 ``(batch_code, teaching_class_type)`` 分组，对每组 ``course_number``
集合以 ``query_content=<course_number>`` 调 ``client.fetch_courses`` 反查，再按
**``teaching_class_id`` 精确匹配**（不是按 course_number，避免同课程号多教学班错配）
更新 ``class_capacity / number_of_selected / number_of_first_volunteer / is_full /
is_choose``；刷新失败保留旧快照并提示「刷新失败，显示本地快照」。刷新**只覆盖占用
字段**，``tactic_name`` 等加入收藏时写入的字段原样保留。

收藏记录字段：除 ``Course`` 的字段外还带 ``tactic_name``（加入收藏时的当前批次
策略名）。收藏页没有批次上下文，渲染卡片时用它算选中概率；老记录缺该键时按空串
处理，由 :func:`~njuxk.api.models.selection_probability` 的兜底分支显示 100%。

一键开始全部抢课：跳过 ``is_choose=="1"``（已在课表）；``is_full=="1"``（已满）
默认也跳过（``CheckBox``「跳过已满课程」控制，默认勾选）；对剩余每条发出
``grabRequested`` 并批量发出 ``grabAllRequested``（由外部接入任务页）。

线程约定（对齐 ``ui/course.py`` / ``ui/login.py``）：
- 所有网络请求走 ``program.THREAD_POOL``（宿主线程池），无宿主时用本地兜底池；
- 结果经信号回主线程，**严禁**工作线程直接操作 QWidget。
"""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
)

import zbWidgetLib as zbw

from ..api import models
from ..api.client import XkClient
from ..core import state
from .cards import CourseCard
from .layout import SPACING, apply_page_margins, apply_tool_row
from .login import _host_program, _host_setting
from .text_select import make_selectable


class FavoritesPage(QWidget):
    """收藏页：本地收藏列表 + 移除 + 刷新占用 + 一键开始全部抢课。

    布局（用户要求「收藏和任务界面的顶栏也要固定」，对齐 ``ui/course.py``）：
    本页**不再**继承 ``zbw.BasicTab``（BasicTab 整页就是一个纵向滚动区，工具行
    会跟着收藏卡片一起滚走），改为普通 ``QWidget`` 页根布局 + 内部唯一一个纵向
    ``SmoothScrollArea``（``favScroll``）—— 工具行（刷新占用 / 一键开始全部抢课 /
    跳过已满课程 / 清空收藏）固定在页根，只有收藏卡片列表（cardGroup /
    emptyLabel）在 ``favScroll`` 里滚动。``BasicTabPage.addPage`` 只要求 widget
    本身，不要求 BasicTab 类型，页签行为不变。

    信号：
        grabRequested(str)      —— 单个抢课请求（teaching_class_id）
        grabAllRequested(list)  —— 批量抢课请求（teaching_class_id 列表）
        dropRequested(str)      —— 退选请求（teaching_class_id），转发自卡片
            （收藏页卡片与选课页共用 ``CourseCard``，已报名时同样显示「退选」）
        favoritesChanged()      —— 收藏集合结构变化（移除/清空）
    """

    # InfoBar / 对话框挂载标记：``ui/cards.py`` 的 ``_dialog_parent`` 沿 parent
    # 链上溯找插件页面（原来只认 ``zbw.BasicTab``），本页改基类后靠此标记继续
    # 被识别为插件页面 —— 弹窗遮罩仍覆盖整个收藏页，绝不提升到宿主主窗口
    _info_parent_flag = True

    grabRequested = Signal(str)
    grabAllRequested = Signal(list)
    dropRequested = Signal(str)
    favoritesChanged = Signal()

    # 内部线程信号（工作线程 emit，主线程槽接收）
    _refreshReady = Signal(object)
    _refreshError = Signal(object)

    def __init__(self, parent=None, client=None, setting=None, program=None):
        super().__init__(parent)
        self.client = client if client is not None else XkClient()
        self._setting = setting
        self._program = program
        # 插件主页面（MainPage）引用：Loading 遮罩挂它（覆盖整个插件页区域，
        # 不覆盖宿主主窗口）；MainPage 创建后经 set_main_page 注入
        self._main_page = None
        self._fallback_pool = None
        self._loading_box = None
        self._clear_confirm_btn = None
        self._clear_cancel_btn = None

        self._build_ui()
        self._connect_signals()
        self.refresh_list()

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
        # 页根布局：工具行 + 收藏列表滚动区（前者固定，后者滚动）。
        # 边距/间距统一取自 ui.layout（与选课页、任务页、设置对话框同一套常量）
        self.vBoxLayout = QVBoxLayout(self)
        apply_page_margins(self.vBoxLayout)

        # 顶部工具行：刷新占用 / 一键开始全部抢课 / 跳过已满课程 / 清空收藏
        # （固定在页根，不随列表滚动）
        toolRow = apply_tool_row(QHBoxLayout())
        self.refreshButton = PushButton(FIF.SYNC, "刷新占用", self)
        self.grabAllButton = PrimaryPushButton(FIF.PLAY, "一键开始全部抢课", self)
        self.skipFullCheck = CheckBox("跳过已满课程", self)
        self.skipFullCheck.setChecked(True)
        self.clearButton = PushButton(FIF.DELETE, "清空收藏", self)
        toolRow.addWidget(self.refreshButton)
        toolRow.addWidget(self.grabAllButton)
        toolRow.addWidget(self.skipFullCheck)
        toolRow.addStretch(1)
        toolRow.addWidget(self.clearButton)
        self.vBoxLayout.addLayout(toolRow)

        # ---- 列表区：唯一纵向滚动区（顶栏固定在滚动内容之外）----
        # 用户需求「收藏和任务界面的顶栏也要固定」：此前整页继承 zbw.BasicTab
        # （整页滚动），工具行跟着收藏卡片一起滚走。现在只有收藏列表在
        # favScroll 里滚动，范式对齐 ui/course.py / ui/settings.py
        # （SmoothScrollArea + widgetResizable + NoFrame + 透明背景）。
        self.favScroll = SmoothScrollArea(self)
        self.favScroll.setWidgetResizable(True)
        self.favScroll.setFrameShape(QFrame.NoFrame)
        self.favScroll.enableTransparentBackground()

        # 滚动内容容器：页面边距已由 apply_page_margins 提供，这里只留少量
        # 上下边距（顶部与固定工具行拉开一点、底部留呼吸），左右为 0
        inner = QWidget(self.favScroll)
        self.favScroll.setWidget(inner)
        # inner 必须显式透明：qfw enableTransparentBackground() 只给「当时已
        # setWidget 的内容 widget」设透明样式（本页在其之前调用 → inner 拿不到）；
        # 而宿主 FluentWindow 的 FLUENT_WINDOW qss 会层叠进页面，使 inner
        # autoFillBackground=True 并涂上系统窗口色（亮 #efefef，暗色主题下与
        # 页面背景形成大色块）。写法对齐旧版 zbw.BasicTab（BetterScrollArea.view）。
        inner.setStyleSheet("QWidget {background-color: rgba(0,0,0,0); border: none}")
        innerLayout = QVBoxLayout(inner)
        innerLayout.setContentsMargins(0, 4, 0, 8)
        innerLayout.setSpacing(SPACING)

        # 列表区：CardGroup 装 CourseCard（在滚动内容里，自然高度不占 stretch）
        self.cardGroup = zbw.CardGroup(inner, show_title=False, is_v=True)
        innerLayout.addWidget(self.cardGroup)

        self.emptyLabel = make_selectable(
            BodyLabel("暂无收藏，可在选课页点击收藏按钮添加", inner)
        )
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setTextColor("#606060", "#d2d2d2")
        innerLayout.addWidget(self.emptyLabel)

        # 收藏列表滚动区独占剩余纵向空间（stretch=1），工具行保持固有高度
        self.vBoxLayout.addWidget(self.favScroll, 1)

    def _connect_signals(self):
        self.refreshButton.clicked.connect(self._on_refresh_clicked)
        self.grabAllButton.clicked.connect(self._on_grab_all_clicked)
        self.clearButton.clicked.connect(self.clear_all)
        self._refreshReady.connect(self._on_refresh_ready)
        self._refreshError.connect(self._on_refresh_error)

    # ------------------------------------------------------------------
    # 列表重建
    # ------------------------------------------------------------------

    def refresh_list(self):
        """从 state 重建卡片列表（收藏徽标恒为已收藏）。"""
        self.cardGroup.clearCard()
        setting = self.setting
        favorites = state.list_favorites(setting) if setting is not None else []
        for fav in favorites:
            if not isinstance(fav, dict):
                continue
            tid = fav.get("teaching_class_id")
            if not tid:
                continue
            card = CourseCard(self)
            card.client = self.client
            # tactic_name 来自收藏记录（加入收藏时由选课页写入）；老记录缺该键
            # 时按空串处理，由 selection_probability 的兜底分支显示 100%
            card.set_course(
                self._record_to_course(fav),
                tactic_name=str(fav.get("tactic_name") or ""),
                favorited=True,
            )
            card.favoriteToggled.connect(self._on_card_favorite_toggled)
            card.grabRequested.connect(self.grabRequested)
            card.dropRequested.connect(self.dropRequested)
            self.cardGroup.addCard(card, wid=tid)
        self._update_empty()

    def _update_empty(self):
        self.emptyLabel.setVisible(self.cardGroup.count() == 0)

    @staticmethod
    def _record_to_course(record):
        """把收藏 dict 安全转成 Course（只取 Course 有的字段，忽略多余键）。"""
        valid = {f.name for f in fields(models.Course)}
        return models.Course(**{
            k: ("" if v is None else str(v))
            for k, v in record.items()
            if k in valid
        })

    # ------------------------------------------------------------------
    # 移除 / 清空
    # ------------------------------------------------------------------

    def set_main_page(self, page):
        """注入插件主页面引用（Loading 遮罩的挂载点，装配层回填）。"""
        self._main_page = page

    def remove_favorite(self, teaching_class_id):
        """按 teaching_class_id 移除收藏并同步移除卡片。"""
        setting = self.setting
        if setting is None:
            return
        state.remove_favorite(setting, teaching_class_id)
        self.cardGroup.removeCard(teaching_class_id)
        self._update_empty()
        logging.debug(f"移除收藏：{teaching_class_id}")
        self.favoritesChanged.emit()

    def _on_card_favorite_toggled(self, teaching_class_id, favorited):
        """收藏页卡片上的收藏按钮：取消收藏即移除。"""
        if not favorited:
            self.remove_favorite(teaching_class_id)

    def clear_all(self):
        """清空全部收藏（二次确认，参考 pySeatShuffle InfoBar 交互范式）。"""
        infoBar = InfoBar(
            InfoBarIcon.WARNING,
            "清空收藏",
            "确定清空全部收藏？（该操作无法撤销！）",
            isClosable=False,
            duration=-1,
            # 提示挂收藏页自身，随页面显示（不挂宿主主窗口）
            parent=self,
        )

        def confirm():
            setting = self.setting
            if setting is not None:
                state.clear_favorites(setting)
            self.refresh_list()
            self.favoritesChanged.emit()
            infoBar.close()
            logging.info("已清空全部收藏")
            InfoBar.info(
                title="成功！",
                content="已清空全部收藏！",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

        def cancel():
            infoBar.close()

        self._clear_confirm_btn = PushButton(text="确定")
        self._clear_confirm_btn.clicked.connect(confirm)
        infoBar.addWidget(self._clear_confirm_btn)

        self._clear_cancel_btn = PushButton(text="取消")
        self._clear_cancel_btn.clicked.connect(cancel)
        infoBar.addWidget(self._clear_cancel_btn)
        infoBar.show()

    # ------------------------------------------------------------------
    # 刷新占用（线程池）
    # ------------------------------------------------------------------

    def _on_refresh_clicked(self):
        self._show_loading()
        self._thread_pool().submit(self._refresh_worker)

    def _refresh_worker(self):
        """工作线程：按 (batch_code, teaching_class_type) 分组反查占用。

        严禁在此操作任何 QWidget；结果经信号回主线程。
        """
        try:
            setting = self.setting
            if setting is None:
                self._refreshError.emit("无本地设置，无法刷新")
                return
            favorites = state.list_favorites(setting)
            groups = {}
            for fav in favorites:
                if not isinstance(fav, dict):
                    continue
                key = (fav.get("batch_code", ""), fav.get("teaching_class_type", ""))
                groups.setdefault(key, []).append(fav)

            patches = []
            for (batch_code, teaching_class_type), favs in groups.items():
                course_kind = favs[0].get("course_kind", "")
                course_numbers = sorted({
                    f.get("course_number", "")
                    for f in favs
                    if f.get("course_number")
                })
                for course_number in course_numbers:
                    rows, _is_last = self.client.fetch_courses(
                        batch_code=batch_code,
                        teaching_class_type=teaching_class_type,
                        course_kind=course_kind,
                        query_content=course_number,
                        page_number=0,
                        page_size=50,
                    )
                    for tid, patch in self._iter_refresh_patches(
                            rows, course_kind, teaching_class_type, batch_code
                    ):
                        patches.append((tid, patch))
            self._refreshReady.emit(patches)
        except Exception as e:
            logging.error(f"刷新收藏占用失败：{traceback.format_exc()}")
            self._refreshError.emit(f"刷新失败：{e}")

    def _iter_refresh_patches(self, rows, course_kind, teaching_class_type, batch_code):
        """从课程查询结果提取 (teaching_class_id, patch) 对（ZY 展开 tcList）。"""
        for row in rows:
            if not isinstance(row, dict):
                continue
            if teaching_class_type == "ZY":
                for tc in row.get("tcList") or []:
                    if isinstance(tc, dict):
                        course = models.from_program_course(
                            row, tc, course_kind, teaching_class_type, batch_code
                        )
                        yield course.teaching_class_id, self._occupancy_patch(course)
            else:
                course = models.from_public_course(
                    row, course_kind, teaching_class_type, batch_code
                )
                yield course.teaching_class_id, self._occupancy_patch(course)

    @staticmethod
    def _occupancy_patch(course):
        """占用相关字段的 patch（供 state.update_favorite 合并）。

        只含占用字段：``update_favorite`` 是合并语义，``tactic_name`` 等其余键
        不在 patch 里就不会被覆盖掉。
        """
        return {
            "class_capacity": course.class_capacity,
            "number_of_selected": course.number_of_selected,
            "number_of_first_volunteer": course.number_of_first_volunteer,
            "is_full": course.is_full,
            "is_choose": course.is_choose,
        }

    def _on_refresh_ready(self, patches):
        self._close_loading()
        setting = self.setting
        if setting is not None:
            for tid, patch in patches:
                state.update_favorite(setting, tid, patch)
        self.refresh_list()
        InfoBar.success(
            title="刷新完成",
            content="已更新收藏占用信息",
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _on_refresh_error(self, msg):
        self._close_loading()
        logging.warning(f"刷新收藏占用失败：{msg}")
        InfoBar.warning(
            title="刷新失败",
            content="刷新失败，显示本地快照",
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=5000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # ------------------------------------------------------------------
    # 一键开始全部抢课
    # ------------------------------------------------------------------

    def _on_grab_all_clicked(self):
        setting = self.setting
        if setting is None:
            return
        favorites = state.list_favorites(setting)
        skip_full = self.skipFullCheck.isChecked()
        to_grab = []
        skipped = 0
        for fav in favorites:
            if not isinstance(fav, dict):
                continue
            tid = fav.get("teaching_class_id")
            if not tid:
                continue
            if str(fav.get("is_choose", "")) == "1":
                skipped += 1
                continue
            if skip_full and str(fav.get("is_full", "")) == "1":
                skipped += 1
                continue
            to_grab.append(tid)
        for tid in to_grab:
            self.grabRequested.emit(tid)
        if to_grab:
            self.grabAllRequested.emit(to_grab)
        InfoBar.info(
            title="一键抢课",
            content=f"已加入 {len(to_grab)} 个抢课任务，跳过 {skipped} 个",
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # ------------------------------------------------------------------
    # 加载态
    # ------------------------------------------------------------------

    def _show_loading(self):
        # 遮罩挂 MainPage（插件页顶层容器）：覆盖整个插件页区域，不覆盖宿主
        # 主窗口；MainPage 未注入时（独立构造/测试）退回收藏页自身
        parent = self._main_page if self._main_page is not None else self
        self._loading_box = zbw.LoadingMessageBox(parent)
        self._loading_box.setText("正在刷新占用…")
        self._loading_box.show()

    def _close_loading(self):
        if self._loading_box is not None:
            try:
                self._loading_box.close()
            except Exception as e:
                logging.warning(f"关闭 LoadingMessageBox 失败：{e}")
            self._loading_box = None
