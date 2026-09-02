"""本地持久化层（计划 todo 9）：账号、会话、本地收藏、调度配置、选中状态。

全部经 `AddonSettingProxy` 读写（命名空间 addonSettings/nju-xk/），
不直接打开 settings.json。收藏完全本地实现，严禁调用任何服务端收藏接口
（favorite.do / queryfavorite.do）。收藏以 `teaching_class_id` 为唯一键。
"""
from __future__ import annotations

import time

from . import settings as _settings


# ==================== 收藏（本地） ====================


def list_favorites(setting) -> list:
    """返回本地收藏列表；数据损坏（非 list）时返回空列表。"""
    value = setting.read("favorites")
    if isinstance(value, list):
        return value
    return []


def add_favorite(setting, course: dict) -> bool:
    """新增收藏，以 `teaching_class_id` 为唯一键。

    已存在则幂等返回 False（不重复添加）；新增加入并返回 True。
    course 非 dict 或缺 teaching_class_id 时返回 False。
    """
    if not isinstance(course, dict):
        return False
    tid = course.get("teaching_class_id")
    if tid is None:
        return False
    favorites = list_favorites(setting)
    for fav in favorites:
        if isinstance(fav, dict) and fav.get("teaching_class_id") == tid:
            return False
    favorites.append(dict(course))
    setting.save("favorites", favorites)
    return True


def remove_favorite(setting, teaching_class_id) -> bool:
    """按 teaching_class_id 移除收藏；不存在返回 False。"""
    favorites = list_favorites(setting)
    kept = [
        fav for fav in favorites
        if not (isinstance(fav, dict) and fav.get("teaching_class_id") == teaching_class_id)
    ]
    if len(kept) == len(favorites):
        return False
    setting.save("favorites", kept)
    return True


def is_favorited(setting, teaching_class_id) -> bool:
    return any(
        isinstance(fav, dict) and fav.get("teaching_class_id") == teaching_class_id
        for fav in list_favorites(setting)
    )


def update_favorite(setting, teaching_class_id, patch: dict) -> bool:
    """按 teaching_class_id 精确匹配后合并字段（刷新人数/容量/概率）。

    只更新目标那条，同 course_number 的其他教学班不受影响；不存在返回 False。
    """
    if not isinstance(patch, dict):
        return False
    favorites = list_favorites(setting)
    for i, fav in enumerate(favorites):
        if isinstance(fav, dict) and fav.get("teaching_class_id") == teaching_class_id:
            merged = dict(fav)
            merged.update(patch)
            favorites[i] = merged
            setting.save("favorites", favorites)
            return True
    return False


def clear_favorites(setting) -> None:
    setting.save("favorites", [])


# ==================== 账号与会话 ====================


def save_account(setting, user, pwd):
    """保存当前账号（顶层键 user/pwd）并同时 upsert 进多账号列表 accounts。

    - 顶层键维持既有语义（凭据 provider / has_saved_account 依赖）；
    - ``user`` 非空时同步 upsert 进 ``accounts`` 列表（同 user 覆盖 pwd 与
      ``last_used``，否则追加）——登录界面据此展示「已保存的账号」；
    - ``user`` 为空串（清空场景）只写顶层键，不往列表里塞空账号。
    """
    setting.save("user", user)
    setting.save("pwd", pwd)
    if isinstance(user, str) and user:
        accounts = list_accounts(setting)
        _upsert_account(accounts, user, pwd)
        setting.save("accounts", accounts)


def load_account(setting) -> tuple:
    """读取账号；缺失或非字符串时回退空串。"""
    user = setting.read("user")
    pwd = setting.read("pwd")
    if not isinstance(user, str):
        user = ""
    if not isinstance(pwd, str):
        pwd = ""
    return user, pwd


def list_accounts(setting) -> list:
    """返回多账号列表（按 ``last_used`` 降序，最近使用的在前）。

    容错：键缺失 / 非 list / 元素非 dict / 学号非字符串或为空 —— 一律过滤，
    只返回合法账号（元素形如 ``{"user": str, "pwd": str, "last_used": float}``）。
    """
    value = setting.read("accounts")
    if not isinstance(value, list):
        return []
    accounts = [
        acc for acc in value
        if isinstance(acc, dict) and isinstance(acc.get("user"), str) and acc["user"]
    ]
    accounts.sort(key=_account_last_used, reverse=True)
    return accounts


