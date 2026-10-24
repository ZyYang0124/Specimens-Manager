# -*- coding: utf-8 -*-
"""自定义组合表（v4.07，SCHEMA 3.18）—— 配置驱动的组合查询视图，无需改代码即可把
多个数据表的相关字段组成一张新表（如「系统发育样本总表」= 标本+采集信息+DNA 提取+UCE 文库）。

核心约束：
- 两层权限分开：① 构建器功能权限（users.can_composite，管理员默认，逐用户勾选授权，
  镜像标本盒 can_boxes 交互）；② 每张组合表的访问权限（composite_tables.permissions，
  管理员维护，r/rw 两档）。撤销任一权限都不删除定义或源数据。
- 数据权限 = 组合表授权 × 源表三态权限 × 记录可见性的交集，服务器逐请求实时计算
  （current_user 每次读 users 表 → 收回后下一次请求即 403，不依赖重新登录）。
  管理员勾「可编辑」不会使用户获得源表原本没有的编辑权（写入前逐目标 require_writer）。
- 行粒度显式选择（一行=一条标本/一条提取/一条文库……）；跨表关联只用已确认的
  关系注册表（关系字典：事件号/标本号/DNA 凭证号），编号相似或物种相同绝不自动建关系。
- 一对多必须显式选 展开多行 / 保留主记录（受控单值）；组合膨胀在预览中报告
  原记录数与输出行数；未匹配/多候选按各自语义显示，绝不静默取第一条。
- 行身份由实际源实体（表+行 id）组成，编辑定位到源记录：复用源记录的 row_version
  乐观锁、统一校验层、幂等键、批次与审计；撤销作用于真实源记录。
- 派生字段白名单（文本连接/数量合计/关联计数/条件标签），禁止任意脚本与 SQL；
  派生默认只读，未知不补 0，不增加原数据没有的精度。
- 定义为草稿→发布→停用状态机，配置修订留痕可恢复（只恢复表定义，不恢复源库）；
  配置修改/停用/删除定义不触碰源数据。
"""
import json
import re

from flask import abort, jsonify, request, Response

from tables_def import TABLES, display_label


# ---------------------------------------------------------------- 关系注册表
# 依据 关系字典_样本链_2026-10-03.md 的已确认语义：只有明确值相等（或已确认别名）
# 才建立引用；不在注册表内的关系不存在，构建器无从选择（不猜测关系）。
def _rel(src_t, src_col, tgt_t, name, basis, cardinality, note="", to_column=None):
    """to_column：目标表的匹配列。正向（多对一）关联通常即目标表主键；
    反向（一对多）关联目标是「目标表里指向源表的引用列」（如 提取行 Specimen #、
    文库行 Sample Code），必须显式给出——匹配列错了会把关系建到错误的字段上。"""
    return {"id": f"{src_t}.{tgt_t}", "from_table": src_t, "from_column": src_col,
            "to_table": tgt_t, "to_column": to_column or TABLES[tgt_t]["key"],
            "name": name, "basis": basis,
            "cardinality": cardinality, "note": note}


RELATIONS = [
    # —— 标本/原蛛/伪蝎/借入/UCE → 采集事件（多对一；事件侧字段权威） ——
    _rel("specimens", "CollectionEventNumber", "events", "标本 → 采集事件",
         "标本行 CollectionEventNumber = 采集事件号（稳定引用，改号经别名继续解析）", "many_to_one"),
    _rel("mygalomorph", "CollectionEventNumber", "events", "原蛛 → 采集事件",
         "原蛛行 CollectionEventNumber = 采集事件号（稳定引用）", "many_to_one"),
    _rel("dali", "CollectionEventNumber", "events", "大理借入标本 → 采集事件",
         "台账行 CollectionEventNumber = 采集事件号（稳定引用）", "many_to_one"),
    _rel("swu", "CollectionEventNumber", "events", "西南大学借入标本 → 采集事件",
         "台账行 CollectionEventNumber = 采集事件号（稳定引用）", "many_to_one"),
    _rel("pseudoscorpiones", "采集编号", "events", "伪蝎 → 采集事件",
         "伪蝎行 采集编号 = 采集事件号（稳定引用）", "many_to_one"),
    _rel("uce", "CollectionEventNumber", "events", "UCE 文库 → 采集事件",
         "文库行 CollectionEventNumber = 采集事件号（稳定引用）", "many_to_one"),
    # —— 采集事件 → 各标本表（一对多：一个事件对应多条标本，可展开） ——
    _rel("events", "CollectionEventNumber", "specimens", "采集事件 → 标本（一对多）",
         "事件号 = 标本行 CollectionEventNumber；一个事件可对应多条标本", "one_to_many"),
    _rel("events", "CollectionEventNumber", "mygalomorph", "采集事件 → 原蛛（一对多）",
         "事件号 = 原蛛行 CollectionEventNumber", "one_to_many"),
    _rel("events", "CollectionEventNumber", "dali", "采集事件 → 大理借入（一对多）",
         "事件号 = 台账行 CollectionEventNumber", "one_to_many"),
    _rel("events", "CollectionEventNumber", "swu", "采集事件 → 西南大学借入（一对多）",
         "事件号 = 台账行 CollectionEventNumber", "one_to_many"),
    _rel("events", "CollectionEventNumber", "pseudoscorpiones", "采集事件 → 伪蝎（一对多）",
         "事件号 = 伪蝎行 采集编号", "one_to_many"),
    _rel("events", "CollectionEventNumber", "uce", "采集事件 → UCE 文库（一对多）",
         "事件号 = 文库行 CollectionEventNumber", "one_to_many"),
    # —— 样本链（凭证追溯） ——
    _rel("dna_extract", "Specimen #", "specimens", "DNA 提取 → 标本",
         "提取行 Specimen # = 标本 MHBU #（值相等才建立；合作方说明文字不是编号，无法解析）",
         "many_to_one",
         note="外部合作样本（倪义编号/MaddisonUBC 等）的 Specimen # 为来源说明文字，不参与编号匹配"),
    _rel("specimens", "MHBU #", "dna_extract", "标本 → DNA 提取（一对多）",
         "标本 MHBU # = 提取行 Specimen #；同一标本可多次提取（各持独立凭证）", "one_to_many",
         note="外部合作样本的说明文字不参与匹配", to_column="Specimen #"),
    _rel("uce", "Sample Code", "dna_extract", "UCE 文库 → DNA 提取",
         "文库行 Sample Code（界面名 DNAVoucherID）= 提取凭证 DNAVoucherID", "many_to_one"),
    _rel("dna_extract", "DNAVoucherID", "uce", "DNA 提取 → UCE 文库（一对多）",
         "凭证 DNAVoucherID = 文库行 Sample Code；同一凭证可多次建库", "one_to_many",
         to_column="Sample Code"),
    # —— 伪蝎样本链（v4.23） ——
    _rel("pseudo_dna_extract", "凭证标本位置", "pseudoscorpiones", "伪蝎 DNA 提取 → 伪蝎标本",
         "提取行 凭证标本位置 = 伪蝎标本 标本位置编号（值相等才建立；空值/笔误无法解析）",
         "many_to_one"),
    _rel("pseudoscorpiones", "标本位置编号", "pseudo_dna_extract", "伪蝎标本 → 伪蝎 DNA 提取（一对多）",
         "标本 标本位置编号 = 提取行 凭证标本位置；同一标本可多次提取（各持独立凭证）",
         "one_to_many", to_column="凭证标本位置"),
]
RELATIONS = {r["id"]: r for r in RELATIONS}

JOIN_MODES = ("expand", "keep")     # expand=一对多展开多行；keep=保留主记录（受控单值）
DERIVED_KINDS = {
    "concat": "文本连接（非空值按分隔符连接，空项自动省略）",
    "sum": "数量合计（仅计数值；全部缺失=未记录，未知不补 0）",
    "count_join": "关联记录计数（已确认关联的行数；无关联=0）",
    "label_when": "条件标签（满足全部条件时显示指定文字）",
}
CT_ID_PAT = re.compile(r"^ct_[0-9a-f]{10}$")
FIELD_ID_PAT = re.compile(r"^[a-z_][a-z0-9_]*:[^:]+$")   # <表>:<列>（列名可含空格/#等）


