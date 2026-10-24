# -*- coding: utf-8 -*-
"""地名标准化：国家 / 省份统一为官方正式地名，市级只做自治州安全展开。

应用于：Excel 初始导入、网页录入/修改、Excel 导入与粘贴。
映射规则只做"补全到官方全称"，不改动未知值，避免误伤境外地名。
"""

import re

# 34 个省级行政区官方全称
PROVINCE_OFFICIAL = [
    "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省",
    "黑龙江省", "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省",
    "山东省", "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区", "海南省",
    "重庆市", "四川省", "贵州省", "云南省", "西藏自治区", "陕西省", "甘肃省",
    "青海省", "宁夏回族自治区", "新疆维吾尔自治区", "香港特别行政区",
    "澳门特别行政区", "台湾省",
]

_PROV_MAP = {
    # 直辖市
    "北京": "北京市", "首都": "北京市",
    "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    # 省：简写补"省"
    **{p[:-1]: p for p in PROVINCE_OFFICIAL if p.endswith("省")},
    # 自治区：常见简写与旧称
    "广西": "广西壮族自治区", "广西省": "广西壮族自治区",
    "内蒙古": "内蒙古自治区", "内蒙": "内蒙古自治区",
    "西藏": "西藏自治区",
    "新疆": "新疆维吾尔自治区", "新疆维吾尔族自治区": "新疆维吾尔自治区", "新疆省": "新疆维吾尔自治区",
    "宁夏": "宁夏回族自治区",
    # 特别行政区
    "香港": "香港特别行政区", "澳门": "澳门特别行政区",
    "台湾地区": "台湾省",
}
_PROV_MAP.update({p: p for p in PROVINCE_OFFICIAL})

# 国家：常见变体归一（未列出的原样保留，不猜测境外地名）
_COUNTRY_MAP = {
    "中国": "中国", "中华人民共和国": "中国", "中国大陆": "中国", "大陆": "中国",
    "china": "中国", "cn": "中国",
    "马来西亚": "马来西亚", "malaysia": "马来西亚",
    "泰国": "泰国", "thailand": "泰国",
    "缅甸": "缅甸", "myanmar": "缅甸", "burma": "缅甸",
    "越南": "越南", "vietnam": "越南",
    "老挝": "老挝", "laos": "老挝",
    "印度": "印度", "india": "印度",
    "尼泊尔": "尼泊尔", "nepal": "尼泊尔",
    "俄罗斯": "俄罗斯", "russia": "俄罗斯",
    "蒙古": "蒙古国", "mongolia": "蒙古国",
    "日本": "日本", "japan": "日本",
    "韩国": "韩国", "朝鲜": "朝鲜",
    "美国": "美国", "usa": "美国", "united states": "美国",
}

# 30 个自治州官方全称（市级"XX州"简称 → 全称，仅展开不缩写）
_PREFECTURES = [
    "甘孜藏族自治州", "阿坝藏族羌族自治州", "凉山彝族自治州",
    "西双版纳傣族自治州", "德宏傣族景颇族自治州", "怒江傈僳族自治州",
    "迪庆藏族自治州", "大理白族自治州", "楚雄彝族自治州",
    "红河哈尼族彝族自治州", "文山壮族苗族自治州",
    "黔东南苗族侗族自治州", "黔南布依族苗族自治州", "黔西南布依族苗族自治州",
    "伊犁哈萨克自治州", "博尔塔拉蒙古自治州", "巴音郭楞蒙古自治州",
    "克孜勒苏柯尔克孜自治州", "昌吉回族自治州",
    "海南藏族自治州", "海北藏族自治州", "黄南藏族自治州", "果洛藏族自治州",
    "玉树藏族自治州", "海西蒙古族藏族自治州", "甘南藏族自治州", "临夏回族自治州",
    "恩施土家族苗族自治州", "湘西土家族苗族自治州", "延边朝鲜族自治州",
]


def norm_country(v):
    if v is None:
        return v
    s = str(v).strip()
    if not s:
        return s
    low = s.lower()
    if low in _COUNTRY_MAP:
        return _COUNTRY_MAP[low]
    return s


