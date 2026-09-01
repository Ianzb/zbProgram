"""抢课调度器（计划 todo 15）：定时开始 / 随机延迟 / 重复次数 / 并发上限 / 令牌桶 / QoS 退避 / 失效重登。

线程模型（对齐计划 Execution strategy）：
- 抢课任务跑在**插件自有** ``ThreadPoolExecutor``（不用宿主池：限速与退避会
  ``sleep`` 阻塞线程，占满宿主池会饿死其他插件）；
- 工作线程 → UI 只经 ``pyqtSignal`` 回传，严禁直接操作 QWidget；
- 定时开始用 ``QTimer``（1s 心跳）在主线程倒计时，到点后提交任务。

状态机（每个转换点都明确，避免「成功后又发一次请求」或「停止后仍在跑」）：
- ``WAITING`` → ``RUNNING``：``start()`` 提交任务，工作线程开始循环；
- ``WAITING`` → ``FAILED``：``start()`` 发现当前时间已过 ``end_time``（批次已结束）；
- ``WAITING`` → ``STOPPED``：``stop()`` 在任务尚未提交（倒计时中）时生效；
- ``RUNNING`` → ``SUCCESS``：轮询 ``studentstatus.do`` 得 ``code=="1"``（成功即停）；
- ``RUNNING`` → ``FAILED``：重登失败 / 重复次数耗尽 / 未捕获异常；
- ``RUNNING`` → ``STOPPED``：``stop()`` 置停止标志，工作线程在**下一次尝试前**检查退出；
- ``STOPPED``/``FAILED`` → ``WAITING``：``start()`` 从终态**重启**（清空停止标志、
  复位提交标志、状态回 ``WAITING`` 并发 ``taskStateChanged``）。

停止语义：``stop()`` 置 ``threading.Event``，任务循环在每次尝试前检查；进行中的
请求最多再等一个超时窗口（不强杀线程）。等待间隔用 ``event.wait`` 实现，停止可
立即打断睡眠。

语义约定（已与用户确认）：``repeat=0`` 表示不限次数（成功即停）；``repeat>0``
表示**每轮**最大尝试次数（``start()`` 重启即开启新一轮）；**成功即停**与重复次数
无关。``attempts`` 是累计值，重启**不归零**（继续计数），``attempts_base`` 记录
本轮起点；若要从头开始，调用方应先 ``remove()`` 再 ``add_task()``。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from qtpy.QtCore import QCoreApplication, QObject, QTimer, Signal

from . import settings as core_settings
from . import state

logger = logging.getLogger(__name__)

# 与参考脚本 xk_quick.py 对齐的速率控制常量
MIN_INTERVAL = 0.35  # 全局最小请求间隔(秒)，约 2.8 req/s

# 「选课数量过多」类提示 → **立即终态失败，不再重试**。
#
# 这类提示是**账户级**限选（本轮次/本类别可选门数已用完），重试既不可能成功，
# 又会在服务端留下高频请求记录（有风控风险），所以命中即停。
#
# 查证说明（2026-08-31）：在网页 JS（``选课_files/*.js``，已解码 ``\uXXXX``）、
# i18n 中文语言包（``[27] response_xkres.nju.edu.cn_message.txt``）、
# ``tests/fixtures/`` 与宿主日志 ``logging.log`` 中**均未找到**该类服务端原文。
# 语言包里只有**课程级/人数级**上限文案（换一门课或等人退课即可重试，不属于
# 账户级限选），**刻意不纳入**本列表，避免误伤：
#   index.onlineNumberLimitError = 在线人数超过上限
#   home.maleLimitWarning        = 男生人数超过上限
#   home.femaleLimitWarning      = 女生人数超过上限
#   home.isFullWarning           = 课程已满，无法选课
#
# 因此下列文案为**保守兜底**的同义子串：文案未经真实响应确认，保留扩展点
# （拿到真实文案后直接往元组里追加即可；判定方式为「msg 包含任一子串」）。
# 注意：不要加入裸的「已达上限」——它会误伤「男生人数已达上限」这类课程级限制。
#
# 2026-08-31 追加（用户实测）：
#   - 用户真实遇到「专业报名不得超过1门」类提示（报名不得超过X门），要求与
#     「立即报名」按钮一致：任务自动完成（失败），不再重试；
#   - 「报名不得超过」为实测句式的精确子串；
#   - 「不得超过」为**更宽的兜底**——用户明确要求该句式一律停止（即使它与
#     某些尚未见过的服务端提示冲突，也以用户需求优先）；
#   - 上述追加与既有排除逻辑（人数/性别类上限提示不在停止模式内）经核对
#     **不冲突**：「男生人数超过上限」「在线人数超过上限」「课程已满」均不含
#     「不得超过」子串，仍按普通重试。
LIMIT_STOP_PATTERNS: tuple[str, ...] = (
    "选课数量过多",
    "选课门数过多",
    "选课数量超过",
    "选课门数超过",
    "选课数量已达上限",
    "选课门数已达上限",
    "选课门数上限",
    "超过限选门数",
    "超出限选门数",
    "超过限选数量",
    "超出限选数量",
    "限选门数",
    "超过限选",
    "超出限选",
    "已达限选上限",
    "超过可选门数",
    "超出可选门数",
    "可选门数已达上限",
    "数量过多",
    "报名不得超过",
    "不得超过",
)

# 倒计时日志节流点（剩余秒数落在这些值上才记一条，避免每秒刷屏）
_COUNTDOWN_LOG_MILESTONES = (60, 30, 10, 5, 1)


def is_limit_stop_msg(msg: Any) -> bool:
    """``msg`` 是否命中「选课数量过多」类账户级限选提示。

    空值/非字符串一律返回 ``False``（拿不到文案就不做停止判定，退回普通重试）。
    """
    if not msg:
        return False
    text = str(msg)
    return any(pattern in text for pattern in LIMIT_STOP_PATTERNS)


class TaskState:
    """任务状态常量。"""

    WAITING = "waiting"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class GrabTask:
    """一个抢课任务：课程参数 + 调度参数 + 运行时状态。"""

    teaching_class_id: str
    course_name: str = ""
    course_kind: str = ""
    teaching_class_type: str = ""
    batch_code: str = ""
    is_choose: str = "0"
    begin_time: str = ""
    end_time: str = ""
    delay_min: float = 1.0
    delay_max: float = 2.0
    repeat: int = 0
    # 新建任务默认开启定时开始（2026-08-31 需求）：批次 begin_time 在未来 →
    # 进倒计时；已过 → start() 立即开抢（_dispatch 语义不变）
    use_timed_start: bool = True
    state: str = TaskState.WAITING
    attempts: int = 0
    attempts_base: int = 0  # 本轮起始尝试次数（start() 重启时置为当前 attempts）
    last_msg: str = ""

    def should_skip(self) -> bool:
        """已在课表（``is_choose=="1"``）视为已完成，不发起请求。"""
        return self.is_choose == "1"


class _RateLimiter:
    """简易令牌桶：保证全局请求间隔 ≥ min_interval 秒（参考 xk_quick.py）。"""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_time = 0.0

    def acquire(self) -> float:
        """等待到可发下一个请求，返回实际等待秒数（供日志记录）。"""
        with self._lock:
            now = time.monotonic()
            wait = self._last_time + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_time = time.monotonic()
        return max(0.0, wait)


class TaskScheduler(QObject):
    """抢课调度器：管理任务表、并发池、令牌桶、定时开始与停止。"""

    taskStateChanged = Signal(str, str)  # (teaching_class_id, state)
    taskProgress = Signal(str, int, int)  # (teaching_class_id, attempts, repeat)
    taskMessage = Signal(str, str)  # (teaching_class_id, msg)
    taskFinished = Signal(str, bool, str)  # (teaching_class_id, ok, msg)

    def __init__(
            self,
            client,
            setting=None,
            program=None,
            min_interval: float = MIN_INTERVAL,
    ):
        super().__init__()
        self._client = client
        self._setting = setting
        self._program = program  # 宿主引用（可为 None，测试环境用本地兜底）
        cfg = self._load_config()
        self._max_workers = max(1, int(cfg.get("max_workers", 3)))
        self._qos_backoff_base = float(cfg.get("qos_backoff_base", 3.0))
        self._qos_backoff_max = float(cfg.get("qos_backoff_max", 15.0))
        self._min_interval = max(0.0, float(min_interval))
        # 插件自有线程池：限速/退避会 sleep 阻塞线程，不用宿主池
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="nju-xk-grab"
        )
        self._rate_limiter = _RateLimiter(self._min_interval)
        self._tasks: Dict[str, GrabTask] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._submitted: Dict[str, bool] = {}
        self._timers: Dict[str, QTimer] = {}
        self._countdown_targets: Dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        if self._setting is not None:
            return state.load_scheduler_config(self._setting)
        return dict(core_settings.DEFAULTS["scheduler"])

    # ------------------------------------------------------------------
    # 任务表管理
    # ------------------------------------------------------------------

    def add_task(self, task: GrabTask) -> bool:
        """加入任务表；重复 id 返回 False。"""
        if not isinstance(task, GrabTask):
            return False
        tid = task.teaching_class_id
        with self._lock:
            if tid in self._tasks:
                return False
            self._tasks[tid] = task
            self._stop_events[tid] = threading.Event()
            self._submitted[tid] = False
        task.state = TaskState.WAITING
        task.attempts = 0
        task.attempts_base = 0
        task.last_msg = ""
        self.taskStateChanged.emit(tid, TaskState.WAITING)
        return True

    def get_task(self, teaching_class_id) -> Optional[GrabTask]:
        with self._lock:
            return self._tasks.get(teaching_class_id)

    def tasks(self) -> list:
        with self._lock:
            return list(self._tasks.values())

    def remove(self, teaching_class_id) -> bool:
        """停止并移除任务；不存在返回 False。"""
        task = self.get_task(teaching_class_id)
        if task is None:
            return False
        self.stop(teaching_class_id)
        with self._lock:
            self._tasks.pop(teaching_class_id, None)
            self._stop_events.pop(teaching_class_id, None)
            self._submitted.pop(teaching_class_id, None)
        self._cancel_timer(teaching_class_id)
        return True

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    def start(self, teaching_class_id) -> bool:
        """启动任务（支持从终态重启：停止后继续 / 失败后重开）。

        边界：
        - 当前时间已过 ``end_time`` → 直接置 ``FAILED``，不发起任何请求；
        - ``use_timed_start`` 且 ``begin_time`` 可解析：未来 → QTimer 倒计时，
          已过期 → 立即开始；不可解析 → 立即开始。

        重启约定：
        - 允许启动：``WAITING``（首次/空闲）、``STOPPED``、``FAILED``；
        - 拒绝：``RUNNING``（已在跑，防重复提交）、``SUCCESS``（已抢到，无需再抢）；
        - 重启时做三件事：清空停止标志 ``_stop_events``、复位 ``_submitted``、
          状态回 ``WAITING`` 并发 ``taskStateChanged``；
        - 重启**保留** ``attempts``（继续计数，不归零），``repeat`` 表示**每轮**
          次数（``attempts_base`` 记录本轮起点）；若要从头开始，调用方应先
          ``remove()`` 再 ``add_task()``。

        返回：``True`` = 已启动/已安排/已判定结束；``False`` = 无法启动。
        """
        task = self.get_task(teaching_class_id)
        if task is None:
            return False
        with self._lock:
            state = task.state
            restarted = False
            if state == TaskState.RUNNING or self._submitted.get(teaching_class_id):
                return False
            if state == TaskState.SUCCESS:
                blocked_by_success = True
            else:
                blocked_by_success = False
                restarted = state in (TaskState.STOPPED, TaskState.FAILED)
                if restarted:
                    # 终态重启：清空停止标志，否则新一轮会立刻退出
                    event = self._stop_events.get(teaching_class_id)
                    if event is not None:
                        event.clear()
                    task.attempts_base = task.attempts
                    task.state = TaskState.WAITING
                self._submitted[teaching_class_id] = True
        if blocked_by_success:
            self.taskMessage.emit(teaching_class_id, "任务已成功，无需再抢")
            return False
        if restarted:
            self.taskStateChanged.emit(teaching_class_id, TaskState.WAITING)
        return self._dispatch(task)

    def _dispatch(self, task: GrabTask) -> bool:
        """按任务当前参数决定「直接失败 / 倒计时 / 立即提交」。

        供 ``start()`` 与 ``set_timed_start()`` 共用，保证两条路径的判定一致。
        """
        # 时间窗边界：批次已结束 → 直接失败，不发起请求
        end_dt = self._parse_time(task.end_time)
        if end_dt is not None and time.time() > end_dt.timestamp():
            logger.warning(
                "任务失败: tid=%s 累计尝试=%d 原因=该批次已结束(end=%s)",
                task.teaching_class_id, task.attempts, task.end_time,
            )
            self._finish(task, TaskState.FAILED, False, "该批次已结束")
            return True

        # 定时开始
        if task.use_timed_start:
            begin_dt = self._parse_time(task.begin_time)
            if begin_dt is not None:
                if time.time() >= begin_dt.timestamp():
                    self._submit(task)
                else:
                    self._start_countdown(task, begin_dt)
                return True

        self._submit(task)
        return True

    def start_all(self) -> int:
        """启动所有**空闲**任务；已在跑/已终态的跳过（重启是显式单任务动作）。"""
        started = 0
        for tid in list(self._tasks.keys()):
            task = self.get_task(tid)
            if task is None or task.state != TaskState.WAITING:
                continue
            if self.start(tid):
                started += 1
        return started

    def stop(self, teaching_class_id) -> bool:
        """置停止标志；任务在下次尝试前检查并退出（不强杀线程）。"""
        task = self.get_task(teaching_class_id)
        if task is None:
            return False
        event = self._stop_events.get(teaching_class_id)
        if event is not None:
            event.set()
        self._cancel_timer(teaching_class_id)
        with self._lock:
            waiting = task.state == TaskState.WAITING
        if waiting:
            # 尚未提交（倒计时中）：直接置 STOPPED
            logger.info(
                "任务停止: tid=%s 累计尝试=%d 原因=倒计时中收到停止信号",
                teaching_class_id, task.attempts,
            )
            self._finish(task, TaskState.STOPPED, False, "已停止")
        return True

    def stop_all(self):
        for tid in list(self._tasks.keys()):
            self.stop(tid)

    # ------------------------------------------------------------------
    # 即时生效的参数更新（供 UI 直接调用，无需重新点「开始」）
    # ------------------------------------------------------------------

    def set_timed_start(self, teaching_class_id, enabled: bool) -> bool:
        """即时切换「定时开始」开关（UI 开关切换后无需重新点「开始」）。

        ``enabled=True``：
        - 任务**运行中** → 只保存设置，不打断当前轮次，返回 True 并提示
          「任务运行中，定时设置已保存，下次启动生效」；
        - 任务**空闲**（WAITING/STOPPED/FAILED）→ ``begin_time`` 可解析且在
          未来则取消旧定时器并立即进入倒计时（提示「将在 N 秒后开始」），
          已过期或不可解析则立即提交开抢；
        - 任务已 ``SUCCESS`` → 返回 False（已抢到，无需再抢）。

        ``enabled=False``：
        - 正在**倒计时**（尚未提交）→ 取消定时器、复位提交标志，状态回到
          ``WAITING``，提示「已取消定时，等待手动开始」；
        - 任务**运行中** → 不打断，继续抢，返回 True。

        全程持 ``self._lock`` 读写状态，且复用 ``_submitted`` 保证不重复提交。
        """
        task = self.get_task(teaching_class_id)
        if task is None:
            return False
        enabled = bool(enabled)
        with self._lock:
            task.use_timed_start = enabled
            state = task.state
            submitted = bool(self._submitted.get(teaching_class_id))
            counting_down = (
                    state == TaskState.WAITING
                    and submitted
                    and teaching_class_id in self._timers
            )

        if not enabled:
            if counting_down:
                with self._lock:
                    self._submitted[teaching_class_id] = False
                self._cancel_timer(teaching_class_id)
                self.taskMessage.emit(teaching_class_id, "已取消定时，等待手动开始")
            return True

        if state == TaskState.RUNNING:
            self.taskMessage.emit(
                teaching_class_id, "任务运行中，定时设置已保存，下次启动生效"
            )
            return True
        if state == TaskState.SUCCESS:
            self.taskMessage.emit(teaching_class_id, "任务已成功，无需再抢")
            return False

        begin_dt = self._parse_time(task.begin_time)
        future = begin_dt is not None and time.time() < begin_dt.timestamp()
        if counting_down:
            # 已在倒计时：先撤旧定时器，再按（可能刚更新的）begin_time 重新判定
            self._cancel_timer(teaching_class_id)
            if future:
                self._announce_countdown(teaching_class_id, begin_dt)
            return self._dispatch(task)
        if future:
            self._announce_countdown(teaching_class_id, begin_dt)
        return self.start(teaching_class_id)

    # 允许 set_task_params 即时更新的字段
    _UPDATABLE_PARAMS = (
        "delay_min", "delay_max", "repeat", "use_timed_start", "begin_time",
    )

    def set_task_params(self, teaching_class_id, **params) -> bool:
        """即时更新任务参数（运行中/等待中都可调，下一次尝试即生效）。

        支持：``delay_min`` / ``delay_max`` / ``repeat`` / ``use_timed_start`` /
        ``begin_time``。非法值（负数、非数字）忽略；``delay_min > delay_max``
        时自动交换。未知键忽略。任务不存在返回 False。

        注意：``use_timed_start`` 这里只写值；若需要「开关即时生效」的调度语义
        （进入/取消倒计时），请调用 :meth:`set_timed_start`。
        """
        task = self.get_task(teaching_class_id)
        if task is None:
            return False
        with self._lock:
            for key in self._UPDATABLE_PARAMS:
                if key not in params:
                    continue
                value = self._coerce_param(key, params[key])
                if value is None:
                    continue
                setattr(task, key, value)
            if task.delay_min > task.delay_max:
                task.delay_min, task.delay_max = task.delay_max, task.delay_min
        return True

    @staticmethod
    def _coerce_param(key: str, value):
        """把外部传入的参数值规整为字段类型；非法返回 ``None``（忽略）。"""
        if key in ("delay_min", "delay_max"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None
        if key == "repeat":
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None
        if key == "use_timed_start":
            return bool(value)
        if key == "begin_time":
            return "" if value is None else str(value)
        return None

    def shutdown(self):
        """停止所有任务并关闭线程池（供 addonDelete 调用）。"""
        self._shutdown = True
        for tid in list(self._tasks.keys()):
            event = self._stop_events.get(tid)
            if event is not None:
                event.set()
            self._cancel_timer(tid)
        self._pool.shutdown(wait=False)

    # ------------------------------------------------------------------
    # 定时开始（QTimer 1s 心跳）
    # ------------------------------------------------------------------

    def _start_countdown(self, task: GrabTask, begin_dt: datetime):
        tid = task.teaching_class_id
        if QCoreApplication.instance() is None:
            # 无事件循环（纯测试/无 Qt 宿主）：退化为立即开始
            self._submit(task)
            return
        self._cancel_timer(tid)
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(lambda: self._on_countdown_tick(tid))
        with self._lock:
            self._timers[tid] = timer
            self._countdown_targets[tid] = begin_dt
        logger.info(
            "倒计时开始: tid=%s 目标=%s 剩余=%ds",
            tid, begin_dt.strftime("%Y-%m-%d %H:%M:%S"),
            max(0, int(begin_dt.timestamp() - time.time())),
        )
        timer.start()
        self._on_countdown_tick(tid)

    def _on_countdown_tick(self, tid: str):
        task = self.get_task(tid)
        if task is None:
            self._cancel_timer(tid)
            return
        if self._is_stopped(tid):
            self._cancel_timer(tid)
            self._finish(task, TaskState.STOPPED, False, "已停止")
            return
        target = self._countdown_targets.get(tid)
        if target is None:
            self._cancel_timer(tid)
            return
        remaining = target.timestamp() - time.time()
        if remaining <= 0:
            logger.info("倒计时结束，立即提交开抢: tid=%s", tid)
            self._cancel_timer(tid)
            self._submit(task)
            return
        # 倒计时期间批次结束
        end_dt = self._parse_time(task.end_time)
        if end_dt is not None and time.time() > end_dt.timestamp():
            logger.warning("倒计时期间批次已结束: tid=%s end=%s", tid, task.end_time)
            self._cancel_timer(tid)
            self._finish(task, TaskState.FAILED, False, "该批次已结束")
            return
        # 节流：只在里程碑秒数记一条，避免每秒刷屏
        seconds_left = int(remaining)
        if seconds_left in _COUNTDOWN_LOG_MILESTONES:
            logger.info("倒计时: tid=%s 剩余=%ds", tid, seconds_left)
        self.taskMessage.emit(tid, f"倒计时 {seconds_left}s")

    def _announce_countdown(self, tid: str, begin_dt: datetime):
        """进入倒计时前发一条中文提示（UI 直接展示）。"""
        remaining = max(0, int(begin_dt.timestamp() - time.time()))
        self.taskMessage.emit(tid, f"将在 {remaining} 秒后开始")

    def _cancel_timer(self, tid: str):
        with self._lock:
            timer = self._timers.pop(tid, None)
            self._countdown_targets.pop(tid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------

    def _submit(self, task: GrabTask):
        if self._shutdown:
            return
        self._pool.submit(self._run_task, task)

    def _run_task(self, task: GrabTask):
        try:
            self._run_task_inner(task)
        except Exception as e:
            task.last_msg = f"任务异常: {e}"
            logger.exception(
                "任务未捕获异常: tid=%s 累计尝试=%d 异常=%s",
                task.teaching_class_id, task.attempts, e,
            )
            self._finish(task, TaskState.FAILED, False, task.last_msg)

    def _run_task_inner(self, task: GrabTask):
        tid = task.teaching_class_id
        self._set_state(task, TaskState.RUNNING)
        logger.info(
            "任务启动: tid=%s 课程=%s 课程类型=%s 教学班类型=%s 批次码=%s "
            "定时开始=%s 重复次数=%s",
            tid, task.course_name or "-", task.course_kind or "-",
                 task.teaching_class_type or "-", task.batch_code or "-",
            bool(task.use_timed_start), task.repeat,
        )
        qos_hit_count = 0
        while True:
            # 停止检查（每次尝试前）
            if self._is_stopped(tid):
                logger.info(
                    "任务停止: tid=%s 累计尝试=%d 原因=收到停止信号",
                    tid, task.attempts,
                )
                self._finish(task, TaskState.STOPPED, False, "已停止")
                return
            # 已在课表检查
            if task.should_skip():
                logger.info(
                    "任务结束: tid=%s 累计尝试=%d 结果=已在课表中，跳过请求",
                    tid, task.attempts,
                )
                self._finish(task, TaskState.SUCCESS, True, "已在课表中")
                return
            # 重复次数检查（repeat 表示「每轮」次数，attempts_base 为本轮起点）
            if task.repeat > 0 and task.attempts - task.attempts_base >= task.repeat:
                logger.warning(
                    "任务失败: tid=%s 累计尝试=%d 原因=已达最大尝试次数(%d)",
                    tid, task.attempts, task.repeat,
                )
                self._finish(task, TaskState.FAILED, False, "已达最大尝试次数")
                return

            outcome, msg = self._do_attempt(task)
            if outcome != "stopped":
                task.attempts += 1
                self.taskProgress.emit(tid, task.attempts, task.repeat)
            logger.debug(
                "尝试结束: tid=%s 第%d次 结果=%s msg=%s",
                tid, task.attempts, outcome, msg,
            )

            if outcome == "success":
                logger.info(
                    "任务成功: tid=%s 累计尝试=%d msg=%s", tid, task.attempts, msg,
                )
                self._finish(task, TaskState.SUCCESS, True, msg)
                return
            if outcome == "failed":
                logger.warning(
                    "任务失败: tid=%s 累计尝试=%d 原因=%s", tid, task.attempts, msg,
                )
                self._finish(task, TaskState.FAILED, False, msg)
                return
            if outcome == "stopped":
                logger.info(
                    "任务停止: tid=%s 累计尝试=%d 原因=%s", tid, task.attempts, msg,
                )
                self._finish(task, TaskState.STOPPED, False, msg)
                return

            # retry / qos → 决定间隔
            if outcome == "qos":
                qos_hit_count += 1
                backoff = min(
                    self._qos_backoff_base * (2 ** (qos_hit_count - 1)),
                    self._qos_backoff_max,
                )
                jitter = random.uniform(0, backoff * 0.3)
                delay = backoff + jitter
                logger.warning(
                    "服务器繁忙(QoS): tid=%s 连续命中=%d 退避=%.1fs(上限%.1fs)",
                    tid, qos_hit_count, delay, self._qos_backoff_max,
                )
                self.taskMessage.emit(tid, f"服务器繁忙，退避 {delay:.1f}s")
                self._sleep_interruptible(tid, delay)
            else:
                qos_hit_count = max(0, qos_hit_count - 1)  # 成功后逐步恢复
                delay = random.uniform(
                    max(0.0, float(task.delay_min)),
                    max(0.0, float(task.delay_max)),
                )
                logger.debug("正常间隔: tid=%s 计划 sleep=%.2fs", tid, delay)
                self._sleep_interruptible(tid, delay)

    def _do_attempt(self, task: GrabTask, allow_relogin: bool = True):
        """执行一次尝试，返回 (outcome, msg)。

        outcome: ``"success"`` / ``"failed"`` / ``"stopped"`` / ``"retry"`` / ``"qos"``
        """
        tid = task.teaching_class_id
        attempt_no = task.attempts + 1
        # 全局令牌桶限速
        waited = self._rate_limiter.acquire()
        logger.debug(
            "限速等待: tid=%s 第%d次尝试 等待=%.3fs", tid, attempt_no, waited,
        )
        if self._is_stopped(tid):
            logger.debug("限速等待后检测到停止标志: tid=%s 第%d次尝试", tid, attempt_no)
            return "stopped", "已停止"

        logger.debug("调用 volunteer: tid=%s 第%d次尝试", tid, attempt_no)
        try:
            resp = self._client.volunteer(
                teaching_class_id=task.teaching_class_id,
                course_kind=task.course_kind,
                teaching_class_type=task.teaching_class_type,
                batch_code=task.batch_code,
            )
        except Exception as e:
            task.last_msg = f"网络异常: {e}"
            logger.warning(
                "volunteer 异常: tid=%s 第%d次尝试 异常=%s", tid, attempt_no, e,
            )
            self.taskMessage.emit(tid, task.last_msg)
            return "qos", task.last_msg

        # 会话失效 → 自动重新登录一次后立即重试本次尝试（不消耗额外延迟）
        if self._client.is_session_expired(resp):
            logger.warning("会话失效: tid=%s 第%d次尝试", tid, attempt_no)
            if not allow_relogin:
                task.last_msg = "会话失效，重登后仍失效"
                logger.warning("重登后会话仍失效: tid=%s", tid)
                self.taskMessage.emit(tid, task.last_msg)
                return "retry", task.last_msg
            self.taskMessage.emit(tid, "会话失效，正在重新登录...")
            ok, msg = self._relogin()
            if not ok:
                task.last_msg = f"重新登录失败: {msg}"
                logger.error("重新登录失败: tid=%s 原因=%s", tid, msg)
                self.taskMessage.emit(tid, task.last_msg)
                return "failed", task.last_msg
            logger.info("重新登录成功: tid=%s，立即重试本次尝试", tid)
            self.taskMessage.emit(tid, "重新登录成功，立即重试")
            return self._do_attempt(task, allow_relogin=False)

        if not isinstance(resp, dict):
            task.last_msg = f"响应格式异常: {resp!r}"
            logger.warning("响应格式异常: tid=%s resp=%r", tid, resp)
            self.taskMessage.emit(tid, task.last_msg)
            return "retry", task.last_msg

        code = str(resp.get("code", ""))
        msg = str(resp.get("msg", ""))
        logger.info(
            "volunteer 响应: tid=%s 第%d次尝试 code=%s msg=%s",
            tid, attempt_no, code, msg,
        )

        # 账户级限选（「选课数量过多」类）→ 立即终态，不再重试。
        # 判定位置：会话失效重登之后、QoS 退避/正常重试之前——既不是会话问题，
        # 也不是临时繁忙，重试无意义且可能触发风控。
        if is_limit_stop_msg(msg):
            logger.warning(
                "检测到限选类提示，立即停止: tid=%s 第%d次尝试 msg=%s",
                tid, attempt_no, msg,
            )
            task.last_msg = msg
            self.taskMessage.emit(tid, msg)
            return "failed", msg

        if code == "1":
            # volunteer.do 返回 code="1" 只表示已入队，需轮询真实结果
            logger.debug("已入队，开始轮询: tid=%s 第%d次尝试", tid, attempt_no)
            try:
                poll = self._client.poll_result(teaching_class_id=task.teaching_class_id)
            except Exception as e:
                task.last_msg = f"轮询异常: {e}"
                logger.warning("轮询异常: tid=%s 异常=%s", tid, e)
                self.taskMessage.emit(tid, task.last_msg)
                return "retry", task.last_msg
            poll_code = str(poll.get("code", "")) if isinstance(poll, dict) else ""
            poll_msg = str(poll.get("msg", "")) if isinstance(poll, dict) else ""
            logger.info(
                "轮询结果: tid=%s 第%d次尝试 code=%s msg=%s",
                tid, attempt_no, poll_code, poll_msg,
            )
            # 轮询结果里的限选文案同样立即停止（服务端常在异步结果里才给出原因）
            if is_limit_stop_msg(poll_msg):
                logger.warning(
                    "检测到限选类提示，立即停止: tid=%s 轮询 msg=%s", tid, poll_msg,
                )
                task.last_msg = poll_msg
                self.taskMessage.emit(tid, poll_msg)
                return "failed", poll_msg
            if poll_code == "1":
                task.last_msg = poll_msg or "选课成功"
                logger.info("轮询确认选课成功: tid=%s msg=%s", tid, task.last_msg)
                return "success", task.last_msg
            if poll_code == "-1":
                task.last_msg = f"选课失败: {poll_msg}"
                logger.info("轮询判定选课失败: tid=%s msg=%s", tid, poll_msg)
                self.taskMessage.emit(tid, task.last_msg)
                return "retry", task.last_msg
            if poll_code == "timeout":
                task.last_msg = "未能确认结果"
                logger.warning("轮询超时，未能确认结果: tid=%s", tid)
                self.taskMessage.emit(tid, task.last_msg)
                return "retry", task.last_msg
            task.last_msg = f"轮询未知状态: code={poll_code}"
            logger.warning("轮询未知状态: tid=%s code=%s", tid, poll_code)
            self.taskMessage.emit(tid, task.last_msg)
            return "retry", task.last_msg

        if "NullPointer" in msg:
            task.last_msg = "服务器繁忙"
            logger.warning("服务器繁忙(NullPointer): tid=%s 第%d次尝试", tid, attempt_no)
            self.taskMessage.emit(tid, task.last_msg)
            return "qos", task.last_msg

        task.last_msg = msg or f"返回 code={code}"
        logger.debug(
            "本次尝试未成功，准备重试: tid=%s 第%d次尝试 code=%s msg=%s",
            tid, attempt_no, code, task.last_msg,
        )
        self.taskMessage.emit(tid, task.last_msg)
        return "retry", task.last_msg

    def _relogin(self):
        """凭据从 ``state.load_account(setting)`` 取，重新登录一次。"""
        if self._setting is None:
            return False, "无凭据（setting 为空）"
        user, pwd = state.load_account(self._setting)
        if not user or not pwd:
            return False, "未配置账号"
        try:
            return self._client.login(user, pwd)
        except Exception as e:
            return False, f"登录异常: {e}"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _set_state(self, task: GrabTask, new_state: str):
        with self._lock:
            task.state = new_state
        self.taskStateChanged.emit(task.teaching_class_id, new_state)

    def _finish(self, task: GrabTask, state: str, ok: bool, msg: str):
        """置终态并通知；幂等（已终态则忽略，避免重复通知）。"""
        with self._lock:
            if task.state in (TaskState.SUCCESS, TaskState.FAILED, TaskState.STOPPED):
                logger.debug(
                    "终态已存在，忽略重复通知: tid=%s 当前=%s 忽略=%s msg=%s",
                    task.teaching_class_id, task.state, state, msg,
                )
                return
            task.state = state
            task.last_msg = msg
            self._submitted[task.teaching_class_id] = False
        logger.debug(
            "置终态: tid=%s state=%s ok=%s 累计尝试=%d msg=%s",
            task.teaching_class_id, state, ok, task.attempts, msg,
        )
        self.taskStateChanged.emit(task.teaching_class_id, state)
        self.taskFinished.emit(task.teaching_class_id, ok, msg)

    def _is_stopped(self, tid: str) -> bool:
        event = self._stop_events.get(tid)
        return event is not None and event.is_set()

    def _sleep_interruptible(self, tid: str, seconds: float) -> float:
        """可被 ``stop()`` 打断的睡眠：停止标志置位时立即返回实际睡眠秒数。"""
        start = time.monotonic()
        event = self._stop_events.get(tid)
        if event is not None:
            event.wait(seconds)
        else:
            time.sleep(seconds)
        elapsed = time.monotonic() - start
        logger.debug(
            "间隔等待结束: tid=%s 计划=%.2fs 实际=%.2fs", tid, seconds, elapsed,
        )
        return elapsed

    @staticmethod
    def _parse_time(text: str) -> Optional[datetime]:
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
