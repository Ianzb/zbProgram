"""文本可选中复制助手（用户需求：「各处的选课相关组件的文本要支持选中复制功能」）。

用法
----
- ``make_selectable(label)``：把**任意** QLabel 子类实例变成可选中复制（原地改造，
  返回同一实例，便于对既有 ``BodyLabel`` / ``StrongBodyLabel`` 逐个升级）；
- ``SelectableBodyLabel``：``BodyLabel`` 子类，构造即带该能力（新代码可直接用）。

行为
----
- ``Qt.TextSelectableByMouse``：鼠标拖选 + 双击选词 + 三击选段；
- 右键菜单（``CustomContextMenu`` + qfw ``RoundMenu``，与项目 qfw 风格一致）：
  - 有选中 → 「复制」（写系统剪贴板）；
  - 无选中 → 「全选」+「复制全文」兜底（不强制用户先拖选）。

实现说明：PySide6 的 QLabel **没有** ``textCursor()`` / ``selectAll()``，选中态
要用它自带的 ``hasSelectedText()`` / ``selectedText()`` / ``setSelection(start,
length)``（全选 = ``setSelection(0, len(text))``）。

菜单构建与菜单弹出拆成两个函数：``build_copy_menu`` 只创建菜单与动作（测试可直接
对动作 ``trigger()`` 断言剪贴板），``_show_copy_menu`` 才真正 ``exec``（模态，
测试不触碰）。
"""
from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QApplication

from qfluentwidgets import Action, BodyLabel, FluentIcon as FIF, RoundMenu

__all__ = ["SelectableBodyLabel", "make_selectable", "build_copy_menu"]


def _copy_selection(label):
    """把标签当前选中文本写入系统剪贴板。"""
    if label.hasSelectedText():
        QApplication.clipboard().setText(label.selectedText())


def _copy_full_text(label):
    """把标签全文写入系统剪贴板（无选中时的兜底动作）。"""
    QApplication.clipboard().setText(label.text())


def _select_all(label):
    """全选标签文本（QLabel 没有 selectAll()，用 setSelection 实现）。"""
    label.setSelection(0, len(label.text()))


def build_copy_menu(label, parent=None) -> RoundMenu:
    """按「有无选中」构建右键菜单（不弹出，测试可直接触发其中动作）。

    - 有选中 → 单项「复制」；
    - 无选中 → 「全选」+「复制全文」兜底。
    """
    menu = RoundMenu(parent=parent or label)
    if label.hasSelectedText():
        copy_act = Action(FIF.COPY, "复制")
        copy_act.triggered.connect(lambda: _copy_selection(label))
        menu.addAction(copy_act)
    else:
        select_all_act = Action("全选")
        select_all_act.triggered.connect(lambda: _select_all(label))
        menu.addAction(select_all_act)
        copy_all_act = Action(FIF.COPY, "复制全文")
        copy_all_act.triggered.connect(lambda: _copy_full_text(label))
        menu.addAction(copy_all_act)
    return menu


def _show_copy_menu(label, pos):
    """``customContextMenuRequested`` 槽：构建菜单并按请求位置弹出。"""
    menu = build_copy_menu(label)
    menu.exec(label.mapToGlobal(pos))


def _drop_native_context_menu(label):
    """断开 qfw ``FluentLabelBase`` 自带的右键菜单连接（若存在）。

    qfw 的 ``LabelContextMenu`` 右键「Copy」只复制构造瞬间的 ``selectedText``
    （无选中时复制空串，且没有「复制全文」兜底），不满足需求；不摘除的话右键会
    连弹两个菜单。普通 QLabel 没有该连接，静默跳过。
    """
    if not hasattr(label, "_onContextMenuRequested"):
        return
    try:
        label.customContextMenuRequested.disconnect(label._onContextMenuRequested)
    except (TypeError, RuntimeError):
        pass


def make_selectable(label):
    """让 QLabel 支持鼠标选中 + 右键复制（原地改造并返回该实例）。"""
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    _drop_native_context_menu(label)
    label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    label.customContextMenuRequested.connect(
        lambda pos: _show_copy_menu(label, pos)
    )
    return label


class SelectableBodyLabel(BodyLabel):
    """自带「选中复制」能力的 BodyLabel（新代码可直接实例化）。

    注意：qfw 标签构造器是重载式（``str`` 分支会**虚调用** ``self.__init__``），
    子类直接 ``super().__init__(text, parent)`` 会带着 text 再入自身构造器
    （``self.__init__(parent)`` → ``super().__init__(None, None)`` → TypeError）。
    因此这里只向基类传 ``parent``，文本用 ``setText`` 补设。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        if text:
            self.setText(text)
        make_selectable(self)
