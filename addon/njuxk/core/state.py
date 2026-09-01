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
    setting.save("user", user)
    setting.save("pwd", pwd)


def load_account(setting) -> tuple:
    """读取账号；缺失或非字符串时回退空串。"""
    user = setting.read("user")
    pwd = setting.read("pwd")
    if not isinstance(user, str):
        user = ""
    if not isinstance(pwd, str):
        pwd = ""
    return user, pwd


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
