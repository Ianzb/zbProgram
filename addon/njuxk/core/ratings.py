"""课程红黑榜（公共评价库）本地逻辑：解析 / 打分 / 匹配 / 缓存。

数据来源：NJU-Hub（https://github.com/Mellow-Winds/NJU-Hub）维护的
``data/merged_ratings.json``，结构为 ``{"课程名#教师": {...}}`` 大对象，
每条值形如::

    {"courseName": "...", "teachers": ["张三", "李四"],
     "reviews": {"nju_course_ratings": ["评价1", ...], "2021": [...], ...}}

匹配规则**逐条照抄** NJU-Hub ``scripts/xk/xk_badges.js`` 的三级回退（课程名 +
教师），保证与网页端插件结果一致：

1. Level 1：课程名与教师**精确**相等；
2. Level 2：去空格后的课程名相等，且教师命中 DB 教师分词中的任一 token；
3. Level 3：整行文本（课程名 + 教师）包含去空格课程名，且包含任一教师 token。

打分算法逐条照抄 NJU-Hub ``red-black/app.js`` 的 ``scoreReviews``：统计评价
原文中红/黑关键词命中数，映射到 18~98 分（仅作信息参考，不强行划分红黑榜）。

线程约定：本模块是**纯逻辑**（除 ``RatingsStore.refresh`` 调用外部注入的
``fetcher`` 外不发网络请求）。``fetcher`` 由装配层注入（真实为
``XkClient.fetch_ratings``），便于测试替换。所有公开方法可在工作线程调用。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

from . import state

# 红/黑关键词（逐条照抄 NJU-Hub red-black/app.js scoreReviews）
_RED_WORDS = (
    "推荐", "给分高", "给分好", "给分不错", "轻松", "简单", "事少", "好课",
    "赚到", "友好", "不卷", "舒服", "红",
)
_BLACK_WORDS = (
    "快跑", "不推荐", "压力大", "事多", "很累", "困难", "极难", "很差",
    "不负责", "地狱", "慎选", "黑",
)

#: 教师分词分隔符（NJU-Hub 用 ``[\s,，、]+``）
_TEACHER_SPLIT = re.compile(r"[\s,，、]+")

#: 归一化时剔除的空白与标点（照抄 app.js normalize 的字符集）
_NORMALIZE_STRIP = re.compile(r"[\s\u3000·•，。、“”\"'（）()【】\[\]：:、/\\_—–-]")


def normalize(value: Any) -> str:
    """NFKC + 小写 + 去空白/标点（与 NJU-Hub ``normalize`` 语义一致）。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _NORMALIZE_STRIP.sub("", text.lower()).strip()


def score_reviews(texts: List[str]) -> int:
    """按红/黑关键词命中数给课程打分（18~98，照抄 NJU-Hub ``scoreReviews``）。"""
    text = " ".join(texts)
    red_hits = sum(1 for word in _RED_WORDS if word in text)
    black_hits = sum(1 for word in _BLACK_WORDS if word in text)
    score = round(72 + (red_hits - black_hits) * 6 + min(len(texts), 8))
    return max(18, min(98, score))


def collect_reviews(raw: Any) -> List[Dict[str, str]]:
    """把 ``reviews`` 节点规整为 ``[{"source": 来源, "text": 评价}]``。

    支持两种形态（NJU-Hub 新旧格式）：
    - ``{"2021": ["评价", ...], "nju_course_ratings": [...]}``（按来源分组）；
    - ``["评价", ...]``（旧格式，来源留空）。
    非 str/非 list 的脏值安全跳过。
    """
    out: List[Dict[str, str]] = []
    if isinstance(raw, dict):
        for source, items in raw.items():
            if isinstance(items, list):
                for item in items:
                    text = str(item).strip()
                    if text:
                        out.append({"source": str(source), "text": text})
            elif isinstance(items, str) and items.strip():
                out.append({"source": str(source), "text": items.strip()})
    elif isinstance(raw, list):
        for item in raw:
            text = str(item).strip()
            if text:
                out.append({"source": "", "text": text})
    return out


