# -*- coding: utf-8 -*-
"""协作编辑包（v3.62 / v3.69 补界面与文件闭环）：离线协作 → 权威基线三方合并。

包结构（服务器权威基线，不信任 Excel 隐藏列）：
  edit_packages 表：包 ID / 创建者 / 允许编辑范围（表+列白名单）/ 过期时间 / 状态
  edit_package_rows：每行稳定记录标识（tname+rid）+ 导出时基础值 + 基础 row_version
  Excel 文件只带包 ID + 数据（身份/版本由服务器核对，不信任文件内隐藏列）

回传三方比较：基础值 vs 当前服务器值 vs 回传值
  → 只有真实变化形成导入计划；非冲突变化可提交；同字段双方修改需审阅（409，
    可 force=回传值为准，逐处留审计）；行版本不符/不在包内/列越范围始终阻止。
幂等：同包重复回传返回原结果；过期/失去基线/权限收回明确阻止。

v3.69 增量（配套界面）：
  GET  /api/collab/packages                包列表（本人创建；管理员全部）
  POST /api/collab/resolve                 标本号列表 → 稳定 rid（创建包用）
  GET  /api/collab/packages/<id>/workbook  离线编辑工作簿（编号列+可编辑列当前值）
  POST /api/collab/packages/<id>/receive-file  回传 xlsx/csv/tsv → 同一三方合并
"""
import json

from flask import abort, jsonify, request, send_file

import db as dbm
from tables_def import TABLES

PKG_EXPIRE_HOURS = 72
_KEY = "MHBU #"  # 离线文件中的行身份列（精确编号 → rid 由服务器解析）


def _srv():
    import server
    return server


def _load_pkg(conn, pkg_id):
    p = conn.execute("SELECT * FROM edit_packages WHERE pkg_id=?", (pkg_id,)).fetchone()
    if not p:
        abort(404, "包不存在")
    return p


def _num_map(conn, tname="specimens"):
    """编号 → rid（精确匹配；删行/改号后自然失配，属预期保护）。"""
    m = {}
    for rid, num in conn.execute(
            "SELECT id, json_extract(data,'$.\"' || ? || '\"') FROM records "
            "WHERE tname=? AND deleted=0", (_KEY, tname)):
        if num is not None and str(num).strip() != "":
            m.setdefault(str(num).strip(), rid)
    return m


def _norm_cell(v):
    """离线文件单元格 → 与 JSON 数据同口径（空串→None、整值浮点→int、去首尾空白）。"""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        t = v.strip()
        return t if t != "" else None
    return v