def norm_province(v):
    if v is None:
        return v
    s = str(v).strip()
    if not s:
        return s
    return _PROV_MAP.get(s, s)


def norm_city(v):
    if v is None:
        return v
    s = str(v).strip()
    if s.endswith("州") and len(s) > 2:
        base = s[:-1]
        for p in _PREFECTURES:
            if p.startswith(base) and p != s:
                return p
    return s


# 已知国家（标准化目标值 + 常见变体），数据体检用于发现"疑似填错的国家"
KNOWN_COUNTRIES = {v.lower() for v in _COUNTRY_MAP.values()} | {k.lower() for k in _COUNTRY_MAP}


def normalize_geo(data):
    """对含 Country/Province/City 的字典原地标准化，返回发生变化的字段名列表。"""
    changed = []
    for key, fn in (("Country", norm_country), ("Province", norm_province), ("City", norm_city)):
        v = data.get(key)
        if v is None:
            continue
        nv = fn(v)
        if nv != v:
            data[key] = nv
            changed.append(key)
    return changed


# ---------------- 坐标书写解析：度分(秒)换算 / 装饰小数度净化 ----------------
# 设计约束：不改变科学事实——度分秒换算、半球字母作方向声明、去掉 °/字母/中文装饰；
# 歧义书写（三位无小数点的分/秒、多段数字、坐标区间）一律不猜，留给人工。
_PRE_HEMI = r"(?:[NSEW]|北纬|南纬|东经|西经)"
_SUF_HEMI = r"(?:[NSEW]|北纬|南纬|东经|西经)"

# 度分(秒)：度符必带，分符必带，秒符可省（27°41′42 与 27°41'41.8" 均可）；
# ° 充当秒符（14°36'29°N 误敲）单独捕获，须以分符为前提，防止 27°41.967°N 误读
_DMS_PAT = re.compile(
    rf"""^\s*(?P<pre>{_PRE_HEMI})?\s*[:：]?\s*
        (?P<deg>-?\d{{1,3}}(?:\.\d+)?)\s*[°度d]\s*
        (?:(?P<min>\d{{1,2}}(?:\.\d+)?)\s*[′'分m]\s*
        (?:(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*(?P<secm>[″"秒s°])?)?)?\s*
        (?P<ns>{_SUF_HEMI})?\s*$""",
    re.X | re.I,
)

# 小数度：可带 ° / 半球字母 / 中文半球 / 前缀（N:28.167124°）
_DEC_PAT = re.compile(
    rf"""^\s*(?P<pre>{_PRE_HEMI})?\s*[:：]?\s*
        (?P<deg>-?\d{{1,3}}(?:\.\d+)?)\s*°?\s*
        (?P<ns>{_SUF_HEMI})?\s*$""",
    re.X | re.I,
)


def _coord_text(text):
    if text is None:
        return None
    s = str(text).strip()
    if not s or not re.search(r"\d", s):
        return None
    # 双撇号/全角引号统一为标准秒符：27°50′053′′N → 27°50′053″N
    for a, b in (("′′", "″"), ("''", '"'), ("′\"", "″"), ("\"′", "″"),
                 ("“", '"'), ("”", '"'), ("′″", "″")):
        s = s.replace(a, b)
    # 小数点两侧误敲的空格：100°51. 710'E → 100°51.710'E
    s = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", s)
    return s


def _hemi_sign(pre, suf):
    h = ((pre or "") + (suf or "")).upper()
    if "S" in h or "南" in h:
        return "S"
    if "W" in h or "西" in h:
        return "W"
    return "N" if h else ""


def _dms_decimal(m):
    mag = abs(float(m.group("deg"))) + float(m.group("min") or 0) / 60 + float(m.group("sec") or 0) / 3600
    val = -mag if float(m.group("deg")) < 0 else mag
    h = _hemi_sign(m.group("pre"), m.group("ns"))
    if h in ("S", "W") and val > 0:
        val = -val  # 半球字母即方向声明；负号原样保留（不再丢符号）
    return round(val, 6)


