# -*- coding: utf-8 -*-
"""自定义/期刊模板导出（v3.66）。

核心约束：
- 字段绑定稳定身份（field_catalog field_id 或表内部键）；输出别名只影响导出列名。
- 复合字段仅允许白名单格式函数（不执行用户代码/SQL/任意脚本）。
- 格式化（日期/坐标/单位/缺失值/分隔符/数值）只影响导出，不改库；保留部分日期与未知，不补造。
- 行身份明确：record（每行=一条记录）/ specimen；一对多关系必须显式选 expand / aggregate /
  representative（人工指定），禁止静默取第一条或自动去重。
- 干净投稿文件 + 独立伴随说明文件（README 不强制插入数据表）。
"""
import json
import re

from flask import abort, jsonify, request, Response

import db as dbm
from tables_def import TABLES
import field_catalog as FC

# 复合字段白名单格式函数：fn(parts: list[str], options) -> str
COMPOSITE_FUNCS = {
    "join": lambda parts, opts, miss: opts.get("sep", ", ").join(p for p in parts if p not in (None, "")),
    "locality": lambda parts, opts, miss: " ".join(p for p in parts if p not in (None, "")),
    "material_desc": lambda parts, opts, miss: " ".join(p for p in parts if p not in (None, "")),
    "first_nonempty": lambda parts, opts, miss: next((p for p in parts if p not in (None, "")), None),
}
MISSING_MARKS = {"empty": "", "dash": "—", "unknown": "未知", "custom": None}


def _srv():
    import server
    return server


def _fmt_date_parts(d, opts):
    """日期三分量 → 显示；保留部分日期与原始写法（如月份区间 25-26 不解析不崩）。"""
    y, m, dd = d.get(opts.get("y", "YYYY1")), d.get(opts.get("m", "MM1")), d.get(opts.get("d", "DD1"))
    parts = [str(x).strip() for x in (y, m, dd) if str(x or "").strip() not in ("", "None")]

    def _norm_num(x):
        return f"{int(x):02d}" if x.isdigit() else x   # 非数字原样保留

    if not parts:
        return _missing(opts)
    if len(parts) == 3:
        return f"{parts[0]}-{_norm_num(parts[1])}-{_norm_num(parts[2])}"
    if len(parts) == 2:
        return f"{parts[0]}-{_norm_num(parts[1])}"
    return parts[0]


def _missing(opts):
    mark = opts.get("missing", "dash")
    if mark == "custom":
        return opts.get("missing_text", "")
    return MISSING_MARKS.get(mark, "—")


def _fmt_number(v, opts):
    """数值格式化：非数字文本（如日期区间 25-26、备注）原样保留——不猜、不崩。"""
    if v is None or str(v).strip() == "":
        return _missing(opts)
    s = str(v).strip()
    try:
        n = float(s)
    except (TypeError, ValueError):
        return v          # 非数字原样保留（部分日期/区间/自由文本）
    if n == int(n) and not opts.get("decimals"):
        return str(int(n))
    return f"{n:.{int(opts.get('decimals', 2))}f}"


def _sci_name(name, opts):
    """学名语义渲染：属种斜体（XLSX 富文本由渲染器处理；这里返回结构标记）。"""
    s = str(name or "").strip()
    if not s:
        return _missing(opts)
    return s  # 标记在 xlsx 渲染器里切分


def render_cell(col, row_data, eidx_src=None):
    """按列定义渲染单元格值。col: {source: key|composite, field, func?, parts?, opts?}"""
    opts = col.get("opts") or {}
    if col.get("source") == "composite":
        fn = COMPOSITE_FUNCS.get(col.get("func") or "")
        if not fn:
            return None, f"未知复合函数 {col.get('func')}（白名单外已拒绝）"
        parts = []
        for part in col.get("parts") or []:
            if part.get("source") == "date":
                parts.append(_fmt_date_parts(row_data, part.get("opts") or {}))
            else:
                v = row_data.get(part.get("field"))
                parts.append("" if v is None else str(v))
        try:
            return fn(parts, opts, opts), None
        except Exception as e:  # 白名单函数不应抛错；防御
            return None, f"复合函数执行失败：{e}"
    if col.get("source") == "date":
        return _fmt_date_parts(row_data, opts), None
    if col.get("source") == "number":
        return _fmt_number(row_data.get(col.get("field")), opts), None
    if col.get("source") == "sci":
        return _sci_name(row_data.get(col.get("field")), opts), None
    v = row_data.get(col.get("field"))
    if v is None or str(v).strip() == "":
        return _missing(opts), None
    return v, None