def _account_last_used(acc) -> float:
    """取 ``last_used`` 并宽松转 float（缺失 / 非法一律 0.0，排最后）。"""
    try:
        return float(acc.get("last_used"))
    except (TypeError, ValueError):
        return 0.0


def _upsert_account(accounts: list, user: str, pwd: str) -> list:
    """同 user 覆盖 pwd / last_used，否则追加；返回同一列表（原地修改）。"""
    now = time.time()
    for acc in accounts:
        if isinstance(acc, dict) and acc.get("user") == user:
            acc["pwd"] = pwd
            acc["last_used"] = now
            return accounts
    accounts.append({"user": user, "pwd": pwd, "last_used": now})
    return accounts


def delete_account(setting, user) -> bool:
    """从多账号列表**永久**移除该账号；返回是否真的删除了。

    若被删的正是当前顶层账号（``user`` 顶层键），同步清空顶层键
    （等价 ``save_account(setting, "", "")``，不产生空列表元素）。
    """
    value = setting.read("accounts")
    accounts = value if isinstance(value, list) else []
    kept = [
        acc for acc in accounts
        if not (isinstance(acc, dict) and acc.get("user") == user)
    ]
    if len(kept) == len(accounts):
        return False
    setting.save("accounts", kept)
    current = setting.read("user")
    if isinstance(current, str) and current == user:
        save_account(setting, "", "")
    return True


def load_last_account(setting) -> tuple:
    """最近使用的账号 -> (user, pwd)；列表为空回退顶层键 user/pwd。

    供启动自动登录使用：多账号场景下自动登录最近一次用过的账号。
    """
    accounts = list_accounts(setting)
    if accounts:
        user = accounts[0].get("user")
        pwd = accounts[0].get("pwd")
        return (
            user if isinstance(user, str) else "",
            pwd if isinstance(pwd, str) else "",
        )
    return load_account(setting)


def migrate_accounts(setting) -> None:
    """老设置迁移：``accounts`` 为空且顶层 ``user`` 非空 → 种子化列表（幂等）。

    老用户 settings.json 里只有顶层 user/pwd，首次运行新版本时把这对凭据
    种进 accounts 列表（无感迁移）；已有 accounts 或顶层无账号时不做任何事，
    可安全重复调用。
    """
    if setting is None:
        return
    accounts = setting.read("accounts")
    if isinstance(accounts, list) and accounts:
        return
    user = setting.read("user")
    if not (isinstance(user, str) and user):
        return
    pwd = setting.read("pwd")
    setting.save("accounts", [{
        "user": user,
        "pwd": pwd if isinstance(pwd, str) else "",
        "last_used": time.time(),
    }])


def save_session(setting, cookies: dict, token: str):
    setting.save("session", {
        "cookies": cookies,
        "token": token,
        "timestamp": int(time.time()),
    })


def load_session(setting) -> tuple:
    """读取会话 -> (cookies, token, timestamp)；缺失/损坏时回退 ({}, "", 0)。"""
    value = setting.read("session")
    if not isinstance(value, dict):
        return {}, "", 0
    cookies = value.get("cookies")
    if not isinstance(cookies, dict):
        cookies = {}
    token = value.get("token")
    if not isinstance(token, str):
        token = ""
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, int):
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            timestamp = 0
    return cookies, token, timestamp


def clear_session(setting):
    setting.save("session", {"cookies": {}, "token": "", "timestamp": 0})


# ==================== 调度配置 ====================


def save_scheduler_config(setting, cfg: dict):
    """保存调度配置：与当前配置合并（部分更新不丢其他键），归一化后落盘。"""
    if not isinstance(cfg, dict):
        return
    merged = _settings.get_scheduler_config(setting)
    merged.update(cfg)
    setting.save("scheduler", _settings._normalize_scheduler(merged))


def load_scheduler_config(setting) -> dict:
    """读取调度配置（复用 settings.get_scheduler_config：合并默认值 + 类型校正）。"""
    return _settings.get_scheduler_config(setting)


# ==================== 选中状态 ====================


def save_selection(setting, batch_code, category):
    setting.save("selected_batch", batch_code)
    setting.save("selected_category", category)


def load_selection(setting) -> tuple:
    """读取选中批次/类别；缺失或非字符串时回退空串。"""
    batch = setting.read("selected_batch")
    category = setting.read("selected_category")
    if not isinstance(batch, str):
        batch = ""
    if not isinstance(category, str):
        category = ""
    return batch, category
