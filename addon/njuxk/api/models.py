"""数据层：课程 / 批次 / 类别数据类、课程响应解析器、选中概率计算。

字段命名约定
------------
- 数据类字段一律 snake_case，默认 ``""``（字符串），避免 None 传播到 UI。
- 服务端 JSON 字段为 camelCase（如 ``teachingClassID``、``numberOfFirstVolunteer``），
  解析器负责映射；缺失或为 None 的字段安全降级为 ``""``。

关键约定（依据抓包）
--------------------
- ``course_kind`` 一律由外部（所选类别菜单）注入，解析器**不得**从课程行读取：
  抓包 ``#44`` 提交的是菜单值 ``"6,7"``，而课程行自身值为 ``7``。
- ``numberOfFirstVolunteer`` 恒为 null（先选先得批次不下发），``kcjj`` 常为空串/None，
   因此选中概率**只用卡片上已显示的两个数字**按抽签制计算：概率 =
   ``classCapacity``（容量）/ ``numberOfSelected``（报名人数），**不依赖**
   ``numberOfFirstVolunteer`` 与 ``tacticName``（详见 :func:`selection_probability`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _s(value: Any) -> str:
    """把任意值安全转成字符串；None 降级为空串。"""
    if value is None:
        return ""
    return str(value)


# 类别代码 → 中文名静态映射（teachingClassType → 类别名）。
#
# 来源：参考脚本 ``NJU-xk-helper/README.md`` 的「courseKind（jxblx）对照表」
# （该表同时给出 courseKind 数字，此处只取展示用的中文名）：
#
#   | 类别            | teachingClassType | courseKind |
#   |-----------------|-------------------|------------|
#   | 专业            | ZY                | 1          |
#   | 体育            | TY                | 2          |
#   | 科学之光        | GG06              | 3          |
#   | 公选课          | GG01              | 4          |
#   | 美育            | MY                | 5          |
#   | 导学/研讨/通识  | GG02              | 6, 7       |
#   | 悦读            | YD                | 8          |
#   | 跨专业          | KZY               | 12         |
#   | 大学数学        | TX01              | 13         |
#   | 大学英语        | TX02              | 14         |
#   | 思政军事类      | TX03              | 15         |
#   | 计算机          | TX04              | 16         |
#
# 为什么需要它：抓包 ``#24``（batch.do）的 ``limitMenuList`` 中 ``menuName`` 多为
# null（本仓库夹具 53 条菜单全为 null），直接退化显示 ``menuCode`` 会出现「TY」
# 这类代码。这里按 ``menu_name → MENU_CODE_NAMES → menu_code`` 三级回退取展示名。
#
# 未收录的 code（如 ``GG03``/``GG04``/``GG05``）**不编造**：回退显示代码本身
# （见 :func:`menu_display_name`）。
MENU_CODE_NAMES: Dict[str, str] = {
    "ZY": "专业",
    "TY": "体育",
    "GG06": "科学之光",
    "GG01": "公选课",
    "MY": "美育",
    "GG02": "导学/研讨/通识",
    "YD": "悦读",
    "KZY": "跨专业",
    "TX01": "大学数学",
    "TX02": "大学英语",
    "TX03": "思政军事类",
    "TX04": "计算机",
}


def menu_display_name(menu: "Menu") -> str:
    """类别展示名：``menu_name → MENU_CODE_NAMES → menu_code`` 三级回退。

    抓包中 ``menuName`` 常为 null，此时用静态中文名表；表里也没有的 code
    （如 ``GG03``/``GG04``/``GG05``）回退为 code 本身，**不编造**中文名。
    """
    if menu.menu_name:
        return menu.menu_name
    return MENU_CODE_NAMES.get(menu.menu_code) or menu.menu_code


@dataclass
class Course:
    """一门可展示/可报名的课程（教学班粒度）。

    所有字段默认 ``""``，保证 UI 层不会遇到 None。
    """

    teaching_class_id: str = ""
    course_number: str = ""
    course_name: str = ""
    credit: str = ""
    teacher_name: str = ""
    teaching_place: str = ""
    campus_name: str = ""
    class_capacity: str = ""
    number_of_selected: str = ""
    number_of_first_volunteer: str = ""
    is_full: str = ""
    is_choose: str = ""
    course_kind: str = ""
    teaching_class_type: str = ""
    elective_type: str = ""
    hours: str = ""
    kcjj: str = ""
    batch_code: str = ""


@dataclass
class Batch:
    """选课批次。``limit_menu_list`` 保留服务端原始 dict 列表。"""

    code: str = ""
    name: str = ""
    begin_time: str = ""
    end_time: str = ""
    type_name: str = ""
    tactic_name: str = ""
    school_term: str = ""
    limit_menu_list: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Menu:
    """类别菜单（来自批次的 ``limitMenuList``）。"""

    menu_code: str = ""
    menu_name: str = ""
    course_kind: str = ""
    limit_volunteer: str = ""
    is_open: str = ""
    is_allow_select: str = ""
    parent_menu_code: str = ""


def from_public_course(
        row: Dict[str, Any],
        course_kind: str,
        teaching_class_type: str,
        batch_code: str,
) -> Course:
    """解析 ``publicCourse.do`` 的扁平行（抓包 ``#41``）。

    ``course_kind`` / ``teaching_class_type`` / ``batch_code`` 由外部注入，
    不从课程行读取（课程行自身的 ``courseKind`` 与所选菜单值可能不一致）。
    """
    return Course(
        teaching_class_id=_s(row.get("teachingClassID")),
        course_number=_s(row.get("courseNumber")),
        course_name=_s(row.get("courseName")),
        credit=_s(row.get("credit")),
        teacher_name=_s(row.get("teacherName")),
        teaching_place=_s(row.get("teachingPlace")),
        campus_name=_s(row.get("campusName")),
        class_capacity=_s(row.get("classCapacity")),
        number_of_selected=_s(row.get("numberOfSelected")),
        number_of_first_volunteer=_s(row.get("numberOfFirstVolunteer")),
        is_full=_s(row.get("isFull")),
        is_choose=_s(row.get("isChoose")),
        course_kind=_s(course_kind),
        teaching_class_type=_s(teaching_class_type),
        elective_type=_s(row.get("electiveType")),
        hours=_s(row.get("hours")),
        kcjj=_s(row.get("kcjj")),
        batch_code=_s(batch_code),
    )


def from_program_course(
        course_row: Dict[str, Any],
        tc: Dict[str, Any],
        course_kind: str,
        teaching_class_type: str,
        batch_code: str,
) -> Course:
    """解析 ``programCourse.do`` 的父子结构（抓包 ``#39``）。

    父子合并规则：
    - ``course_name`` / ``credit`` / ``hours`` 取**父级** ``course_row``
      （``tcList`` 子行里这些字段为空串/缺失）；
    - ``teaching_class_id`` / ``course_number`` / ``teacher_name`` /
      ``teaching_place`` / ``class_capacity`` / ``number_of_selected`` /
      ``number_of_first_volunteer`` / ``is_full`` / ``is_choose`` /
      ``campus_name`` / ``elective_type`` 取**子行** ``tc``。
    """
    return Course(
        teaching_class_id=_s(tc.get("teachingClassID")),
        course_number=_s(tc.get("courseNumber")),
        course_name=_s(course_row.get("courseName")),
        credit=_s(course_row.get("credit")),
        teacher_name=_s(tc.get("teacherName")),
        teaching_place=_s(tc.get("teachingPlace")),
        campus_name=_s(tc.get("campusName")),
        class_capacity=_s(tc.get("classCapacity")),
        number_of_selected=_s(tc.get("numberOfSelected")),
        number_of_first_volunteer=_s(tc.get("numberOfFirstVolunteer")),
        is_full=_s(tc.get("isFull")),
        is_choose=_s(tc.get("isChoose")),
        course_kind=_s(course_kind),
        teaching_class_type=_s(teaching_class_type),
        elective_type=_s(tc.get("electiveType")),
        hours=_s(course_row.get("hours")),
        kcjj=_s(course_row.get("kcjj")),
        batch_code=_s(batch_code),
    )


def parse_batch(batch_dict: Dict[str, Any]) -> Batch:
    """解析 ``batch.do`` 的单个批次（抓包 ``#24``）。"""
    return Batch(
        code=_s(batch_dict.get("code")),
        name=_s(batch_dict.get("name")),
        begin_time=_s(batch_dict.get("beginTime")),
        end_time=_s(batch_dict.get("endTime")),
        type_name=_s(batch_dict.get("typeName")),
        tactic_name=_s(batch_dict.get("tacticName")),
        school_term=_s(batch_dict.get("schoolTerm")),
        limit_menu_list=list(batch_dict.get("limitMenuList") or []),
    )


