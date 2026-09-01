"""本地持久化默认配置（计划 todo 9）。

单一数据源：`DEFAULTS` 与 `main.py` 中 `setting.adds({...})` 注册的 7 项保持一致。
`register_defaults` 供 `addonInit` 调用，避免两处维护默认值。
"""
from __future__ import annotations

DEFAULTS = {
    "user": "",
    "pwd": "",
    "session": {"cookies": {}, "token": "", "timestamp": 0},
    "favorites": [],
    "scheduler": {
        # 随机延迟默认 1~2 秒（2026-08-31 由 0.5~1.5 调整；旧值经
        # get_scheduler_config 的迁移逻辑自动升级，见 _OLD_DELAY_DEFAULTS）
        "delay_min": 1.0,
        "delay_max": 2.0,
        "repeat": 0,
        "max_workers": 3,
        "qos_backoff_base": 3.0,
        "qos_backoff_max": 15.0,
    },
    "selected_batch": "",
    "selected_category": "",
}


def _to_int(value):
    """宽松 int 转换：int(float(value))；失败抛异常由 _coerce 兜底回退默认值。"""
    return int(float(value))


def _coerce(value, default, cast):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


# scheduler 数值字段 -> (默认值, 转换函数)
_SCHEDULER_NUMERIC = {
    "delay_min": (1.0, float),
    "delay_max": (2.0, float),
    "repeat": (0, _to_int),
    "max_workers": (3, _to_int),
    "qos_backoff_base": (3.0, float),
    "qos_backoff_max": (15.0, float),
}

# 旧版默认延迟区间（0.5, 1.5）：老用户 settings.json 里已持久化了这对值
# （setting.adds 注册时会把默认值写进实际存储），读取时视为「未定制」迁移到新默认。
_OLD_DELAY_DEFAULTS = (0.5, 1.5)


def _normalize_scheduler(raw) -> dict:
    """将任意读取值归一化为合法调度配置（缺失键补默认、类型校正、交换倒置区间）。"""
    cfg = dict(DEFAULTS["scheduler"])
    if isinstance(raw, dict):
        cfg.update(raw)
    for key, (default, cast) in _SCHEDULER_NUMERIC.items():
        cfg[key] = _coerce(cfg[key], default, cast)
    if cfg["delay_min"] > cfg["delay_max"]:
        cfg["delay_min"], cfg["delay_max"] = cfg["delay_max"], cfg["delay_min"]
    return cfg


def register_defaults(setting):
    """注册默认设置项（供 addonInit 调用，保持单一数据源）。"""
    setting.adds(DEFAULTS)


def get_scheduler_config(setting) -> dict:
    """读取调度配置并与 DEFAULTS 合并、做类型校正。

    - 缺失键用默认值补齐
    - 数值字段转 float/int（转换失败回退默认值）
    - delay_min > delay_max 时交换
    - 迁移：保存值恰好等于旧默认对 (0.5, 1.5) 视为「未定制」，
      替换为新默认 (1.0, 2.0)；用户自定义过的其他值不动
    """
    cfg = _normalize_scheduler(setting.read("scheduler"))
    if (cfg["delay_min"], cfg["delay_max"]) == _OLD_DELAY_DEFAULTS:
        cfg["delay_min"] = DEFAULTS["scheduler"]["delay_min"]
        cfg["delay_max"] = DEFAULTS["scheduler"]["delay_max"]
    return cfg
