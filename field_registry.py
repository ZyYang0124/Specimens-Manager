# -*- coding: utf-8 -*-
"""字段注册表：日期/坐标/保存浓度/个体数量的类型、单位、范围、缺失语义、
解析与显示规则的集中定义。VERSION 随解析规则变更递增，与 raw 原文一起落库。

设计口径：
  - raw 原文永远保留（field_raw 表），解析值写入业务列；
  - 缺失语义：日期各分量可独立缺失 → 精度自动降级（完整日/年月/仅年/未知）；
    严禁为仅年/仅年月记录补 1 月 1 日；
  - 数量：非负整数或「未知」；未知不当作 0；
  - 坐标：十进制度、度分(秒) DMS、N/S/E/W 半球；歧义文本（如 30.03.965′N）
    解析置信度=ambiguous，进入人工队列，绝不自动反推地点；
  - 范围与省界检查只提示疑点（scan 端点），不自动写值。
"""
import re

VERSION = "1.0"

# ---------------------------------------------------------------- 日期
DATE_GROUP = {"start": ("DD1", "MM1", "YYYY1"), "end": ("DD2", "MM2", "YYYY2")}
_MONTH_LEN = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def parse_date_parts(dd, mm, yyyy):
    """解析日期三分量 → {precision, iso, error}。
    precision: day（完整日，闰日合法）/ month（仅年月）/ year（仅年）/ unknown。
    严禁补 1 月 1 日：仅年/仅年月只返回对应精度字符串。"""
    s_dd, s_mm, s_yy = (str(dd or "").strip(), str(mm or "").strip(), str(yyyy or "").strip())
    if not (s_dd or s_mm or s_yy):
        return {"precision": "unknown", "iso": "", "error": "日期未知"}
    if not s_yy:
        return {"precision": "unknown", "iso": "", "error": "有日/月但缺年份"}
    if not s_yy.isdigit() or not (1700 <= int(s_yy) <= 2200):
        return {"precision": "unknown", "iso": "", "error": f"年份无效：{s_yy}"}
    y = int(s_yy)
    if not s_mm:
        if s_dd:
            return {"precision": "unknown", "iso": "", "error": "有日无月（缺月份）"}
        return {"precision": "year", "iso": "%04d" % y, "error": ""}
    if not s_mm.isdigit():
        return {"precision": "unknown", "iso": "", "error": f"月份无效：{s_mm}"}
    m = int(s_mm)
    if not 1 <= m <= 12:
        return {"precision": "unknown", "iso": "", "error": f"月份 {m} 无效（1-12）"}
    if not s_dd:
        return {"precision": "month", "iso": "%04d-%02d" % (y, m), "error": ""}
    if not s_dd.isdigit():
        return {"precision": "unknown", "iso": "", "error": f"日无效：{s_dd}"}
    d = int(s_dd)
    maxd = _MONTH_LEN[m] + (1 if (m == 2 and _is_leap(y)) else 0)
    if not 1 <= d <= maxd:
        extra = "（闰日合法）" if (m == 2 and d == 29 and _is_leap(y)) else ""
        return {"precision": "unknown", "iso": "", "error": f"日 {d} 无效{extra}"}
    return {"precision": "day", "iso": "%04d-%02d-%02d" % (y, m, d), "error": ""}


# ---------------------------------------------------------------- 坐标
_DMS = re.compile(
    r"^\s*(?P<deg>\d{1,3})(?:[°度]\s*(?P<min>\d{1,2}(?:\.\d+)?)(?:[′'分]\s*"
    r"(?P<sec>\d{1,2}(?:\.\d+)?)?[″\"秒]?)?)?\s*"
    r"(?P<hemi>[NnSsEeWw][^0-9.,]*|北纬|南纬|东经|西经)?\s*$")
_AMBIG = re.compile(r"\d+\.\d+\.\d+")          # 30.03.965′N —— 两段小数点，歧义
_DEC = re.compile(r"^\s*(?P<deg>-?\d{1,3}(?:\.\d+)?)\s*°?\s*(?P<hemi>[NnSsEeWw]|北纬|南纬|东经|西经)?\s*$")


def parse_coordinate(raw, axis):
    """解析坐标 → {decimal, precision(dms|decimal), hemi, confidence(high|ambiguous), error}。
    axis: lat/lon（决定半球方向与范围提示）。歧义文本 confidence=ambiguous（人工队列）。"""
    s = str(raw or "").strip()
    if not s:
        return {"confidence": "missing", "error": "坐标缺失", "decimal": None}
    if _AMBIG.search(s):
        return {"confidence": "ambiguous", "decimal": None,
                "error": f"歧义坐标「{s}」（多段数字无法判定度分），已入人工队列"}
    hemi_lat, hemi_lon = ("北纬", "南纬"), ("东经", "西经")
    neg = 0
    for pre, hset in (("北纬", hemi_lat), ("南纬", hemi_lat), ("东经", hemi_lon), ("西经", hemi_lon)):
        if s.startswith(pre):
            s = s[len(pre):].strip()
            neg = 1 if pre in ("南纬", "西经") else 0
            break
    m = _DMS.match(s) or _DEC.match(s)
    if not m:
        return {"confidence": "ambiguous", "decimal": None,
                "error": f"无法解析坐标「{s}」（请使用十进制度或 度分[秒]+N/S/E/W）"}
    gd = m.groupdict()
    hemi = (gd.get("hemi") or "").upper()
    deg = float(gd.get("deg") or 0)
    mi = float(gd.get("min") or 0)
    se = float(gd.get("sec") or 0)
    if hemi in ("S", "W") and deg > 0:
        deg = -deg          # 半球字母给方向；裸负号视为西/南（十进制）
    val = deg + mi / 60 + se / 3600
    if neg and val > 0:
        val = -val
    precision = "dms" if (mi or se) else "decimal"
    lim = 90 if axis == "lat" else 180
    if abs(val) > lim:
        return {"confidence": "ambiguous", "decimal": None,
                "error": f"坐标超{ '纬度' if axis == 'lat' else '经度' }范围（±{lim}）：{val}"}
    return {"decimal": round(val, 6), "precision": precision,
            "hemi": "S" if val < 0 and axis == "lat" else ("W" if val < 0 and axis == "lon" else hemi),
            "confidence": "high", "error": ""}