def _srv():
    import server
    return server


# ---------------------------------------------------------------- 权限原语
def can_build(user):
    """构建器功能权限：管理员默认拥有；普通账号由管理员勾选授予。"""
    return bool(user["admin"] or user.get("can_composite"))


def _load_def(conn, ct_id):
    r = conn.execute("SELECT * FROM composite_tables WHERE ct_id=?", (ct_id,)).fetchone()
    return dict(r) if r else None


def parse_grants(perms_raw):
    try:
        v = json.loads(perms_raw or "{}")
    except (TypeError, ValueError):
        v = {}
    if not isinstance(v, dict):
        v = {}
    return {str(k): p for k, p in v.items() if p in ("r", "rw")}


def my_access(user, d, grants=None):
    """组合表访问档位 none/r/rw：管理员与所有者 rw；其余按授权名单。"""
    if user["admin"] or d["owner"] == user["username"]:
        return "rw"
    grants = grants if grants is not None else parse_grants(d["permissions"])
    return grants.get(user["username"], "none")


def _def_visible(user, d):
    """普通用户只见已发布且授权给自己的；管理员与所有者见全部状态。"""
    if user["admin"] or d["owner"] == user["username"]:
        return True
    return d["status"] == "published" and my_access(user, d) in ("r", "rw")


def _require_build(user):
    if not can_build(user):
        abort(403, "使用组合表构建器需要管理员授权（成员与邀请码中勾选「允许组合表构建器」）")


def _require_def(user, ct_id, need=None):
    """载入定义并检查访问；need=r 使用、rw=配置管理（管理员或所有者）。返回 (d, access)。"""
    conn = _srv().get_db()
    d = _load_def(conn, ct_id)
    if not d:
        abort(404, "组合表定义不存在")
    access = my_access(user, d)
    if not _def_visible(user, d):
        abort(403, "组合表「%s」未对您的账号开放" % d["name"])
    if need == "rw" and access != "rw":
        abort(403, "只有管理员或定义所有者可以修改这张组合表的配置")
    return d, access


# ---------------------------------------------------------------- 配置校验
def _check_field(user, base_table, joins, fid):
    """单字段校验 → (表, 列) 或 abort。字段必须来自 主表 或 已配置关联的目标表，
    且该表对配置者可见（构建者只能选择自己有权使用的数据源和字段）。"""
    if not FIELD_ID_PAT.match(fid or ""):
        abort(400, f"字段标识 {fid!r} 不合法（应为 表:列）")
    t, col = fid.split(":", 1)
    if t not in TABLES:
        abort(400, f"字段 {fid} 的来源表 {t} 不存在")
    vis = _srv().visible_tables(user)
    if t not in vis:
        abort(403, f"字段 {fid} 的来源表「{display_label(t)}」对您不可见，不能选用")
    colnames = {c["name"] for c in TABLES[t]["columns"]}
    if col not in colnames:
        abort(400, f"字段 {fid} 不存在于表「{display_label(t)}」（可能已被结构调整移除）")
    if t != base_table and not any(j["path"] == f"{_join_path_key(j)}" and _join_target(j) == t for j in joins):
        abort(400, f"字段 {fid} 来自关联表「{display_label(t)}」，请先配置对应关联路径")
    return t, col


def _join_path_key(j):
    return j.get("path") or ""


def _join_target(j):
    rel = RELATIONS.get(j.get("path") or "")
    return rel["to_table"] if rel else None


def validate_config(user, base_table, cfg):
    """配置结构校验（不做数据查询）。返回 (规范化 cfg, checks, warnings)；问题直接 abort 400。"""
    checks, warnings = [], []
    if base_table not in TABLES:
        abort(400, f"主数据源 {base_table} 不存在")
    if base_table not in _srv().visible_tables(user):
        abort(403, f"主数据源「{display_label(base_table)}」对您不可见")
    joins = []
    seen_targets = set()
    for j in (cfg.get("joins") or []):
        rel = RELATIONS.get(j.get("path") or "")
        if not rel:
            abort(400, f"关联路径 {j.get('path')!r} 不在关系注册表中（编号相似/名称相同不构成关系）")
        if rel["from_table"] != base_table:
            abort(400, f"关联 {rel['id']} 的起点是「{display_label(rel['from_table'])}」，"
                       f"与主数据源「{display_label(base_table)}」不符")
        if rel["to_table"] in seen_targets:
            abort(400, f"关联目标「{display_label(rel['to_table'])}」重复：每张表最多一条关联路径")
        if rel["to_table"] not in _srv().visible_tables(user):
            abort(403, f"关联目标表「{display_label(rel['to_table'])}」对您不可见，不能使用该关联")
        mode = j.get("mode") or ("expand" if rel["cardinality"] == "one_to_many" else "keep")
        if mode not in JOIN_MODES:
            abort(400, f"关联 {rel['id']} 的模式需为 expand/keep")
        if rel["cardinality"] == "many_to_one" and mode == "expand":
            abort(400, f"关联 {rel['id']} 是多对一（每行至多匹配一条目标记录），不支持展开；"
                       f"如需一对多请从对方表为主数据源")
        seen_targets.add(rel["to_table"])
        joins.append({"path": rel["id"], "mode": mode})
        checks.append({"kind": "join", "path": rel["id"], "mode": mode,
                       "basis": rel["basis"], "cardinality": rel["cardinality"]})
    fields = []
    seen_ids = set()
    for f in (cfg.get("fields") or []):
        fid = f.get("id") or ""
        t, col = _check_field(user, base_table, joins, fid)
        if fid in seen_ids:
            abort(400, f"字段 {fid} 重复")
        seen_ids.add(fid)
        colmeta = next(c for c in TABLES[t]["columns"] if c["name"] == col)
        linked_to = colmeta.get("linked")
        fields.append({
            "id": fid, "table": t, "column": col,
            "alias": str(f.get("alias") or "").strip()[:60],
            "width": min(600, max(70, int(f.get("width") or 0) or 0)) or None,
            "pinned": bool(f.get("pinned")), "group": str(f.get("group") or "").strip()[:30],
            "help": str(f.get("help") or "").strip()[:200],
            "editable": bool(f.get("editable")),
        })
        if linked_to:
            src = TABLES[t].get("linked_source")
            checks.append({"kind": "linked_field", "id": fid,
                           "basis": f"关联列，实时取自「{display_label(src)}」"
                                    f"（列 {linked_to}）；在来源表修改一次、全部引用同步"})
        if not f.get("editable"):
            reason = ("来源为「%s」的关联展示列，请到来源表修改" % (display_label(src))
                      if linked_to else None)
            if reason:
                checks.append({"kind": "readonly_reason", "id": fid, "reason": reason})
    if not fields:
        abort(400, "至少选择一个字段")
    derived = []
    for i, dr in enumerate(cfg.get("derived") or []):
        kind = dr.get("kind") or ""
        if kind not in DERIVED_KINDS:
            abort(400, f"派生字段 {i + 1} 的类型 {kind!r} 不在白名单（仅允许：{'、'.join(DERIVED_KINDS)}）；"
                       f"不接受脚本/SQL/文件访问")
        did = str(dr.get("id") or "").strip() or f"derived_{i + 1}"
        if not re.match(r"^derived_[A-Za-z0-9_]{1,30}$", did):
            abort(400, f"派生字段标识 {did!r} 不合法")
        if did in seen_ids:
            abort(400, f"字段标识 {did} 重复")
        seen_ids.add(did)
        item = {"id": did, "kind": kind,
                "label": str(dr.get("label") or "").strip()[:60] or did,
                "width": min(600, max(70, int(dr.get("width") or 0) or 0)) or None,
                "group": str(dr.get("group") or "").strip()[:30],
                "help": str(dr.get("help") or "").strip()[:200]}
        if kind == "concat":
            srcs = dr.get("sources") or []
            if not srcs:
                abort(400, f"派生字段 {did}（文本连接）需要来源字段")
            for s in srcs:
                _check_field(user, base_table, joins, s)
            item["sources"] = srcs
            item["sep"] = str(dr.get("sep") or " ")
            item["missing"] = "dash" if dr.get("missing") != "blank" else "blank"
        elif kind == "sum":
            srcs = dr.get("sources") or []
            if not srcs:
                abort(400, f"派生字段 {did}（数量合计）需要来源字段")
            for s in srcs:
                t, col = _check_field(user, base_table, joins, s)
                colmeta = next(c for c in TABLES[t]["columns"] if c["name"] == col)
                if not colmeta.get("numeric"):
                    warnings.append(f"派生字段 {did} 的来源 {s} 不是数值列：非数字文本将跳过（未知不补 0）")
            item["sources"] = srcs
        elif kind == "count_join":
            path = dr.get("join") or ""
            rel = RELATIONS.get(path)
            if not rel or not any(j["path"] == path for j in joins):
                abort(400, f"派生字段 {did}（关联计数）的关联 {path!r} 未在本表关联配置中")
            item["join"] = path
        elif kind == "label_when":
            conds = dr.get("conditions") or []
            if not conds:
                abort(400, f"派生字段 {did}（条件标签）需要条件")
            norm_conds = []
            for c0 in conds:
                fid = c0.get("field") or ""
                _check_field(user, base_table, joins, fid)
                op = c0.get("op")
                if op not in ("empty", "notempty", "equals"):
                    abort(400, f"派生字段 {did} 的条件类型 {op!r} 不在白名单（empty/notempty/equals）")
                norm_conds.append({"field": fid, "op": op, "value": str(c0.get("value") or "")})
            item["conditions"] = norm_conds
            item["label"] = str(dr.get("label") or "标签")
        derived.append(item)
    # 默认筛选/排序：字段必须存在于本表
    filters = cfg.get("default_filters") or {}
    for fid in filters:
        if fid not in seen_ids:
            abort(400, f"默认筛选的字段 {fid} 不在本表字段清单中")
    sort = cfg.get("default_sort") or {}
    if sort and sort.get("field") not in seen_ids:
        abort(400, f"默认排序字段 {sort.get('field')} 不在本表字段清单中")
    norm = {"joins": joins, "fields": fields, "derived": derived,
            "default_filters": filters, "default_sort": sort,
            "options": {"require_join": bool(cfg.get("options", {}).get("require_join"))
                        if isinstance(cfg.get("options"), dict) else False}}
    return norm, checks, warnings