def classify_coord(text):
    """坐标书写分类。返回 (decimal or None, kind)：
      'dms'       度分(秒)，decimal 为换算结果（数值改变）
      'decorated' 小数度带装饰（°/半球/前缀），decimal 为净化值（数值不变*）
      'plain'     纯小数度，无需处理（decimal=None）
      None        无法识别或歧义（多段数字/三位分秒/区间），decimal=None，需人工
    * 唯一例外：正数带 S/W 半球字母按声明取负（27.699S → -27.699）。"""
    s = _coord_text(text)
    if s is None:
        return (None, "empty" if text is None or not str(text).strip() else None)
    m = _DMS_PAT.match(s)
    if m and m.group("min"):  # 度分(秒)须有分；仅带 ° 的数不算（见 _DEC_PAT）
        return (_dms_decimal(m), "dms")
    md = _DEC_PAT.match(s)
    if md:
        val = float(md.group("deg"))
        h = _hemi_sign(md.group("pre"), md.group("ns"))
        if h in ("S", "W") and val > 0:
            val = -val
        decorated = ("°" in s) or bool(h) or (":" in s)
        if not decorated:
            return (None, "plain")
        return (round(val, 6), "decorated")
    return (None, None)


def normalize_coord_value(text):
    """可自动处理的坐标 → (decimal, kind)；kind 为 'dms' 或 'decorated'。
    纯小数度与无法识别的输入一律返回 (None, None)（保持原样）。"""
    val, kind = classify_coord(text)
    if kind in ("dms", "decorated"):
        return (val, kind)
    return (None, None)


def parse_coord(text):
    """兼容旧签名：返回 (decimal, was_dms, hemisphere) 或 None。"""
    s = _coord_text(text)
    if s is None:
        return None
    val, kind = classify_coord(s)
    if val is None:
        return None
    if kind == "dms":
        return (val, True, "")
    return (val, False, "")


def convert_dms_value(text):
    """把度分(秒)格式坐标转为小数度。非 DMS 输入返回 (None, '')。
    返回 (decimal or None, hemisphere)。"""
    val, kind = classify_coord(text)
    if kind == "dms":
        return (val, "")
    return (None, "")


def is_plausible(lat, lon, country=""):
    """粗粒度合理性核实：数值范围 + 常见国家的粗边界框。"""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False, "非数字"
    if not (-90 <= la <= 90):
        return False, "纬度超范围"
    if not (-180 <= lo <= 180):
        return False, "经度超范围"
    cn = str(country or "").strip().lower()
    # 常用国家的粗边界框（仅用于提示，不精确）
    boxes = {
        "中国": (15, 55, 70, 140), "china": (15, 55, 70, 140),
        "马来西亚": (-5, 10, 95, 125), "malaysia": (-5, 10, 95, 125),
        "泰国": (5, 22, 95, 108), "thailand": (5, 22, 95, 108),
        "越南": (8, 25, 100, 112), "vietnam": (8, 25, 100, 112),
        "老挝": (13, 24, 99, 110), "laos": (13, 24, 99, 110),
        "缅甸": (9, 30, 90, 105), "myanmar": (9, 30, 90, 105),
        "印度尼西亚": (-12, 8, 93, 142), "indonesia": (-12, 8, 93, 142),
        "印度": (6, 38, 68, 100), "india": (6, 38, 68, 100),
        "尼泊尔": (26, 31, 79, 89), "nepal": (26, 31, 79, 89),
        "日本": (30, 46, 128, 146), "japan": (30, 46, 128, 146),
        "俄罗斯": (40, 82, 25, 180), "russia": (40, 82, 25, 180),
        "美国": (17, 72, -180, -60), "united states": (17, 72, -180, -60),
        "巴西": (-35, 6, -75, -30), "brazil": (-35, 6, -75, -30),
        "澳大利亚": (-45, -8, 110, 155), "australia": (-45, -8, 110, 155),
    }
    for k, (la1, la2, lo1, lo2) in boxes.items():
        if cn == k or cn in k or k in cn:
            ok = (la1 <= la <= la2) and (lo1 <= lo <= lo2)
            return ok, "" if ok else "坐标超出该国粗边界"
    return True, ""
