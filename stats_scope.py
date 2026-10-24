# -*- coding: utf-8 -*-
"""统计/地图统一范围表达（v3.60 → v4.11 可筛选统计）。

明确统计对象、空间范围、时间范围、筛选条件、可见数据范围、缺失值分母。
三种计数口径可切换（记录数/独立事件数/已知个体数）。
空间精度分级：precise（经纬度齐）/ province（仅省级聚合）/ unknown（完全未知位置）。
仅知省份 → 区域统计（省份名做标签），不用省会中心点冒充标本坐标。

v4.11 统计筛选：统计范围可选（默认主表 / 单张可见表 / 类群联合=该类群下所有
「标本实体表」（有采集关联键的表，如跳蛛系统=标本+大理+西南大学三张台账）），
叠加 搜索词 + 列筛选（与表格筛选同一语义：in/contains/empty/notempty，作用于
关联解析后的展示行）；筛选字段必须存在于范围内每张表（否则 400 具体指出）。
新增输出：属/族/国家分布、物种数（最终名去重，附暂定名去重与物种清单）、
性别与个体数量分解（各表数量列名不同，按统一性别键归并；未知不当 0）、
每表行数明细。图表数据下载（CSV）携带同一筛选并记录条件。
"""
import hashlib
import json

from flask import abort, jsonify, request, Response

from tables_def import TABLES, display_label

_QC_KEYS = ("AdultMale", "AdultFemale", "Subadult", "Juvenile")
_SEX_MAP = {"male": ("AdultMale", "雄性数量"), "female": ("AdultFemale", "雌性数量"),
            "subadult": ("Subadult",), "juvenile": ("Juvenile", "幼体数量")}
# 分类分布列名（各表不同：跳蛛/原蛛等英文列，伪蝎为中文 科/属/种；与 server.GROUP_COLS 同口径）
_TAXO_MAP = (("fams", ("Family", "科")),
             ("genera", ("Genus", "属")),
             ("tribes", ("Tribe",)))
_SPECIES_COL = ("FinalSpeciesName", "Final Name", "种")   # 已鉴定种名
_TOP_N = 15


def _first(d, cols):
    """多候选列名取第一个非空值（各表列名不同时的统一读取）。"""
    for c in cols:
        v = str(d.get(c) or "").strip()
        if v:
            return v
    return ""


def _norm_province(p):
    import geo
    return geo.norm_province(p)


def _loc_tier(d, eidx_src):
    """空间精度分级（不虚构坐标）：precise / province / unknown。
    Latitude1/Longitude1 是关联列（实时取自采集事件），检查本行值再回退到事件源。"""
    for src_lat, src_lon in ((d.get("Latitude1"), d.get("Longitude1")),
                             ((eidx_src or {}).get("Latitude1"), (eidx_src or {}).get("Longitude1"))):
        lat, lon = src_lat, src_lon
        if lat is not None and lon is not None:
            try:
                la, lo = float(lat), float(lon)
                if -90 <= la <= 90 and -180 <= lo <= 180:
                    return "precise", {"lat": la, "lon": lo}
            except (TypeError, ValueError):
                pass
    if eidx_src and eidx_src.get("Province"):
        return "province", {"province": _norm_province(eidx_src["Province"])}
    return "unknown", {}


_srv = None


def _srv_mod():
    global _srv
    if _srv is None:
        import server
        _srv = server
    return _srv


def current_user():
    return _srv_mod().current_user()


def visible_tables(me):
    return _srv_mod().visible_tables(me)


def linked_eidx(tname):
    return _srv_mod().linked_eidx(tname)


def load_rows(tname):
    return _srv_mod().load_rows(tname)


def now():
    return _srv_mod().now()


# ---------------------------------------------------------------- 范围解析
def _sex_cols(cfg):
    """该表的数量列 → 统一性别键（不同表列名不同：跳蛛 AdultMale / 伪蝎 雄性数量）。"""
    names = {c["name"] for c in cfg["columns"]}
    out = {}
    for k, cands in _SEX_MAP.items():
        for c in cands:
            if c in names:
                out[k] = c
                break
    return out


def specimen_tables(vis):
    """标本实体表：有采集关联键（可统计采集地分布/事件去重）的可见表。"""
    return {t: c for t, c in vis.items() if c.get("linked_key")}