# ---------------------------------------------------------------- 关联装配（一次扫描）
def _rows_live(tname, cache):
    if tname not in cache:
        cache[tname] = [r for r in _srv().load_rows(tname) if not r["deleted"]]
    return cache[tname]


def _join_index(rel, cache):
    """目标表按主键值 → 行清单（重复主键全部保留为候选，绝不静默取第一条）；
    另注册已确认别名（number_aliases，稳定引用：改号/合并后旧值继续解析）。
    返回 (idx, rid_idx, alias_map)。"""
    conn = _srv().get_db()
    tgt = rel["to_table"]
    key = rel["to_column"] or TABLES[tgt]["key"]
    idx, rid_idx = {}, {}
    for r in _rows_live(tgt, cache):
        rid_idx[r["id"]] = r
        k = str(r["data"].get(key) or "").strip()
        if k:
            idx.setdefault(k, []).append(r)
    alias_map = {}
    try:
        for a in conn.execute("SELECT alias, rid FROM number_aliases WHERE tname=?", (tgt,)):
            if a["alias"] and a["rid"] in rid_idx:
                alias_map[str(a["alias"]).strip().lower()] = a["rid"]
    except Exception:
        pass
    return idx, rid_idx, alias_map


def _resolve_targets(join_val, idx, rid_idx, alias_map):
    """值 → 命中的目标行清单。精确值相等 + 已确认别名（原文→规范行）；
    未匹配返回 []；多候选返回全部（语义由上层按 keep/expand 处理）。"""
    v = str(join_val or "").strip()
    if not v:
        return [], "empty"
    hits = list(idx.get(v) or [])
    rid = alias_map.get(v.lower())
    if rid is not None:
        extra = rid_idx.get(rid)
        if extra is not None and extra not in hits:
            hits.append(extra)
    return hits, ("matched" if hits else "unmatched")


# ---------------------------------------------------------------- 派生字段（白名单求值）
def _num_or_none(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _eval_derived(dr, cells, join_counts):
    kind = dr["kind"]
    if kind == "concat":
        parts = [str(cells.get(s) or "").strip() for s in dr.get("sources") or []]
        parts = [p for p in parts if p]
        if not parts:
            return "—" if dr.get("missing") != "blank" else ""
        return dr.get("sep", " ").join(parts)
    if kind == "sum":
        vals = [_num_or_none(cells.get(s)) for s in dr.get("sources") or []]
        nums = [v for v in vals if v is not None]
        if not nums:
            return "—"   # 全部缺失=未记录（未知不补 0）
        total = sum(nums)
        return int(total) if float(total).is_integer() else round(total, 2)
    if kind == "count_join":
        return join_counts.get(dr.get("join"), 0)
    if kind == "label_when":
        for c0 in dr.get("conditions") or []:
            v = cells.get(c0.get("field"))
            sv = "" if v is None else str(v).strip()
            op = c0.get("op")
            if op == "empty" and sv:
                return ""
            if op == "notempty" and not sv:
                return ""
            if op == "equals" and sv != str(c0.get("value") or ""):
                return ""
        return dr.get("label") or ""
    return None   # 白名单外：不应出现（校验已拦截）


# ---------------------------------------------------------------- 执行组合查询
def _effective_sort_key(sort, direction, colmeta_numeric, cells_getter):
    def sk(row):
        v = cells_getter(row).get(sort)
        if colmeta_numeric:
            n = _num_or_none(v)
            return (0, n) if n is not None else (1, 0)
        s = "" if v is None else str(v)
        return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]
    return sk