def parse_menu(menu_dict: Dict[str, Any]) -> Menu:
    """解析 ``limitMenuList`` 中的单个菜单项。"""
    return Menu(
        menu_code=_s(menu_dict.get("menuCode")),
        menu_name=_s(menu_dict.get("menuName")),
        course_kind=_s(menu_dict.get("courseKind")),
        limit_volunteer=_s(menu_dict.get("limitVolunteer")),
        is_open=_s(menu_dict.get("isopen")),
        is_allow_select=_s(menu_dict.get("isAllowSelect")),
        parent_menu_code=_s(menu_dict.get("parentMenuCode")),
    )


def filter_selectable_menus(menus: List[Menu]) -> List[Menu]:
    """过滤出可参与选课的类别菜单（用于类别区 Tab）。

    过滤规则：
    1. 显式剔除 ``menu_code`` 为 ``SC``（服务端收藏入口，本插件禁用）与
       ``QB``（课表查询）的项；
    2. 剔除 ``course_kind`` 为 ``None``/空/``"-"`` 的**父级菜单**——父级菜单
       即被其他菜单以 ``parentMenuCode`` 引用的分组节点（抓包实证：``GG``、
       ``TX`` 等父级在 ``limitMenuList`` 中 ``courseKind`` 为 null，在
       ``sysparam.menuMap`` 中为 ``"-"``）。叶子菜单（``ZY``/``TY``/``GG01``
       等）即使 ``courseKind`` 为空也保留，其真实取值来自 ``menuMap``。
    """
    parent_codes = {m.parent_menu_code for m in menus if m.parent_menu_code}
    result: List[Menu] = []
    for m in menus:
        if m.menu_code in ("SC", "QB"):
            continue
        if m.course_kind in ("", "-") and m.menu_code in parent_codes:
            continue
        result.append(m)
    return result