def _default_templates(tname="specimens"):
    """三类通用起始模板（未核实期刊规范 → 明确标记 custom/通用，rules_source=personal）。"""
    return [
        {"name": "系统发育样本信息表（通用）", "template_kind": "phylo", "row_identity": "record",
         "one_to_many": "expand",
         "columns_def": [
             {"field": "MHBU #", "out": "SpecimenCode", "source": "key"},
             {"field": "Genus", "out": "Genus", "source": "plain"},
             {"field": "FinalSpeciesName", "out": "Species", "source": "sci"},
             {"field": "CollectionEventNumber", "out": "EventID", "source": "plain"},
             {"source": "date", "out": "CollectionDate",
              "opts": {"y": "YYYY1", "m": "MM1", "d": "DD1", "missing": "dash"}},
             {"field": "Latitude1", "out": "Latitude", "source": "plain"},
             {"field": "Longitude1", "out": "Longitude", "source": "plain"},
             {"field": "Country", "out": "Country", "source": "plain"},
             {"field": "Province", "out": "Province", "source": "plain"},
             {"field": "Locality", "out": "Locality", "source": "plain"},
         ]},
        {"name": "演化分析元数据表（通用）", "template_kind": "evolution", "row_identity": "record",
         "one_to_many": "expand",
         "columns_def": [
             {"field": "MHBU #", "out": "Voucher", "source": "key"},
             {"field": "DNAVoucherID", "out": "DNAVoucher", "source": "plain"},
             {"field": "Genus", "out": "Genus", "source": "plain"},
             {"field": "FinalSpeciesName", "out": "Species", "source": "sci"},
             {"source": "composite", "out": "Locality", "func": "locality",
              "parts": [{"field": "Country"}, {"field": "Province"}, {"field": "Locality"}]},
             {"source": "date", "out": "CollectionDate",
              "opts": {"y": "YYYY1", "m": "MM1", "d": "DD1", "missing": "dash"}},
             {"field": "Collector", "out": "Collectors", "source": "plain"},
         ]},
        {"name": "分类学 Material examined（通用，作者可自定义）", "template_kind": "material",
         "row_identity": "record", "one_to_many": "expand",
         "columns_def": [
             {"field": "FinalSpeciesName", "out": "Species", "source": "sci"},
             {"field": "Country", "out": "Country", "source": "plain"},
             {"field": "Province", "out": "Province", "source": "plain"},
             {"field": "Locality", "out": "Locality", "source": "plain"},
             {"field": "Collector", "out": "Collectors", "source": "plain"},
             {"source": "date", "out": "Date", "opts": {"y": "YYYY1", "m": "MM1", "d": "DD1"}},
             {"source": "composite", "out": "MaterialDescription", "func": "material_desc",
              "parts": [{"field": "AdultMale"}, {"field": "AdultFemale"},
                        {"field": "Subadult"}, {"field": "Juvenile"}]},
             {"field": "MHBU #", "out": "Specimens", "source": "key"},
         ]},
    ]