def _merge_receive(user, p, updates, force=False):
    """三方合并核心（receive 与 receive-file 共用）。返回 (payload, http_status)。"""
    conn = _srv().get_db()
    if p["status"] != "active":
        abort(409, f"包状态 {p['status']}（已过期/已收回/已合并），不能自动合并")
    # 权限按当前用户（不继承文件来源）
    _srv().require_writer(user, p["tname"])
    allowed_cols = json.loads(p["allowed_cols"] or "[]")
    plans, conflicts, forced, noop = [], [], [], 0
    seen_rids = set()
    for u in updates:
        rid = int(u.get("rid") or 0)
        if rid in seen_rids:
            continue
        seen_rids.add(rid)
        pr = conn.execute(
            "SELECT * FROM edit_package_rows WHERE package_id=? AND rid=? AND tname=?",
            (p["id"], rid, p["tname"])).fetchone()
        if not pr:
            conflicts.append({"rid": rid, "reason": "不在包内（删行不代表删除数据库记录；新增需另行走导入流程）"})
            continue
        base_vals = json.loads(pr["base_values"])
        cur_row = conn.execute("SELECT data, row_version FROM records WHERE id=? AND tname=?",
                               (rid, p["tname"])).fetchone()
        if not cur_row or cur_row["row_version"] != pr["base_row_version"]:
            # 导出后服务器已修改：进入冲突审阅（force 不放行——须重新下载工作簿）
            conflicts.append({"rid": rid, "reason": "导出后服务器已修改（行版本不符）"})
            continue
        cur_data = json.loads(cur_row["data"])
        changes = {}
        for col, new_v in (u.get("values") or {}).items():
            if col not in allowed_cols:
                conflicts.append({"rid": rid, "col": col, "reason": "列不在允许编辑范围"})
                continue
            old_base = base_vals.get(col)
            cur_val = cur_data.get(col)
            if _srv().same(cur_val, new_v):
                continue  # 无变化
            if not _srv().same(old_base, cur_val):
                # 同字段双方修改 → 审阅；force 时回传值为准（逐处留审计）
                if force:
                    changes[col] = new_v
                    forced.append({"rid": rid, "col": col, "base": old_base,
                                   "server": cur_val, "returned": new_v})
                    continue
                conflicts.append({"rid": rid, "col": col, "base": old_base,
                                  "server": cur_val, "returned": new_v,
                                  "reason": "同字段双方修改（服务器与回传都改了）"})
                continue
            changes[col] = new_v
        if changes:
            plans.append({"rid": rid, "changes": changes})
        else:
            noop += 1
    if conflicts:
        # 冲突项展开为字段级审阅项：行版本不符/越范围/不在包内的行，其回传字段逐个成项
        # （基线值/服务器当前值/回传值三值并列），供逐字段决策；空值/0/未知按原值区分不合并。
        cfg = _srv().TABLES[p["tname"]]
        keyf = cfg.get("key")
        field_items = []
        for c0 in conflicts:
            rid0 = c0.get("rid")
            row0 = conn.execute("SELECT data FROM records WHERE id=? AND tname=?", (rid0, p["tname"])).fetchone()
            cur_data = json.loads(row0["data"]) if row0 else {}
            base_row = conn.execute("SELECT base_values FROM edit_package_rows WHERE package_id=? AND rid=?",
                                    (p["id"], rid0)).fetchone()
            base_vals = json.loads(base_row["base_values"]) if base_row else {}
            upd = next((u for u in updates if u.get("rid") == rid0), None)
            vals = {k2: v2 for k2, v2 in ((upd or {}).get("values") or {}).items()
                    if k2 in allowed_cols}
            if not vals:
                field_items.append({"rid": rid0, "col": None, "base": None, "server": None, "returned": None,
                                    "reason": c0.get("reason") or "不在包内（删行不代表删除记录）"})
                continue
            for col, returned in vals.items():
                base_v = base_vals.get(col)
                server_v = cur_data.get(col)
                same = _srv().same(server_v, returned)
                field_items.append({"rid": rid0, "col": col,
                                    "base": base_v, "server": server_v, "returned": returned,
                                    "same_as_server": same,
                                    "row_version": (conn.execute(
                                        "SELECT row_version FROM records WHERE id=? AND tname=?",
                                        (rid0, p["tname"])).fetchone() or [None])[0],
                                    "reason": c0.get("reason") or ("回传值与当前值一致（无需应用）" if same else None)})
        pending = {"updates": updates, "conflicts": conflicts, "field_items": field_items,
                   "forced": forced, "created_at": _srv().now(), "statuses": {}}
        conn.execute("UPDATE edit_packages SET pending_review=? WHERE pkg_id=?",
                     (json.dumps(pending, ensure_ascii=False), p["pkg_id"]))
        conn.commit()
        return ({"ok": False, "pending": True,
                 "conflicts": conflicts, "plans": plans, "forced": forced,
                 "field_items": len(field_items), "pending_updates": len(updates),
                 "note": "冲突已展开为字段级审阅项（基线/当前/回传三值并列）；"
                         "决策提交时会重新核验行版本、字段当前值、身份与权限；"
                         "未应用修改与未决冲突保存在包内可继续处理"}, 409)
    # 应用非冲突变化（走 _update_record 保持权限/校验/审计一致）
    applied = []
    batch_id = _srv()._new_batch("CP")
    for plan in plans:
        row = conn.execute("SELECT row_version FROM records WHERE id=? AND tname=?",
                           (plan["rid"], p["tname"])).fetchone()
        if not row:
            continue
        r = _srv()._update_record(user, p["tname"], plan["rid"],
                                  {"changes": plan["changes"], "base_version": row["row_version"]},
                                  batch_id=batch_id)
        if not r.get("noop"):
            applied.append(plan)
    summary = (f"协作编辑包 {p['pkg_id']} 回传：应用 {len(applied)} 行"
               + (f"，force 压过 {len(forced)} 处同字段冲突" if forced else "")
               + f"（创建者 {p['created_by']}，实际提交者 {user['username']}，协作者署名未认证）")
    log_extra = {
        "_summary": summary,
        "_pkg_id": p["pkg_id"], "_pkg_creator": p["created_by"], "_submitter": user["username"],
        "_coauthor_verified": False, "_forced": forced}
    _srv().log_edit(conn, user["username"], p["tname"], None, None, "update",
                    log_extra, batch_id=batch_id)
    conn.commit()
    conn.execute("UPDATE edit_packages SET status='merged' WHERE pkg_id=?", (p["pkg_id"],))
    conn.commit()
    return ({"ok": True, "applied": len(applied), "noop": noop, "forced": forced,
             "batch": batch_id, "pkg_id": p["pkg_id"],
             "audit": {"pkg_creator": p["created_by"], "submitter": user["username"],
                       "coauthor_verified": False}}, 200)