def execute_rows(user, d, *, q="", filters=None, sort=None, direction="asc",
                 page=1, per=100, resolve_ids=False, id_subset=None, sample_only=False):
    """执行组合查询：一次扫描主表 + 每条关联一次扫描装配，无逐行请求。
    返回 (rows, meta)；meta 含 input_rows/output_rows/膨胀原因/flags 计数/checks。"""
    conn = _srv().get_db()
    base = d["base_table"]
    cfg = json.loads(d["config"] or "{}")
    joins = cfg.get("joins") or []
    fields = cfg.get("fields") or []
    derived = cfg.get("derived") or []
    bcfg = TABLES[base]
    cache = {}
    base_rows = _rows_live(base, cache)
    eidx = _srv().linked_eidx(base)
    join_ctx = {}
    for j in joins:
        rel = RELATIONS[j["path"]]
        idx, rid_idx, amap = _join_index(rel, cache)
        join_ctx[j["path"]] = {"rel": rel, "idx": idx, "rid_idx": rid_idx, "alias": amap,
                               "src_col": rel["from_column"], "mode": j.get("mode", "keep")}
    field_tables = {f["id"]: f["table"] for f in fields}
    flags_counter = {}
    out = []
    input_rows = 0
    for brow in base_rows:
        input_rows += 1
        bdisp, bmiss = _srv().display_row(base, brow, eidx)
        base_key = bdisp.get(bcfg["key"])
        # 每条 join 解析目标行（基于主行原始关联值）
        per_join = {}
        for path, ctx in join_ctx.items():
            jv = bdisp.get(ctx["src_col"])
            if jv is None:
                jv = brow["data"].get(ctx["src_col"])
            hits, st = _resolve_targets(jv, ctx["idx"], ctx["rid_idx"], ctx["alias"])
            per_join[path] = {"hits": hits, "status": st, "value": jv}
        # 笛卡尔展开：仅 expand 且有命中的 join 参与展开
        expand_paths = [p for p, c in per_join.items() if c and join_ctx[p]["mode"] == "expand"]
        combos = [[]]
        exploded = False
        for p in expand_paths:
            hits = per_join[p]["hits"]
            if len(hits) > 1:
                exploded = True
            combos = [c + [(p, h)] for c in combos for h in hits] if hits else combos
        for combo in combos:
            cells = {}
            flags = {}
            identity = [{"tname": base, "rid": brow["id"], "key": base_key,
                         "label": display_label(base), "rv": brow["row_version"]}]
            rv_map = {base: brow["row_version"]}
            row_id = str(brow["id"])
            for f in fields:
                t = f["table"]
                if t == base:
                    cells[f["id"]] = bdisp.get(f["column"])
                    continue
                # 关联字段：优先 expand 命中行；keep 模式取唯一候选（多候选/未匹配不猜）
                picked = None
                for (p, h) in combo:
                    if join_ctx[p]["rel"]["to_table"] == t:
                        picked = h
                        break
                if picked is None:
                    ctxhit = per_join.get(_path_for_table(join_ctx, t))
                    if ctxhit:
                        if ctxhit["status"] == "empty":
                            flags[f["id"]] = "unmatched"
                        elif len(ctxhit["hits"]) == 1:
                            picked = ctxhit["hits"][0]
                        elif len(ctxhit["hits"]) > 1:
                            flags[f["id"]] = "multi:%d" % len(ctxhit["hits"])
                        else:
                            flags[f["id"]] = "unmatched"
                    else:
                        flags[f["id"]] = "unmatched"
                if picked is not None:
                    cells[f["id"]] = picked["data"].get(f["column"])
                    rv_map[t] = picked["row_version"]
                    if not any(x["tname"] == t for x in identity):
                        identity.append({"tname": t, "rid": picked["id"],
                                         "key": picked["data"].get(TABLES[t]["key"]),
                                         "label": display_label(t), "rv": picked["row_version"]})
                else:
                    cells[f["id"]] = None
            join_counts = {p: len(per_join[p]["hits"]) for p in per_join}
            for dr in derived:
                cells[dr["id"]] = _eval_derived(dr, cells, join_counts)
            if combo:
                row_id += "|" + "|".join(f"{p}@{h['id']}" for (p, h) in combo)
            out.append({"id": row_id, "cells": cells, "flags": flags,
                        "identity": identity, "row_versions": rv_map,
                        "expanded": bool(combo)})
    for r in out:
        for fid, fl in r["flags"].items():
            flags_counter[fl.split(":")[0]] = flags_counter.get(fl.split(":")[0], 0) + 1
    meta = {"input_rows": input_rows, "output_rows": len(out),
            "expanded_any": exploded,
            "expansion": [p for p in expand_paths],
            "flags": flags_counter,
            "checks": [dict(c) for c in (json.loads(d.get("_checks") or "[]") if d.get("_checks") else [])]}
    # ---- 过滤管道：id 子集 → 列筛选 → 搜索 → 排序 → 分页/固化 ----
    if id_subset is not None:
        keep = set(id_subset)
        out = [r for r in out if r["id"] in keep]
    out = _apply_filters(out, filters or {})
    if q:
        ql = q.lower()
        out = [r for r in out if any(v is not None and ql in str(v).lower() for v in r["cells"].values())]
    numeric_fields = {f["id"]: bool(next((c for c in TABLES[f["table"]]["columns"]
                                          if c["name"] == f["column"]), {}).get("numeric"))
                      for f in fields}
    for dr in derived:
        numeric_fields[dr["id"]] = dr["kind"] == "sum"
    if not sort:
        ds = cfg.get("default_sort") or {}
        sort = ds.get("field") or ""
        direction = ds.get("dir") or direction
    if sort and sort in numeric_fields:
        sk = _effective_sort_key(sort, direction, numeric_fields[sort], lambda r: r["cells"])
        out.sort(key=sk, reverse=(direction == "desc"))
    elif sort:
        sk = _effective_sort_key(sort, direction, False, lambda r: r["cells"])
        out.sort(key=sk, reverse=(direction == "desc"))
    meta["sort"] = sort
    meta["dir"] = direction
    if resolve_ids:
        cap = 20000
        sample = [{"id": r["id"], "key": r["identity"][0]["key"]} for r in out[:5]]
        return ({"ids": [r["id"] for r in out[:cap]], "count": len(out),
                 "truncated": len(out) > cap, "cap": cap, "sample": sample}, meta)
    total = len(out)
    if sample_only:
        out = out[:10]
        page, per = 1, 10
    else:
        out = out[(page - 1) * per: page * per]
    meta.update({"total": total, "page": page, "per": per})
    return out, meta


def _path_for_table(join_ctx, tname):
    for p, ctx in join_ctx.items():
        if ctx["rel"]["to_table"] == tname:
            return p
    return None


def _apply_filters(out, filters):
    if not filters:
        return out
    def ok(r):
        for fid, f in filters.items():
            if not isinstance(f, dict):
                continue
            v = r["cells"].get(fid)
            sv = "" if v is None else str(v).strip()
            mode = f.get("mode")
            if mode == "in":
                vals = [str(x).strip() for x in (f.get("values") or [])]
                has_empty = "（空）" in vals
                vals = [x for x in vals if x != "（空）"]
                if has_empty:
                    if sv != "" and sv not in vals:
                        return False
                elif sv not in vals:
                    return False
            elif mode == "contains":
                if str(f.get("text") or "").lower() not in sv.lower():
                    return False
            elif mode == "empty":
                if sv:
                    return False
            elif mode == "notempty":
                if not sv:
                    return False
        return True
    return [r for r in out if ok(r)]


# ---------------------------------------------------------------- 审计辅助
def _log_ct(conn, user, d, action, changes, batch_id=None, reason=None):
    _srv().log_edit(conn, user["username"], "composite_tables", d.get("id"), d["ct_id"],
                    action, changes, batch_id=batch_id, reason=reason)


def _snapshot_def(d):
    return {"ct_id": d["ct_id"], "name": d["name"], "description": d["description"],
            "nav_group": d["nav_group"], "base_table": d["base_table"],
            "config": json.loads(d["config"] or "{}"), "status": d["status"]}


