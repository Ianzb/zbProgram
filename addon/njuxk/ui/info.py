"""通知挂载点解析：所有 InfoBar / Loading 遮罩 / 对话框统一挂插件最高层级页面。

需求「统一所有通知都在插件的最高层级页面显示」：各子页面（选课/收藏/任务/结果/
登录卡）自己发提示时，父窗口不应是子页面自身，而应统一提升到插件顶层
``MainPage``（``main.py``），保证提示随插件页面显示、位置一致，且**绝不**挂到
宿主主窗口上。

用法::

    from .info import info_parent

    InfoBar.success(..., parent=info_parent(self))
    dialog = SomeDialog(parent=info_parent(self))
"""
from __future__ import annotations


def info_parent(widget):
    """返回通知应挂载的父窗口：插件最高层级页面（MainPage）。

    解析顺序：

    1. 显式注入的 ``widget._main_page``（装配层 ``set_main_page`` / 构造注入）；
    2. 沿 ``parentWidget()`` 链向上找到带 ``_is_main_page`` 标记的顶层插件页
       （``main.MainPage``）；
    3. 都没有时退回 ``widget`` 自身 —— 仍是插件页面，绝不挂宿主主窗口。
    """
    page = getattr(widget, "_main_page", None)
    if page is not None:
        return page
    current = widget
    while current is not None:
        if getattr(current, "_is_main_page", False):
            return current
        current = current.parentWidget()
    return widget
