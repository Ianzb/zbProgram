import logging

from app.addon import *

try:
    from zbProgram.app.addon import *
except Exception as e:
    # 兜底语义不变：导入失败不抛异常，回退 app.addon；仅留 debug 日志便于排查
    logging.debug("zbProgram.app.addon 导入失败，回退 app.addon：%s", e)

from qtpy.QtCore import QPoint
from qtpy.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentIcon as FIF,
    PushButton,
    RoundMenu,
    SubtitleLabel,
)

import zbWidgetLib as zbw

from .ui.layout import LOGIN_CARD_MAX_WIDTH, SPACING, apply_page_margins
from .ui.login import LoginCard

addonBase = AddonBase()

# 全局应用装配层实例（addonInit 创建，addonDelete 关闭）
xk_app = None


def addonInit():
    global program, setting, window, progressCenter, addonInfo, xk_app
    program = addonBase.program
    setting = addonBase.setting
    window = addonBase.window
    progressCenter = addonBase.progress_center
    addonInfo = addonBase.addon_info
    # 注册默认设置项（单一数据源：core.settings.DEFAULTS，键名不变）
    from .core.settings import register_defaults
    register_defaults(setting)
    # 创建应用装配层（调度器 ↔ 选课/收藏/任务 三页接线）
    # window 用于安装关闭事件过滤器：宿主关闭窗口真的退出时自动调用退出登录 API
    from .core.app import XkApp
    xk_app = XkApp(setting=setting, program=program, window=window)


def addonDelete():
    # 关闭时先调退出登录 API（logout.do → authlogout.do，串行两步，短超时兜底），
    # 再关闭调度器线程池与定时器；不得抛异常（异常留日志，便于排查）
    global xk_app
    try:
        if xk_app is not None:
            xk_app.shutdown()
    except Exception:
        logging.exception("关闭选课插件失败")
    finally:
        xk_app = None


def addonWidget():
    global xk_app
    if xk_app is None:
        from .core.app import XkApp
        xk_app = XkApp(setting=setting, program=program, window=window)
    return MainPage(window, xk_app)


