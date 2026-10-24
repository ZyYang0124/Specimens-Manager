# -*- coding: utf-8 -*-
"""统一写入校验服务层 —— 所有写入入口（单行新增/修改、批量、导入、恢复、撤销）
共用同一套规则：字段白名单、权限、必填、主键、锁定枚举、单位归一、标本盒、
关联列与分类链。规则只在这里定义一次。

兼容例外（必须显式声明，不允许静默）：
  COMPAT_STRICT        默认。违反任何规则 → 拦截。
  COMPAT_HISTORY       「历史数据兼容」：存量 Excel/外部数据导入，允许违反必填/枚举
                       等当前约束，违规项作为警告返回并由调用方写入审计日志
                       （changes._compat = "history_import"）。
  COMPAT_EXACT_RESTORE 「精确历史恢复」：版本恢复/撤销按历史值原样写回，跳过业务
                       规则校验，仅做结构校验；调用方必须在日志标注
                       changes._compat = "exact_restore"。

注意：server.py 在请求处理器内延迟 import 本模块（避免循环导入）；
本模块在函数体内延迟访问 server 的原语（same/clean_val/...），保证首次调用时
server 已完成初始化。
"""


import re

import field_registry as FR


class WriteValidationError(Exception):
    """校验失败（可安全转为 400 响应；payload 携带字段级细节）。"""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


COMPAT_STRICT = "strict"
COMPAT_HISTORY = "history"
COMPAT_EXACT_RESTORE = "exact_restore"


def _srv():
    import server
    return server


# ---------------------------------------------------------------- 权限
def guard_write(user, tname):
    """权限闸：表可见 + 该表可写。所有写入入口的第一道检查。"""
    s = _srv()
    s.ensure_visible(user, tname)
    s.require_writer(user, tname)
    s.chk_table(tname)


# ---------------------------------------------------------------- 白名单 / 关联列
def whitelist(tname, data_in, *, allow_linked_key=True):
    """字段白名单：只允许本表自有列（+ 可选的关联键）。
    未知字段 → 400 并列出字段名（不静默丢弃）；关联列 → 指引到来源表修改。"""
    s = _srv()
    cfg = s.TABLES[tname]
    own = set(s.own_columns(tname))
    if allow_linked_key and cfg.get("linked_key"):
        own.add(cfg["linked_key"])
    linked = set(s.linked_columns(tname))
    unknown = [k for k in data_in if k not in own and k not in linked]
    if unknown:
        raise WriteValidationError(
            f"未知字段：{'、'.join(sorted(unknown))}。"
            f"请核对列名（可从工具栏「导出表头」获取本表全部有效列名）",
            {"unknown": sorted(unknown)})
    in_linked = [k for k in data_in if k in linked and k not in own]
    if in_linked:
        raise WriteValidationError(
            f"以下列来自“采集地信息”等关联表，请到对应来源表修改：{'、'.join(sorted(in_linked))}",
            {"linked": sorted(in_linked)})
    return {k: s.clean_val(v) for k, v in data_in.items() if k in own}


def check_changes_writable(tname, changes):
    """单行/批量修改的 changes 白名单：禁止改关联列；未知字段 400。"""
    s = _srv()
    linked = set(s.linked_columns(tname))
    bad = linked & set(changes)
    if bad:
        raise WriteValidationError(
            f"以下列来自“采集地信息”，请到采集地信息页修改：{'、'.join(sorted(bad))}",
            {"linked": sorted(bad)})
    own = set(s.own_columns(tname))
    unknown = [k for k in changes if k not in own]
    if unknown:
        raise WriteValidationError(
            f"未知字段：{'、'.join(sorted(unknown))}",
            {"unknown": sorted(unknown)})


# ---------------------------------------------------------------- 单位 / 枚举 / 编号归一
def normalize(tname, data, *, is_create=False):
    """单位与取值归一（原地修改）：酒精浓度、锁定枚举、主键编号格式。"""
    s = _srv()
    cfg = s.TABLES[tname]
    if cfg.get("alcohol_col") and cfg["alcohol_col"] in data:
        data[cfg["alcohol_col"]] = s.normalize_alcohol(data[cfg["alcohol_col"]])
    s.normalize_locked_enums(tname, data, apply_defaults=is_create)
    if tname in s.KEY_PREFIX_RE and data.get("MHBU #"):
        data["MHBU #"] = s.normalize_spec_key(data["MHBU #"], kind="specimens")
    if tname == "mygalomorph" and data.get("KYUARA #"):
        data["KYUARA #"] = s.normalize_spec_key(data["KYUARA #"], kind="mygalomorph")
    return data


# ---------------------------------------------------------------- 必填 / 主键 / 盒号
def validate_constraints(tname, data, *, exclude_rid=None, old=None):
    """结构性约束：必填（创建严格 / 编辑仅拦清空）、主键查重、盒号占用与号段。
    old 传入时按「编辑」口径（只拦清空必填）。"""
    s = _srv()
    if old is not None:
        d2 = dict(data)
        d2["_old"] = old
        s._check_required(tname, d2, exclude_rid=exclude_rid, only_clearing=True)
    else:
        s._check_required(tname, data, exclude_rid=exclude_rid)
    cfg = s.TABLES[tname]
    key = cfg["key"]
    if key and key in data and not cfg.get("key_not_unique"):
        s.check_key_unique(tname, data[key], exclude_rid=exclude_rid)
    if tname == "specimens":
        box = data.get("Box # (Start#-End#)")
        if box:
            s._check_box_available(box, exclude_rid=exclude_rid)
            s._check_box_number_range(box, data.get("MHBU #"))
    # 字段注册表：日期三分量（月13/日越界拒绝；仅年/年月精度不补 1 月 1 日）与数量（非负或「未知」）
    for gname, (fdd, fmm, fyy) in FR.DATE_GROUP.items():
        if fdd in data or fmm in data or fyy in data:
            p0 = s.FR.parse_date_parts(data.get(fdd), data.get(fmm), data.get(fyy))
            if p0["error"]:
                raise WriteValidationError(f"{gname} 日期：{p0['error']}")
    for f in FR.COUNT_FIELDS_ALL:
        if f in data:
            p0 = s.FR.parse_count(data[f])
            if p0["error"]:
                raise WriteValidationError(f"{f}：{p0['error']}")