def resolve_scope(me, table=None, group=None):
    """解析统计范围 → ({tname: cfg}, 范围标签)。group=类群联合（仅标本实体表）。"""
    vis = visible_tables(me)
    if group:
        gkey = str(group)
        tabs = {t: c for t, c in specimen_tables(vis).items()
                if (c.get("taxon_group") or "spider") == gkey}
        if not tabs:
            abort(400, f"类群「{gkey}」下没有您可见的标本实体表")
        lbl = next((c.get("taxon_group_label") for c in tabs.values() if c.get("taxon_group_label")), gkey)
        return tabs, f"全部{lbl}标本表（联合）"
    if table:
        t = str(table)
        if t not in vis:
            abort(403, f"表「{t}」对您的账号不可见，不能纳入统计")
        return {t: vis[t]}, display_label(t)
    PT = next((t for t, c in vis.items() if c.get("primary")), None)
    if not PT:
        return {}, ""
    return {PT: vis[PT]}, display_label(PT)


def _validate_filters(tables, filters):
    """筛选字段必须存在于范围内每张表（联合统计口径一致；否则 400 具体指出）。"""
    if not isinstance(filters, dict):
        abort(400, "filters 需为 {列名: {mode,...}}")
    for col in filters:
        for t, cfg in tables.items():
            names = {c["name"] for c in cfg["columns"]}
            if col not in names:
                abort(400, f"筛选字段「{col}」在表「{cfg['label']}」中不存在——联合统计的条件字段"
                           f"须为范围内各表共有列（可改选单表统计）")
    return filters


def _rows_filtered(tname, q, filters, vis):
    """单表展示行（关联解析）+ 搜索 + 列筛选（与表格筛选同一语义）。"""
    cfg = TABLES[tname]
    eidx = linked_eidx(tname) if "events" in vis else None
    disp = []
    for r in load_rows(tname):
        if r["deleted"]:
            continue
        d, _miss = _srv_mod().display_row(tname, r, eidx)
        disp.append({"id": r["id"], "data": d})
    if filters:
        disp = _srv_mod()._apply_col_filters(disp, filters)
    if q:
        ql = q.lower()
        disp = [x for x in disp if any(v is not None and ql in str(v).lower()
                                       for v in x["data"].values())]
    return disp, eidx


def _empty_tally():
    return {"total": 0, "events": set(), "no_event_records": 0, "named": 0,
            "known_ind": 0, "with_qty": 0, "zero_qty": 0, "partial_qty": 0, "unknown_qty": 0,
            "fams": {}, "genera": {}, "tribes": {}, "provs": {}, "countries": {}, "years": {},
            "types": {}, "species": {}, "species_tmp": {},
            "sex": {"male": 0, "female": 0, "subadult": 0, "juvenile": 0, "unknown_records": 0},
            "tiers": {"precise": [], "province": {}, "unknown": 0}}


def _tally_table(tname, q, filters, vis):
    cfg = TABLES[tname]
    sexc = _sex_cols(cfg)
    lkey = cfg.get("linked_key") or "CollectionEventNumber"
    st = _empty_tally()
    disp, eidx = _rows_filtered(tname, q, filters, vis)
    for x in disp:
        d = x["data"]
        st["total"] += 1
        ce = str(d.get(lkey) or "").strip()
        src = eidx.get(ce) if eidx and ce else None
        if ce:
            st["events"].add(ce)
        else:
            st["no_event_records"] += 1
        if _first(d, _SPECIES_COL):
            st["named"] += 1
        sp = _first(d, _SPECIES_COL)
        if sp:
            st["species"][sp] = st["species"].get(sp, 0) + 1
        sp2 = str(d.get("TemporarySpeciesName") or d.get("Species name") or d.get("Temporary Name") or "").strip()
        if sp2:
            st["species_tmp"][sp2] = st["species_tmp"].get(sp2, 0) + 1
        # 数量（各表列名不同；非数字文本按 0 记录；全空=未知不当 0）
        vals = {}
        for k, col in sexc.items():
            v = str(d.get(col) or "").strip()
            vals[k] = int(v) if v.isdigit() else (0 if v else None)
        known = {k: v for k, v in vals.items() if v is not None}
        if known:
            st["with_qty"] += 1
            st["known_ind"] += sum(known.values())
            for k, v in known.items():
                st["sex"][k] += v
            if all(v == 0 for v in known.values()):
                st["zero_qty"] += 1
            if len(known) < len(sexc):
                st["partial_qty"] += 1
        else:
            st["unknown_qty"] += 1
            if sexc:
                st["sex"]["unknown_records"] += 1
        for key, cols in _TAXO_MAP:
            v = _first(d, cols)
            if v:
                st[key][v] = st[key].get(v, 0) + 1
        # 空间精度 + 省份/国家分布（省份=全部行的关联解析值，不止无坐标行）
        tier, loc = _loc_tier(d, src)
        if tier == "precise":
            st["tiers"]["precise"].append(loc)
        elif tier == "province":
            p = loc["province"]
            st["tiers"]["province"][p] = st["tiers"]["province"].get(p, 0) + 1
        else:
            st["tiers"]["unknown"] += 1
        prov = str(d.get("Province") or (src or {}).get("Province") or "").strip()
        if prov:
            prov = _norm_province(prov)
            st["provs"][prov] = st["provs"].get(prov, 0) + 1
        cty = str(d.get("Country") or (src or {}).get("Country") or "").strip()
        if cty:
            st["countries"][cty] = st["countries"].get(cty, 0) + 1
        y = str((src or {}).get("YYYY1") or d.get("YYYY1") or "")[:4]
        if y.isdigit():
            st["years"][y] = st["years"].get(y, 0) + 1
        t = d.get("TypeSpecimens") or d.get("IsTypeSpecimens")
        if t and str(t).strip().lower() != "no":
            st["types"][str(t)] = st["types"].get(str(t), 0) + 1
    return st