# ---------------------------------------------------------------- 端点注册
def register(app):
    @app.get("/api/composite/available")
    def api_ct_available():
        """构建器数据源目录：可用主表/关系注册表/字段清单（仅调用者可见表的字段）。"""
        user = _srv().current_user()
        _require_build(user)
        vis = _srv().visible_tables(user)
        conn = _srv().get_db()
        base_tables = []
        for t in sorted(vis, key=lambda x: TABLES[x].get("sort", 99)):
            total = conn.execute("SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=0",
                                 (t,)).fetchone()["c"]
            base_tables.append({"t": t, "label": display_label(t), "key": TABLES[t]["key"],
                                "total": total,
                                "linked_source": TABLES[t].get("linked_source")})
        rels = []
        for rel in RELATIONS.values():
            if rel["from_table"] in vis and rel["to_table"] in vis:
                rels.append(rel)
        fields = {}
        for t in vis:
            fields[t] = [{"name": c["name"], "label": c.get("label") or c["name"],
                          "linked": bool(c.get("linked")), "linked_to": c.get("linked"),
                          "numeric": bool(c.get("numeric")), "wide": bool(c.get("wide")),
                          "options": c.get("options"),
                          "doc": _srv().COLUMN_DOCS.get(c["name"], "")}
                         for c in TABLES[t]["columns"]]
        return jsonify({"can_build": True, "base_tables": base_tables,
                        "relations": rels, "fields": fields,
                        "derived_kinds": DERIVED_KINDS,
                        "join_modes": {"expand": "一对多：展开为多行（每条关联记录一行）",
                                       "keep": "保留主记录：关联字段取唯一匹配；未匹配留空、多候选明确标注"}})

    @app.get("/api/composite/definitions")
    def api_ct_definitions():
        """我有权看到的组合表（普通用户=已发布且授权；构建者=另加自己的全部；管理员=全部）。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        rows = conn.execute("SELECT * FROM composite_tables ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if not _def_visible(user, d):
                continue
            grants = parse_grants(d["permissions"])
            cfg = json.loads(d["config"] or "{}")
            out.append({
                "ct_id": d["ct_id"], "name": d["name"], "description": d["description"],
                "nav_group": d["nav_group"], "owner": d["owner"],
                "base_table": d["base_table"],
                "base_label": display_label(d["base_table"]),
                "status": d["status"], "revision": d["revision"],
                "updated_at": d["updated_at"], "published_at": d["published_at"],
                "field_count": len(cfg.get("fields") or []),
                "my_access": my_access(user, d, grants),
                "is_owner": d["owner"] == user["username"],
                "grants": grants if (user["admin"] or d["owner"] == user["username"]) else None,
            })
        return jsonify({"definitions": out, "can_build": can_build(user),
                        "admin": user["admin"]})

    @app.post("/api/composite/definitions")
    def api_ct_create():
        """新建草稿（需要构建器权限；字段/关联必须在自己可见范围内）。"""
        user = _srv().current_user()
        _require_build(user)
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            abort(400, "组合表名称必填")
        base_table = body.get("base_table") or "specimens"
        cfg, checks, warnings = validate_config(user, base_table, body.get("config") or {})
        conn = _srv().get_db()
        ct_id = "ct_" + _srv().secrets.token_hex(5)
        ts = _srv().now()
        conn.execute(
            "INSERT INTO composite_tables(ct_id,name,description,nav_group,owner,base_table,"
            "config,status,revision,permissions,updated_by,updated_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ct_id, name[:80], str(body.get("description") or "").strip()[:300],
             str(body.get("nav_group") or "组合表").strip()[:20] or "组合表",
             user["username"], base_table, json.dumps(cfg, ensure_ascii=False),
             "draft", 1, "{}", user["username"], ts, user["username"], ts))
        d = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_create",
                {"_summary": f"创建组合表草稿「{name}」（主数据源 {display_label(base_table)}，"
                             f"{len(cfg['fields'])} 个字段，{len(cfg['joins'])} 条关联）",
                 "base_table": base_table, "config": cfg})
        conn.commit()
        return jsonify({"ok": True, "ct_id": ct_id, "revision": 1,
                        "checks": checks, "warnings": warnings})

    @app.get("/api/composite/definitions/<ct_id>")
    def api_ct_detail(ct_id):
        """定义详情：配置者/管理员得完整配置；使用者得字段结构（含只读原因）。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        cfg = json.loads(d["config"] or "{}")
        is_conf = access == "rw" and can_build(user)
        if not is_conf:
            cfg = {k: cfg.get(k) for k in ("joins", "fields", "derived",
                                           "default_filters", "default_sort", "options")}
        return jsonify({"ct_id": d["ct_id"], "name": d["name"], "description": d["description"],
                        "nav_group": d["nav_group"], "owner": d["owner"],
                        "base_table": d["base_table"],
                        "base_label": display_label(d["base_table"]),
                        "base_key": TABLES[d["base_table"]]["key"],
                        "status": d["status"], "revision": d["revision"],
                        "config": cfg, "my_access": access,
                        "can_configure": is_conf,
                        "grants": parse_grants(d["permissions"]) if is_conf else None})

    @app.put("/api/composite/definitions/<ct_id>")
    def api_ct_update(ct_id):
        """修改配置/元数据：并发保护（base_revision 不符 → 409 重读）；
        变更前自动保存修订快照（可恢复，只恢复定义）。"""
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        body = request.get_json(silent=True) or {}
        conn = _srv().get_db()
        checks = warnings = []
        if "base_revision" in body and int(body["base_revision"] or 0) != d["revision"]:
            return jsonify({"error": f"该定义刚被他人修改（当前修订 {d['revision']}，您基于 {body['base_revision']}），"
                                     "请刷新后重试，避免覆盖他人配置", "current_revision": d["revision"]}), 409
        sets, args = [], []
        new_cfg = json.loads(d["config"] or "{}")
        if "config" in body:
            norm, checks, warnings = validate_config(user, d["base_table"], body["config"] or {})
            new_cfg = norm
            sets.append("config=?")
            args.append(json.dumps(norm, ensure_ascii=False))
            sets.append("status='draft'")
            conn.execute("INSERT INTO composite_revisions(ct_id,revision,config,changed_by,note,created_at) "
                         "VALUES(?,?,?,?,?,?)",
                         (d["ct_id"], d["revision"], d["config"], d["updated_by"] or d["owner"],
                          "修改前快照", _srv().now()))
        for k in ("name", "description", "nav_group"):
            if k in body:
                v = str(body[k] or "").strip()
                if k == "name" and not v:
                    abort(400, "组合表名称不能为空")
                sets.append(f"{k}=?")
                args.append(v[:300])
        if not sets:
            abort(400, "没有要修改的内容")
        sets.append("revision=revision+1")
        sets.append("updated_by=?")
        args.append(user["username"])
        sets.append("updated_at=?")
        args.append(_srv().now())
        args.append(d["ct_id"])
        conn.execute(f"UPDATE composite_tables SET {', '.join(sets)} WHERE ct_id=?", args)
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_update",
                {"_summary": f"修改组合表「{d2['name']}」配置（修订 → {d2['revision']}）",
                 "before_revision": d["revision"], "after_revision": d2["revision"],
                 "fields_before": len(json.loads(d["config"] or "{}").get("fields") or []),
                 "fields_after": len(new_cfg.get("fields") or [])},
                reason="配置修改（旧配置已存修订快照，可恢复；不影响源数据）")
        conn.commit()
        return jsonify({"ok": True, "revision": d2["revision"], "checks": checks, "warnings": warnings})

    @app.post("/api/composite/definitions/<ct_id>/publish")
    def api_ct_publish(ct_id):
        """发布：先做结构/权限/关联完整性检查（字段存在、类型、路径、配置者权限）。"""
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        if d["status"] == "published":
            return jsonify({"ok": True, "noop": True})
        cfg = json.loads(d["config"] or "{}")
        validate_config(user, d["base_table"], cfg)   # 发布前检查（不通过 abort 400）
        conn = _srv().get_db()
        conn.execute("UPDATE composite_tables SET status='published', published_at=? WHERE ct_id=?",
                     (_srv().now(), ct_id))
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_publish",
                {"_summary": f"发布组合表「{d2['name']}」（修订 {d2['revision']}）——"
                             "对已授权用户可见；开放名单由管理员在授权面板维护"})
        conn.commit()
        return jsonify({"ok": True})

    @app.post("/api/composite/definitions/<ct_id>/disable")
    def api_ct_disable(ct_id):
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        conn = _srv().get_db()
        conn.execute("UPDATE composite_tables SET status='disabled' WHERE ct_id=?", (ct_id,))
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_disable",
                {"_summary": f"停用组合表「{d2['name']}」——从普通导航隐藏；源数据不受影响；可重新启用"})
        conn.commit()
        return jsonify({"ok": True})

    @app.post("/api/composite/definitions/<ct_id>/enable")
    def api_ct_enable(ct_id):
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        if d["status"] != "disabled":
            abort(400, "只有已停用的定义可以重新启用")
        conn = _srv().get_db()
        conn.execute("UPDATE composite_tables SET status='published' WHERE ct_id=?", (ct_id,))
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_enable", {"_summary": f"重新启用组合表「{d2['name']}」"})
        conn.commit()
        return jsonify({"ok": True})

    @app.post("/api/composite/definitions/<ct_id>/copy")
    def api_ct_copy(ct_id):
        """复制已有定义为新草稿（需构建器权限 + 对原表有访问权）；副本归属复制者。"""
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id)
        conn = _srv().get_db()
        ct_id2 = "ct_" + _srv().secrets.token_hex(5)
        ts = _srv().now()
        name2 = (d["name"] + "（副本）")[:80]
        conn.execute(
            "INSERT INTO composite_tables(ct_id,name,description,nav_group,owner,base_table,"
            "config,status,revision,permissions,updated_by,updated_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ct_id2, name2, d["description"], d["nav_group"], user["username"],
             d["base_table"], d["config"], "draft", 1, "{}", user["username"], ts,
             user["username"], ts))
        d2 = _load_def(conn, ct_id2)
        _log_ct(conn, user, d2, "ct_create",
                {"_summary": f"从「{d['name']}」复制新草稿「{name2}」（副本归属 {user['username']}，"
                             "原定义与授权不受影响）", "copied_from": d["ct_id"]})
        conn.commit()
        return jsonify({"ok": True, "ct_id": ct_id2})

    @app.delete("/api/composite/definitions/<ct_id>")
    def api_ct_delete(ct_id):
        """删除定义：所有者可删自己的草稿/无外部授权的定义；已向他人开放的定义需管理员。
        只删定义与修订，不触碰源数据。"""
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        grants = parse_grants(d["permissions"])
        external = [u for u in grants if u != d["owner"]]
        if external and not user["admin"]:
            abort(403, "该组合表已向其他用户开放（%d 人），删除需管理员操作" % len(external))
        conn = _srv().get_db()
        conn.execute("DELETE FROM composite_tables WHERE ct_id=?", (ct_id,))
        conn.execute("DELETE FROM composite_revisions WHERE ct_id=?", (ct_id,))
        _log_ct(conn, user, d, "ct_delete",
                {"_summary": f"删除组合表定义「{d['name']}」（仅删除定义配置；源数据完全不受影响）"})
        conn.commit()
        return jsonify({"ok": True})

    @app.put("/api/composite/definitions/<ct_id>/permissions")
    def api_ct_permissions(ct_id):
        """每表访问授权（仅管理员）：{用户名: r|rw|null}。变更前后值入审计。"""
        user = _srv().current_user()
        if not user["admin"]:
            abort(403, "组合表的开放授权由管理员维护")
        body = request.get_json(silent=True) or {}
        incoming = body.get("perms") or {}
        if not isinstance(incoming, dict):
            abort(400, "perms 需为 {用户名: r|rw|null}")
        conn = _srv().get_db()
        d = _load_def(conn, ct_id)
        if not d:
            abort(404, "组合表定义不存在")
        before = parse_grants(d["permissions"])
        merged = dict(before)
        for uname, p in incoming.items():
            u = conn.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone()
            if not u:
                abort(400, f"用户 {uname} 不存在")
            if p in (None, "", "none", 0):
                merged.pop(uname, None)
            elif p in ("r", "rw"):
                merged[uname] = p
            else:
                abort(400, f"用户 {uname} 的权限需为 r/rw/none")
        conn.execute("UPDATE composite_tables SET permissions=? WHERE ct_id=?",
                     (json.dumps(merged, ensure_ascii=False), ct_id))
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_perm_update",
                {"_summary": f"调整组合表「{d2['name']}」访问授权（{len(before)} → {len(merged)} 人）",
                 "before": before, "after": merged},
                reason="每表访问授权变更（记录操作人/对象/前后值）")
        conn.commit()
        return jsonify({"ok": True, "grants": merged})

    @app.get("/api/composite/definitions/<ct_id>/revisions")
    def api_ct_revisions(ct_id):
        user = _srv().current_user()
        d, access = _require_def(user, ct_id, need="rw")
        conn = _srv().get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT id, revision, changed_by, note, created_at, length(config) size "
            "FROM composite_revisions WHERE ct_id=? ORDER BY revision DESC", (ct_id,))]
        return jsonify({"revisions": rows, "current_revision": d["revision"]})

    @app.post("/api/composite/definitions/<ct_id>/restore-revision")
    def api_ct_restore_revision(ct_id):
        """恢复旧配置：只恢复表定义（存为新修订），绝不恢复/回滚源数据库。"""
        user = _srv().current_user()
        _require_build(user)
        d, access = _require_def(user, ct_id, need="rw")
        body = request.get_json(silent=True) or {}
        rev = int(body.get("revision") or 0)
        conn = _srv().get_db()
        r = conn.execute("SELECT * FROM composite_revisions WHERE ct_id=? AND revision=?",
                         (ct_id, rev)).fetchone()
        if not r:
            abort(404, "修订不存在")
        old_cfg = json.loads(r["config"] or "{}")
        norm, checks, warnings = validate_config(user, d["base_table"], old_cfg)
        conn.execute("INSERT INTO composite_revisions(ct_id,revision,config,changed_by,note,created_at) "
                     "VALUES(?,?,?,?,?,?)",
                     (ct_id, d["revision"], d["config"], d["updated_by"] or d["owner"],
                      "恢复前快照", _srv().now()))
        conn.execute("UPDATE composite_tables SET config=?, status='draft', revision=revision+1, "
                     "updated_by=?, updated_at=? WHERE ct_id=?",
                     (json.dumps(norm, ensure_ascii=False), user["username"], _srv().now(), ct_id))
        d2 = _load_def(conn, ct_id)
        _log_ct(conn, user, d, "ct_restore",
                {"_summary": f"把组合表「{d2['name']}」配置恢复到修订 {rev}（定义恢复为新修订 "
                             f"{d2['revision']}；源数据库不受影响）", "restored_from": rev},
                reason="恢复旧配置（只恢复表定义）")
        conn.commit()
        return jsonify({"ok": True, "revision": d2["revision"], "checks": checks, "warnings": warnings})

    @app.post("/api/composite/preview")
    def api_ct_preview():
        """构建器预览（草稿即可，不必先保存）：样例行 + 全量结构检查 + 展开膨胀诊断。"""
        user = _srv().current_user()
        _require_build(user)
        body = request.get_json(silent=True) or {}
        base_table = body.get("base_table") or "specimens"
        cfg, checks, warnings = validate_config(user, base_table, body.get("config") or {})
        d = {"ct_id": "preview", "name": body.get("name") or "预览",
             "base_table": base_table, "config": json.dumps(cfg, ensure_ascii=False),
             "owner": user["username"], "status": "draft", "revision": 0,
             "permissions": "{}", "_checks": json.dumps(checks, ensure_ascii=False)}
        rows, meta = execute_rows(user, d, sample_only=True)
        miss_counts = {}
        multi_counts = {}
        for r0 in rows:
            for fid, fl in r0["flags"].items():
                if fl == "unmatched":
                    miss_counts[fid] = miss_counts.get(fid, 0) + 1
                elif fl.startswith("multi:"):
                    multi_counts[fid] = multi_counts.get(fid, 0) + 1
        meta["unmatched_preview"] = miss_counts
        meta["multi_preview"] = multi_counts
        return jsonify({"rows": rows, "fields": cfg.get("fields"),
                        "derived": cfg.get("derived"), "checks": checks,
                        "warnings": warnings,
                        "meta": {k: meta.get(k) for k in
                                 ("input_rows", "output_rows", "expanded_any", "expansion", "flags")}})

    @app.get("/api/composite/<ct_id>/rows")
    def api_ct_rows(ct_id):
        """组合表数据查询：q/列筛选/排序/分页/resolve=ids（导出与批量共用的稳定范围）。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        if d["status"] == "disabled" and not (user["admin"] or d["owner"] == user["username"]):
            abort(403, "该组合表已停用")
        page = max(1, int(request.args.get("page", 1)))
        per = min(500, max(10, int(request.args.get("per", 100))))
        q = (request.args.get("q") or "").strip()
        filters = {}
        try:
            filters = json.loads(request.args.get("filters") or "{}")
            if not isinstance(filters, dict):
                filters = {}
        except ValueError:
            filters = {}
        rows, meta = execute_rows(user, d, q=q, filters=filters,
                                  sort=request.args.get("sort") or "",
                                  direction=request.args.get("dir", "asc"),
                                  page=page, per=per,
                                  resolve_ids=(request.args.get("resolve") == "ids"))
        if request.args.get("resolve") == "ids":
            meta.update(rows)
            meta["my_access"] = access
            return jsonify(meta)
        return jsonify({"rows": rows, **meta, "my_access": access,
                        "name": d["name"], "revision": d["revision"],
                        "scope": "组合表查询遵守您的源表权限与该表开放档位"})

    @app.get("/api/composite/<ct_id>/values")
    def api_ct_values(ct_id):
        """字段去重值+计数（筛选面板）：在调用者可访问的行集合上统计。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        fid = request.args.get("field_id") or ""
        cfg = json.loads(d["config"] or "{}")
        known = {f["id"] for f in (cfg.get("fields") or [])} | {x["id"] for x in (cfg.get("derived") or [])}
        if fid not in known:
            abort(400, "未知字段")
        rows_all, _m2 = execute_rows(user, d, q="", filters=None, page=1, per=10 ** 9)
        counter = {}
        empty = 0
        for r0 in rows_all:
            v = r0["cells"].get(fid)
            sv = "" if v is None else str(v).strip()
            if sv == "":
                empty += 1
            else:
                counter[sv] = counter.get(sv, 0) + 1
        vals = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:500]
        return jsonify({"values": [{"value": k, "count": n} for k, n in vals],
                        "empty": empty, "total": len(rows_all)})

    @app.get("/api/composite/<ct_id>/row/<path:row_id>")
    def api_ct_row(ct_id, row_id):
        """行详情：单元格值 + 源实体身份 + 可写字段清单 + 权限/只读原因。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        cfg = json.loads(d["config"] or "{}")
        rows, _meta = execute_rows(user, d, page=1, per=10 ** 9)
        row = next((r for r in rows if r["id"] == row_id), None)
        if row is None:
            abort(404, "行不存在或已不在您的可见范围内（可能被删除或权限变化）")
        writable = {}
        readonly_reason = {}
        identity_tables = {x["tname"] for x in row["identity"]}
        for f in (cfg.get("fields") or []):
            t = f["table"]
            can_w = False
            reason = ""
            if not f.get("editable"):
                reason = "构建者将此字段设置为只读"
            else:
                colmeta = next((c for c in TABLES[t]["columns"] if c["name"] == f["column"]), None)
                if colmeta and colmeta.get("linked"):
                    src = colmeta.get("linked_source") or TABLES[t].get("linked_source")
                    reason = f"来源为「{display_label(src)}」的关联展示列，请到来源表修改"
                elif t not in TABLES:
                    reason = "来源表不存在"
                elif access != "rw":
                    reason = "您在该组合表为只读"
                else:
                    perm = "rw" if user["admin"] else (user["perms"].get(t) or "none")
                    if perm == "rw":
                        can_w = True
                    elif perm == "r":
                        reason = f"您对源表「{display_label(t)}」只有查看权限"
                    else:
                        reason = f"您对源表「{display_label(t)}」无权限"
            if can_w and t not in identity_tables:
                can_w = False
                reason = "该行未包含此字段的来源记录（关联缺失）"
            if can_w:
                writable[f["id"]] = True
            elif reason:
                readonly_reason[f["id"]] = reason
        return jsonify({"row": row, "writable": writable, "readonly_reason": readonly_reason,
                        "my_access": access})

    @app.post("/api/composite/<ct_id>/row/<path:row_id>/update")
    def api_ct_row_update(ct_id, row_id):
        """组合表行内编辑：按字段归并到源记录，走源记录的 row_version/校验/幂等/审计通道。
        跨记录修改为原子事务：任一失败整体不执行（与批量修改默认口径一致）。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        if d["status"] == "disabled":
            abort(403, "该组合表已停用，不能编辑")
        if access != "rw":
            abort(403, "您在该组合表为只读")
        body = request.get_json(silent=True) or {}
        changes = body.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            abort(400, "没有要修改的字段")
        cfg = json.loads(d["config"] or "{}")
        fmap = {f["id"]: f for f in (cfg.get("fields") or [])}
        conn = _srv().get_db()
        # 行 → 源实体：用与展示同一执行路径取行身份（含 keep 关联实体与其 row_version），
        # 客户端可携带 base_versions（{表: 行版本}）保持乐观锁；未携带时以当前版本为准。
        rows_f, _m = execute_rows(user, d, page=1, per=10 ** 9, id_subset=[str(row_id)])
        if not rows_f:
            abort(404, "行不存在或已不在您的可见范围内（可能被删除或权限变化）")
        by_t = {}
        for x in rows_f[0]["identity"]:
            by_t.setdefault(x["tname"], x)
        client_rv = body.get("base_versions") if isinstance(body.get("base_versions"), dict) else {}
        # 逐字段归并到 (表, 行) 组
        groups = {}
        for fid, val in changes.items():
            f = fmap.get(fid)
            if not f:
                abort(400, f"未知字段 {fid}")
            if not f.get("editable"):
                abort(400, f"字段 {fid} 在组合表定义中为只读")
            t = f["table"]
            tgt = by_t.get(t)
            if not tgt:
                abort(400, f"字段 {fid} 的来源记录不在此行（该行未包含「{TABLES.get(t, {}).get('label', t)}」实体）")
            colmeta = next((c for c in TABLES[t]["columns"] if c["name"] == f["column"]), None)
            if colmeta and colmeta.get("linked"):
                src = colmeta.get("linked_source") or TABLES[t].get("linked_source")
                abort(400, f"字段 {fid} 是来自「{display_label(src)}」的关联展示列，"
                           "请到来源表修改")
            if not user["admin"]:
                perm = user["perms"].get(t) or "none"
                if perm != "rw":
                    abort(403, f"修改「{display_label(t)}」需要该表编辑权限；"
                               f"您的组合表开放档位不会扩展源表权限")
            rv_use = client_rv.get(t) or tgt["rv"]
            groups.setdefault((t, tgt["rid"]), {"changes": {}, "rv": rv_use, "key": tgt["key"]})
            groups[(t, tgt["rid"])]["changes"][f["column"]] = val
        bid = _srv()._new_batch("C")
        results = []
        touched = set()
        try:
            for (t, rid), g in groups.items():
                payload = _srv()._update_record(user, t, rid, {
                    "changes": g["changes"], "base_version": g["rv"]}, batch_id=bid)
                touched.add(t)
                results.append({"tname": t, "rid": rid, "key": g["key"],
                                "row_version": payload.get("row_version"),
                                "updated_at": payload.get("updated_at"),
                                "noop": payload.get("noop")})
            for t in touched:
                _srv().invalidate(t)
        except _srv().WriteConflict as c:
            _srv()._tx_rollback(conn)
            return jsonify(c.payload), 409
        except _srv().sqlite3.OperationalError as e:
            return _srv()._tx_fail(e)
        except Exception:
            _srv()._tx_rollback(conn)
            raise
        conn.execute("UPDATE composite_tables SET updated_at=? WHERE ct_id=?", (_srv().now(), ct_id))
        dcur = _load_def(conn, ct_id)
        _log_ct(conn, user, dcur, "ct_edit",
                {"_summary": f"经组合表「{dcur['name']}」修改 {len(groups)} 条源记录"
                             f"（{len(changes)} 个字段；批次 {bid}，可随批次撤销）",
                 "targets": [{"tname": t, "rid": rid} for (t, rid) in groups]},
                batch_id=bid, reason="组合表入口编辑（写入真实源记录）")
        return _srv()._finish_write(conn, {"ok": True, "batch": bid, "results": results}, None)

    @app.post("/api/composite/<ct_id>/batch_update")
    def api_ct_batch_update(ct_id):
        """组合表批量修改：先按源记录和字段归并（同一源记录只更新一次，同值不构成冲突）；
        原子事务；对同一字段提交不同值的场景在单值批量接口下不存在（一次只有一个值）。"""
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        if d["status"] == "disabled":
            abort(403, "该组合表已停用，不能编辑")
        if access != "rw":
            abort(403, "您在该组合表为只读")
        body = request.get_json(silent=True) or {}
        row_ids = [str(x) for x in (body.get("row_ids") or [])]
        fid = body.get("field_id") or ""
        val = body.get("value")
        if not row_ids or not fid:
            abort(400, "未选择行或未指定字段")
        cfg = json.loads(d["config"] or "{}")
        fmap = {f["id"]: f for f in (cfg.get("fields") or [])}
        f = fmap.get(fid)
        if not f:
            abort(400, f"未知字段 {fid}")
        if not f.get("editable"):
            abort(400, f"字段 {fid} 在组合表定义中为只读")
        rows, _meta = execute_rows(user, d, page=1, per=10 ** 9, id_subset=row_ids)
        got = {r0["id"] for r0 in rows}
        missing = [x for x in row_ids if x not in got]
        if missing:
            abort(400, f"{len(missing)} 条所选行已不在您的可见范围（可能被删除或权限变化），请刷新后重试")
        t = f["table"]
        colmeta = next((c for c in TABLES[t]["columns"] if c["name"] == f["column"]), None)
        if colmeta and colmeta.get("linked"):
            abort(400, "关联展示列不能在此批量修改，请到来源表修改")
        if not user["admin"] and (user["perms"].get(t) or "none") != "rw":
            abort(403, f"批量修改「{display_label(t)}」需要该表编辑权限；组合表开放档位不扩展源表权限")
        groups = {}
        for r0 in rows:
            tgt = next((x for x in r0["identity"] if x["tname"] == t), None)
            if not tgt:
                abort(400, "所选行缺少来源实体，无法定位编辑目标")
            groups.setdefault(tgt["rid"], tgt)   # 归并：同一源记录只更新一次（同值）
        conn = _srv().get_db()
        bid = _srv()._new_batch("C")
        results = []
        try:
            for rid, tgt in groups.items():
                payload = _srv()._update_record(user, t, rid, {
                    "changes": {f["column"]: val}, "base_version": tgt["rv"]}, batch_id=bid)
                results.append({"tname": t, "rid": rid, "key": tgt["key"],
                                "row_version": payload.get("row_version"),
                                "noop": payload.get("noop")})
            _srv().invalidate(t)
        except _srv().WriteConflict as c:
            _srv()._tx_rollback(conn)
            return jsonify(c.payload), 409
        except _srv().sqlite3.OperationalError as e:
            return _srv()._tx_fail(e)
        except Exception:
            _srv()._tx_rollback(conn)
            raise
        dcur = _load_def(conn, ct_id)
        _log_ct(conn, user, dcur, "ct_edit",
                {"_summary": f"经组合表「{dcur['name']}」批量修改字段「{f['column']}」："
                             f"{len(row_ids)} 行归并为 {len(groups)} 条源记录（批次 {bid}）",
                 "targets": [{"tname": t, "rid": rid} for rid in groups]},
                batch_id=bid, reason="组合表批量修改（按源记录归并）")
        return _srv()._finish_write(conn, {"ok": True, "batch": bid,
                                           "merged_records": len(groups), "results": results}, None)

    @app.post("/api/composite/<ct_id>/export")
    def api_ct_export(ct_id):
        """导出：勾选记录 / 当前筛选全部 / 主动选择全表（显式 scope，绝不静默扩大）。
        文件绑定定义修订、行粒度与关联规则；遵守导出人当时权限；前导零与公式防护沿用 Excel 契约。"""
        import io as _io
        user = _srv().current_user()
        d, access = _require_def(user, ct_id)
        body = request.get_json(silent=True) or {}
        scope = body.get("scope") or ""
        if scope not in ("all", "filtered", "selected"):
            abort(400, "导出范围需显式给出：all（全表）/ filtered（当前筛选全部）/ selected（勾选记录）")
        q = (body.get("q") or "").strip()
        filters = body.get("filters") or {}
        sort = body.get("sort") or ""
        direction = body.get("dir", "asc")
        id_subset = None
        if scope == "selected":
            id_subset = [str(x) for x in (body.get("row_ids") or [])]
            if not id_subset:
                abort(400, "未勾选任何行")
        rows, meta = execute_rows(user, d, q=q, filters=filters if scope != "selected" else None,
                                  sort=sort, direction=direction, page=1, per=10 ** 9,
                                  id_subset=id_subset)
        cfg = json.loads(d["config"] or "{}")
        cols = []
        for f in (cfg.get("fields") or []):
            cols.append({"key": f["id"], "header": f.get("alias") or f["id"].split(":", 1)[1],
                         "width": f.get("width")})
        for dr in (cfg.get("derived") or []):
            cols.append({"key": dr["id"], "header": dr.get("label") or dr["id"],
                         "width": dr.get("width")})
        if body.get("cols"):
            want = [str(x) for x in body["cols"]]
            known = {c["key"] for c in cols}
            unknown = [w for w in want if w not in known]
            if unknown:
                abort(400, f"未知导出列：{'、'.join(unknown)}")
            cols = [c for w in want for c in cols if c["key"] == w]
        import openpyxl
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter as _gcl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        ws = wb.create_sheet((d["name"] or "组合表")[:31])
        base_key = TABLES[d["base_table"]]["key"]
        base_key_col_idx = None
        ws.append([c["header"] for c in cols])
        for c0 in ws[1]:
            c0.font = Font(bold=True)
        for i, c0 in enumerate(cols, start=1):
            if c0["key"] == f"{d['base_table']}:{base_key}":
                base_key_col_idx = i
        for r0 in rows:
            vals = []
            for c0 in cols:
                v = r0["cells"].get(c0["key"])
                if isinstance(v, (list, dict)):
                    v = json.dumps(v, ensure_ascii=False)
                s = "" if v is None else str(v)
                if s.startswith(("=", "+", "-", "@")):
                    s = "'" + s
                vals.append(s)
            ws.append(vals)
        for i in range(1, len(cols) + 1):
            w = cols[i - 1].get("width") or 110
            ws.column_dimensions[_gcl(i)].width = max(10, min(60, w / 8))
            if base_key_col_idx == i:
                ws.column_dimensions[_gcl(i)].number_format = "@"
        ws.freeze_panes = "A2"
        ws0 = wb.create_sheet("README", 0)
        rv = _srv()._rules_versions()
        scope_text = {"all": "全表（主动选择）", "filtered": "当前筛选全部",
                      "selected": f"勾选记录 {len(id_subset or [])} 条"}[scope]
        readme = [
            ["项目", "值"],
            ["导出人", user["username"]],
            ["服务器时点（导出时点）", _srv().now()],
            ["组合表", f"{d['name']}（{d['ct_id']}，配置修订 r{d['revision']}）"],
            ["行粒度", f"一行 = 一条「{display_label(d['base_table'])}」记录"],
            ["关联规则", "；".join(f"{j['path']}（{'展开多行' if j.get('mode') == 'expand' else '保留主记录'}）"
                                    for j in (cfg.get('joins') or [])) or "无（单表）"],
            ["导出范围", scope_text + (f"；筛选 {json.dumps(filters, ensure_ascii=False)}" if filters else "")],
            ["输入记录数 / 输出行数", f"{meta['input_rows']} / {meta['output_rows']}"],
            ["应用 / 结构版本", f"{_srv().APP_VERSION} / {rv['schema']}"],
            ["权限说明", "本文件仅包含导出人当时有权限查看的数据；组合表开放档位不扩展源表权限"],
            ["缺失值口径", "空单元格或「—」表示未记录，与 0 不同；派生列（合计）全部缺失=未记录，未知不补 0"],
            ["业务编号说明", "编号按文本保存（前导零不丢）；以 ' 开头的单元格表示原文以 = + - @ 开头，去掉撇号即原文"],
        ]
        for row in readme:
            ws0.append(row)
        ws0.column_dimensions["A"].width = 26
        ws0.column_dimensions["B"].width = 110
        for c0 in ws0[1]:
            c0.font = Font(bold=True)
        _srv().log_edit(_srv().get_db(), user["username"], d["base_table"], None, None, "export",
                        {"_summary": f"组合表「{d['name']}」导出（{scope_text}）：{len(rows)} 行",
                         "_scope": {"composite": d["ct_id"], "revision": d["revision"],
                                    "rows": len(rows), "scope": scope}},
                        source="composite-export")
        _srv().get_db().commit()
        from urllib.parse import quote as _q
        safe = "".join(ch for ch in d["name"] if ch not in '\\/:*?"<>|') or "composite"
        buf = _io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{_q(safe)}.xlsx; filename=composite.xlsx"})