# ---------------------------------------------------------------- 分类链 / 外部名录
def resolve_taxonomy(tname, data, *, old=None, force_name=False):
    """分类与名录解析（外部数据源：WSC/WAC 本地名录文件）。
    必须在写事务开始之前调用——名录文件读取不占用数据库写锁；
    提交阶段仅需核对行的基础状态（row_version 条件更新已覆盖）。"""
    s = _srv()
    cfg = s.TABLES[tname]
    warnings = []
    if cfg.get("genus_lookup"):
        tc = s.TD.taxon_cols(tname)
        if data.get(tc["final"]):
            sp = s.check_species_name(data[tc["final"]], force=force_name, cols=tc)
            if sp:
                if sp["name"] != data[tc["final"]]:
                    if sp["corrected"]:
                        warnings.append(f"已按 WSC 将物种名更新为接受名「{sp['name']}」")
                    data[tc["final"]] = sp["name"]
                if sp.get("genus") and data.get(tc["genus"]) != sp["genus"]:
                    data[tc["genus"]] = sp["genus"]
        fill = s.taxonomy_fill(data, tname)
        if fill:
            data.update(fill)
        else:
            for k, v in s.taxonomy_fill_upward(data, tname).items():
                data.setdefault(k, v)
    if tname == "pseudoscorpiones" and ("属" in data or "种" in data):
        fixed, wac_warn = s._wac_apply(tname, dict(data))
        for k in ("属", "种", "科"):
            if k in data and k in fixed:
                data[k] = fixed[k]
        if wac_warn:
            warnings.append(wac_warn)
    return data, warnings


# ---------------------------------------------------------------- 鉴定名称限定符（cf./aff./sp.）
_QUAL_WORDS = {"cf": "cf.", "cfr": "cf.", "aff": "aff.", "sp": "sp.",
               "gen": "gen.", "sp.nov": "sp. nov."}
_QUAL_DETAIL = {
    "cf.": "cf.（与该种相近，鉴定未完全确定）",
    "aff.": "aff.（接近该种，分类归属待定）",
    "sp.": "sp.（属内未定种）",
    "sp. nov.": "sp. nov.（疑似新种，尚未发表）",
    "gen.": "gen.（属级鉴定，未到种）",
}


def parse_det_name(name):
    """从鉴定名称中识别 cf./aff./sp.(nov.)/gen. 限定符并给出可核对的「核心名」。

    返回 {"qualifier": "cf."|None, "detail": 说明|None,
          "core": 去掉限定符/命名人后的名（属+种加词 或 仅属）}。
    核心名用于名录核对（cf./sp. 名称本身必然不在名录中，按核心名核对收录状态）。"""
    toks = [t for t in re.split(r"[\s,;]+", str(name or "").strip()) if t]
    quals, core = [], []
    for i, t in enumerate(toks):
        tl = t.lower().strip("()").rstrip(".").replace(" ", "")
        if tl == "nov":
            if quals and quals[-1] == "sp.":
                quals[-1] = "sp. nov."
            continue
        q = _QUAL_WORDS.get(tl) or _QUAL_WORDS.get(tl.rstrip("."))
        if q:
            quals.append(q)
            continue
        if t.startswith("(") or tl.isdigit() or "&" in t:
            continue  # 命名人/年份不入核心名
        if not core:
            core.append(t)                    # 属名（首词）
        elif len(core) < 3 and t[0].islower():
            core.append(t)                    # 种加词/亚种加词（拉丁名为小写起头）
    detail = _QUAL_DETAIL[quals[0]] if quals else None
    return {"qualifier": quals[0] if quals else None, "detail": detail,
            "core": " ".join(core)}


# ---------------------------------------------------------------- 兼容例外（显式，不静默）
def compat_check_create(tname, data, compat):
    """兼容模式下的约束降级检查。返回 warnings 列表（调用方必须写入审计日志，
    changes._compat 标注例外类型——不允许静默放行）。
    仅 COMPAT_HISTORY 会降级；strict/exact_restore 仍走常规/跳过逻辑。"""
    warnings = []
    if compat != COMPAT_HISTORY:
        return warnings
    s = _srv()
    cfg = s.TABLES[tname]
    missing = [f for f in (cfg.get("required_fields") or [])
               if not str(data.get(f) or "").strip()]
    if missing:
        warnings.append(f"历史数据兼容：必填字段缺失 {'、'.join(missing)}（按原值保留）")
    colmeta = {c["name"]: c for c in cfg["columns"]}
    for k, v in data.items():
        col = colmeta.get(k)
        if col and col.get("enum_locked") and v not in (None, ""):
            opts = [str(o) for o in (col.get("options") or [])]
            if str(v) not in opts:
                warnings.append(f"历史数据兼容：{k} 值「{v}」不在当前枚举内（按原值保留）")
    return warnings