def register(app):
    @app.get("/api/collab/packages")
    def api_collab_list():
        """包列表：本人创建的；管理员可见全部。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        where, params = "", []
        if not user["admin"]:
            where, params = " WHERE p.created_by=? ", [user["username"]]
        rows = conn.execute(
            "SELECT p.pkg_id, p.created_by, p.tname, p.allowed_cols, p.status, p.created_at,"
            " (SELECT COUNT(*) FROM edit_package_rows r WHERE r.package_id=p.id) AS rows_n"
            f" FROM edit_packages p {where} ORDER BY p.id DESC LIMIT 200", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["allowed_cols"] = json.loads(d["allowed_cols"] or "[]")
            out.append(d)
        return jsonify({"packages": out})

    @app.post("/api/collab/resolve")
    def api_collab_resolve():
        """标本号列表 → 稳定 rid（创建包用）；未命中原样返回 missing，绝不静默丢弃。"""
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        nums = [str(x).strip() for x in (body.get("numbers") or []) if str(x).strip()]
        if not nums:
            abort(400, "numbers 必填（标本号列表）")
        if len(nums) > 5000:
            abort(400, "一次最多解析 5000 个编号")
        conn = _srv().get_db()
        nm = _num_map(conn, "specimens")
        rids, missing = [], []
        seen = set()
        for n in nums:
            if n in seen:
                continue
            seen.add(n)
            if n in nm:
                rids.append(nm[n])
            else:
                missing.append(n)
        return jsonify({"ok": True, "rids": rids, "missing": missing,
                        "note": "rid 为服务端稳定记录标识；建议下载工作簿发给协作者而不是自制文件"})

    @app.post("/api/collab/packages")
    def api_collab_create():
        """创建协作编辑包：绑定表+列白名单+行集合（稳定标识+基础值+基础 row_version）。"""
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        rids = body.get("rids") or []
        if not rids:
            abort(400, "rids 必填（选择行）")
        if len(rids) > 5000:
            abort(400, "一个包最多 5000 行（更大的批量请拆包）")
        allowed_cols = body.get("allowed_cols") or ["Notes", "DetBy", "DetDate"]
        if _KEY in allowed_cols:
            abort(400, f"{_KEY} 是行身份列，不可放入可编辑范围")
        conn = _srv().get_db()
        import secrets
        pkg_id = "PKG" + secrets.token_hex(8)
        now = _srv().now()
        cur = conn.execute(
            "INSERT INTO edit_packages(pkg_id,created_by,tname,allowed_cols,status,created_at) "
            "VALUES(?,?,?,?, 'active', ?)",
            (pkg_id, user["username"], "specimens", json.dumps(allowed_cols), now))
        pid = cur.lastrowid
        kept = 0
        for rid in rids:
            row = conn.execute("SELECT data, row_version FROM records WHERE id=? AND tname='specimens' AND deleted=0",
                               (rid,)).fetchone()
            if not row:
                continue
            d = json.loads(row["data"])
            base = {c: d.get(c) for c in allowed_cols}
            conn.execute(
                "INSERT INTO edit_package_rows(package_id,tname,rid,base_values,base_row_version,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (pid, "specimens", rid, json.dumps(base, ensure_ascii=False), row["row_version"], now))
            kept += 1
        conn.commit()
        _srv().log_edit(conn, user["username"], "specimens", None, None, "create",
                        {"_summary": f"创建协作编辑包 {pkg_id}（{kept} 行，可编辑列：{'、'.join(allowed_cols)}）",
                         "_pkg_id": pkg_id})
        conn.commit()
        return jsonify({"ok": True, "pkg_id": pkg_id, "rows": kept,
                        "allowed_cols": allowed_cols,
                        "note": "权威基线存服务器；Excel 文件只带包 ID+数据（不含令牌/身份/版本）"})

    @app.get("/api/collab/packages/<pkg_id>")
    def api_collab_get(pkg_id):
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        rows = []
        for r in conn.execute("SELECT * FROM edit_package_rows WHERE package_id=?", (p["id"],)):
            d = dict(r)
            d["base_values"] = json.loads(d["base_values"])
            rows.append(d)
        return jsonify({"package": dict(p), "rows": rows,
                        "note": "回传 Excel 只需带 包 ID+编号列+可编辑列；身份/版本由服务器核对"})

    @app.get("/api/collab/packages/<pkg_id>/workbook")
    def api_collab_workbook(pkg_id):
        """离线编辑工作簿：包 ID+编号列+可编辑列的当前值（不带令牌/身份/版本）。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        allowed = json.loads(p["allowed_cols"] or "[]")
        import io as _io
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "package"
        ws.append([f"协作编辑包 {p['pkg_id']}"])
        ws["A1"].font = Font(bold=True, size=12)
        ws.append(["说明：直接在列内修改后整簿回传（xlsx/csv/tsv）；不要改「标本号」列与表头；"
                   "删行不代表删除记录，新增请走导入流程。"])
        ws.append([])
        ws.append([_KEY] + allowed)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        n = 0
        for r in conn.execute("SELECT rid FROM edit_package_rows WHERE package_id=? ORDER BY rid", (p["id"],)):
            rec = conn.execute("SELECT data FROM records WHERE id=? AND tname=?",
                               (r["rid"], p["tname"])).fetchone()
            if not rec:
                continue
            d = json.loads(rec["data"])
            ws.append([d.get(_KEY)] + [d.get(c) for c in allowed])
            n += 1
        buf = _io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        _srv().log_edit(conn, user["username"], p["tname"], None, None, "export",
                        {"_summary": f"下载协作编辑包 {p['pkg_id']} 工作簿（{n} 行）",
                         "_pkg_id": p["pkg_id"]})
        conn.commit()
        return send_file(buf, as_attachment=True,
                         download_name=f"协作包_{p['pkg_id']}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def _parse_return_file(fobj, fname, allowed, nm):
        """回传文件 → (updates, skipped, missing_cols)。xlsx/csv/tsv；表头须含编号列。"""
        rows = []
        low = (fname or "").lower()
        if low.endswith((".xlsx", ".xlsm")):
            import openpyxl
            wb = openpyxl.load_workbook(fobj, data_only=True, read_only=True)
            ws = wb.active
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            wb.close()
        else:
            import csv as _csv
            import io as _io
            raw = fobj.read().decode("utf-8-sig", "replace")
            if low.endswith(".tsv"):
                sep = "\t"
            elif low.endswith(".csv"):
                sep = ","
            else:
                first = raw.splitlines()[0] if raw.splitlines() else ""
                sep = "\t" if "\t" in first else ","
            rows = list(_csv.reader(_io.StringIO(raw), delimiter=sep))
        hdr_i = None
        for i, r in enumerate(rows[:200]):
            vals = [str(_norm_cell(x)) for x in r if _norm_cell(x) is not None]
            if _KEY in vals:
                hdr_i = i
                break
        if hdr_i is None:
            abort(400, f"未找到表头行（需含「{_KEY}」列；请勿改动工作簿表头）")
        hdr = [str(_norm_cell(x) or "") for x in rows[hdr_i]]
        ki = hdr.index(_KEY)
        col_idx = {c: hdr.index(c) for c in allowed if c in hdr}
        missing_cols = [c for c in allowed if c not in hdr]
        updates, skipped = [], []
        for r in rows[hdr_i + 1:]:
            if not r or all(_norm_cell(x) is None for x in r):
                continue
            num = _norm_cell(r[ki]) if ki < len(r) else None
            if num is None:
                continue
            num = str(num).strip()
            rid = nm.get(num)
            if rid is None:
                skipped.append({"number": num, "reason": "编号在库中不存在或已删除（删行不代表删除记录；新增需导入流程）"})
                continue
            vals = {c: (_norm_cell(r[i]) if i < len(r) else None) for c, i in col_idx.items()}
            updates.append({"rid": rid, "values": vals})
        return updates, skipped, missing_cols

    @app.post("/api/collab/packages/<pkg_id>/receive")
    def api_collab_receive(pkg_id):
        """接收回传（JSON updates）：三方比较 → 应用/冲突 409。同包重复回传幂等阻止。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        body = request.get_json(silent=True) or {}
        payload, status = _merge_receive(user, p, body.get("updates") or [],
                                         force=bool(body.get("force")))
        return jsonify(payload), status

    @app.post("/api/collab/packages/<pkg_id>/receive-file")
    def api_collab_receive_file(pkg_id):
        """接收回传文件（xlsx/csv/tsv）：解析为 updates 后走同一三方合并。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        if p["status"] != "active":
            abort(409, f"包状态 {p['status']}（已过期/已收回/已合并），不能自动合并")
        _srv().require_writer(user, p["tname"])
        f = request.files.get("file")
        if not f:
            abort(400, "缺少 file（回传的 xlsx/csv/tsv）")
        allowed = json.loads(p["allowed_cols"] or "[]")
        updates, skipped, missing_cols = _parse_return_file(
            f, f.filename, allowed, _num_map(conn, p["tname"]))
        payload, status = _merge_receive(user, p, updates,
                                         force=(request.form.get("force") == "1"))
        if status == 200:
            payload["parsed_rows"] = len(updates)
            payload["skipped_numbers"] = skipped[:20]
            payload["skipped_n"] = len(skipped)
            payload["missing_cols_in_file"] = missing_cols
        return jsonify(payload), status

    @app.get("/api/collab/packages/<pkg_id>/pending")
    def api_collab_pending(pkg_id):
        """待处理审阅读取：逐条冲突附 业务编号/中文字段名/基线/服务器当前/回传值/状态。
        状态 undecided/applied/kept 记录在 pending_review 内，随决策更新。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        if not p["pending_review"]:
            return jsonify({"pending": None})
        pr = json.loads(p["pending_review"])
        cfg = _srv().TABLES[p["tname"]]
        keyf = cfg.get("key")
        labels = {c["name"]: (c.get("label") or c["name"]) for c in cfg["columns"]}
        def rowinfo(rid):
            r = conn.execute("SELECT data, deleted FROM records WHERE id=? AND tname=?", (rid, p["tname"])).fetchone()
            if not r:
                return None
            d = json.loads(r["data"])
            return {"key": str(d.get(keyf) or "#" + str(rid)), "deleted": bool(r["deleted"])}
        def disp(v):
            if v is None:
                return {"v": None, "disp": "（空）"}
            if v == 0:
                return {"v": 0, "disp": "0（明确零，非未知）"}
            return {"v": v, "disp": str(v)}
        items = []
        for f in pr.get("field_items") or []:
            info = rowinfo(f.get("rid"))
            sk = str(f.get("rid")) + "|" + str(f.get("col"))
            items.append({**f, "key": info["key"] if info else "(已删除)",
                          "col_label": labels.get(f.get("col") or "", f.get("col") or ""),
                          "base_disp": disp(f.get("base")), "server_disp": disp(f.get("server")),
                          "returned_disp": disp(f.get("returned")),
                          "status": pr.get("statuses", {}).get(sk, "undecided")})
        return jsonify({"pending": True, "field_items": items,
                        "row_reasons": [c for c in pr.get("conflicts", []) if not c.get("col")],
                        "pending_updates": len(pr.get("updates") or []),
                        "statuses": pr.get("statuses", {})})

    @app.post("/api/collab/packages/<pkg_id>/resolve")
    def api_collab_resolve_conflicts(pkg_id):
        """提交冲突决策：逐项重检（行版本/字段范围/删除态/审阅期间再修改），
        通过项原子应用（独立审计批次）；保留项记账；失效项回冲突并带最新值。
        部分合并：已应用与待处理分开记账，包保持 active 保留后续处理；全部解决才置 merged。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        p = _load_pkg(conn, pkg_id)
        if p["status"] != "active":
            abort(409, f"包状态 {p['status']}，不能处理决策")
        _srv().require_writer(user, p["tname"])
        body = request.get_json(silent=True) or {}
        decisions = body.get("decisions") or []
        if not decisions:
            abort(400, "decisions 必填（[{rid, col, decision: accept|keep}]）")
        pr = json.loads(p["pending_review"]) if p["pending_review"] else None
        if not pr:
            abort(404, "该包没有待处理审阅")
        batch_id = _srv()._new_batch("CR")
        field_items = pr.get("field_items") or []
        statuses = pr.get("statuses", {})
        allowed_cols = json.loads(p["allowed_cols"] or "[]")
        applied, kept, reconflicts, unknown = [], [], [], []
        # 按记录分组：同记录多字段决策合并为一次条件更新（前一字段应用不会使同记录其余字段失效）
        groups = {}
        order = []
        for d0 in decisions:
            rid, col, dec = int(d0.get("rid") or 0), str(d0.get("col") or ""), str(d0.get("decision") or "")
            if rid not in groups:
                groups[rid] = []
                order.append(rid)
            groups[rid].append((col, dec))
        for rid in order:
            decs = groups[rid]
            # 逐字段重检（基于决策时的项与当前记录快照）
            row = conn.execute("SELECT data, deleted, row_version FROM records WHERE id=? AND tname=?",
                               (rid, p["tname"])).fetchone()
            if not row or row["deleted"]:
                for col, _ in decs:
                    reconflicts.append({"rid": rid, "col": col, "reason": "记录已不存在或已删除"})
                continue
            cur_data = json.loads(row["data"])
            changes, kept_local = {}, []
            failed = None
            for col, dec in decs:
                sk = f"{rid}|{col}"
                item = next((f for f in field_items
                             if f.get("rid") == rid and f.get("col") == col
                             and statuses.get(sk, "undecided") == "undecided"), None)
                if not item:
                    unknown.append({"rid": rid, "col": col, "reason": "决策项不存在或已处理"})
                    continue
                if col not in allowed_cols:
                    reconflicts.append({"rid": rid, "col": col, "reason": "列不在允许编辑范围"})
                    continue
                # 字段级重检：行版本与字段当前值都必须与审阅时一致
                if row["row_version"] != item.get("row_version"):
                    failed = failed or {"rid": rid, "col": col,
                                        "reason": "审阅期间记录又被修改（行版本不符）",
                                        "server": cur_data.get(col), "returned": item.get("returned")}
                    break
                if not _srv().same(cur_data.get(col), item.get("server")):
                    failed = failed or {"rid": rid, "col": col,
                                        "reason": "审阅期间该字段又被修改",
                                        "server": cur_data.get(col), "returned": item.get("returned")}
                    break
                if dec == "keep":
                    statuses[sk] = "kept"
                    kept_local.append({"rid": rid, "col": col, "value": item.get("server")})
                    continue
                changes[col] = item.get("returned")
            if failed:
                reconflicts.append(failed)
                continue
            if changes:
                r = _srv()._update_record(user, p["tname"], rid,
                                          {"changes": changes, "base_version": row["row_version"]},
                                          batch_id=batch_id)
                for col, v in changes.items():
                    statuses[f"{rid}|{col}"] = "applied"
                    applied.append({"rid": rid, "col": col, "value": v})
            kept.extend(kept_local)
            for kk in kept_local:
                statuses[f"{rid}|{kk['col']}"] = "kept"
        # 未决策项保持在 pending；全部字段项已决策且无非冲突待应用 → merged
        undecided = [f for f in field_items
                     if statuses.get(str(f.get("rid")) + "|" + str(f.get("col")), "undecided") == "undecided"]
        pr["statuses"] = statuses
        conn.execute("UPDATE edit_packages SET pending_review=? WHERE pkg_id=?",
                     (json.dumps(pr, ensure_ascii=False), p["pkg_id"]))
        log_extra = {"_summary": f"协作包 {p['pkg_id']} 冲突决策：应用 {len(applied)}、保留 {len(kept)}、"
                                 f"失效回冲突 {len(reconflicts)}、未决 {len(undecided)}",
                     "_pkg_id": p["pkg_id"]}
        _srv().log_edit(conn, user["username"], p["tname"], None, None, "update", log_extra)
        conn.commit()
        if not undecided and not pr.get("updates"):
            conn.execute("UPDATE edit_packages SET status='merged' WHERE pkg_id=?", (p["pkg_id"],))
            conn.commit()
        return jsonify({"ok": True, "applied": applied, "kept": kept,
                        "reconflicts": reconflicts, "unknown": unknown,
                        "remaining": len(undecided), "merged": not undecided and not pr.get("updates"),
                        "note": "已应用项与保留项已分别记账；未决冲突与未应用修改保留在包内可继续处理"})

    @app.post("/api/collab/packages/<pkg_id>/revoke")
    def api_collab_revoke(pkg_id):
        user = _srv().current_user()
        if not user["admin"]:
            abort(403, "仅管理员可收回包")
        conn = _srv().get_db()
        conn.execute("UPDATE edit_packages SET status='revoked' WHERE pkg_id=?", (pkg_id,))
        conn.commit()
        return jsonify({"ok": True})