def register(app):
    @app.get("/api/export-templates")
    def api_tpl_list():
        user = _srv().current_user()
        conn = _srv().get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM export_templates ORDER BY favorite DESC, id DESC")]
        vis = _srv().visible_tables(user)
        out = [r for r in rows if r["scope"] == "personal" and r["owner"] == user["username"]
               or r["scope"] == "team"]
        for r in out:
            r["columns_def"] = json.loads(r["columns_def"] or "[]")
        return jsonify({"templates": out, "defaults": _default_templates(),
                        "note": "团队模板共享不含数据访问权；修改团队模板生成新修订"})

    @app.post("/api/export-templates")
    def api_tpl_create():
        user = _srv().current_user()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            abort(400, "模板名称必填")
        cols = body.get("columns_def") or []
        if not cols:
            abort(400, "columns_def 必填（至少一列）")
        _validate_columns(cols)
        scope = body.get("scope") or "personal"
        if scope == "team" and not user.get("admin"):
            abort(403, "团队模板创建/修改需管理员（修订留痕）")
        conn = _srv().get_db()
        cur = conn.execute(
            "INSERT INTO export_templates(name,scope,owner,base_template_id,template_kind,tname,"
            "row_identity,one_to_many,columns_def,group_by,sort_by,sheet_name,title,footer,fmt,"
            "journal,article_type,author_guide_url,rules_checked_at,rules_source,"
            "project_default_for,favorite,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, scope, user["username"], body.get("base_template_id"),
             body.get("template_kind") or "custom", body.get("tname") or "specimens",
             body.get("row_identity") or "record", body.get("one_to_many") or "expand",
             json.dumps(cols, ensure_ascii=False), body.get("group_by"), body.get("sort_by"),
             body.get("sheet_name"), body.get("title"), body.get("footer"),
             body.get("fmt") or "xlsx", body.get("journal"), body.get("article_type"),
             body.get("author_guide_url"), body.get("rules_checked_at"),
             body.get("rules_source") or "personal",
             body.get("project_default_for"), 1 if body.get("favorite") else 0,
             user["username"], _srv().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})

    @app.put("/api/export-templates/<int:tid>")
    def api_tpl_update(tid):
        user = _srv().current_user()
        conn = _srv().get_db()
        t = conn.execute("SELECT * FROM export_templates WHERE id=?", (tid,)).fetchone()
        if not t:
            abort(404, "模板不存在")
        if t["scope"] == "personal" and t["owner"] != user["username"]:
            abort(403, "只能修改自己的个人模板")
        body = request.get_json(silent=True) or {}
        if t["scope"] == "team" and "columns_def" in body:
            if not user.get("admin"):
                abort(403, "团队模板修改需管理员；修改生成新修订")
            conn.execute("INSERT INTO template_revisions(template_id,revision,columns_def,changed_by,note,created_at) "
                         "VALUES(?,?,?,?,?,?)",
                         (tid, t["revision"], t["columns_def"], user["username"],
                          body.get("revision_note") or "修订", _srv().now()))
            conn.execute("UPDATE export_templates SET revision=revision+1 WHERE id=?", (tid,))
        sets, args = [], []
        simple = {"name", "sheet_name", "title", "footer", "fmt", "journal", "article_type",
                  "author_guide_url", "rules_checked_at", "rules_source", "group_by", "sort_by",
                  "row_identity", "one_to_many", "template_kind"}
        for k in simple:
            if k in body:
                sets.append(f"{k}=?")
                args.append(str(body[k] or ""))
        for k in ("favorite",):
            if k in body:
                sets.append(f"{k}=?")
                args.append(1 if body[k] else 0)
        if "columns_def" in body:
            _validate_columns(body["columns_def"])
            sets.append("columns_def=?")
            args.append(json.dumps(body["columns_def"], ensure_ascii=False))
        if not sets:
            abort(400, "没有要修改的字段")
        conn.execute(f"UPDATE export_templates SET {','.join(sets)} WHERE id=?", (*args, tid))
        conn.commit()
        return jsonify({"ok": True})

    @app.delete("/api/export-templates/<int:tid>")
    def api_tpl_delete(tid):
        user = _srv().current_user()
        conn = _srv().get_db()
        t = conn.execute("SELECT * FROM export_templates WHERE id=?", (tid,)).fetchone()
        if not t:
            abort(404, "模板不存在")
        if t["scope"] == "team" and not user.get("admin"):
            abort(403, "团队模板删除需管理员")
        if t["scope"] == "personal" and t["owner"] != user["username"]:
            abort(403, "只能删除自己的个人模板")
        conn.execute("DELETE FROM export_templates WHERE id=?", (tid,))
        conn.commit()
        return jsonify({"ok": True})

    @app.post("/api/export-templates/<int:tid>/copy")
    def api_tpl_copy(tid):
        """复制模板（生成个人副本；团队模板复制不授予数据访问权）。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        t = conn.execute("SELECT * FROM export_templates WHERE id=?", (tid,)).fetchone()
        if not t:
            abort(404, "模板不存在")
        cur = conn.execute(
            "INSERT INTO export_templates(name,scope,owner,base_template_id,template_kind,tname,"
            "row_identity,one_to_many,columns_def,group_by,sort_by,sheet_name,title,footer,fmt,"
            "journal,article_type,author_guide_url,rules_checked_at,rules_source,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(body_name(t, user)), "personal", user["username"], tid,
             t["template_kind"], t["tname"], t["row_identity"], t["one_to_many"],
             t["columns_def"], t["group_by"], t["sort_by"], t["sheet_name"], t["title"],
             t["footer"], t["fmt"], t["journal"], t["article_type"], t["author_guide_url"],
             t["rules_checked_at"], "personal", user["username"], _srv().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})

    def body_name(t, user):
        import datetime
        return f"{t['name']}（副本 {datetime.datetime.now().strftime('%m-%d %H:%M')}）"

    @app.post("/api/export-templates/preview")
    def api_tpl_preview():
        """导出向导统一预览：前 N 行 + 全量检查（问题分级：阻断/需确认/信息）+ 全量缺失统计（含输出策略）。"""
        user = _srv().current_user()
        body = request.get_json(silent=True) or {}
        cols = body.get("columns_def") or []
        _validate_columns(cols)
        _srv().ensure_visible(user, body.get("tname") or "specimens")
        rows_in, vinfo = _srv()._snapshot_rows(body.get("tname") or "specimens",
                                               body.get("version") or "current")
        rows_in = [r for r in rows_in if not r.get("deleted")]
        eidx = _srv().linked_eidx(body.get("tname") or "specimens")
        # 可选行级收敛（如 q/filters/ids），避免大表中目标行不在预览前 20 行
        id_set = None
        if body.get("rids"):
            id_set = {int(x) for x in body.get("rids")}
        # 缺失统计口径：每列的具体缺项字段与输出策略（全量行统计，不笼统报合格）
        def col_missing_spec(col):
            src = col.get("source") or "plain"
            o = col.get("opts") or {}
            if src == "composite":
                parts = [p.get("field") for p in col.get("parts") or []]
                return parts, "复合列所有组成部分为空 → 该项自动省略"
            if src == "date":
                fy, fm, fd = (str(o.get("y") or "YYYY1"), str(o.get("m") or "MM1"), str(o.get("d") or "DD1"))
                mark = o.get("missing", "dash")
                text = o.get("missing_text") if mark == "custom" else MISSING_MARKS.get(mark, "—")
                return [fy, fm, fd], f"日期三分量（{fy}/{fm}/{fd}）全空 → 输出「{text}」"
            if src == "number":
                mark = o.get("missing", "dash")
                text = o.get("missing_text") if mark == "custom" else MISSING_MARKS.get(mark, "—")
                return [col.get("field")], f"数值缺失 → 输出「{text}」；非数字文本原样保留"
            mark = o.get("missing", "dash")
            text = o.get("missing_text") if mark == "custom" else MISSING_MARKS.get(mark, "—")
            return [col.get("field")], f"字段缺失/空白 → 输出「{text}」"

        miss_specs = []
        for col in cols:
            out_name = col.get("out") or col.get("field") or col.get("source")
            flds, strat = col_missing_spec(col)
            miss_specs.append((out_name, [f for f in flds if f], strat))
        rows_out, problems = [], []
        miss_count = {spec[0]: 0 for spec in miss_specs}
        scanned = 0
        for r in rows_in:
            if id_set is not None and r["id"] not in id_set:
                continue
            scanned += 1
            d, miss = _srv().display_row(body.get("tname") or "specimens", r, eidx)
            if miss:
                problems.append({"rid": r["id"], "kind": "source_missing", "severity": "confirm",
                                 "detail": f"引用的事件号 {miss} 在采集地信息未找到"})
            rendered = {}
            for col, (out_name, flds, _strat) in zip(cols, miss_specs):
                v, err = render_cell(col, d)
                if err:
                    problems.append({"rid": r["id"], "kind": "render_error", "severity": "blocking",
                                     "col": out_name, "detail": err})
                if flds and all(not (d.get(f) and str(d.get(f)).strip()) for f in flds):
                    miss_count[out_name] += 1
                rendered[out_name] = v
            rows_out.append({"rid": r["id"], "cells": rendered})
        preview = rows_out[:20]
        # 检查：分析ID重复 / 必要字段缺失 / 一对多膨胀提示
        id_col = next((c for c in cols if c.get("out") in ("SpecimenCode", "Voucher", "Specimens")), None)
        if id_col:
            seen = {}
            for ro in rows_out:
                k = ro["cells"].get(id_col.get("out"))
                seen.setdefault(k, 0)
                seen[k] += 1
            dups = [k for k, n in seen.items() if n > 1 and k not in (None, "")]
            if dups:
                problems.append({"kind": "duplicate_ids", "severity": "info",
                                 "detail": f"重复标识：{dups[:5]}（一对多展开时属预期，请按需确认）"})
        missing = [{"out": name, "count": miss_count[name], "strategy": strat}
                   for name, _flds, strat in miss_specs if miss_count[name] > 0]
        return jsonify({"preview": preview, "total": len(rows_out), "input_rows": scanned,
                        "problems": problems[:80], "problem_count": len(problems),
                        "missing": missing,
                        "note": "预览前 20 行；检查与缺失统计为全量（基于与生成相同的范围/模板/版本）"})

    @app.post("/api/export-templates/generate")
    def api_tpl_generate():
        """按模板生成文件：XLSX/CSV/TSV + 独立伴随说明文件（README 不插入数据表）。"""
        user = _srv().current_user()
        body = request.get_json(silent=True) or {}
        cols = body.get("columns_def") or []
        _validate_columns(cols)
        tname = body.get("tname") or "specimens"
        _srv().ensure_visible(user, tname)
        _srv().require_writer(user, tname)
        conn = _srv().get_db()
        if body.get("template_id"):
            t = conn.execute("SELECT * FROM export_templates WHERE id=?", (body["template_id"],)).fetchone()
            if t and not body.get("columns_def"):
                cols = json.loads(t["columns_def"] or "[]")
        rows_in, vinfo = _srv()._snapshot_rows(tname, body.get("version") or "current")
        rows_in = [r for r in rows_in if not r.get("deleted")]
        if body.get("rids"):
            id_set = {int(x) for x in body["rids"]}
            rows_in = [r for r in rows_in if r["id"] in id_set]
        eidx = _srv().linked_eidx(tname)
        out_cols = [c.get("out") or c.get("field") for c in cols]
        data_rows = []
        for r in rows_in:
            d, _ = _srv().display_row(tname, r, eidx)
            cells = []
            for col in cols:
                v, _err = render_cell(col, d)
                cells.append(v)
            data_rows.append(cells)
        fmt = (body.get("fmt") or "xlsx").lower()
        readme = _build_readme(body, cols, len(data_rows), user, vinfo)
        _exdb = _srv().get_db()
        _srv().log_edit(_exdb, user["username"], tname, None, None, "export",
                        {"_summary": f"模板导出「{body.get('template_name') or '未命名'}」（{fmt.upper()}）："
                                     f"{tname} {len(data_rows)} 行"
                                     + (f"，指定 {len(body.get('rids') or [])} 条" if body.get("rids") else "，全表"),
                         "_scope": {"fmt": fmt, "rows": len(data_rows),
                                    "rids_n": len(body.get("rids") or []),
                                    "template": body.get("template_name")}},
                        source="template-export")
        _exdb.commit()
        from urllib.parse import quote as _q
        safe = "".join(ch for ch in (body.get("template_name") or "template_export")
                       if ch not in '\\/:*?"<>|') or "template_export"
        if fmt == "xlsx":
            return _gen_xlsx(out_cols, data_rows, body, readme, safe)
        sep = "\t" if fmt == "tsv" else ","
        lines = [sep.join(f'{_csv_cell(c, sep)}' for c in out_cols)]
        for cells in data_rows:
            lines.append(sep.join(_csv_cell(c, sep) for c in cells))
        return Response("\n".join(lines), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{_q(safe)}.{fmt}; filename=template_export.{fmt}",
                                 })


def _csv_cell(v, sep):
    s = "" if v is None else str(v)
    if any(ch in s for ch in (sep, '"', "\n")) or s.startswith(("=", "+", "-", "@")):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _gen_xlsx(out_cols, data_rows, body, readme, safe_name="template_export"):
    import io as _io
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheet = body.get("sheet_name") or "data"
    sheet = re.sub(r"[\/:*?\[\]]", "-", str(sheet))[:31]  # 与 server._sheet_title 同口径防触发修复
    ws = wb.create_sheet(sheet)
    if body.get("title"):
        ws.append([body["title"]])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([])
    ws.append(out_cols)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    # 学名斜体/性别符号：XLSX 对 sci 源列做富文本拆分（属种斜体）——简化为按列标记 italic 列
    for cells in data_rows:
        ws.append([_xlsx_safe(v) for v in cells])
    ws2 = wb.create_sheet("README（伴随说明）") if body.get("readme_in_file") else None
    if ws2:
        for row in readme:
            ws2.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from urllib.parse import quote as _q
    safe = "".join(ch for ch in (body.get("template_name") or "template_export") if ch not in '\\/:*?"<>|') or "template_export"
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{_q(safe_name)}.xlsx; filename=template_export.xlsx"})


def _xlsx_safe(v):
    if v is None:
        return None
    s = str(v)
    if s and s[0] in ("=", "+", "@"):
        return "'" + s
    return s


def _build_readme(body, cols, n, user, vinfo):
    return [["项目", "值"],
            ["导出人", user["username"]],
            ["导出时点", _srv().now()],
            ["模板", body.get("template_name") or body.get("template_id") or "临时"],
            ["行数", n],
            ["列", "、".join(c.get("out") or c.get("field") or "" for c in cols)],
            ["期刊", body.get("journal") or "未填写"],
            ["规则来源", body.get("rules_source") or "personal"],
            ["说明", "本文件为伴随说明；数据表不含 README（干净投稿文件）"]]


def _validate_columns(cols):
    """列定义校验：复合函数必须在白名单；来源合法；输出名非空。"""
    if not cols:
        abort(400, "columns_def 为空集合：请至少定义一列")
    for c in cols:
        src = c.get("source") or "plain"
        if src not in ("plain", "key", "sci", "date", "number", "composite"):
            abort(400, f"未知列来源 {src}")
        if src == "composite":
            if (c.get("func") or "") not in COMPOSITE_FUNCS:
                abort(400, f"复合函数 {c.get('func')} 不在白名单（仅允许：{'、'.join(COMPOSITE_FUNCS)}）；"
                           f"不接受用户代码/SQL/任意脚本")
            if not c.get("parts"):
                abort(400, "复合列需要 parts 字段清单")
        elif src not in ("date",) and not c.get("field"):
            abort(400, f"列 {c.get('out') or '?'} 缺少 field（稳定字段身份）")
        if not (c.get("out") or c.get("field")):
            abort(400, "输出列名不能全空")