def _top(d, n=_TOP_N):
    return [{"name": k, "value": v} for k, v in sorted(d.items(), key=lambda x: (-x[1], x[0]))[:n]]


def _merge_tallies(sts):
    """多表合并：计数相加、集合/字典合并（事件全局去重、物种按名去重）。"""
    m = _empty_tally()
    for st in sts:
        m["total"] += st["total"]
        m["events"] |= st["events"]
        m["no_event_records"] += st["no_event_records"]
        m["named"] += st["named"]
        m["known_ind"] += st["known_ind"]
        m["with_qty"] += st["with_qty"]
        m["zero_qty"] += st["zero_qty"]
        m["partial_qty"] += st["partial_qty"]
        m["unknown_qty"] += st["unknown_qty"]
        for k in ("male", "female", "subadult", "juvenile", "unknown_records"):
            m["sex"][k] += st["sex"][k]
        for key in ("fams", "genera", "tribes", "provs", "countries", "years", "types",
                    "species", "species_tmp"):
            for k, v in st[key].items():
                m[key][k] = m[key].get(k, 0) + v
        m["tiers"]["precise"] += st["tiers"]["precise"]
        for p, v in st["tiers"]["province"].items():
            m["tiers"]["province"][p] = m["tiers"]["province"].get(p, 0) + v
        m["tiers"]["unknown"] += st["tiers"]["unknown"]
    return m