class MainPage(QWidget):
    """南大选课主页面：顶栏（标题 + 账号按钮）+ 登录层/页签层堆叠。

    结构（需求「登录界面处于三个标签页上层」「账号按钮显示在页签区域之外的
    右上角」）::

        MainPage(QWidget)
        ├── 顶栏（左：标题「南大选课」；右：账号按钮 + 下拉菜单）
        └── QStackedWidget
            ├── index 0: 登录层（LoginCard 居中）
            └── index 1: BasicTabPage（选课 / 收藏 / 任务 三页签）

    - 未登录：堆叠停在登录层（三页签被盖住，不可见也不可切换），账号按钮隐藏；
    - 登录成功：切到页签层，账号按钮显示学号；
    - 登出（账号菜单）：切回登录层，账号按钮隐藏。

    登录态判定与切换由装配层 ``XkApp`` 编排（``attach_main_page`` 接线）；
    本页只提供登录层 / 页签层 / 账号按钮的展示与切换方法。

    宿主契约：``addonWidget()`` 返回对象仍提供 ``title()`` / ``icon()``。
    """

    def title(self):
        return "南大选课"

    def icon(self):
        return FIF.EDUCATION

    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self._app = app
        self.login_card = None
        self._build_ui()
        if app is not None:
            self._build_login_layer(app)
            self._build_tabs(app)
            # 回填装配层引用并接线登录态：
            # 登录卡成功 → 页签层；课程页会话失效 → 切回登录层
            app.attach_main_page(self)
            self.sync_login_state()
        else:
            # 占位分支（app is None）：保持构造可用，直接显示占位页签
            self._build_placeholder_tabs()
            self.stack.setCurrentWidget(self.tabs)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        # 页面级边距/间距统一取自 ui.layout（与各页同一套常量，不写死）
        root = QVBoxLayout(self)
        apply_page_margins(root)

        # ---- 顶栏：左标题 + 右账号按钮（页签区域之外的插件页级常驻区域）----
        self.topBar = QWidget(self)
        topLayout = QHBoxLayout(self.topBar)
        topLayout.setContentsMargins(0, 0, 0, 0)
        topLayout.setSpacing(SPACING)
        self.titleLabel = SubtitleLabel("南大选课", self.topBar)
        topLayout.addWidget(self.titleLabel)
        topLayout.addStretch(1)
        self.accountButton = PushButton(FIF.PEOPLE, "未登录", self.topBar)
        self.accountButton.setToolTip("当前账号：点击打开菜单（退出登录）")
        self.accountButton.setVisible(False)
        self.accountButton.clicked.connect(self._show_account_menu)
        topLayout.addWidget(self.accountButton)
        root.addWidget(self.topBar)

        # ---- 登录层 / 页签层堆叠：未登录停在登录层，三页签被盖住不可切换 ----
        self.stack = QStackedWidget(self)
        root.addWidget(self.stack, 1)

        # index 0: 登录层（LoginCard 由 _build_login_layer 填充）
        self.loginLayer = QWidget(self)
        self.stack.addWidget(self.loginLayer)

        self._build_account_menu()

    def _build_login_layer(self, app):
        """登录层（index 0）：LoginCard 居中 + 限宽（复用登录卡组件）。"""
        loginLayout = QVBoxLayout(self.loginLayer)
        loginLayout.setContentsMargins(0, 0, 0, 0)
        loginLayout.addStretch(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.login_card = LoginCard(
            client=app.client, program=app.program, setting=app.setting,
            main_page=self,
        )
        self.login_card.setMaximumWidth(LOGIN_CARD_MAX_WIDTH)
        row.addWidget(self.login_card)
        row.addStretch(1)
        loginLayout.addLayout(row)
        loginLayout.addStretch(1)

    def _build_tabs(self, app):
        """页签层（index 1）：选课 / 收藏 / 任务 三个页签。"""
        self.tabs = zbw.BasicTabPage(self)
        self.tabs.addPage(app.course_page, "选课", FIF.EDUCATION)
        self.tabs.addPage(app.favorites_page, "收藏", FIF.HEART)
        self.tabs.addPage(app.task_page, "任务", FIF.PLAY)
        self.stack.addWidget(self.tabs)

    def _build_placeholder_tabs(self):
        """占位页签（app is None 时保持构造可用）。"""
        self.tabs = zbw.BasicTabPage(self)
        self.tabs.addPage(BodyLabel("选课页占位", self), "选课", FIF.EDUCATION)
        self.tabs.addPage(BodyLabel("收藏页占位", self), "收藏", FIF.HEART)
        self.tabs.addPage(BodyLabel("任务页占位", self), "任务", FIF.PLAY)
        self.stack.addWidget(self.tabs)

    # ------------------------------------------------------------------
    # 登录态切换
    # ------------------------------------------------------------------

    def sync_login_state(self):
        """启动登录态判定：已登录 → 页签层；本地存了账号 → 自动登录；否则登录层。"""
        app = self._app
        if app is None:
            return
        if app.course_page.is_logged_in():
            self.enter_logged_in(
                str(getattr(app.client, "student_code", "") or "")
            )
        elif app.course_page.has_saved_account():
            # 重启后没有可用会话，但本地存了账号密码 → 自动重新登录一次
            app.start_auto_login()
        else:
            self.show_login_layer()

    def enter_logged_in(self, student_code: str = ""):
        """登录成功：切到页签层，账号按钮显示学号。"""
        self.stack.setCurrentWidget(self.tabs)
        self.set_account(student_code)

    def show_login_layer(self):
        """未登录：切回登录层（三页签被盖住，不可见也不可切换），账号按钮隐藏。

        每次切回登录层都复位登录卡（清空状态标签 / 恢复按钮 / 关闭遗留
        Loading），避免上一次登录的「登录成功」文案残留（用户实测反馈）。
        覆盖三条路径：启动初始态、账号菜单登出完成、课程页会话失效。
        """
        self.stack.setCurrentWidget(self.loginLayer)
        if self.login_card is not None:
            self.login_card.reset()
        self.set_account("")

    def on_logged_out(self):
        """登出收尾：切回登录层并隐藏账号按钮。"""
        self.show_login_layer()

    def set_account(self, student_code: str):
        """显示当前账号（学号）；空串表示未登录，此时隐藏账号按钮。"""
        code = (student_code or "").strip()
        self.accountButton.setText(code or "未登录")
        self.accountButton.setVisible(bool(code))

    # ------------------------------------------------------------------
    # 登录卡状态反馈（启动自动登录期间由装配层调用）
    # ------------------------------------------------------------------

    def set_login_busy(self, busy: bool, status_text: str = ""):
        """自动登录期间禁用登录按钮并显示状态文案。"""
        if self.login_card is None:
            return
        self.login_card.loginButton.setEnabled(not busy)
        if status_text:
            self.login_card.statusLabel.setText(status_text)

    def set_login_status(self, text: str):
        """更新登录卡状态标签（自动登录结果等）。"""
        if self.login_card is not None:
            self.login_card.statusLabel.setText(text)

    # ------------------------------------------------------------------
    # 顶栏账号菜单（自选课页迁来）：退出登录
    # ------------------------------------------------------------------

    def _build_account_menu(self):
        """账号下拉菜单：退出登录（交给装配层执行）。

        菜单项用 ``Action`` 持有，测试可直接 ``trigger()`` 断言行为，不必依赖
        菜单的弹出动画或坐标命中。
        """
        self.accountMenu = RoundMenu(parent=self)
        self.logoutAction = Action(FIF.POWER_BUTTON, "退出登录")
        self.logoutAction.triggered.connect(self._on_logout_action)
        self.accountMenu.addAction(self.logoutAction)

    def _show_account_menu(self):
        """点击账号按钮 → 在按钮下方弹出菜单（``popup`` 非阻塞，不用 ``exec``）。"""
        if self._app is None:
            return
        pos = self.accountButton.mapToGlobal(
            QPoint(0, self.accountButton.height())
        )
        self.accountMenu.popup(pos)

    def _on_logout_action(self):
        if self._app is not None:
            self._app.request_logout()
