"""登录卡片（计划 todo 10）：学号/密码输入卡 + 线程化登录（验证码全自动识别）。

线程约定：
- 一次性请求走 ``program.THREAD_POOL``（宿主线程池）；
- 本模块可在**无 zbProgram 宿主**的测试环境运行：``program`` / ``setting`` 的获取
  做兜底（``main`` 模块缺失时返回 None），测试时注入带 ``THREAD_POOL`` 的 stub
  program 与 stub setting；
- **严禁**在工作线程直接操作任何 QWidget；所有 UI 更新只通过
  ``loginFinishedSignal`` 信号槽回主线程。

登录是全自动的：用户只输入学号密码，验证码由 ONNX 模型识别，UI 不显示验证码图片。
"""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import QVBoxLayout

from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
)

import zbWidgetLib as zbw

from ..api.client import XkClient
from ..core import state
from .text_select import make_selectable


def _host_program():
    """读取宿主 ``program``（含 ``THREAD_POOL``）；无宿主时返回 None。"""
    try:
        from .. import main as _main
        return getattr(_main, "program", None)
    except Exception as e:
        # 无宿主（测试环境）是正常场景，用 debug 级别
        logging.debug("读取宿主 program 失败（无宿主时正常）：%s", e)
        return None


def _host_setting():
    """读取宿主 ``setting``（AddonSettingProxy）；无宿主时返回 None。"""
    try:
        from .. import main as _main
        return getattr(_main, "setting", None)
    except Exception as e:
        # 无宿主（测试环境）是正常场景，用 debug 级别
        logging.debug("读取宿主 setting 失败（无宿主时正常）：%s", e)
        return None


