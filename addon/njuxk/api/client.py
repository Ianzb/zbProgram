"""选课系统 API 客户端（会话/登录/批次/系统参数/课程查询/报名/结果轮询）。

请求保真约定（与抓包逐字段一致，见 .omo/plans/nju-xk-gui.md todo 7）：
- 会话初始化：GET /（抓包 #23，取 JSESSIONID），失败回退 GET {APP}/*default/index.do；
- 验证码：POST vcode.do，取 data.uuid 与 data.vode（去掉 ``data:image/gif;base64,`` 前缀）；
- 登录：坐标格式 ``f"{x}-{int(y*5/6)}"`` 逗号连接，表单
  ``loginName/loginPwd/verifyCode/vtoken=""/uuid``；验证码识别失败换新验证码重试；
- 登录后所有请求带 ``token`` 头、``language: zh_cn``、``Referer`` 指向
  ``{APP}/*default/grablessons.do?token=``；
- 课程查询：``teachingClassType=="ZY"`` → programCourse.do（queryContent 形如
  ``ZXZY:{selectMajor},ZXNJ:{grade},``，取自 fetch_student()），否则 → publicCourse.do
  （带 ``checkConflict="2"``、``checkCapacity="2"``）；
- 分页判定：``is_last = 本页行数 < page_size``，严禁读取 totalCount（抓包中恒为 0）；
- 报名：volunteer.do 表单 ``addParam=AES(encrypt_add_param(payload)) + studentCode``，
  ``course_kind`` 原样传字符串（可为 ``"6,7"``），禁止转 int；
- 退课：deleteVolunteer.do 表单 ``deleteParam=AES(...) + studentCode``，载荷
  ``operationType="2"``、``isMajor="1"``（网页 JS 硬编码字面量），**不含**
  courseKind / teachingClassType（只有报名才有）；
- 结果轮询：studentstatus.do，code ``0``=处理中 / ``1``=成功 / ``-1``=失败 / timeout；
  ``type`` 区分操作：``"1"``=报名后轮询、``"0"``=退课后轮询（网页源码确证）；
- 课程详情：querykcxx.do 表单 ``kch/jxbid/xklcdm``（抓包 [12]）、
  courseSchedule.do 表单 ``querySetting={data:{studentCode, other, electiveBatchCode}}``
  （抓包 [14]），响应原样返回 dict，编码键映射表见 ``ui/cards.py``；
- 退出登录：串行两步 ``student/logout.do``（表单 ``studentNumber``）→ 成功后
  ``student/authlogout.do``（空表单），均带 token 头；**不调用 CAS 登出**
  （本页 ``loginType='ldap'``，CAS 分支不触发，且会踢掉统一身份认证单点登录）。

线程安全：本类实例被调度器自有线程池与宿主 ``program.THREAD_POOL`` 共享，故所有
共享可变状态（token / student_code / cookies / 认证头 / 学生信息缓存）的写入都加
``self._lock``；``_post`` 整个方法体加锁以序列化请求发送。锁只包住状态写入与单次
请求发送，不包住整个网络调用链（如 ``login`` 只在写 token 的那一刻加锁，不在持锁时
调用 ``_post``），避免不必要的串行化与死锁。

HTTP 层：走 **zbToolLib**（与程序本体和其他插件用法一致，``zb.postUrl`` /
``zb.getUrl``，自带「正在Post请求…/成功/失败」日志）。zbToolLib 内部用自己的全局
Session（verify=False、挂宿主 REQUEST_HEADER），与本类**不同源**，因此：
- cookie 用显式字典 ``self._cookies`` 管理，每次响应后回读合并 ``resp.cookies``；
- 请求头（UA / token / language / Referer）逐请求传入，不依赖 Session 常驻头。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import zbToolLib as zb

from .crypto import encrypt_add_param, encrypt_password
from ..captcha.solver import solve_captcha_from_base64

BASE = "https://xk.nju.edu.cn"
APP = "/xsxkapp/sys/xsxkapp"

# 端点（相对 {BASE}{APP}）
ELECTIVE_BATCH = "/elective/batch.do"
SYSPARAM = "/publicinfo/sysparam.do"
VCODE = "/student/4/vcode.do"
LOGIN = "/student/check/login.do"
STUDENT = "/student/{code}.do"
PROGRAM_COURSE = "/elective/programCourse.do"
PUBLIC_COURSE = "/elective/publicCourse.do"
VOLUNTEER = "/elective/volunteer.do"
DELETE_VOLUNTEER = "/elective/deleteVolunteer.do"
STUDENT_STATUS = "/elective/studentstatus.do"
COURSE_INFO = "/publicinfo/querykcxx.do"
COURSE_SCHEDULE = "/elective/courseSchedule.do"
COURSE_RESULT = "/elective/courseResult.do"
LOGOUT = "/student/logout.do"
AUTH_LOGOUT = "/student/authlogout.do"

# 与抓包一致的 Chrome UA（抓包 #23/#31 等）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"
)

# 语言包错误文案映射（登录失败 msg → 中文原因）
_LOGIN_ERROR_MAP = {
    "index.nameMessageError": "登录名或密码不正确",
    "index.verificationCodeError": "验证码不正确",
    "index.verificationCodeExpired": "验证码失效",
    "index.onlineNumberLimitError": "在线人数超过上限",
}


class XkClient:
    """选课系统 API 客户端。

    HTTP 层基于 zbToolLib（``zb.postUrl`` / ``zb.getUrl``，自带请求日志），登录后
    持有 ``token`` 与 ``student_code``，所有业务请求自动携带认证头；cookie 用显式
    字典管理（zbToolLib 的全局 Session 与本类不同源，见模块 docstring）。
    """

    def __init__(self) -> None:
        # 可重入锁：保护共享可变状态与请求发送（见模块 docstring「线程安全」）
        self._lock = threading.RLock()
        self.token = ""
        self.student_code = ""
        self._student_info: Optional[Dict[str, Any]] = None
        # 凭据 provider（() -> (user, pwd)）：会话失效时请求函数层自动重登用；
        # 由装配层经 set_credential_provider 注入（XkApp 接 state.load_account）
        self._credential_provider: Optional[Any] = None
        # cookie 显式字典：zbToolLib 用自己的全局 Session（verify=False、挂宿主
        # REQUEST_HEADER），Set-Cookie 不会自动进本类，必须显式传并回读合并
        self._cookies: Dict[str, str] = {}
        # 请求头：基础头（与抓包一致）+ 登录后认证头，逐请求传给 zb.postUrl/getUrl
        self._base_headers: Dict[str, str] = {
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": BASE,
            "Referer": f"{BASE}/",
        }
        self._auth_headers: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def init_session(self) -> bool:
        """初始化会话：GET /（抓包 #23，取 JSESSIONID）。

        失败回退 GET {APP}/*default/index.do（参考脚本行为）。
        两者都失败时抛出可捕获异常。
        """
        try:
            self._get(f"{BASE}/", timeout=10)
            logging.info("初始化会话成功")
            return True
        except Exception as e:
            logging.info("初始化会话失败，回退 index.do：%s", e)
            try:
                self._get(f"{BASE}{APP}/*default/index.do", timeout=10)
                logging.info("初始化会话成功（index.do 回退）")
                return True
            except Exception as e2:
                raise RuntimeError(f"初始化会话失败: {e2}") from e2

    def set_credential_provider(self, provider) -> None:
        """注入凭据 provider（``() -> (user, pwd)``），供会话失效时自动重登。

        装配层接线为 ``lambda: state.load_account(setting)``；不注入则会话
        失效时原样返回失效响应，由上层既有处理兜底。
        """
        self._credential_provider = provider

    def _try_relogin(self) -> bool:
        """用凭据 provider 自动重登一次；成功 True，失败/无凭据 False。

        只读学号用于日志，严禁记录密码 / token / cookie。
        """
        provider = self._credential_provider
        user = pwd = ""
        if provider is not None:
            try:
                user, pwd = provider() or ("", "")
            except Exception as e:
                logging.warning("读取本地凭据失败，跳过自动重登：%s", e)
                return False
        if not user or not pwd:
            logging.info("会话失效但无可用凭据，跳过自动重登")
            return False
        try:
            ok, msg = self.login(user, pwd)
        except Exception as e:
            logging.warning("自动重登异常：%s", e)
            return False
        if ok:
            logging.info("会话失效，已自动重新登录（学号=%s）", user)
            return True
        logging.info("自动重登失败：%s", msg)
        return False

    def _apply_auth_headers(self) -> None:
        """登录后设置认证头：token / language / Referer(grablessons.do?token=)。

        zbToolLib 的请求头逐请求传入（其全局 Session 挂的是宿主通用
        REQUEST_HEADER），认证头保存在 ``self._auth_headers``，由
        ``_request_headers`` 与基础头合并。
        """
        with self._lock:
            self._auth_headers["token"] = self.token
            self._auth_headers["language"] = "zh_cn"
            self._auth_headers["Referer"] = (
                f"{BASE}{APP}/*default/grablessons.do?token={self.token}"
            )

    def _request_headers(self) -> Dict[str, str]:
        """合并基础头与认证头（调用方多已持锁，这里独立加锁保幂等）。"""
        with self._lock:
            headers = dict(self._base_headers)
            headers.update(self._auth_headers)
            return headers

    def _merge_cookies(self, resp: Any) -> None:
        """把响应 Set-Cookie 合并进显式 cookie 字典（须在持锁状态下调用）。"""
        self._cookies.update(resp.cookies.get_dict())

    def _get(self, url: str, timeout: int = 10) -> Any:
        """GET url（zbToolLib getUrl，times=1），返回响应并合并 cookie。

        ``times=1`` 的取舍见 ``_post`` docstring：内部重试会绕过令牌桶限速。
        """
        with self._lock:
            resp = zb.getUrl(
                url,
                times=1,
                timeout=timeout,
                headers=self._request_headers(),
                cookies=dict(self._cookies),
            )
            if resp is None:
                raise RuntimeError(f"网络请求失败: {url}")
            resp.raise_for_status()
            self._merge_cookies(resp)
            return resp

    def clear_session(self) -> None:
        """清空内存会话（token / student_code / cookies / 认证头 / 学生信息缓存）。

        供登出收尾（``core.app._clear_session_only`` / ``course.reset_to_login``）
        探测调用；不动本地保存的账号密码。
        """
        with self._lock:
            self.token = ""
            self.student_code = ""
            self._cookies = {}
            self._auth_headers = {}
            self._student_info = None

    def export_session(self) -> Dict[str, Any]:
        """导出会话（cookies + token + student_code），供持久化。"""
        with self._lock:
            return {
                "cookies": dict(self._cookies),
                "token": self.token,
                "student_code": self.student_code,
            }

    def import_session(
            self,
            cookies: Dict[str, str],
            token: str,
            student_code: str = "",
    ) -> None:
        """导入会话（cookies + token），恢复登录态。"""
        with self._lock:
            self._cookies = dict(cookies or {})
            self.token = token or ""
            self.student_code = student_code or ""
            if self.token:
                self._apply_auth_headers()

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------

    def fetch_captcha(self, allow_relogin: bool = True) -> Tuple[str, str]:
        """获取点选验证码，返回 (uuid, gif_b64)。

        gif_b64 已去掉 ``data:image/gif;base64,`` 前缀，可直接交给 solver。
        ``allow_relogin`` 由 :meth:`login` 传 False（登录流程内部严禁再触发
        自动重登，否则 302 → login → 302 会无限递归）。
        """
        data = self._post(VCODE, allow_relogin=allow_relogin)
        node = data.get("data") or {}
        uuid = node.get("uuid")
        vode = node.get("vode") or node.get("vcode")
        if not uuid or not vode:
            raise ValueError(f"验证码响应缺少 uuid/vode: {data}")
        gif_b64 = vode.split(",", 1)[1] if "," in vode else vode
        return uuid, gif_b64

    def login(self, user: str, pwd: str, max_retries: int = 3) -> Tuple[bool, str]:
        """执行登录（验证码识别失败或服务端拒绝时换新验证码重试，最多 max_retries 次）。

        与参考脚本 authenticator.py 一致：只在成功时 return，失败进入下一轮换新验证码重试
        （验证码识别本身不完美，点错位置/识别失败都应重试而非放弃）。
        例外：凭据类错误（登录名或密码不正确 / 在线人数超过上限）重试无意义，立即返回。

        返回 (ok, msg)：成功时保存 self.token / self.student_code 并返回
        (True, "登录成功")；失败返回 (False, 中文原因)。
        网络异常等直接抛出（由调用方捕获）。
        """
        encrypted_pwd = encrypt_password(pwd)
        last_msg = ""
        for _ in range(max_retries):
            # 登录流程内部严禁触发自动重登（302 → login → 302 无限递归）
            uuid, gif_b64 = self.fetch_captcha(allow_relogin=False)
            points = solve_captcha_from_base64(gif_b64)
            if not points:
                # 验证码识别偶发失败：换新验证码重试
                last_msg = "验证码识别失败"
                continue
            verify_code = ",".join(f"{int(p[0])}-{int(p[1] * 5 / 6)}" for p in points)
            payload = {
                "loginName": user,
                "loginPwd": encrypted_pwd,
                "verifyCode": verify_code,
                "vtoken": "",
                "uuid": uuid,
            }
            data = self._post(
                LOGIN, data=payload, timeout=15, allow_relogin=False
            )
            code = str(data.get("code", ""))
            node = data.get("data") or {}
            if code == "1" and str(node.get("number")) == str(user):
                # 只在写状态的这一刻加锁：不把 _post 等网络 I/O 包进临界区，
                # 避免持锁等待网络（也避免与 _post 自身的加锁形成长临界区）
                with self._lock:
                    self.token = node.get("token") or ""
                    self.student_code = str(node.get("number") or "")
                self._apply_auth_headers()
                # 日志只记学号，严禁记录密码 / token / cookie
                logging.info("登录成功：学号=%s", user)
                return True, "登录成功"
            msg = self._map_login_error(data.get("msg") or "")
            last_msg = msg
            logging.info("登录被拒绝（学号=%s）：%s", user, msg)
            # 凭据类错误重试无意义，立即返回；其余（验证码不正确/失效/未知文案）换新验证码重试
            if msg in ("登录名或密码不正确", "在线人数超过上限"):
                return False, msg
        return False, last_msg

    @staticmethod
    def _map_login_error(msg: str) -> str:
        """把语言包错误文案映射为中文原因；未知文案原样返回。"""
        if not msg:
            return "未知错误"
        return _LOGIN_ERROR_MAP.get(msg, msg)

    # ------------------------------------------------------------------
    # 数据查询
    # ------------------------------------------------------------------

    def fetch_batches(self) -> List[Dict[str, Any]]:
        """获取选课批次列表（batch.do 的 dataList）。"""
        data = self._post(ELECTIVE_BATCH)
        return data.get("dataList") or []

    def fetch_sysparam(self) -> Dict[str, Any]:
        """获取系统参数（sysparam.do 的 data，含 menuMap）。"""
        data = self._post(SYSPARAM)
        return data.get("data") or {}

    def fetch_student(self) -> Dict[str, Any]:
        """获取学生基础信息（student/{code}.do 的 data）。

        含 selectMajor / grade / electiveBatchList，供 ZY 课程查询构造 queryContent。
        """
        if not self.student_code:
            raise ValueError("尚未登录，缺少 student_code")
        data = self._post(STUDENT.format(code=self.student_code))
        node = data.get("data") or {}
        with self._lock:
            self._student_info = node
        return node

    def fetch_courses(
            self,
            batch_code: str,
            teaching_class_type: str,
            course_kind: str,
            query_content: str = "",
            page_number: int = 0,
            page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """查询课程列表，返回 (dataList, is_last)。

        路由：``teaching_class_type == "ZY"`` → programCourse.do（queryContent 形如
        ``ZXZY:{selectMajor},ZXNJ:{grade},``，取自 fetch_student()）；
        否则 → publicCourse.do（带 ``checkConflict="2"``、``checkCapacity="2"``）。

        ``is_last`` = 本页行数 < page_size（严禁用 totalCount，抓包中恒为 0）。
        """
        data = {
            "studentCode": self.student_code,
            "electiveBatchCode": batch_code,
            "teachingClassType": teaching_class_type,
            "queryContent": query_content,
        }
        if teaching_class_type == "ZY":
            endpoint = PROGRAM_COURSE
            with self._lock:
                student = self._student_info
            student = student or self.fetch_student()
            select_major = student.get("selectMajor") or ""
            grade = student.get("grade") or ""
            data["queryContent"] = f"ZXZY:{select_major},ZXNJ:{grade},"
        else:
            endpoint = PUBLIC_COURSE
            data["checkConflict"] = "2"
            data["checkCapacity"] = "2"
        query_setting = {
            "data": data,
            "pageSize": str(page_size),
            "pageNumber": str(page_number),
            "order": "isChoose -",
        }
        result = self._post(
            endpoint,
            data={"querySetting": json.dumps(query_setting)},
            timeout=15,
        )
        rows = result.get("dataList") or []
        return rows, len(rows) < page_size

    # ------------------------------------------------------------------
    # 课程详情（详细信息大纲 + 教学周历，抓包 [12]/[14]）
    # ------------------------------------------------------------------

    def fetch_course_info(
            self,
            teaching_class_id: str,
            course_number: str,
            batch_code: str,
    ) -> Dict[str, Any]:
        """查询课程详细信息（publicinfo/querykcxx.do，抓包 [12]）。

        表单字段与抓包逐字一致：``kch=课程号 & jxbid=教学班号 & xklcdm=批次码``
        （xklcdm 取值与周历接口 querySetting 里的 electiveBatchCode 相同）。
        响应原样返回 dict（``data`` 节点为课程大纲各字段，编码键的映射表见
        ``ui/cards.py`` 的 ``_COURSE_INFO_FIELDS`` / ``_COURSE_HOURS_FIELDS`` /
        ``_COURSE_DETAIL_SECTIONS`` 注释），解析在 UI 层做，与项目风格一致。
        """
        form = {
            "kch": course_number,
            "jxbid": teaching_class_id,
            "xklcdm": batch_code,
        }
        return self._post(COURSE_INFO, data=form, timeout=15)

    def fetch_course_schedule(
            self,
            teaching_class_id: str,
            batch_code: str,
    ) -> Dict[str, Any]:
        """查询教学周历（elective/courseSchedule.do，抓包 [14]）。

        表单只有 ``querySetting`` 一个字段：``{"data":{"studentCode":学号,
        "other":教学班号,"electiveBatchCode":批次码}}``（JSON 序列化后由
        requests 做 URL 编码，与抓包一致）。响应原样返回 dict，周历逐条在
        ``dataList``（编码键的映射表见 ``ui/cards.py`` 的
        ``_SCHEDULE_FIELDS`` 注释）。
        """
        query_setting = {
            "data": {
                "studentCode": self.student_code,
                "other": teaching_class_id,
                "electiveBatchCode": batch_code,
            }
        }
        return self._post(
            COURSE_SCHEDULE,
            data={"querySetting": json.dumps(query_setting)},
            timeout=15,
        )

    def fetch_course_results(
            self,
            other: str,
            batch_code: str,
            page_size: int = 50,
            page_number: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询选课结果（elective/courseResult.do，抓包 [2]/[3]）。

        ``other`` 语义（网页 JS 确证，两种请求**仅该字段不同**）：
        - ``"01"`` → **我的报名**（报名/抽签队列，行 ``selectStatus`` 全 ``"01"``，
          ``canDelete=="1"`` 可取消报名）；
        - ``"99"`` → **我的课程**（已选中的课，行 ``selectStatus`` 全 ``"99"``，
          ``canDelete`` 同样为 ``"1"/"0"``，删除走同一接口）。

        表单与抓包逐字段一致：``querySetting={"data":{studentCode,
        electiveBatchCode, other, teachingClassType:"QB", queryContent:""},
        pageSize, pageNumber, order:""}``（teachingClassType 固定 ``"QB"``
        = 全部类别；pageSize/pageNumber 用 50/0 单页拉全，抓包原值 10/0）。

        返回 ``dataList``（行含 ``comment`` 备注 / ``canDelete`` / ``kclx`` /
        ``teachingPlace`` / ``numberOfFirstVolunteer``（**可能是「已满」这类
        字符串**，解析层不得参与数值计算）等 16+ 字段），缺失时为空列表。
        """
        if other not in ("01", "99"):
            raise ValueError(f"other 只允许 '99'（我的课程）/ '01'（我的报名）：{other!r}")
        query_setting = {
            "data": {
                "studentCode": self.student_code,
                "electiveBatchCode": batch_code,
                "other": other,
                "teachingClassType": "QB",
                "queryContent": "",
            },
            "pageSize": str(page_size),
            "pageNumber": str(page_number),
            "order": "",
        }
        result = self._post(
            COURSE_RESULT,
            data={"querySetting": json.dumps(query_setting)},
            timeout=15,
        )
        return result.get("dataList") or []

    # ------------------------------------------------------------------
    # 报名与结果轮询
    # ------------------------------------------------------------------

    def volunteer(
            self,
            teaching_class_id: str,
            course_kind: str,
            teaching_class_type: str,
            batch_code: str,
    ) -> Dict[str, Any]:
        """发起选课报名（volunteer.do）。

        addParam = AES 加密的 ``{"data":{...}}?timestrap={ms}``；
        ``course_kind`` 原样传字符串（可为 ``"6,7"``），禁止转 int。
        """
        payload = {
            "data": {
                "operationType": "1",
                "studentCode": self.student_code,
                "electiveBatchCode": batch_code,
                "teachingClassId": teaching_class_id,
                "courseKind": course_kind,
                "teachingClassType": teaching_class_type,
            }
        }
        form = {
            "addParam": encrypt_add_param(payload),
            "studentCode": self.student_code,
        }
        return self._post(VOLUNTEER, data=form, timeout=15)

    def delete_volunteer(
            self,
            teaching_class_id: str,
            batch_code: str,
    ) -> Dict[str, Any]:
        """发起退课（deleteVolunteer.do）。

        deleteParam = AES 加密的 ``{"data":{...}}?timestrap={ms}``，与报名同构但：
        - 表单字段名是 ``deleteParam``（不是 ``addParam``）；
        - ``operationType`` 为 ``"2"``（报名是 ``"1"``）；
        - ``isMajor`` 恒为 ``"1"``（网页 JS 硬编码字面量，不随课程动态取值）；
        - **不含** ``courseKind`` / ``teachingClassType``（只有报名才带）。
        """
        payload = {
            "data": {
                "operationType": "2",
                "studentCode": self.student_code,
                "electiveBatchCode": batch_code,
                "teachingClassId": teaching_class_id,
                "isMajor": "1",
            }
        }
        form = {
            "deleteParam": encrypt_add_param(payload),
            "studentCode": self.student_code,
        }
        return self._post(DELETE_VOLUNTEER, data=form, timeout=15)

    def poll_result(
            self,
            teaching_class_id: str,
            max_attempts: int = 10,
            interval: float = 1.0,
            op_type: str = "1",
    ) -> Dict[str, Any]:
        """轮询 studentstatus.do 获取选课操作的真实结果。

        ``op_type``：``"1"``=报名后轮询、``"0"``=退课后轮询（网页源码确证）。

        code ``"0"``=处理中（继续轮询）、``"1"``=成功、``"-1"``=失败；
        耗尽返回 ``{"code": "timeout", ...}``。
        """
        for _ in range(max_attempts):
            form = {
                "studentCode": self.student_code,
                "teachingClassId": teaching_class_id,
                "type": op_type,
            }
            data = self._post(STUDENT_STATUS, data=form, timeout=10)
            code = str(data.get("code", ""))
            if code == "0":
                time.sleep(interval)
                continue
            # 服务端可能返回 "msg": null（键存在值为 null），get 默认值不生效，
            # 必须 or 兜底为空串，避免上层 f-string 拼出 "...None"
            return {"code": code, "msg": data.get("msg") or ""}
        return {"code": "timeout", "msg": f"轮询 {max_attempts} 次仍未完成"}

    # ------------------------------------------------------------------
    # 退出登录
    # ------------------------------------------------------------------

    def logout(self) -> Tuple[bool, str]:
        """退出登录：串行两步，返回 (ok, msg)，**不抛异常**。

        1. POST student/logout.do，表单 ``{studentNumber: 学号}``；
        2. 上一步成功后 POST student/authlogout.do，空表单。

        顺序固定（网页 ``grablessons.min.js`` 在 ``studentLogOut(...).done(...)``
        回调里串行调用 ``autoLogOut()``）。两步都成功才算成功；任一步失败或网络
        异常都返回 ``(False, 原因)``——调用方是关闭流程，抛异常会中断退出。

        **不调用 CAS 登出**（``authserver.nju.edu.cn/authserver/logout``）：本页
        ``loginType='ldap'``，CAS 分支在前端不触发，且 CAS 登出会踢掉统一身份
        认证的单点登录（影响其他南大系统），副作用不可控。

        只清会话语义由调用方处理：本方法不动本地保存的账号密码（留给下次自动登录），
        也不改 self.token / self.student_code。
        """
        try:
            # 登出流程严禁触发自动重登（会话已失效时重登只为登出毫无意义，
            # 且可能顶掉用户新建立的会话；失效响应由上方 code!="1" 分支兜底）
            data = self._post(
                LOGOUT,
                data={"studentNumber": self.student_code},
                timeout=10,
                allow_relogin=False,
            )
        except Exception as e:
            logging.warning("退出登录失败（logout.do）：%s", e)
            return False, f"退出登录失败: {e}"
        if str(data.get("code", "")) != "1":
            reason = self._logout_reason(data)
            logging.warning("退出登录失败（logout.do）：%s", reason)
            return False, f"退出登录失败: {reason}"

        try:
            data = self._post(AUTH_LOGOUT, data={}, timeout=10, allow_relogin=False)
        except Exception as e:
            logging.warning("退出登录失败（authlogout.do）：%s", e)
            return False, f"退出登录失败: {e}"
        if str(data.get("code", "")) != "1":
            reason = self._logout_reason(data)
            logging.warning("退出登录失败（authlogout.do）：%s", reason)
            return False, f"退出登录失败: {reason}"
        logging.info("退出登录成功（logout.do + authlogout.do）")
        return True, "退出登录成功"

    @staticmethod
    def _logout_reason(data: Dict[str, Any]) -> str:
        """取登出失败原因：优先服务端 msg，其次 code，都没有给占位文案。"""
        return str(data.get("msg") or data.get("code") or "未知错误")

    # ------------------------------------------------------------------
    # 会话失效判定
    # ------------------------------------------------------------------

    @staticmethod
    def is_session_expired(resp: Any) -> bool:
        """登录失效检测（与前端 bh_utils.js / grablessons.min.js 一致）。

        resp 含非空 loginURL，或 code == "302"。
        """
        if not isinstance(resp, dict):
            return False
        if resp.get("loginURL"):
            return True
        if str(resp.get("code", "")) == "302":
            return True
        return False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _post(
            self,
            path: str,
            data: Optional[Dict[str, Any]] = None,
            timeout: int = 10,
            allow_relogin: bool = True,
    ) -> Dict[str, Any]:
        """POST {BASE}{APP}{path}，返回解析后的 JSON dict。

        HTTP 层走 zbToolLib（与程序本体和其他插件用法一致）：``zb.postUrl`` 自带
        「正在Post请求…/成功/失败」日志。

        ``times=1`` 是有意取舍：zb.postUrl 默认内部重试 5 次，会绕过调度器的
        令牌桶限速与 QoS 退避，打乱抢课节奏；本插件保持「限速 → 一次请求 →
        QoS 退避」语义，重试由调度器统一编排，不在 HTTP 层静默重试。

        会话失效（``is_session_expired``，含 ``{"code":"302"}`` 无 loginURL 的
        形态）且 ``allow_relogin``：用凭据 provider 自动重登**一次**并原样重发
        当前请求（重试传 ``allow_relogin=False`` 防无限递归）；重登失败/无凭据
        则原样返回失效响应，由上层既有处理兜底。

        整个方法体加锁：序列化请求发送，避免共享可变状态（cookies / 认证头）
        被并发破坏。
        """
        with self._lock:
            resp = zb.postUrl(
                f"{BASE}{APP}{path}",
                times=1,
                data=data,
                timeout=timeout,
                headers=self._request_headers(),
                cookies=dict(self._cookies),
            )
            if resp is None:
                # zb.postUrl 重试耗尽/网络失败时返回 None（不抛异常），
                # 这里转成我们自己的异常，与既有异常处理链兼容
                raise RuntimeError(f"网络请求失败: {path}")
            resp.raise_for_status()
            # zbToolLib 的全局 Session 与本类不同源，Set-Cookie 必须显式回读合并
            self._merge_cookies(resp)
            parsed = resp.json()
        if allow_relogin and self.is_session_expired(parsed):
            logging.info("会话失效（%s），尝试自动重登后重发", path)
            if self._try_relogin():
                # 重登成功：原样重发一次当前请求（不再二次重登）
                return self._post(
                    path, data=data, timeout=timeout, allow_relogin=False
                )
            # 重登失败/无凭据：原样返回失效响应，上层既有处理兜底显示
        return parsed