def build_enhanced_stats(me, table=None, group=None, q="", filters=None):
    tabs, scope_lbl = resolve_scope(me, table=table, group=group)
    q = str(q or "").strip()
    filters = _validate_filters(tabs, filters or {})
    vis = visible_tables(me)
    if not tabs:
        return {"empty": True, "scope": {"note": "无可见主表"}, "tiers": {}, "metrics": {}}

    sts, per_table = [], []
    for t, cfg in tabs.items():
        st = _tally_table(t, q, filters, vis)
        sts.append(st)
        per_table.append({"table": t, "label": display_label(t), "records": st["total"]})
    m = _merge_tallies(sts)

    qf = hashlib.sha256(json.dumps(
        {"tables": sorted(tabs.keys()), "group": group or "", "q": q, "filters": filters,
         "user": me["username"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    total = m["total"]
    sp_items = _top(m["species"], 100)
    exclusions = ["回收站行不参与统计", "无权限表不参与统计（按逐表三态权限隔离）"]
    if q:
        exclusions.append(f"仅统计搜索「{q}」命中的行")
    if filters:
        exclusions.append("仅统计满足全部列筛选条件的行（条件见 scope.filters）")

    return {
        "empty": False,
        "scope": {
            "tables": per_table,
            "table_label": scope_lbl,
            "table": per_table[0]["table"] if len(per_table) == 1 else None,
            "taxon_group": group or me.get("group") or "全部类群",
            "visible_tables": sorted(vis.keys()),
            "query_fingerprint": qf,
            "filter_q": q,
            "filters": filters,
            "denominator": " / ".join(f"{p['label']} {p['records']} 行" for p in per_table)
                           + (f"（搜索+筛选后；共 {total} 行）" if (q or filters) else ""),
            "exclusions": exclusions,
            "no_composite_score": True,
        },
        "metrics": {
            "records": {"value": total,
                        "definition": "标本记录数：未删除且命中当前筛选的行数",
                        "denominator": "范围内全部未删除标本记录"},
            "unique_events": {"value": len(m["events"]),
                              "definition": "独立采集事件数：按稳定事件身份（CollectionEventNumber）去重（跨表去重）",
                              "denominator": f"标本记录 {total} 条",
                              "exclusions": f"无事件号或未匹配事件的 {m['no_event_records']} 条记录单独统计，不算新事件"},
            "no_event_records": {"value": m["no_event_records"],
                                 "definition": "无事件号记录数：未关联采集事件，单独统计",
                                 "denominator": f"标本记录 {total} 条",
                                 "exclusions": "不计入独立事件数"},
            "named_records": {"value": m["named"],
                              "definition": "最终种名已填记录数（与个体数量分开）",
                              "denominator": f"标本记录 {total} 条",
                              "exclusions": "cf./sp. nov. 等限定名按已填名计入"},
            "species_count": {"value": len(m["species"]),
                              "definition": "物种数：最终种名去重（跨表同名算一个物种；未定名行不计入）",
                              "denominator": f"标本记录 {total} 条（已定名 {m['named']} 条）",
                              "exclusions": f"另有暂定名去重 {len(m['species_tmp'])} 个（含未正式鉴定名，单列不计入本数）"},
            "known_individuals": {"value": m["known_ind"],
                                  "definition": "已记录个体数：各表数量字段（雄/雌/亚成/幼）求和",
                                  "denominator": f"标本记录 {total} 条（有数量 {m['with_qty']} 条）",
                                  "exclusions": "仅统计已记录数量；数量缺失时不推断实际总数"},
            "recorded_qty_completeness": {"value": m["with_qty"],
                                          "definition": "有数量记录数（含部分缺失）",
                                          "denominator": f"标本记录 {total} 条",
                                          "exclusions": f"明确全 0 的 {m['zero_qty']} 条不算未知；"
                                                        f"部分缺失 {m['partial_qty']} 条显示完整性不推断总数"},
            "unknown_quantity": {"value": m["unknown_qty"],
                                 "definition": "数量未知：数量列全空（与明确全 0 不同）",
                                 "denominator": f"标本记录 {total} 条",
                                 "exclusions": "缺少个体数量不当 0；明确全 0 不算未知"},
        },
        "sex": {"male": m["sex"]["male"], "female": m["sex"]["female"],
                "subadult": m["sex"]["subadult"], "juvenile": m["sex"]["juvenile"],
                "unknown_records": m["sex"]["unknown_records"],
                "note": "个体数按各表数量列求和（跳蛛 AdultMale/AdultFemale…、伪蝎 雄性数量/雌性数量…）；"
                        "空值不当 0；非数字文本按 0 计并可在体检中核查；unknown_records=数量列全空的记录数"},
        "species": {"count": len(m["species"]), "temp_count": len(m["species_tmp"]),
                    "top": sp_items,
                    "note": "按最终种名去重；未定名行不计入。暂定名单列（含 sp. nov./cf. 等未正式鉴定名）"},
        "tiers": {
            "precise": {"count": len(m["tiers"]["precise"]), "points": m["tiers"]["precise"][:200],
                        "note": "有经纬度的标本（坐标精度取决于原始记录）"},
            "province": {"count": sum(m["tiers"]["province"].values()), "provinces": m["tiers"]["province"],
                         "note": "仅知省份的标本：按省名做区域聚合，不用省会坐标冒充标本位置"},
            "unknown": {"count": m["tiers"]["unknown"],
                        "note": "完全未知位置（无经纬度且无省份）：不计入地图"},
        },
        "families": _top(m["fams"]),
        "genera": _top(m["genera"]),
        "tribes": _top(m["tribes"]),
        "provinces": _top(m["provs"]),
        "countries": _top(m["countries"]),
        "years": [{"name": k, "value": v} for k, v in sorted(m["years"].items())[:30]],
        "types": _top(m["types"], 10),
        "per_table": per_table,
        "no_event_count": m["no_event_records"],
    }


def register(app):
    def _params():
        table = request.args.get("table") or None
        group = request.args.get("group") or None
        if table and group:
            abort(400, "table 与 group 只能二选一")
        q = request.args.get("q") or ""
        filters = {}
        raw = request.args.get("filters") or ""
        if raw:
            try:
                filters = json.loads(raw)
                if not isinstance(filters, dict):
                    filters = {}
            except ValueError:
                abort(400, "filters 不是合法 JSON")
        return table, group, q, filters

    @app.get("/api/stats/enhanced")
    def api_stats_enhanced():
        me = current_user()
        table, group, q, filters = _params()
        return jsonify(build_enhanced_stats(me, table=table, group=group, q=q, filters=filters))

    @app.get("/api/stats")
    def api_stats():
        """图表轻量端点（与 enhanced 同一筛选参数、同一聚合管道）。"""
        me = current_user()
        table, group, q, filters = _params()
        d = build_enhanced_stats(me, table=table, group=group, q=q, filters=filters)
        return jsonify({"table": d.get("scope", {}).get("table"),
                        "empty": d.get("empty", True),
                        "total": sum(p["records"] for p in d.get("per_table", [])),
                        "families": d.get("families", []), "provinces": d.get("provinces", []),
                        "types": d.get("types", []), "years": d.get("years", [])})

    @app.get("/api/stats/chart-download")
    def api_stats_chart_download():
        """图表数据下载（CSV）：附范围/筛选条件、数据子集、指标定义和排除数量。"""
        me = current_user()
        table, group, q, filters = _params()
        data = build_enhanced_stats(me, table=table, group=group, q=q, filters=filters)
        scope = data.get("scope", {})
        lines = ["# 统计数据导出", f"# 统计范围: {scope.get('table_label', '')}",
                 "# 统计表: " + "、".join(f"{p['label']}({p['records']}行)" for p in scope.get("tables", []))]
        if scope.get("filter_q"):
            lines.append(f"# 搜索词: {scope['filter_q']}")
        for col, f in (scope.get("filters") or {}).items():
            cond = {"in": f"属于 {'、'.join(f.get('values') or [])}",
                    "contains": f"包含「{f.get('text') or ''}」",
                    "empty": "为空", "notempty": "不为空"}.get(f.get("mode"), str(f))
            lines.append(f"# 筛选: {col} {cond}")
        lines += [f"# 分母: {scope.get('denominator', '')}",
                  f"# 排除: {'; '.join(scope.get('exclusions', []))}",
                  f"# 无综合得分: {scope.get('no_composite_score', True)}",
                  f"# 生成时点: {now()}", ""]
        for m_key, m_def in data.get("metrics", {}).items():
            lines.append(f"# 指标 {m_key}: {m_def['definition']}（分母: {m_def['denominator']}）")
        sx = data.get("sex") or {}
        if sx:
            lines += ["", "性别/数量,个体数",
                      f"雄,{sx.get('male', 0)}", f"雌,{sx.get('female', 0)}",
                      f"亚成,{sx.get('subadult', 0)}", f"幼体,{sx.get('juvenile', 0)}",
                      f"数量未知的记录数,{sx.get('unknown_records', 0)}"]
        spc = data.get("species") or {}
        if spc:
            lines += ["", f"# 物种数（最终名去重）: {spc.get('count', 0)}；暂定名去重: {spc.get('temp_count', 0)}"]
        lines += ["", "维度,名称,计数"]
        for prov in data.get("provinces", []):
            lines.append(f"省份,{prov['name']},{prov['value']}")
        for c in data.get("countries", []):
            lines.append(f"国家,{c['name']},{c['value']}")
        for fam in data.get("families", []):
            lines.append(f"科,{fam['name']},{fam['value']}")
        for g in data.get("genera", []):
            lines.append(f"属,{g['name']},{g['value']}")
        for tr in data.get("tribes", []):
            lines.append(f"族,{tr['name']},{tr['value']}")
        for sp0 in (spc.get("top") or [])[:50]:
            lines.append(f"物种,{sp0['name']},{sp0['value']}")
        for yr in data.get("years", []):
            lines.append(f"年份,{yr['name']},{yr['value']}")
        for t0 in data.get("types", []):
            lines.append(f"模式类型,{t0['name']},{t0['value']}")
        lines += ["", "空间精度,计数"]
        for tk, tv in data.get("tiers", {}).items():
            lines.append(f"{tk},{tv['count'] if isinstance(tv, dict) else tv}")
        content = "\n".join(lines)
        return Response(content, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=stats_export.csv"})