class LoginCard(zbw.HeaderCardWidget):
    """登录卡片：学号/密码输入 + 线程化登录（验证码全自动识别）。

    信号：
        loginFinishedSignal(bool, str) —— 登录成功与否 + 中文消息（主线程发出）。
    属性：
        client —— ``XkClient`` 实例，可由外部注入以便测试。
    """

    loginFinishedSignal = Signal(bool, str)

    def __init__(self, parent=None, client=None, program=None, setting=None,
                 main_page=None):
        self.client = client if client is not None else XkClient()
        self._program = program
        self._setting = setting
        # 插件主页面（MainPage）引用：Loading 遮罩挂它（覆盖整个插件页区域，
        # 不覆盖宿主主窗口）；未注入时退回登录卡自身
        self._main_page = main_page
        self._loading_box = None
        self._fallback_pool = None
        # 注意：HeaderCardWidget.__init__ 是 singledispatchmethod，其 (title, parent)
        # 重载会调用 self.__init__(parent) 再次进入子类 __init__ 造成无限递归，
        # 因此这里只传 parent（命中基类默认重载），标题随后用 setTitle 设置。
        super().__init__(parent)
        self.setTitle("登录")
        self.loginFinishedSignal.connect(self._on_login_finished)

    def _postInit(self):
        """HeaderCardWidget 在 ``__init__`` 末尾调用：构建表单。"""
        self.userEdit = LineEdit(self)
        self.userEdit.setPlaceholderText("学号")
        self.pwdEdit = PasswordLineEdit(self)
        self.pwdEdit.setPlaceholderText("密码")
        self.rememberCheck = CheckBox("记住密码", self)
        self.rememberCheck.setChecked(True)
        self.loginButton = PrimaryPushButton(FIF.PEOPLE, "登录", self)
        self.statusLabel = make_selectable(BodyLabel("", self))
        self.statusLabel.setTextColor("#606060", "#d2d2d2")

        self.loginButton.clicked.connect(self.do_login)

        self.formLayout = QVBoxLayout()
        self.formLayout.setSpacing(12)
        self.formLayout.addWidget(self.userEdit)
        self.formLayout.addWidget(self.pwdEdit)
        self.formLayout.addWidget(self.rememberCheck)
        self.formLayout.addWidget(self.loginButton)
        self.formLayout.addWidget(self.statusLabel)
        self.viewLayout.addLayout(self.formLayout)

    # ------------------------------------------------------------------
    # program / setting 兜底
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

    def set_main_page(self, page):
        """注入插件主页面引用（Loading 遮罩的挂载点）。"""
        self._main_page = page

    def _loading_parent(self):
        """Loading 遮罩父窗口：MainPage（插件页顶层容器），未注入退回自身。

        需求：遮罩要覆盖整个插件页区域（parent 层级够高），但绝不提升到
        宿主主窗口（那是上一轮明令禁止的挂法）。
        """
        return self._main_page if self._main_page is not None else self

    def reset(self):
        """复位登录卡：清空状态标签、恢复按钮可用、关闭遗留 Loading。

        每次切回登录层时调用（登出 / 会话失效 / 启动初始态），避免上一次
        登录的「登录成功」文案与禁用按钮残留到下一次登录。
        """
        if self._loading_box is not None:
            try:
                self._loading_box.close()
            except Exception as e:
                logging.warning(f"关闭 LoadingMessageBox 失败：{e}")
            self._loading_box = None
        self.loginButton.setEnabled(True)
        self.statusLabel.setText("")

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------

    def do_login(self):
        """校验输入并在线程池中发起登录（验证码全自动识别）。

        空学号/空密码直接提示并返回，不提交任务。
        """
        user = self.userEdit.text().strip()
        pwd = self.pwdEdit.text()
        if not user or not pwd:
            self.statusLabel.setText("请输入学号和密码")
            return
        remember = self.rememberCheck.isChecked()
        self.loginButton.setEnabled(False)
        self.statusLabel.setText("正在登录（自动识别验证码）…")
        # 遮罩挂 MainPage：覆盖整个插件页区域，不覆盖宿主主窗口
        self._loading_box = zbw.LoadingMessageBox(self._loading_parent())
        self._loading_box.setText("正在登录…")
        self._loading_box.show()
        self._thread_pool().submit(self._login_worker, user, pwd, remember)

    def _login_worker(self, user, pwd, remember):
        """工作线程：init_session → login → 持久化 → 信号回主线程。

        严禁在此操作任何 QWidget；所有 UI 更新只通过 ``loginFinishedSignal``。
        异常兜底：捕获所有异常并 ``emit(False, "登录失败：{e}")``。
        """
        try:
            self.client.init_session()
            ok, msg = self.client.login(user, pwd)
            if ok:
                setting = self.setting
                if setting is not None:
                    if remember:
                        state.save_account(setting, user, pwd)
                    session = self.client.export_session()
                    state.save_session(
                        setting,
                        session.get("cookies", {}),
                        session.get("token", ""),
                    )
            self.loginFinishedSignal.emit(ok, msg)
        except Exception as e:
            logging.error(f"登录失败：{traceback.format_exc()}")
            self.loginFinishedSignal.emit(False, f"登录失败：{e}")

    def _on_login_finished(self, ok, msg):
        """主线程槽：关闭 Loading、恢复按钮、更新状态标签；成功时弹 InfoBar。"""
        if self._loading_box is not None:
            try:
                self._loading_box.close()
            except Exception as e:
                logging.warning(f"关闭 LoadingMessageBox 失败：{e}")
            self._loading_box = None
        self.loginButton.setEnabled(True)
        self.statusLabel.setText(msg)
        # 行为日志（不含密码 / token / cookie）
        if ok:
            logging.info("登录卡登录成功")
        else:
            logging.info(f"登录卡登录失败：{msg}")
        if ok:
            # 明确提示：标题「登录成功」，内容含学号（拿不到学号时退为「已登录」）
            detail = msg or "已登录"
            student_code = getattr(self.client, "student_code", "")
            if student_code:
                detail = f"{detail}（学号 {student_code}）"
            InfoBar.success(
                title="登录成功",
                content=detail,
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                # 提示挂插件自己的组件（登录卡），绝不挂宿主主窗口
                parent=self,
            )