def _menu_kind_pairs(menu_list: Any) -> Dict[str, str]:
    """从 ``limitMenuList`` 形态的列表抽取 ``{menu_code: course_kind}``。

    跳过 ``menu_code`` 为空、``course_kind`` 为 None/空/``"-"`` 的项（``"-"`` 是
    父级菜单在 ``sysparam.menuMap`` 中的占位值，抓包 ``#26`` 实证）。
    """
    pairs: Dict[str, str] = {}
    for item in menu_list or []:
        if not isinstance(item, dict):
            continue
        code = _s(item.get("menuCode"))
        kind = _s(item.get("courseKind"))
        if not code or not kind or kind == "-":
            continue
        pairs[code] = kind
    return pairs


def build_course_kind_map(client, batch_limit_menu_list=None) -> Dict[str, str]:
    """构建 ``menu_code → course_kind`` 映射（三级降级，高级覆盖低级）。

    抓包 ``#24``（batch.do）的 ``limitMenuList`` 中 ``courseKind`` 可能全为 null
    （本仓库夹具 53 条菜单全为 null），真实取值在两处：

    1. 抓包 ``#26``（sysparam.do）的 ``data.menuMap``：``GG02 → "6,7"``、
       ``ZY → "1"``、``TY → "2"``、``GG01 → "4"``、``MY → "5"``、``KZY → "12"``
       （父级 ``GG``/``TX``/``SC``/``QB`` 为 ``"-"``）；
    2. 抓包 ``#33``（student/{code}.do）的 ``data.electiveBatchList[].limitMenuList``
       —— 参考脚本 ``xk_favorites.py:149-187`` 与 ``tools/query_course_v2.py:43-93``
       正是从这里取 courseKind。

    按 3（batch 兜底）→ 2（学生信息）→ 1（sysparam）的顺序写入，后者覆盖前者，
    因此返回的是「高级优先」的合并映射。任一步抛异常（含 client 未实现该方法）
    只记 ``logging.debug`` 并降级，**不冒泡**；网络不可用时返回已得到的部分或空 dict。

    ``course_kind`` 一律保持字符串（可能是 ``"6,7"`` 这类含逗号值），禁止转 int。
    """
    mapping: Dict[str, str] = {}

    # 3. 兜底：batch.do 的 limitMenuList（可能全为空）
    mapping.update(_menu_kind_pairs(batch_limit_menu_list))

    # 2. student/{code}.do 的 electiveBatchList[].limitMenuList
    try:
        data = client.fetch_student() or {}
        for batch in data.get("electiveBatchList") or []:
            mapping.update(_menu_kind_pairs((batch or {}).get("limitMenuList")))
    except Exception as e:
        logging.debug("从学生信息接口构建 course_kind 映射失败：%s", e)

    # 1. sysparam.do 的 menuMap（优先级最高）
    try:
        data = client.fetch_sysparam() or {}
        menu_map = data.get("menuMap") or {}
        if isinstance(menu_map, dict):
            for code, kind in menu_map.items():
                code_s, kind_s = _s(code), _s(kind)
                if code_s and kind_s and kind_s != "-":
                    mapping[code_s] = kind_s
    except Exception as e:
        logging.debug("从 sysparam 构建 course_kind 映射失败：%s", e)

    return mapping


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def selection_probability(course: Course, tactic_name: str | None = None) -> str:
    """抽签制选中概率 = 容量 / 报名人数（展示用）。

    用户原话：「选课的逻辑是从报名人数中抽取指定人数，如果1000个人报容量
    100人的课程，概率就是10%」。卡片 ``X/Y`` 中 ``X``（``number_of_selected``）
    是**报名人数**，``Y``（``class_capacity``）是**容量**，概率即
    ``容量 / 报名人数``。

    规则（**顺序即语义，不可调换**）：
    1. ``is_full == "1"``（服务端标记已满）→ ``"0%"``，优先于一切；
    2. 容量不可解析（缺失/非数字）→ ``"—"``；容量 ``<= 0`` → ``"0%"``；
    3. 报名人数不可解析/为空/``None``/``<= 0`` → 视为无人报名 → 无人竞争
       → ``"100%"``（如 ``0/185``）；
    4. ``pct = round(容量 / 报名人数 * 100)``，夹取到 ``[0, 100]``
       （容量 > 报名人数时结果 ``> 100%``，夹取为 ``"100%"``）。

    为什么只用这两个数字（用户明确要求）：
    - ``numberOfFirstVolunteer`` 在真实响应中**恒为 null**（夹具 12 条全为 null），
      此前依赖它做分支是显示「—」的根因，故**不再读取**；
    - ``tacticName`` 在不同批次下取值不一致（「先选先得」/「可选可退」/…），
      分支判断不稳定，故**不再依赖**。``tactic_name`` 形参仅为兼容既有调用保留
      （``ui/cards.py``、``ui/favorites.py`` 仍按位置传参），**不参与计算**。

    健壮性：字段为 ``None``/空串/非数字都不抛异常（``_to_float`` 返回 ``None``）。
    """
    # tactic_name 不参与计算（见 docstring），仅为兼容既有调用保留
    if course.is_full == "1":
        return "0%"  # 1. 服务端标记已满，优先于一切
    capacity = _to_float(course.class_capacity)
    if capacity is None:
        return "—"  # 2. 容量缺失，无法计算
    if capacity <= 0:
        return "0%"  # 2. 容量为 0（或异常负值），无人能被抽中
    applicants = _to_float(course.number_of_selected)
    if applicants is None or applicants <= 0:
        return "100%"  # 3. 无人报名（0/185）→ 无人竞争 → 100%
    pct = round(capacity / applicants * 100)  # 4. 抽签：容量 / 报名人数
    pct = max(0, min(100, pct))  # 夹取：容量 > 报名人数等异常数据 → 100%
    return f"{pct}%"
