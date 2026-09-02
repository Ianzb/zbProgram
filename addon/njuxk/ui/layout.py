"""共享布局常量（边距 / 间距 / 宽度约束），供各页面统一引用。

背景（用户实测反馈「各页面边距不一致」「一个组件一行，窗口被撑得太宽」）：
此前选课页、收藏页、任务页、设置对话框各自硬编码 ``16 / 12 / 10 / 8`` 等数字，
改一处忘一处，导致外边距与控件间距逐页漂移。本模块把这些数字收敛成**单一数据源**，
四个页面一律从这里取，改一次全局生效。

约定（与 Qt 的 ``setContentsMargins(left, top, right, bottom)`` 顺序一致）：
- ``MARGIN`` / ``MARGIN_TOP`` —— 页面级外边距（顶部略小，视觉上更贴页签）；
- ``SPACING`` —— 页面内相邻控件（工具行 / 列表 / 空态）的间距；
- ``CARD_MARGIN`` / ``CARD_SPACING`` —— 卡片内边距与卡片内控件间距；
- ``TOOL_SPACING`` —— 工具行内相邻控件的间距（比 ``SPACING`` 紧，同一行要挤得下）。

宽度约束（需求「不要一个组件一行」）：
- ``BATCH_COMBO_MIN_WIDTH`` / ``BATCH_COMBO_MAX_WIDTH`` —— 选课活动下拉**限宽**，
  避免被长批次名（如「【老生】2026年秋季课程补选」）撑满整行；
- ``CATEGORY_MIN_WIDTH`` —— 课程类别 ``SegmentedWidget`` 的**强制最小宽度**。
  类别数量不定（夹具 6 个，真实批次可达 10+），自然宽度会远超窗口宽度；这里给它
  一个最小宽度并放进**横向滚动容器**（见 ``ui/course.py``），超出时横向滚动而不是
  把窗口撑宽。

用法：::

    from .layout import MARGIN, MARGIN_TOP, SPACING, apply_page_margins

    apply_page_margins(self.vBoxLayout)   # 等价于下面两行
    # self.vBoxLayout.setContentsMargins(MARGIN, MARGIN_TOP, MARGIN, MARGIN)
    # self.vBoxLayout.setSpacing(SPACING)
"""
from __future__ import annotations

from qtpy.QtWidgets import QLayout

# ==================== 页面级 ====================

#: 页面外边距（左 / 右 / 下）
MARGIN = 16
#: 页面上边距（比左右略小，视觉上更贴页签）
MARGIN_TOP = 12
#: 页面内相邻控件的间距
SPACING = 10

# ==================== 卡片级 ====================

#: 卡片内边距（左 / 上 / 右 / 下）
CARD_MARGIN = 16
#: 卡片内相邻控件的间距，同时用作卡片之间的间距
CARD_SPACING = 8

# ==================== 工具行 ====================

#: 工具行内相邻控件的间距（同一行要挤得下，比 SPACING 紧）
TOOL_SPACING = 8

# ==================== 宽度约束 ====================

#: 选课活动下拉最小宽度（低于此值长批次名会被截断得看不出是哪个批次）
BATCH_COMBO_MIN_WIDTH = 200
#: 选课活动下拉最大宽度（**关键**：不让它被长批次名撑满整行）
BATCH_COMBO_MAX_WIDTH = 260
#: 课程类别控件强制最小宽度（超出部分横向滚动，不撑宽窗口）
CATEGORY_MIN_WIDTH = 400
#: 搜索框最小宽度（工具行里唯一可伸缩的控件，缩到这个值就不再让位）
SEARCH_MIN_WIDTH = 180
#: 登录卡最大宽度（MainPage 登录层里居中显示，不随窗口拉宽）
LOGIN_CARD_MAX_WIDTH = 560


def apply_page_margins(layout: QLayout) -> QLayout:
    """把页面级外边距与间距套到 ``layout`` 上（返回同一对象便于链式调用）。"""
    layout.setContentsMargins(MARGIN, MARGIN_TOP, MARGIN, MARGIN)
    layout.setSpacing(SPACING)
    return layout


def apply_card_margins(layout: QLayout) -> QLayout:
    """把卡片级内边距与间距套到 ``layout`` 上（返回同一对象便于链式调用）。"""
    layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
    layout.setSpacing(CARD_SPACING)
    return layout


def apply_tool_row(layout: QLayout) -> QLayout:
    """把工具行的间距套到 ``layout`` 上（外边距为 0，由页面统一控制）。"""
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(TOOL_SPACING)
    return layout
