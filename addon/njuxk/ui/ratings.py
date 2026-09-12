"""课程红黑榜评价详情弹窗（用户需求：卡片徽标 + 详情弹窗看评价）。

数据来自 ``core/ratings.RatingsStore.lookup`` 的匹配结果（NJU-Hub 公共评价库），
本弹窗只负责展示，**不做网络请求**：

- 标题：课程名 + 「红黑榜评分 N 分（M 条评价）」（评分仅信息参考，不强行分榜）；
- 正文：按评价来源（年份 / 平台）分组，逐条显示评价原文，长文本自动换行、
  可鼠标拖选 + 右键复制（``ui/text_select.make_selectable``）；
- 结构照 ``ui/settings.py`` 的 MessageBoxBase 范式：内容放进 ``SmoothScrollArea``
  + ``enableTransparentBackground``，**禁用 TextEdit 家族**。
"""
from __future__ import annotations

import html

from qtpy.QtWidgets import QFrame, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    MessageBoxBase,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)

from .text_select import make_selectable

#: 红黑榜评分配色（高/中/低 = 绿/黄/红；明暗主题各一档，保证可读）
RATING_COLOR_RED = ("#c42b1c", "#ff99a4")
RATING_COLOR_YELLOW = ("#9a5b00", "#ffb02e")
RATING_COLOR_GREEN = ("#107c10", "#6ccb5f")

#: 评分分档阈值：>= 高档 → 绿；>= 中档 → 黄；其余 → 红
RATING_HIGH_THRESHOLD = 80
RATING_MID_THRESHOLD = 60


def rating_color(score):
    """按评分高低返回 (浅色, 深色) 颜色对（高分绿 / 中分黄 / 低分红）。

    非法/缺失分数按中档（黄）处理。
    """
    try:
        value = int(score)
    except (TypeError, ValueError):
        return RATING_COLOR_YELLOW
    if value >= RATING_HIGH_THRESHOLD:
        return RATING_COLOR_GREEN
    if value >= RATING_MID_THRESHOLD:
        return RATING_COLOR_YELLOW
    return RATING_COLOR_RED


#: 评价来源展示名（照 NJU-Hub xk_badges.js 的 SRC_LABELS；未收录的用来源原文）
_SOURCE_LABELS = {
    "nju_course_ratings": "鼓励你学哪门课",
    "2020": "2020 红黑榜",
    "2021": "2021 南小宝",
    "2022": "2022 红黑榜",
    "2023": "2023 红黑榜",
    "2024冬": "2024冬 红黑榜",
    "2024春": "2024春 红黑榜",
    "2025春": "2025春 红黑榜",
}


def _source_label(source: str) -> str:
    source = (source or "").strip()
    if not source:
        return "评价"
    return _SOURCE_LABELS.get(source, source)


def _group_reviews(reviews) -> list:
    """按来源分组（保持首次出现顺序）：``[(来源, [评价文本, ...]), ...]``。"""
    order: list = []
    groups: dict = {}
    for item in reviews or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        source = str(item.get("source") or "")
        if source not in groups:
            groups[source] = []
            order.append(source)
        groups[source].append(text)
    return [(source, groups[source]) for source in order]


class RatingsDialog(MessageBoxBase):
    """红黑榜评价详情弹窗（只读、可滚动、文本可选中复制）。"""

    WIDTH = 640

    def __init__(self, course, info, parent=None):
        super().__init__(parent=parent)
        self.course = course
        self.info = info if isinstance(info, dict) else {}
        self._build_ui()

    def _build_ui(self):
        self.widget.setFixedWidth(self.WIDTH)
        self.yesButton.setText("关闭")
        self.hideCancelButton()

        name = getattr(self.course, "course_name", "") or "课程"
        teacher = getattr(self.course, "teacher_name", "") or ""
        score = self.info.get("score", "")
        count = self.info.get("count", 0)

        title = name if not teacher else f"{name}（{teacher}）"
        self.titleLabel = SubtitleLabel(title, self.widget)
        make_selectable(self.titleLabel)

        self.scoreLabel = StrongBodyLabel(
            f"红黑榜评分：{score} 分（{count} 条评价）", self.widget
        )
        self.scoreLabel.setTextColor(*rating_color(score))
        make_selectable(self.scoreLabel)

        self.hintLabel = BodyLabel(
            "评价来自红黑榜数据库，评分仅作信息参考", self.widget
        )
        self.hintLabel.setTextColor("#808080", "#a0a0a0")
        make_selectable(self.hintLabel)

        self.scrollArea = SmoothScrollArea(self.widget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setFrameShape(QFrame.NoFrame)
        inner = QWidget(self.scrollArea)
        self.scrollArea.setWidget(inner)
        self.contentLayout = QVBoxLayout(inner)
        self.contentLayout.setContentsMargins(24, 4, 24, 8)
        self.contentLayout.setSpacing(8)
        self.scrollArea.enableTransparentBackground()

        self.contentLayout.addWidget(self.titleLabel)
        self.contentLayout.addWidget(self.scoreLabel)
        self.contentLayout.addWidget(self.hintLabel)

        groups = _group_reviews(self.info.get("reviews"))
        if not groups:
            empty = BodyLabel("暂无评价原文。", self.widget)
            empty.setTextColor("#808080", "#a0a0a0")
            self.contentLayout.addWidget(empty)
        for source, texts in groups:
            self._add_source(source, texts)

        self.contentLayout.addStretch(1)
        self.viewLayout.addWidget(self.scrollArea, 1)

    def _add_source(self, source: str, texts):
        header = BodyLabel(f"{_source_label(source)}（{len(texts)} 条）", self.widget)
        header.setTextColor("#606060", "#d2d2d2")
        self.contentLayout.addWidget(header)
        for text in texts:
            label = make_selectable(BodyLabel(self.widget))
            label.setWordWrap(True)
            # 原文转义后展示，避免评价里的 < > 被当 HTML 解析
            label.setText(f"· {html.escape(text)}")
            self.contentLayout.addWidget(label)
