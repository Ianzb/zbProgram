"""搜索排序模块：服务端 ``queryContent`` 全文本检索 + 客户端优先级打分排序。

纯逻辑模块，无 UI 依赖。服务端 ``publicCourse.do`` / ``programCourse.do`` 的
``data.queryContent`` 本身做课程名/课程号/教师的全文本检索（语言包占位符为
「课程名称/课程号/教师」），客户端只需对返回结果按优先级重排即可。

打分规则（命中即加分，可累加）
----------------------------
- ``course_name`` 命中：+100
- ``course_number`` 命中：+90
- ``teacher_name`` 命中：+60
- 其他字段命中（``department_name`` / ``campus_name`` / ``teaching_place`` /
  ``elective_type`` / ``kcjj`` 等描述性字段）：+20

匹配方式：大小写不敏感的子串匹配（``keyword.lower() in field.lower()``）；
字段为 ``None`` 时按空串处理。排序稳定：同分保持原相对顺序。
"""

from __future__ import annotations

from typing import List

from ..api.models import Course

# 高优先级字段：名称 > 编号 > 教师
_PRIMARY_FIELDS = (
    ("course_name", 100),
    ("course_number", 90),
    ("teacher_name", 60),
)

# 其他描述性字段：命中 +20（course_name 之外的描述性字段）
_SECONDARY_FIELDS = (
    "department_name",
    "campus_name",
    "teaching_place",
    "elective_type",
    "kcjj",
)


def _score(course: Course, keyword: str) -> int:
    """计算单门课程对关键字的命中得分（未命中为 0）。

    字段为 ``None`` 或缺失时按空串处理（``getattr`` 兜底），不抛异常。
    """
    kw = keyword.lower()
    score = 0
    for field, points in _PRIMARY_FIELDS:
        value = getattr(course, field, None)
        if value is not None and kw in str(value).lower():
            score += points
    for field in _SECONDARY_FIELDS:
        value = getattr(course, field, None)
        if value is not None and kw in str(value).lower():
            score += 20
    return score


def rank_courses(courses: List[Course], keyword: str) -> List[Course]:
    """按「名称·编号 > 教师 > 其他信息」优先级对课程排序（纯函数）。

    - 不修改入参，返回新列表；
    - 只排序不过滤（未命中项保留在末尾，过滤由服务端 ``queryContent`` 负责）；
    - 稳定排序：同分保持原相对顺序；
    - 空关键字（空串或纯空白）→ 直接返回原列表的副本，不做任何过滤或重排。
    """
    if not keyword or not keyword.strip():
        return list(courses)
    kw = keyword.strip()
    return sorted(courses, key=lambda c: -_score(c, kw))


def build_query_content(keyword: str) -> str:
    """把关键字转成传给服务端的 ``queryContent``（去首尾空白即可）。"""
    return keyword.strip()
