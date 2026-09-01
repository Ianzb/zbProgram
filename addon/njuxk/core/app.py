"""应用装配层（计划 todo 16）：调度器 ↔ 任务页 ↔ 课程页 ↔ 收藏页 的信号接线。

职责：
- 持有共享 ``client`` / ``scheduler`` 与三个页面（``course_page`` /
  ``favorites_page`` / ``task_page``）；
- 把页面信号（抢课 / 收藏 / 任务控制）接到调度器与本地收藏；
- 把调度器信号（状态 / 进度 / 消息 / 完成）接到任务卡片与 InfoBar；
- **即时生效**：任务卡片的「定时开始」开关与「延迟/重复次数」参数改动直连
  ``scheduler.set_timed_start`` / ``scheduler.set_task_params``，不经过 ``start()``，
  切换/改动即生效；
- **状态驱动按钮**：``taskStateChanged`` 除更新状态文本外，还调
  ``TaskCard.apply_state`` 同步「开始/继续」与「停止」的可用性与文案；
- **收藏实时同步**：收藏切换 / 清空收藏 / 清空任务后都调
  ``CoursePage.refresh_favorite_badges()``，使选课页徽标与本地收藏集一致；
- **立即报名（单次）**：课程卡「立即报名」→ ``enrollRequested`` → 提交一次
  ``volunteer.do``（会话失效自动重登一次并重试一次），结果经 ``enrollFinished``
  回主线程弹 InfoBar；``self._enrolling`` 防重复点击；
- **退选（带二次确认）**：课程卡/收藏卡「退选」→ ``dropRequested`` → 主线程弹
  ``MessageBox`` 二次确认 → 工作线程 ``delete_volunteer`` + ``poll_result(op_type="0")``
  （退课轮询用 ``type="0"``，报名才是 ``"1"``）→ 结果经 ``dropFinished`` 回主线程
  弹 InfoBar 并刷新列表；``self._dropping`` 防重复点击；
- **登录态编排**：登录卡（``MainPage`` 登录层）登录成功 → 切页签层 + 账号按钮 +
  课程页加载批次；启动自动登录（本地存了账号密码 → 工作线程完整登录一次）也由
  本层编排（``start_auto_login``），UI 反馈经 ``main_page``；
- **账号菜单（MainPage 顶栏）**：退出登录 → 工作线程 ``client.logout()`` → 清会话
  与账号密码 → 课程页清空 + 切回登录层；
- **提示挂插件页**：跨页 InfoBar / 确认框一律挂 ``main_page``
  （``_info_parent``），绝不挂宿主主窗口；MainPage 未创建时（纯装配层测试）
  退回任务页自身；
- **关闭时自动退出登录**（三层保障，见 ``logout_on_exit``）：``addonDelete()`` /
  ``QApplication.aboutToQuit`` / 宿主主窗口 ``Close`` 事件过滤器。串行两步
  ``logout.do`` → ``authlogout.do``（``client.logout()`` 已封装），**不调用 CAS
  登出**；只清会话、保留本地账号密码（留给下次自动登录）；短超时 + 全异常兜底，
  任何异常都不得阻止关闭；
- ``shutdown()`` 供 ``addonDelete`` 调用（内含退出登录）。

线程约定（对齐 ``zbProgram/app/interface/widget.py`` 的信号连 UI 槽范式）：
- 调度器工作线程 → UI 只经 ``pyqtSignal`` 回主线程，严禁直接操作 QWidget；
- 本层所有 ``_on_*`` 槽都在**主线程**执行（跨线程信号自动排队到接收者线程）；
- 收藏纯本地（``core.state``，以 ``teaching_class_id`` 为唯一键），严禁调用
  任何服务端收藏接口（favorite.do / queryfavorite.do）。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

from qtpy.QtCore import QEvent, QObject, Qt, Signal
from qtpy.QtWidgets import QApplication, QWidget

from qfluentwidgets import InfoBar, InfoBarPosition, MessageBox

from ..api import models
from ..api.client import XkClient
from . import state
from .scheduler import GrabTask, TaskScheduler, TaskState
from ..ui.course import CoursePage
from ..ui.favorites import FavoritesPage
from ..ui.tasks import TaskPage

# 调度器状态 → 任务卡片状态文案（与 ui/tasks.py 的 clear_finished 判定一致）
_STATE_TEXT = {
    TaskState.WAITING: "等待中",
    TaskState.RUNNING: "运行中",
    TaskState.SUCCESS: "成功",
    TaskState.FAILED: "失败",
    TaskState.STOPPED: "已停止",
}

# 关闭时退出登录的最长等待（秒）：关闭流程不能被网络拖住
_LOGOUT_TIMEOUT = 3.0

# 一键清空：等待运行中任务停止的最长时间（秒）。stop_all() 后，运行中的
# worker 会在当前尝试结束后的下一个停止检查点退出（此时停止事件仍挂在
# 调度器任务表上，必须先等它停止再 remove，否则事件被 pop、worker 永远
# 看不到停止标志而无限重试）；超过该时长则照常移除（与单任务「移除」
# 按钮的既有语义一致，清空流程不被卡死的网络请求拖住）。
_CLEAR_ALL_STOP_TIMEOUT = 5.0


class _CloseEventFilter(QObject):
    """宿主主窗口 ``Close`` 事件过滤器（插件自装，不改宿主源码）。

    宿主 ``MainWindow.closeEvent`` 会 ``QCloseEvent.ignore()``，然后按全局设置
    ``hideWhenClose`` 二选一：为真 → 只 ``hide()``（缩到托盘，进程还在，不能登出）；
    为假 → ``program.close()``（真的退出，此时必须登出，否则占用服务端登录会话）。

    因此这里只在「关闭即退出」时触发登出，并且**始终返回 False**（不拦截事件），
    保证宿主自己的关闭逻辑照常执行。
    """

    def __init__(self, setting, callback, parent=None):
        super().__init__(parent)
        self._setting = setting
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 回调命名
        if event.type() == QEvent.Type.Close:
            try:
                if not self._hide_when_close():
                    self._callback()
            except Exception:
                # 关闭流程里任何异常都不得冒泡（会打断宿主关闭）
                logging.exception("关闭时退出登录失败")
        return False

    def _hide_when_close(self) -> bool:
        """读取宿主全局设置 ``hideWhenClose``（非插件命名空间）。"""
        setting = self._setting
        if setting is None:
            return False
        try:
            return bool(setting.read("hideWhenClose", use_addon_path=False))
        except TypeError:
            # 老 setting 代理不支持 use_addon_path 关键字
            return bool(setting.read("hideWhenClose"))


class XkApp(QObject):
    """应用装配层：把调度器与三个页面接线成完整应用。

    构造参数：
        setting —— 本地设置（AddonSettingProxy 或测试 stub）；
        program —— 宿主 program（可为 None，测试环境用本地兜底）；
        client  —— 共享 API 客户端（测试注入 stub；缺省创建真实 XkClient）；
        window  —— 宿主主窗口（可为 None；非 None 时安装关闭事件过滤器，
                   用于关闭时自动退出登录）。
    """

    # 立即报名结果：(teaching_class_id, ok, msg) —— 工作线程 emit，主线程槽接收
    enrollFinished = Signal(str, bool, str)
    # 退选结果：(teaching_class_id, ok, msg) —— 工作线程 emit，主线程槽接收
    dropFinished = Signal(str, bool, str)
    # 账号菜单「退出登录」：工作线程调完 client.logout() 后 emit，主线程槽收尾
    _logoutReady = Signal()
    # 启动自动登录结果：(ok, msg) —— 工作线程 emit，主线程槽收尾
    _autoLoginReady = Signal(bool, str)
    # 一键清空：工作线程完成「等停止 + 调度器任务表移除」后 emit，主线程清卡片
    _clearAllReady = Signal()

    def __init__(self, setting, program=None, client=None, window=None):
        super().__init__()
        self._setting = setting
        self._program = program
        self._window = window
        self.client = client if client is not None else XkClient()
        # 会话失效自动重登：凭据 provider 接本地保存的账号密码（_post 层消化
        # 302 失效响应；选课页/调度器/报名退选的既有过期处理保留为兜底）
        if hasattr(self.client, "set_credential_provider"):
            self.client.set_credential_provider(
                lambda: state.load_account(self._setting)
            )
        self._fallback_pool = None
        # 插件主页面（MainPage）引用：MainPage 创建时经 attach_main_page 回填。
        # 跨页 InfoBar / 确认框都挂它（提示弹在插件页面上，不挂宿主主窗口）
        self.main_page = None
        # 正在报名中的 teaching_class_id（防重复点击）
        self._enrolling = set()
        # 报名请求时的课程名（结果提示用，避免刷新后查不到）
        self._enroll_names = {}
        # 正在退选中的 teaching_class_id（防重复点击）
        self._dropping = set()
        # 退选请求时的课程名（结果提示用，避免刷新后查不到）
        self._drop_names = {}
        # 启动自动登录只做一次（MainPage.sync_login_state 触发）
        self._auto_login_started = False
        # 关闭登出只做一次（三层保障可能同时命中）
        self._logout_done = False
        self._close_filter = None
        self._about_to_quit_connected = False
        self.scheduler = TaskScheduler(self.client, setting=setting, program=program)
        self.course_page = CoursePage(
            client=self.client, setting=setting, program=program
        )
        self.favorites_page = FavoritesPage(
            client=self.client, setting=setting, program=program
        )
        self.task_page = TaskPage(setting=setting)
        self._fallback_parent = None  # 无父窗口时给确认框用的隐藏占位
        self._connect()
        self._install_exit_hooks()

    # ------------------------------------------------------------------
    # 宿主注入对象的只读访问（MainPage 构造登录卡时需要）
    # ------------------------------------------------------------------

    @property
    def setting(self):
        """本地设置（AddonSettingProxy 或测试 stub）。"""
        return self._setting

    @property
    def program(self):
        """宿主 program（可为 None）。"""
        return self._program

    # ------------------------------------------------------------------
    # 接线清单
    # ------------------------------------------------------------------

    def _connect(self):
        # 1/3. 课程页 / 收藏页 单卡「加入抢课」→ 创建任务并启动
        self.course_page.grabRequested.connect(self._on_grab_requested)
        self.favorites_page.grabRequested.connect(self._on_grab_requested)
        # 1b. 课程页单卡「立即报名」→ 单次提交，结果经 enrollFinished 回主线程
        self.course_page.enrollRequested.connect(self._on_enroll_requested)
        self.enrollFinished.connect(self._on_enroll_finished)
        # 1c. 课程页/收藏页单卡「退选」→ 二次确认 → 单次退课，结果经 dropFinished
        self.course_page.dropRequested.connect(self._on_drop_requested)
        self.favorites_page.dropRequested.connect(self._on_drop_requested)
        self.dropFinished.connect(self._on_drop_finished)
        # 2. 课程页收藏切换 → 刷新收藏页（state 写入由 CoursePage 完成）
        self.course_page.favoriteToggled.connect(self._on_course_favorite_toggled)
        # 4. 收藏页「一键开始全部抢课」→ 批量创建任务并启动全部
        self.favorites_page.grabAllRequested.connect(self._on_grab_all_requested)
        # 5. 任务页控制 → 调度器
        self.task_page.taskStartRequested.connect(self._on_task_start_requested)
        self.task_page.taskStopRequested.connect(self._on_task_stop_requested)
        self.task_page.taskRemoved.connect(self._on_task_removed)
        # 5a. 任务页「一键清空」→ 停止并移除全部任务（二次确认在任务页完成）
        self.task_page.clearAllRequested.connect(self._on_clear_all_requested)
        self._clearAllReady.connect(self._on_clear_all_ready)
        # 5b. 参数/开关即时生效：直连调度器，不经过 start()
        self.task_page.timedStartToggled.connect(self.scheduler.set_timed_start)
        self.task_page.paramsChanged.connect(self._on_task_params_changed)
        # 6/7/8/9. 调度器信号 → 任务页 / InfoBar
        self.scheduler.taskStateChanged.connect(self._on_task_state_changed)
        self.scheduler.taskProgress.connect(self._on_task_progress)
        self.scheduler.taskMessage.connect(self._on_task_message)
        self.scheduler.taskFinished.connect(self._on_task_finished)
        # 启动自动登录：结果经信号回主线程收尾（切页签层 + 加载批次）
        self._autoLoginReady.connect(self._on_auto_login_ready)
        # 账号菜单「退出登录」（MainPage 顶栏）：工作线程登出 → 主线程收尾
        self._logoutReady.connect(self._on_account_logout_ready)

    # ------------------------------------------------------------------
    # MainPage 装配（main.addonWidget 创建 MainPage 后调用）
    # ------------------------------------------------------------------

    def attach_main_page(self, page):
        """MainPage 创建后回填引用并接线登录态（装配层职责）。

        - ``main_page`` 引用：跨页 InfoBar / 确认框挂插件页（不挂宿主窗口）；
        - 登录卡登录成功 → 切页签层 + 账号按钮显示学号 + 课程页加载批次；
        - 课程页会话失效且重登失败（``loginRequired``）→ 切回登录层。
        """
        self.main_page = page
        if page.login_card is not None:
            page.login_card.loginFinishedSignal.connect(self._on_login_card_finished)
        # 收藏页 Loading 遮罩挂 MainPage（覆盖整个插件页区域，不挂宿主窗口）
        self.favorites_page.set_main_page(page)
        self.course_page.loginRequired.connect(page.show_login_layer)

    def _on_login_card_finished(self, ok, msg):
        """登录卡登录成功（主线程）：进入页签层 + 账号按钮 + 加载批次。

        登录卡自身的收尾（关 Loading / 恢复按钮 / 状态标签 / InfoBar）由
        ``LoginCard._on_login_finished`` 完成（先连接先执行）。
        """
        if not ok:
            return
        if self.main_page is not None:
            self.main_page.enter_logged_in(
                str(getattr(self.client, "student_code", "") or "")
            )
        self.course_page.load_batches()

    # ------------------------------------------------------------------
    # 启动自动登录（本地保存了账号密码 → 每次打开重新登录一次）
    # ------------------------------------------------------------------

    def start_auto_login(self):
        """用本地保存的账号密码在工作线程自动登录一次。

        走完整 ``client.login``（含验证码识别）而非只恢复 session：用户要求
        「每次打开重新登录一次」，且只恢复 session 拿不到 ``student_code``。
        UI 上先切到登录层并把状态标签置为「正在自动登录（识别验证码）…」，
        成功后切到页签层并弹 InfoBar，失败则留在登录层并给出中文原因。
        由 ``MainPage.sync_login_state`` 触发。
        """
        if self._auto_login_started:
            return
        user, pwd = ("", "")
        if self._setting is not None:
            user, pwd = state.load_account(self._setting)
        if not user or not pwd:
            if self.main_page is not None:
                self.main_page.show_login_layer()
            return
        self._auto_login_started = True
        if self.main_page is not None:
            self.main_page.show_login_layer()
            self.main_page.set_login_busy(True, "正在自动登录（识别验证码）…")
        self._thread_pool().submit(self._auto_login_worker, user, pwd)

    def _auto_login_worker(self, user, pwd):
        """工作线程：init_session → login → 持久化会话 → 信号回主线程。

        严禁在此操作任何 QWidget；所有 UI 更新只通过 ``_autoLoginReady``。
        """
        try:
            if hasattr(self.client, "init_session"):
                self.client.init_session()
            ok, msg = self.client.login(user, pwd)
            if ok and self._setting is not None and hasattr(self.client, "export_session"):
                session = self.client.export_session()
                state.save_session(
                    self._setting,
                    session.get("cookies", {}),
                    session.get("token", ""),
                )
            self._autoLoginReady.emit(ok, msg)
        except Exception as e:
            logging.error(f"自动登录失败：{traceback.format_exc()}")
            self._autoLoginReady.emit(False, f"自动登录失败：{e}")

    def _on_auto_login_ready(self, ok, msg):
        """主线程槽：成功 → 页签层 + InfoBar + 加载批次；失败 → 登录层 + 原因。"""
        if self.main_page is not None:
            self.main_page.set_login_busy(False)
        if ok:
            if self.main_page is not None:
                self.main_page.set_login_status(msg or "已登录")
                self.main_page.enter_logged_in(
                    str(getattr(self.client, "student_code", "") or "")
                )
                InfoBar.success(
                    title="已自动登录",
                    content=msg or "已登录",
                    orient=Qt.Orientation.Vertical,
                    isClosable=True,
                    duration=5000,
                    position=InfoBarPosition.TOP_RIGHT,
                    parent=self._info_parent(),
                )
            self.course_page.load_batches()
        else:
            # 不静默失败：把中文原因留在登录卡状态标签上
            if self.main_page is not None:
                self.main_page.set_login_status(f"自动登录失败：{msg}")

    # ------------------------------------------------------------------
    # 抢课任务创建
    # ------------------------------------------------------------------

    def _on_grab_requested(self, teaching_class_id):
        """课程页/收藏页单卡「加入抢课」→ 创建任务加入任务页与调度器并启动。

        重复创建（任务已存在）时由 ``TaskPage.add_task`` 弹「任务已存在」提示，
        不重复启动、不弹「已加入」。
        """
        course = self._find_course(teaching_class_id)
        if course is None:
            return
        card = self.task_page.add_task(
            course, begin_time=self._batch_begin_time(course.batch_code)
        )
        task = self._build_task(course, card)
        if self.scheduler.add_task(task):
            self.scheduler.start(teaching_class_id)
            InfoBar.info(
                title="已加入抢课任务",
                content=f"「{course.course_name}」已加入抢课",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )

    def _on_grab_all_requested(self, teaching_class_ids):
        """收藏页「一键开始全部抢课」→ 批量创建任务并启动全部。

        收藏页会先逐条发 ``grabRequested`` 再发 ``grabAllRequested``，因此这里
        只补建尚未存在的任务，避免重复弹「已加入」提示。
        """
        for tid in teaching_class_ids:
            if self.scheduler.get_task(tid) is None:
                self._on_grab_requested(tid)
        self.scheduler.start_all()
        logging.info("一键开始全部抢课：%d 个任务", len(teaching_class_ids))

    def _build_task(self, course, card) -> GrabTask:
        """用课程数据 + 任务卡片当前参数构造 GrabTask。"""
        cfg = card.config()
        return GrabTask(
            teaching_class_id=course.teaching_class_id,
            course_name=course.course_name,
            course_kind=course.course_kind,
            teaching_class_type=course.teaching_class_type,
            batch_code=course.batch_code,
            is_choose=course.is_choose,
            begin_time=cfg["begin_time"],
            end_time=self._batch_end_time(course.batch_code),
            delay_min=cfg["delay_min"],
            delay_max=cfg["delay_max"],
            repeat=cfg["repeat"],
            use_timed_start=cfg["timed"],
        )

    def _find_course(self, teaching_class_id):
        """从课程页或收藏页卡片取 Course；两处都没有返回 None。"""
        card = self.course_page.cardGroup.getCard(teaching_class_id)
        if card is not None and card.course is not None:
            return card.course
        card = self.favorites_page.cardGroup.getCard(teaching_class_id)
        if card is not None and card.course is not None:
            return card.course
        return None

    def _batch_begin_time(self, batch_code) -> str:
        for b in self.course_page._batches:
            if b.code == batch_code:
                return b.begin_time
        return ""

    def _batch_end_time(self, batch_code) -> str:
        for b in self.course_page._batches:
            if b.code == batch_code:
                return b.end_time
        return ""

    # ------------------------------------------------------------------
    # 立即报名（单次提交 + 即时弹结果）
    # ------------------------------------------------------------------

    def _on_enroll_requested(self, teaching_class_id):
        """课程卡「立即报名」→ 单次提交一次 volunteer.do，结果经信号回主线程。

        与「加入抢课」的区别：本方法**只试一次**，不进任务列表、不重试、不轮询
        多轮（``code=="1"`` 时只调一次 ``poll_result`` 取真实结果）。

        防重复点击：``self._enrolling`` 记录正在报名中的 id，重复触发直接返回
        （用户连点不会刷请求）。
        """
        course = self._find_enroll_course(teaching_class_id)
        if course is None:
            InfoBar.warning(
                title="无法报名",
                content="未找到课程信息，请刷新列表后重试",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
            return
        if teaching_class_id in self._enrolling:
            InfoBar.info(
                title="正在报名",
                content="正在报名中，请稍候",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
            return
        self._enrolling.add(teaching_class_id)
        self._enroll_names[teaching_class_id] = course.course_name
        self._thread_pool().submit(self._enroll_worker, course)

    def _find_enroll_course(self, teaching_class_id):
        """取报名所需 Course：先选课页当前列表，再退到本地收藏记录。

        两处都没有返回 ``None``（调用方弹「未找到课程信息」）。收藏记录是 dict，
        转成 ``models.Course`` 后与选课页卡片走同一条提交路径。
        """
        card = self.course_page.cardGroup.getCard(teaching_class_id)
        if card is not None and getattr(card, "course", None) is not None:
            return card.course
        if self._setting is not None:
            for fav in state.list_favorites(self._setting):
                if isinstance(fav, dict) and fav.get("teaching_class_id") == teaching_class_id:
                    return self._favorite_to_course(fav)
        return None

    @staticmethod
    def _favorite_to_course(record) -> models.Course:
        """收藏 dict → Course（只取 Course 有的字段，忽略多余键）。"""
        valid = {f.name for f in fields(models.Course)}
        return models.Course(**{
            k: ("" if v is None else str(v))
            for k, v in record.items()
            if k in valid
        })

    def _enroll_worker(self, course):
        """工作线程：提交一次报名并取结果，**严禁**操作 QWidget。

        兜底捕获：任何未预期异常也要 emit 结果，否则该 id 会永久卡在
        ``self._enrolling`` 里（用户再也点不动「立即报名」）。
        """
        teaching_class_id = course.teaching_class_id
        try:
            ok, msg = self._do_enroll(course)
        except Exception as e:
            ok, msg = False, f"报名异常：{e}"
        self.enrollFinished.emit(teaching_class_id, ok, msg)

    def _do_enroll(self, course, allow_relogin: bool = True):
        """执行一次报名，返回 ``(ok, msg)``。

        - 会话失效 → 用本地凭据重新登录**一次**，成功后立即重试一次；
        - ``code=="1"``（已入队）→ ``poll_result`` 取真实结果（成功/失败/超时）；
        - 其他 code → 直接把服务端 msg 作为结果（如「当前时间不在选课开放时间
          范围内」）；
        - 网络异常 → 「网络异常：{e}」。
        """
        try:
            resp = self.client.volunteer(
                teaching_class_id=course.teaching_class_id,
                course_kind=course.course_kind,
                teaching_class_type=course.teaching_class_type,
                batch_code=course.batch_code,
            )
        except Exception as e:
            return False, f"网络异常：{e}"

        if self.client.is_session_expired(resp):
            if not allow_relogin:
                return False, "会话失效，请重新登录"
            ok, msg = self._relogin()
            if not ok:
                return False, f"重新登录失败：{msg}"
            return self._do_enroll(course, allow_relogin=False)

        if not isinstance(resp, dict):
            return False, f"响应格式异常：{resp!r}"

        code = str(resp.get("code", ""))
        # 服务端可能返回 "msg": null（键存在值为 null），get 默认值不生效，
        # 必须 or 兜底为空串，避免 f-string 拼出 "...None"
        msg = str(resp.get("msg") or "")
        if code != "1":
            # 未开放 / 人数已满 / 参数错误等：服务端 msg 即最终结果
            return False, msg or f"返回 code={code}"

        # code=="1" 只表示已入队，真实结果需轮询 studentstatus.do
        try:
            poll = self.client.poll_result(course.teaching_class_id)
        except Exception as e:
            return False, f"网络异常：{e}"
        poll_code = str(poll.get("code", "")) if isinstance(poll, dict) else ""
        poll_msg = str(poll.get("msg") or "") if isinstance(poll, dict) else ""
        if poll_code == "1":
            return True, poll_msg or "选课成功"
        if poll_code == "-1":
            return False, poll_msg or "选课失败"
        if poll_code == "timeout":
            return False, "未能确认结果，请稍后查看选课结果"
        return False, poll_msg or f"轮询未知状态：code={poll_code}"

    def _relogin(self):
        """凭据从 ``state.load_account(setting)`` 取，重新登录一次。"""
        if self._setting is None:
            return False, "无凭据（setting 为空）"
        user, pwd = state.load_account(self._setting)
        if not user or not pwd:
            return False, "未配置账号"
        try:
            return self.client.login(user, pwd)
        except Exception as e:
            return False, f"登录异常：{e}"

    def _on_enroll_finished(self, teaching_class_id, ok, msg):
        """主线程：弹结果 InfoBar；成功时同步本地收藏、刷新课程列表与收藏页。

        成功后的刷新与退选成功一致（用户实测反馈）：重新加载当前类别课程列表
        （复用「刷新课程」路径，不重拉批次）+ 刷新收藏页 + 刷新课程页徽标，
        使卡片从「未报名态」更新为「已报名态」。
        """
        self._enrolling.discard(teaching_class_id)
        name = self._enroll_names.pop(teaching_class_id, "") or teaching_class_id
        # 兜底：服务端 msg 可能为 null/空，拼接前统一清洗，绝不出现 "...None"
        msg = (msg or "").strip()
        if ok:
            if self._setting is not None:
                # 已收藏则标记 is_choose="1"（未收藏时 update_favorite 返回 False）
                state.update_favorite(
                    self._setting, teaching_class_id, {"is_choose": "1"}
                )
            self.course_page.refresh_favorite_badges()
            self.favorites_page.refresh_list()
            # 与退选成功相同：重新查询当前类别，让卡片回到最新报名态
            self.course_page.reload_current_category()
            logging.info(f"立即报名成功：{teaching_class_id}（{name}）")
            InfoBar.success(
                title="报名成功",
                content=f"「{name}」{msg}",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
        else:
            logging.info(f"立即报名失败：{teaching_class_id}（{name}）：{msg}")
            InfoBar.warning(
                title="报名失败",
                content=f"「{name}」{msg}",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )

    # ------------------------------------------------------------------
    # 退选（二次确认 + 单次退课 + 结果轮询 type="0"）
    # ------------------------------------------------------------------

    def _on_drop_requested(self, teaching_class_id):
        """课程卡/收藏卡「退选」→ 二次确认 → 工作线程退课，结果经信号回主线程。

        防重复点击：``self._dropping`` 记录正在退选中的 id，重复触发直接返回
        （用户连点不会刷请求）。确认框在**主线程**弹出（``MessageBox.exec``），
        未确认前**不会**发出任何网络请求。
        """
        course = self._find_enroll_course(teaching_class_id)
        if course is None:
            InfoBar.warning(
                title="无法退选",
                content="未找到课程信息，请刷新列表后重试",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
            return
        if teaching_class_id in self._dropping:
            InfoBar.info(
                title="正在退选",
                content="正在退选中，请稍候",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
            return
        if not self._confirm_drop(course):
            return
        self._dropping.add(teaching_class_id)
        self._drop_names[teaching_class_id] = course.course_name
        self._thread_pool().submit(self._drop_worker, course)

    def _confirm_drop(self, course) -> bool:
        """退选二次确认（网页也是强制的）：确定 → True，取消/关闭 → False。

        文案含课程名 + 课程号 + 一行警示，按钮为「确定退选」「取消」，避免误触。
        """
        parent = self._info_parent()
        if parent is None:
            # MaskDialogBase 构造会对 parent 取 width/height，None 会崩溃；
            # 用隐藏占位 QWidget 兜底（必须持有引用，否则被回收会连带删除对话框）
            parent = QWidget()
            self._fallback_parent = parent
        box = MessageBox(
            "确认退选？",
            f"课程：{course.course_name}（课程号 {course.course_number}）\n"
            "退选后可能需要重新抢课，且名额可能被他人占用。",
            parent,
        )
        box.yesButton.setText("确定退选")
        box.cancelButton.setText("取消")
        try:
            return box.exec() == MessageBox.Accepted
        finally:
            box.deleteLater()

    def _drop_worker(self, course):
        """工作线程：退课并取结果，**严禁**操作 QWidget。

        兜底捕获：任何未预期异常也要 emit 结果，否则该 id 会永久卡在
        ``self._dropping`` 里（用户再也点不动「退选」）。
        """
        teaching_class_id = course.teaching_class_id
        try:
            ok, msg = self._do_drop(course)
        except Exception as e:
            ok, msg = False, f"退选异常：{e}"
        self.dropFinished.emit(teaching_class_id, ok, msg)

    def _do_drop(self, course, allow_relogin: bool = True):
        """执行一次退选，返回 ``(ok, msg)``。

        - 会话失效 → 用本地凭据重新登录**一次**，成功后立即重试一次；
        - ``code=="1"``（已受理）→ ``poll_result(..., op_type="0")`` 取真实结果
          （退课轮询用 ``type="0"``，报名才是 ``"1"``，网页源码确证）；
        - 其他 code → 直接把服务端 msg 作为结果；
        - 网络异常 → 「网络异常：{e}」。
        """
        try:
            resp = self.client.delete_volunteer(
                teaching_class_id=course.teaching_class_id,
                batch_code=course.batch_code,
            )
        except Exception as e:
            return False, f"网络异常：{e}"

        if self.client.is_session_expired(resp):
            if not allow_relogin:
                return False, "会话失效，请重新登录"
            ok, msg = self._relogin()
            if not ok:
                return False, f"重新登录失败：{msg}"
            return self._do_drop(course, allow_relogin=False)

        if not isinstance(resp, dict):
            return False, f"响应格式异常：{resp!r}"

        code = str(resp.get("code", ""))
        # 服务端可能返回 "msg": null（键存在值为 null），get 默认值不生效，
        # 必须 or 兜底为空串，避免 f-string 拼出 "...None"
        msg = str(resp.get("msg") or "")
        if code != "1":
            # 未开放 / 参数错误等：服务端 msg 即最终结果
            return False, msg or f"返回 code={code}"

        # code=="1" 只表示已受理，真实结果需轮询 studentstatus.do（退课 type="0"）
        try:
            poll = self.client.poll_result(
                course.teaching_class_id, op_type="0"
            )
        except Exception as e:
            return False, f"网络异常：{e}"
        poll_code = str(poll.get("code", "")) if isinstance(poll, dict) else ""
        poll_msg = str(poll.get("msg") or "") if isinstance(poll, dict) else ""
        if poll_code == "1":
            return True, poll_msg or "退选成功"
        if poll_code == "-1":
            return False, poll_msg or "退选失败"
        if poll_code == "timeout":
            return False, "未能确认结果，请稍后查看选课结果"
        return False, poll_msg or f"轮询未知状态：code={poll_code}"

    def _on_drop_finished(self, teaching_class_id, ok, msg):
        """主线程：弹结果 InfoBar；成功时同步本地收藏、刷新列表与徽标。"""
        self._dropping.discard(teaching_class_id)
        name = self._drop_names.pop(teaching_class_id, "") or teaching_class_id
        # 兜底：服务端 msg 可能为 null/空，拼接前统一清洗，绝不出现 "...None"
        msg = (msg or "").strip()
        if ok:
            if self._setting is not None:
                # 已收藏则把 is_choose 恢复为 "0"（未收藏时 update_favorite 返回 False）
                state.update_favorite(
                    self._setting, teaching_class_id, {"is_choose": "0"}
                )
            self.course_page.refresh_favorite_badges()
            self.favorites_page.refresh_list()
            # 重新查询当前类别：让卡片回到未报名态（隐藏「退选」、显示报名按钮）
            self.course_page.reload_current_category()
            logging.info(f"退选成功：{teaching_class_id}（{name}）")
            InfoBar.success(
                title="退选成功",
                content=f"「{name}」{msg}",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
        else:
            logging.info(f"退选失败：{teaching_class_id}（{name}）：{msg}")
            InfoBar.warning(
                title="退选失败",
                content=f"「{name}」{msg}",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )

    # ------------------------------------------------------------------
    # 收藏
    # ------------------------------------------------------------------

    def _on_course_favorite_toggled(self, teaching_class_id, favorited):
        """课程页收藏切换（state 写入由 CoursePage 完成）→ 刷新收藏页 + 课程页徽标。

        课程页徽标也要刷：同一门课可能在多个类别下出现多张卡片，点收藏后其他
        卡片不会自动刷新（用户实测反馈）。
        """
        logging.debug("收藏切换：%s -> %s", teaching_class_id, bool(favorited))
        self.favorites_page.refresh_list()
        self.course_page.refresh_favorite_badges()

    # ------------------------------------------------------------------
    # 任务页控制 → 调度器
    # ------------------------------------------------------------------

    def _on_task_start_requested(self, teaching_class_id):
        """任务卡「开始/继续」→ 先把卡片当前参数同步到调度器任务，再启动。

        停止/失败后点「继续」走同一条路径：``scheduler.start`` 支持从
        ``STOPPED``/``FAILED`` 重启（清空停止标志、复位提交标志、attempts 继续计数）。
        """
        task = self.scheduler.get_task(teaching_class_id)
        card = self.task_page.get_task(teaching_class_id)
        if task is not None and card is not None:
            cfg = card.config()
            task.delay_min = cfg["delay_min"]
            task.delay_max = cfg["delay_max"]
            task.repeat = cfg["repeat"]
            task.begin_time = cfg["begin_time"]
            task.use_timed_start = cfg["timed"]
        self.scheduler.start(teaching_class_id)

    def _on_task_stop_requested(self, teaching_class_id):
        self.scheduler.stop(teaching_class_id)

    def _on_task_removed(self, teaching_class_id):
        self.scheduler.remove(teaching_class_id)

    def _on_clear_all_requested(self):
        """任务页「一键清空」（已二次确认）：停止全部 → 调度器任务表清空 →
        移除全部任务卡片。

        语义是**全清**（不只是清已完成）。顺序很关键：先 ``stop_all()``，
        **等运行中的 worker 到达终态后再** ``remove``——remove 会把停止事件从
        调度器任务表 pop 掉，若 worker 还卡在一次尝试里，它永远看不到停止
        标志而无限重试（僵尸 worker）。等待在工作线程做（受
        ``_CLEAR_ALL_STOP_TIMEOUT`` 上限约束，不卡 UI），完成后经
        ``_clearAllReady`` 回主线程清卡片并提示。
        """
        tids = [
            card.teaching_class_id
            for card in self.task_page.getCards()
            if getattr(card, "teaching_class_id", None) is not None
        ]
        self.scheduler.stop_all()
        logging.info("一键清空：停止全部任务（共 %d 个）", len(tids))
        self._thread_pool().submit(self._clear_all_worker, tids)

    def _clear_all_worker(self, tids):
        """工作线程：等运行中任务到达终态后，从调度器任务表移除全部任务。

        严禁操作 QWidget；完成后经 ``_clearAllReady`` 回主线程收尾。
        """
        tid_set = set(tids)
        deadline = time.monotonic() + _CLEAR_ALL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            tasks = [
                t for t in self.scheduler.tasks()
                if t.teaching_class_id in tid_set
            ]
            if all(t.state != TaskState.RUNNING for t in tasks):
                break
            time.sleep(0.05)
        for tid in tids:
            self.scheduler.remove(tid)
        logging.info("一键清空：已从调度器移除全部任务（共 %d 个）", len(tids))
        self._clearAllReady.emit()

    def _on_clear_all_ready(self):
        """主线程收尾：移除全部任务卡片并提示。"""
        self.task_page.clear_all()
        InfoBar.success(
            title="已清空",
            content="已清空全部任务",
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self._info_parent(),
        )

    def _on_task_params_changed(self, teaching_class_id, params):
        """任务卡片参数改动 → 即时同步给调度器（下一次尝试即生效）。

        直连 ``scheduler.set_task_params``，不经过 ``start()``：运行中改延迟/重复
        次数不必重新点「开始」。任务不存在时调度器返回 False，静默忽略。
        """
        if isinstance(params, dict):
            logging.debug("任务参数变更：%s %s", teaching_class_id, params)
            self.scheduler.set_task_params(teaching_class_id, **params)

    # ------------------------------------------------------------------
    # 调度器信号 → 任务页 / InfoBar
    # ------------------------------------------------------------------

    def _on_task_state_changed(self, teaching_class_id, state):
        card = self.task_page.get_task(teaching_class_id)
        if card is not None:
            card.set_status(_STATE_TEXT.get(state, state))
            # 状态驱动按钮：开始/继续 与 停止 的可用性与文案跟随实际状态
            card.apply_state(state)

    def _on_task_progress(self, teaching_class_id, attempts, repeat):
        card = self.task_page.get_task(teaching_class_id)
        if card is not None:
            card.set_progress(attempts, repeat)

    def _on_task_message(self, teaching_class_id, msg):
        card = self.task_page.get_task(teaching_class_id)
        if card is not None:
            card.set_status(msg)

    def _on_task_finished(self, teaching_class_id, ok, msg):
        """任务终态：更新卡片、弹 InfoBar；成功时标记 is_choose="1"。

        - 任务已被移除（``task is None``）→ 忽略；
        - 已停止（用户主动 stop）→ 状态已由 taskStateChanged 置为「已停止」，
          不弹失败提示；
        - 成功 → 卡片「成功」+ ``InfoBar.success``，并把本地收藏该课程的
          ``is_choose`` 标记为 ``"1"``（默认不从收藏移除，避免误删）；
        - 失败 → 卡片「失败」+ ``InfoBar.warning``。
        """
        task = self.scheduler.get_task(teaching_class_id)
        if task is None:
            return
        if task.state == TaskState.STOPPED:
            return
        # 兜底：服务端 msg 可能为 null/空，拼接前统一清洗，绝不出现 "...None"
        msg = (msg or "").strip()
        card = self.task_page.get_task(teaching_class_id)
        if card is not None:
            card.set_finished(ok, msg)
        if ok:
            if self._setting is not None:
                state.update_favorite(
                    self._setting, teaching_class_id, {"is_choose": "1"}
                )
                self.favorites_page.refresh_list()
            InfoBar.success(
                title="抢课成功",
                content=f"「{self._course_name(teaching_class_id)}」{msg}",
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )
        else:
            InfoBar.warning(
                title="抢课失败",
                content=msg,
                orient=Qt.Orientation.Vertical,
                isClosable=True,
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self._info_parent(),
            )

    def _course_name(self, teaching_class_id) -> str:
        task = self.scheduler.get_task(teaching_class_id)
        if task is not None and task.course_name:
            return task.course_name
        course = self._find_course(teaching_class_id)
        if course is not None:
            return course.course_name
        return teaching_class_id

    # ------------------------------------------------------------------
    # 账号菜单（MainPage 顶栏）：退出登录
    # ------------------------------------------------------------------

    def request_logout(self):
        """账号菜单「退出登录」→ 工作线程调 ``client.logout()``，结果经信号回主线程。

        与关闭时自动登出（``logout_on_exit``）的区别：这里是**显式退出**，成功后
        连本地保存的账号密码一起清掉，下次启动不应再自动登录。登出请求走工作
        线程（网络 IO，不能卡 UI 线程），严禁在线程里碰 QWidget。
        """
        self._thread_pool().submit(self._account_logout_worker)

    def _account_logout_worker(self):
        """工作线程：串行两步登出（logout.do → authlogout.do），**严禁**操作 QWidget。

        登出失败也要 emit：本地清理与回到登录层必须照常执行（用户点了退出登录，
        不能因为服务端没响应就卡在已登录界面）。
        """
        try:
            logout = getattr(self.client, "logout", None)
            if callable(logout):
                logout()
        except Exception as e:  # noqa: BLE001 - 登出失败不影响本地清理
            logging.debug(f"退出登录请求失败：{e}")
        self._logoutReady.emit()

    def _on_account_logout_ready(self):
        """主线程：清会话与账号密码 → 课程页清空 + 切回登录层 → 弹提示。"""
        if self._setting is not None:
            state.clear_session(self._setting)
            # 显式退出 ≠ 关闭程序：清掉账号密码，下次启动不自动登录
            state.save_account(self._setting, "", "")
        self._clear_session_only()
        self._enter_logged_out()
        logging.info("已退出登录并清除本地会话与保存的账号密码")
        InfoBar.success(
            title="已退出登录",
            content="已清除本地会话与保存的账号密码",
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self._info_parent(),
        )

    def _enter_logged_out(self):
        """进入未登录态：课程页清空 + MainPage 切回登录层并隐藏账号按钮。"""
        self.course_page.reset_to_login()
        if self.main_page is not None:
            self.main_page.on_logged_out()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _thread_pool(self):
        """返回线程池：宿主 ``program.THREAD_POOL``，无宿主时用本地兜底池。

        与 ``CoursePage._thread_pool`` 同一套兜底机制（立即报名是短请求，不占
        用调度器自有池，避免与抢课任务互相饿死）。
        """
        program = self._program
        if program is not None and hasattr(program, "THREAD_POOL"):
            return program.THREAD_POOL
        if self._fallback_pool is None:
            self._fallback_pool = ThreadPoolExecutor(max_workers=1)
        return self._fallback_pool

    def _info_parent(self):
        """InfoBar / 对话框父窗口：插件主页面（MainPage）。

        需求「插件的所有提示要展示在插件的页面上不是主窗口」：跨页提示一律挂
        ``main_page``（随插件页面显示）；MainPage 尚未创建时（纯装配层测试）
        退回任务页自身 —— 仍是插件页面，绝不挂宿主主窗口。
        """
        if self.main_page is not None:
            return self.main_page
        for page in (self.task_page, self.course_page, self.favorites_page):
            if page is not None:
                return page
        return None

    # ------------------------------------------------------------------
    # 关闭时自动退出登录（三层保障）
    # ------------------------------------------------------------------

    def _install_exit_hooks(self):
        """安装关闭登出的第二、三层保障（第一层是 ``addonDelete`` → ``shutdown``）。

        - 第二层：``QApplication.aboutToQuit``（正常退出）；
        - 第三层：宿主主窗口 ``Close`` 事件过滤器（``window`` 非 None 时安装）。
          宿主 ``closeEvent`` 里 ``hideWhenClose`` 为真只是缩到托盘（进程还在，
          不能登出），为假才真的退出 —— 由 ``_CloseEventFilter`` 判断。

        注意（宿主限制）：``program.close()`` 内部直接 ``os._exit(0)``，极端情况下
        （如登出请求卡在网络上）清理来不及执行。因此账号菜单里的「退出登录」
        手动入口必须始终可用，作为保底手段。
        """
        window = self._window
        if window is None:
            # 无宿主窗口（测试环境）：不装全局钩子，避免 XkApp 被 aboutToQuit
            # 长期持有而无法回收
            return
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_about_to_quit)
            self._about_to_quit_connected = True
        self._close_filter = _CloseEventFilter(
            self._setting, self.logout_on_exit, window
        )
        window.installEventFilter(self._close_filter)

    def _uninstall_exit_hooks(self):
        """摘掉关闭钩子（``shutdown`` 调用，避免对象销毁后仍被回调）。"""
        if self._about_to_quit_connected:
            self._about_to_quit_connected = False
            app = QApplication.instance()
            if app is not None:
                try:
                    app.aboutToQuit.disconnect(self._on_about_to_quit)
                except Exception as e:
                    logging.debug("断开 aboutToQuit 失败：%s", e)
        if self._close_filter is not None and self._window is not None:
            try:
                self._window.removeEventFilter(self._close_filter)
            except Exception as e:
                logging.debug("移除关闭事件过滤器失败：%s", e)
            self._close_filter = None

    def _on_about_to_quit(self):
        """``QApplication.aboutToQuit`` 槽：任何异常都不得阻止退出。"""
        try:
            self.logout_on_exit()
        except Exception:
            logging.exception("退出时登出失败")

    def logout_on_exit(self, timeout: float = _LOGOUT_TIMEOUT) -> bool:
        """关闭时退出登录：串行两步，短超时，全异常兜底，返回是否成功。

        规格（网页源码确证）：``POST /student/logout.do``（体 ``studentNumber``）
        → 成功后 ``POST /student/authlogout.do``（体空），由 ``client.logout()``
        封装。**不调用 CAS 登出**（本页 ``loginType='ldap'``，CAS 分支不触发，且会
        踢掉用户统一身份认证的单点登录）。

        设计约束：
        - 未登录（无 token）或客户端不支持 logout → 直接返回，不发请求；
        - 用**独立守护线程**执行并 ``join(timeout)``（默认 3 秒），不依赖宿主
          线程池的生命周期，也绝不把关闭流程拖过 3 秒；
        - 任何异常都吞掉（调用方是关闭流程，抛异常会中断退出）；
        - 只清会话（``state.clear_session`` + 客户端内存会话），**保留本地保存的
          账号密码**，留给下次启动自动登录；
        - ``_logout_done`` 保证三层保障只真正执行一次。
        """
        if self._logout_done:
            return False
        client = self.client
        if not getattr(client, "token", ""):
            return False  # 未登录，没有会话可释放
        logout = getattr(client, "logout", None)
        if not callable(logout):
            return False
        self._logout_done = True

        box = {}

        def worker():
            try:
                box["result"] = logout()
            except Exception as e:  # noqa: BLE001 - 关闭流程吞掉一切
                box["error"] = e

        thread = threading.Thread(target=worker, name="xk-logout", daemon=True)
        try:
            thread.start()
        except Exception as e:
            logging.debug(f"启动登出线程失败：{e}")
            return False
        thread.join(timeout)
        if thread.is_alive():
            logging.warning("退出登录超时（%.1fs），放弃等待", timeout)
        try:
            self._clear_session_only()
        except Exception as e:
            logging.debug(f"清理本地会话失败：{e}")
        result = box.get("result")
        if isinstance(result, (tuple, list)) and result:
            return bool(result[0])
        return bool(result)

    def _clear_session_only(self):
        """只清会话（本地 + 客户端内存），**保留**本地保存的账号密码。"""
        if self._setting is not None:
            state.clear_session(self._setting)
        client = self.client
        if hasattr(client, "clear_session"):
            client.clear_session()
        else:
            client.token = ""
            client.student_code = ""
            session = getattr(client, "session", None)
            if session is not None and hasattr(session, "cookies"):
                session.cookies.clear()

    def shutdown(self):
        """退出登录 + 停止调度器并关闭线程池（供 addonDelete 调用）。

        第一层关闭保障：宿主卸载插件 / 正常关闭时调用。登出失败不影响后续清理。
        """
        try:
            self.logout_on_exit()
        except Exception:
            logging.exception("关闭时退出登录失败")
        self._uninstall_exit_hooks()
        self.scheduler.shutdown()