# ---------------------------------------------------------------- 保存浓度 / 数量
CONC_PATTERN_NOTE = "百分比数字（75/85/95/100）或小数比例（0.95→95）；带说明文字原样保留"


def parse_concentration(raw):
    """保存浓度：0.95/95/95%/75%ethanol → 百分比数字 + 原单位说明；
    带中文说明（如 95%（死后泡））→ 原样保留（历史信息不丢）。"""
    if raw is None:
        return {"value": None, "unit_note": "", "error": ""}
    if isinstance(raw, (int, float)):
        f = float(raw)
        if 0 < f <= 1.0001:
            return {"value": round(f * 100) if abs(f * 100 - round(f * 100)) < 0.01 else round(f * 100, 1),
                    "unit_note": "小数比例 ×100", "error": ""}
        return {"value": round(f, 1) if abs(f - round(f)) < 0.001 else f, "unit_note": "", "error": ""}
    s = str(raw).strip()
    if not s:
        return {"value": None, "unit_note": "", "error": ""}
    if re.fullmatch(r"0\.[0-9]+|1(\.0{0,2})?", s):
        f = float(s)
        return {"value": round(f * 100) if abs(f * 100 - round(f * 100)) < 0.01 else round(f * 100, 1),
                "unit_note": "小数比例 ×100", "error": ""}
    if re.fullmatch(r"[0-9]{2,3}", s):
        n = int(s)
        if 0 < n <= 100:
            return {"value": n, "unit_note": "", "error": ""}
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(%|％)(.*)", s)
    if m and not re.search(r"[一-鿿]", m.group(3) or ""):
        num = float(m.group(1))
        return {"value": round(num) if abs(num - round(num)) < 0.01 else num,
                "unit_note": "百分比写法", "error": ""}
    return {"value": s, "unit_note": "含说明文字，原样保留", "error": ""}


COUNT_UNKNOWN = ("未知", "?", "unknown")


def parse_count(raw):
    """个体数量：非负整数；未知（未知/?/unknown）→ unknown=True（不当 0）；负数/文本 → error。"""
    if raw is None:
        return {"value": None, "unknown": False, "error": ""}
    if isinstance(raw, str) and raw.strip() in COUNT_UNKNOWN:
        return {"value": None, "unknown": True, "error": ""}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {"value": None, "unknown": False, "error": ""}
        if not re.fullmatch(r"\d+", s):
            return {"value": None, "unknown": False,
                    "error": f"数量需为非负整数或「未知」（当前：{raw}）"}
        raw = int(s)
    v = int(raw)
    if v < 0:
        return {"value": None, "unknown": False, "error": f"数量不能为负数（当前：{v}）"}
    return {"value": v, "unknown": False, "error": ""}


# ---------------------------------------------------------------- 注册表（供表单提示/模板说明）
COUNT_FIELDS_ALL = ["AdultMale", "AdultFemale", "Subadult", "Juvenile",
                    "标本数量", "雄性数量", "雌性数量", "幼体数量"]

FIELD_HINTS = {
    "DD1": "采集开始日-日（1-31；闰日 2 月 29 日合法）",
    "MM1": "采集开始日-月（1-12；仅年月精度时留空日）",
    "YYYY1": "采集开始年（1700-2200；仅填年 = 仅年精度，不补 1 月 1 日）",
    "DD2": "采集结束日-日（起止区间时填写）",
    "MM2": "采集结束月（起止区间时填写）",
    "YYYY2": "采集结束年",
    "Latitude1": "纬度：十进制度（22.033）或 度分(秒)+N/S（27°41.967′N）；南纬用 S 或负数",
    "Longitude1": "经度：十进制度或 度分(秒)+E/W（108°50.306′E）；西经用 W 或负数",
    "Latitude2": "纬度结束值（跨纬度采集区间）",
    "Longitude2": "经度结束值",
    "PreserveCondition(75%, 85%, 95%, 100%)": "保存浓度 %（75/85/95/100）；0.95 等小数自动×100；可带说明文字原样保留",
    "AdultMale": "成体雄性数量：非负整数；未知填「未知」（不当作 0）",
    "AdultFemale": "成体雌性数量：非负整数或「未知」",
    "Subadult": "亚成体数量：非负整数或「未知」",
    "Juvenile": "幼体数量：非负整数或「未知」",
    "标本数量": "标本总数量：非负整数或「未知」",
    "雄性数量": "雄性数量：非负整数或「未知」",
    "雌性数量": "雌性数量：非负整数或「未知」",
    "幼体数量": "幼体数量：非负整数或「未知」",
}
