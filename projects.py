# -*- coding: utf-8 -*-
"""项目工作集（v3.61）：成员以稳定记录标识引用主库（不复制第二套事实）。

两种工作集：
  fixed    固定成员：成员显式加入（从动态结果"纳入本次研究"生成明确版本）
  dynamic  动态查询：成员由 filters 动态计算；条件变化提示新增/退出候选，不自动改已确认范围

项目内备注/任务状态/展示顺序/排除原因 与主库字段分开（存 member 行，不写 records.data）。
移除成员不删主库记录；项目页改主库字段遵守主库权限并标注作用域。
分享工作集不增加对底层记录的访问权——计数按接收者权限重算。
"""
import json

from flask import abort, jsonify, request

import db as dbm
from tables_def import TABLES

WS_KINDS = ("fixed", "dynamic")
WS_MEMBER_STATUSES = ("active", "excluded")


def _srv():
    import server
    return server


def register(app):
    @app.get("/api/projects")
    def api_projects_list():
        user = _srv().current_user()
        conn = _srv().get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id DESC")]
        for r in rows:
            r["member_count"] = conn.execute(
                "SELECT COUNT(*) FROM project_members WHERE project_id=? AND status='active'",
                (r["id"],)).fetchone()[0]
        return jsonify({"projects": rows})

    @app.post("/api/projects")
    def api_project_create():
        user = _srv().current_user()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            abort(400, "项目名称必填")
        kind = body.get("kind") or "fixed"
        if kind not in WS_KINDS:
            abort(400, "kind 需为 fixed/dynamic")
        conn = _srv().get_db()
        cur = conn.execute(
            "INSERT INTO projects(name,kind,purpose,owner,filters,stage,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name, kind, str(body.get("purpose") or ""), user["username"],
             json.dumps(body.get("filters") or {}, ensure_ascii=False) if kind == "dynamic" else None,
             body.get("stage") or "筹备", user["username"], _srv().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})

    @app.get("/api/projects/<int:pid>")
    def api_project_detail(pid):
        user = _srv().current_user()
        conn = _srv().get_db()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "项目不存在")
        members = [dict(r) for r in conn.execute(
            "SELECT * FROM project_members WHERE project_id=? AND status='active' "
            "ORDER BY sort_order, id", (pid,))]
        vis = _srv().visible_tables(user)
        visible_members = []
        for m in members:
            if m["tname"] in vis:
                row = conn.execute("SELECT data FROM records WHERE id=? AND tname=?",
                                   (m["rid"], m["tname"])).fetchone()
                m["current_key"] = (json.loads(row["data"]).get(TABLES[m["tname"]].get("key"))
                                    if row else "(已删除)")
                visible_members.append(m)
        return jsonify({"project": dict(p),
                        "members": visible_members,
                        "restricted_member_count": len(members) - len(visible_members),
                        "note": "成员以稳定记录标识引用主库（不复制事实）；无权限成员不返回详情"})

    @app.post("/api/projects/<int:pid>/members")
    def api_project_add_member(pid):
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        tname = body.get("tname") or "specimens"
        rid = int(body.get("rid") or 0)
        conn = _srv().get_db()
        p = conn.execute("SELECT kind, filters FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "项目不存在")
        if not conn.execute("SELECT 1 FROM records WHERE id=? AND tname=? AND deleted=0",
                            (rid, tname)).fetchone():
            abort(404, "记录不存在")
        conn.execute(
            "INSERT INTO project_members(project_id,tname,rid,status,join_reason,added_by,added_at,"
            "remove_reason,removed_by,removed_at) VALUES(?,?,?,?,?,?,?,'','', '') "
            "ON CONFLICT(project_id,tname,rid) DO UPDATE SET status='active',"
            "join_reason=excluded.join_reason, added_by=excluded.added_by, added_at=excluded.added_at,"
            "remove_reason='', removed_by='', removed_at='', exclude_reason=''",
            (pid, tname, rid, "active", str(body.get("join_reason") or "纳入本次研究"),
             user["username"], _srv().now()))
        conn.commit()
        return jsonify({"ok": True})

    @app.delete("/api/projects/<int:pid>/members/<int:mid>")
    def api_project_remove_member(pid, mid):
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        conn = _srv().get_db()
        m = conn.execute("SELECT * FROM project_members WHERE id=? AND project_id=?",
                         (mid, pid)).fetchone()
        if not m:
            abort(404, "成员不存在")
        conn.execute(
            "UPDATE project_members SET status='excluded', remove_reason=?, removed_by=?, removed_at=? "
            "WHERE id=?",
            (str(body.get("remove_reason") or "排除出本次研究"),
             user["username"], _srv().now(), mid))
        conn.commit()
        # 移除成员不删主库记录
        return jsonify({"ok": True, "note": "主库记录未受影响"})

    @app.post("/api/projects/<int:pid>/from-dynamic")
    def api_project_freeze_dynamic(pid):
        """从动态结果"纳入本次研究"生成明确成员版本（固定成员快照）。"""
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        conn = _srv().get_db()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p or p["kind"] != "dynamic":
            abort(400, "仅动态工作集可固化成员")
        filters = json.loads(p["filters"] or "{}")
        eidx = _srv().linked_eidx("specimens") if "events" in _srv().visible_tables(user) else None
        from server import _apply_col_filters, display_row
        count = 0
        for r in _srv().load_rows("specimens"):
            if r["deleted"]:
                continue
            d, _ = display_row("specimens", r, eidx)
            if filters.get("q") and filters["q"].lower() not in json.dumps(d, ensure_ascii=False).lower():
                continue
            if filters.get("filters"):
                kept = {x["id"] for x in _apply_col_filters([{"id": r["id"], "data": d}], filters["filters"])}
                if r["id"] not in kept:
                    continue
            conn.execute(
                "INSERT OR IGNORE INTO project_members(project_id,tname,rid,status,join_reason,added_by,added_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, "specimens", r["id"], "active", "从动态结果纳入本次研究（固化成员版本）",
                 user["username"], _srv().now()))
            count += 1
        conn.commit()
        return jsonify({"ok": True, "frozen_members": count})

    @app.post("/api/projects/<int:pid>/member-note")
    def api_project_member_note(pid):
        """项目内备注/任务状态/展示顺序/排除原因（与主库字段分开，不写 records.data）。"""
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        mid = int(body.get("member_id") or 0)
        conn = _srv().get_db()
        m = conn.execute("SELECT * FROM project_members WHERE id=? AND project_id=?", (mid, pid)).fetchone()
        if not m:
            abort(404, "成员不存在")
        sets, args = [], []
        for k in ("project_note", "task_status", "sort_order", "exclude_reason"):
            if k in body:
                sets.append(f"{k}=?")
                args.append(str(body[k] or ""))
        if not sets:
            abort(400, "没有要修改的字段")
        conn.execute(f"UPDATE project_members SET {','.join(sets)} WHERE id=?", (*args, mid))
        conn.commit()
        # 明确：项目内备注不写主库
        return jsonify({"ok": True, "scope": "项目内（主库 records.data 未改动）"})

    @app.get("/api/projects/dynamic-count")
    def api_project_dynamic_count():
        """动态项目筛选的实时匹配数（与固化成员 from-dynamic 完全同一匹配语义）：
        q=整行显示内容子串；filters=查询模型列条件（in/contains/empty/notempty）。
        校验具体到列与条件，供可视化筛选器在控件旁显示。"""
        user = _srv().current_user()
        _srv().ensure_visible(user, "specimens")
        q = str(request.args.get("q") or "")
        raw = request.args.get("filters") or ""
        filters = None
        if raw:
            try:
                filters = json.loads(raw)
            except ValueError:
                abort(400, "filters 不是合法 JSON")
        if filters is not None and not isinstance(filters, dict):
            abort(400, "filters 需为 {列: 条件} 对象")
        valid_modes = ("in", "contains", "empty", "notempty")
        cols = {c["name"] for c in _srv().TABLES["specimens"]["columns"]}
        if filters:
            for col, f in filters.items():
                if not isinstance(f, dict):
                    abort(400, f"列「{col}」的条件需为对象")
                mode = f.get("mode")
                if mode not in valid_modes:
                    abort(400, f"列「{col}」的条件「{mode}」不受支持（可用：属于以下值 in / 包含文本 contains / 为空 empty / 不为空 notempty）")
                if col not in cols:
                    abort(400, f"列「{col}」不在 跳蛛: 河北大学标本信息 的字段清单中")
        eidx = _srv().linked_eidx("specimens") if "events" in _srv().visible_tables(user) else None
        from server import _apply_col_filters, display_row
        n = 0
        for r in _srv().load_rows("specimens"):
            if r["deleted"]:
                continue
            d, _ = display_row("specimens", r, eidx)
            if q and q.lower() not in json.dumps(d, ensure_ascii=False).lower():
                continue
            if filters:
                kept = {x["id"] for x in _apply_col_filters([{"id": r["id"], "data": d}], filters)}
                if r["id"] not in kept:
                    continue
            n += 1
        return jsonify({"ok": True, "count": n,
                        "note": "动态成员随数据变化：符合筛选的行实时计入，之后新增/修改也会改变成员（可随时固化成员版本）"})

    @app.get("/api/projects/<int:pid>/versions")
    def api_project_versions(pid):
        """项目冻结版本列表（导出向导「指定冻结版本」数据源；仅返回用户可见表）。"""
        user = _srv().current_user()
        conn = _srv().get_db()
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
            abort(404, "项目不存在")
        vis = _srv().visible_tables(user)
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tname, name, created_at, created_by, total FROM versions "
            "WHERE note LIKE ? ORDER BY id DESC", (f"项目工作集 {pid} 冻结%",))]
        return jsonify({"versions": [r for r in rows if r["tname"] in vis]})

    @app.post("/api/projects/<int:pid>/freeze")
    def api_project_freeze(pid):
        """项目冻结：只冻结【确认纳入的项目成员】（排除已移除/排除原因行），
        支持多表成员（specimens/mygalomorph/dali/swu）；每表生成独立受保护版本，
        并把采集地/分类的关联内容一并冻结（依赖数据本身而非仅摘要——摘要只用于校验）。"""
        user = _srv().current_user()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip() or f"项目冻结 {pid}"
        conn = _srv().get_db()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "项目不存在")
        members = [dict(r) for r in conn.execute(
            "SELECT tname, rid FROM project_members WHERE project_id=? AND status='active' "
            "AND IFNULL(exclude_reason,'')=''", (pid,))]
        if not members:
            abort(400, "项目没有确认纳入的成员（排除/移除的行不冻结）；请先纳入成员")
        by_table = {}
        for m in members:
            by_table.setdefault(m["tname"], []).append(m["rid"])
        # 采集地/分类依赖内容快照（可重建关联；deps 摘要只作校验）
        dep_content = {}
        for t in ("events", "taxonomy"):
            if t in _srv().TABLES:
                dep_content[t] = [{"id": r["id"], "deleted": r["deleted"], "data": r["data"]}
                                  for r in _srv().load_rows(t)]
        created = []
        for tname, rids in by_table.items():
            if tname not in _srv().TABLES:
                continue
            user_perm = user
            _srv().require_writer(user_perm, tname)
            id_set = set(rids)
            trows = [r for r in _srv().load_rows(tname)
                     if r["id"] in id_set and not r["deleted"]]
            snap = _srv()._snapshot_envelope(tname, [
                {"id": r["id"], "deleted": r["deleted"], "data": r["data"],
                 "row_version": r["row_version"]} for r in trows])
            snap["project_id"] = pid
            snap["member_rids"] = sorted(id_set)
            snap["dep_content"] = dep_content
            rv = _srv()._rules_versions()
            total = len(trows)
            cur = conn.execute(
                "INSERT INTO versions(tname,name,note,created_at,created_by,snapshot,total,"
                "category,description,protected,schema_ver,tables_rev,rules_ver,"
                "content_sha256,size_bytes,format,deps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tname, f"{name} · {_srv().TABLES[tname]['label']}",
                 f"项目工作集 {pid} 冻结（{total} 行项目成员；关联内容已冻结）",
                 _srv().now(), user["username"],
                 json.dumps(_srv()._pack_snapshot(snap), ensure_ascii=False), total,
                 "manual", f"项目工作集 {pid} 冻结", 1,
                 rv["schema"], _srv().TABLES_REV, rv.get("wsc_sync") or "",
                 __import__("hashlib").sha256(json.dumps(snap, ensure_ascii=False).encode()).hexdigest(),
                 len(json.dumps(snap, ensure_ascii=False)), "v3",
                 json.dumps(_srv()._version_deps(tname), ensure_ascii=False)))
            created.append({"version_id": cur.lastrowid, "tname": tname, "rows": total})
        if not created:
            abort(400, "没有可冻结的成员表")
        conn.commit()
        return jsonify({"ok": True, "versions": created,
                        "note": "冻结=项目确认纳入的成员（排除已移除）；关联内容已随版本冻结，"
                                "修改当前采集地不影响已冻结结果；依赖摘要仅作校验"})