def parse_ratings(raw: Any) -> Dict[str, dict]:
    """把原始 JSON 大对象解析成 ``{key: 评价信息}``（供匹配/展示）。

    每条信息：``{"name", "teachers", "score", "count", "reviews"}``。
    无评价的条目跳过；非 dict/list 的值跳过。
    """
    data: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return data
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        name = key.split("#", 1)[0]
        teachers = key.split("#", 1)[1] if "#" in key else ""
        if isinstance(value, dict):
            name = str(value.get("courseName") or name)
            raw_teachers = value.get("teachers")
            if isinstance(raw_teachers, list):
                teachers = "、".join(
                    str(t) for t in raw_teachers if t
                )
            elif isinstance(raw_teachers, str):
                teachers = raw_teachers
            elif value.get("teacher"):
                teachers = str(value.get("teacher"))
            reviews = collect_reviews(value.get("reviews"))
        elif isinstance(value, list):
            reviews = collect_reviews(value)
        else:
            continue
        if not reviews:
            continue
        texts = [r["text"] for r in reviews]
        data[key] = {
            "name": name,
            "teachers": teachers,
            "score": score_reviews(texts),
            "count": len(reviews),
            "reviews": reviews,
        }
    return data


class RatingsStore:
    """红黑榜数据仓库：缓存读取、云端同步、按课程匹配。

    - 构造时同步读本地缓存（无网络），``loaded`` 表示是否已有可用数据；
    - ``refresh(fetcher)`` 拉取云端数据并落缓存（工作线程调用，异常转
      ``(False, 中文原因)``，不抛）；
    - ``lookup(name, teacher)`` 返回匹配到的评价信息 dict 或 ``None``。
    """

    def __init__(self, setting=None, fetcher=None):
        self._setting = setting
        self._fetcher = fetcher
        self._raw: Dict[str, Any] = {}
        self._data: Dict[str, dict] = {}
        self._by_name: Dict[str, list] = {}
        self._entries: list = []
        self._loaded = False
        self._load_cache()

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def count(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _load_cache(self):
        if self._setting is None:
            return
        try:
            raw = state.load_ratings_cache(self._setting)
        except Exception as e:  # noqa: BLE001 - 缓存损坏不得影响插件启动
            logging.debug("读取红黑榜缓存失败：%s", e)
            return
        if isinstance(raw, dict) and raw:
            self._set_raw(raw)

    def _set_raw(self, raw: Dict[str, Any]):
        self._raw = raw if isinstance(raw, dict) else {}
        self._data = parse_ratings(self._raw)
        self._build_index()
        self._loaded = bool(self._data)

    def _save_cache(self):
        if self._setting is None:
            return
        try:
            state.save_ratings_cache(self._setting, self._raw)
        except Exception as e:  # noqa: BLE001 - 缓存写入失败不影响本次使用
            logging.debug("写入红黑榜缓存失败：%s", e)

    # ------------------------------------------------------------------
    # 云端同步
    # ------------------------------------------------------------------

    def refresh(self, fetcher=None) -> tuple:
        """拉取云端数据并覆盖缓存，返回 ``(ok, msg)``（不抛异常）。

        ``fetcher`` 缺省用构造时注入的；都没有则返回失败（测试环境无网络）。
        """
        func = fetcher if fetcher is not None else self._fetcher
        if func is None:
            return False, "未配置红黑榜数据源"
        try:
            raw = func()
        except Exception as e:  # noqa: BLE001 - 网络异常统一转中文提示
            logging.warning("红黑榜同步失败：%s", e)
            return False, f"红黑榜同步失败：{e}"
        if not isinstance(raw, dict) or not raw:
            return False, "红黑榜数据为空或格式不正确"
        self._set_raw(raw)
        self._save_cache()
        return True, f"已同步 {len(self._data)} 门课程评价"

    # ------------------------------------------------------------------
    # 匹配（照抄 xk_badges.js 三级回退）
    # ------------------------------------------------------------------

    def _build_index(self):
        self._by_name = {}
        self._entries = []
        for key, info in self._data.items():
            name, _, teacher = key.partition("#")
            name_clean = normalize(name)
            tokens = [t for t in _TEACHER_SPLIT.split(teacher) if t]
            entry = (name, name_clean, teacher, tokens, info)
            self._entries.append(entry)
            self._by_name.setdefault(name_clean, []).append(entry)

    def lookup(self, name, teacher) -> Optional[dict]:
        """按课程名 + 教师匹配评价信息；未匹配返回 ``None``。"""
        if not self._data:
            return None
        name = str(name or "")
        teacher = str(teacher or "")
        name_clean = normalize(name)

        # Level 1/2：按归一化课程名定位候选，再比教师
        for c, c_clean, t, tokens, info in self._by_name.get(name_clean, []):
            if name == c and teacher == t:
                return info
            if c_clean == name_clean and any(
                    tok and tok in teacher for tok in tokens):
                return info

        # Level 3：整行文本包含匹配（课程名 + 教师）
        row_text = normalize(name + teacher)
        if row_text:
            for c, c_clean, t, tokens, info in self._entries:
                if (c_clean and c_clean in row_text
                        and any(tok and tok in row_text for tok in tokens)):
                    return info
        return None
