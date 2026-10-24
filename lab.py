# -*- coding: utf-8 -*-
"""实验板/孔/混池/条码 API（v3.58，最小关系模型；规格与阈值配置化）。

实测依据（2026-10-03，udi_i5/udi_i7 台账）：i5/i7 各 96 行、序列长度 8bp、
无非法碱基、UDI# 无重复。阈值/组合策略以 config.PLATE_SPECS 与
config.LAB_BARCODE_RULES 为准（来自实测与实验室试剂流程；修改需实验室确认）。
"""
import json
import re
import sqlite3

from flask import abort, jsonify, request

import db as dbm
from tables_def import TABLES

import importlib
_srv = None


def _srv_mod():
    global _srv
    if _srv is None:
        _srv = importlib.import_module("server")
    return _srv


def current_user(required=True):
    return _srv_mod().current_user(required)


def require_writer(tname):
    return _srv_mod().require_writer(_srv_mod().current_user(), tname)


def now():
    return _srv_mod().now()


def log_edit(*a, **kw):
    return _srv_mod().log_edit(*a, **kw)


PLATE_SPECS = {
    "96": {"rows": ["A", "B", "C", "D", "E", "F", "G", "H"], "cols": list(range(1, 13))},
    "384": {"rows": [chr(ord("A") + i) for i in range(16)], "cols": list(range(1, 25))},
}
# 条码规则（来自实测：8bp ACGTN；其他长度/组合策略需实验室确认后才可修改）
LAB_BARCODE_RULES = {
    "min_len": 8, "max_len": 8,
    "alphabet": "ACGTN",
    "require_distinct_within_pool": True,   # 同一有效混池内 i5/i7 组合不得重复
    "direction_i5": "read1", "direction_i7": "read2",
}
WELL_KINDS = ("sample", "control", "blank")


def _plate(pid):
    r = dbm.connect().execute("SELECT * FROM plate WHERE id=?", (pid,)).fetchone()
    if not r:
        abort(404, "板不存在")
    return dict(r)


def _check_well(spec, row, col):
    if row not in PLATE_SPECS[spec]["rows"] or col not in PLATE_SPECS[spec]["cols"]:
        abort(400, f"孔位 {row}{col} 超出板规格 {spec}（行 {PLATE_SPECS[spec]['rows'][0]}-{PLATE_SPECS[spec]['rows'][-1]}，列 1-{PLATE_SPECS[spec]['cols'][-1]}）")


def _validate_barcodes(udi5, udi7, same_pool=None):
    """条码校验：长度/碱基按实验室规则；「同池组合冲突」仅在加入指定池时检查
    （池作用域——跨池合理复用允许）。barcode_pair 登记不做池检查。"""
    rules = LAB_BARCODE_RULES
    for tag, seq in (("i5", udi5), ("i7", udi7)):
        if not seq:
            continue
        if len(seq) < rules["min_len"] or len(seq) > rules["max_len"]:
            abort(400, f"{tag} 序列长度 {len(seq)} 超出实验室规则 [{rules['min_len']},{rules['max_len']}]bp")
        if re.search(f"[^{rules['alphabet'].lower()}{rules['alphabet']}]", seq):
            abort(400, f"{tag} 序列含非法碱基（允许 {rules['alphabet']}）")
    if rules.get("require_distinct_within_pool") and udi5 and udi7 and same_pool:
        dup = dbm.connect().execute(
            "SELECT id FROM pool_member WHERE pool_id=? AND udi5=? AND udi7=?",
            (same_pool, udi5, udi7)).fetchone()
        if dup:
            abort(409, "该 i5+i7 组合已存在于同一混池中（同池组合冲突）；跨池复用不受限")


def register(app):
    @app.get("/api/lab/batches")
    def api_batches_list():
        """批次列表（新建/送测状态的台账视图）。"""
        current_user()
        rows = [dict(r) for r in dbm.connect().execute(
            "SELECT * FROM experiment_batch ORDER BY id DESC LIMIT 100")]
        return jsonify({"batches": rows})

    @app.get("/api/lab/pools")
    def api_pools_list():
        """混池列表（含成员：UDI 组合/板孔）。"""
        current_user()
        conn = dbm.connect()
        pools = [dict(r) for r in conn.execute("SELECT * FROM pool ORDER BY id DESC LIMIT 100")]
        for p in pools:
            p["members"] = [dict(m) for m in conn.execute(
                "SELECT * FROM pool_member WHERE pool_id=? ORDER BY id", (p["id"],))]
        return jsonify({"pools": pools})

    @app.get("/api/lab/barcode-pairs")
    def api_barcode_pairs_list():
        """条码对列表（源序列/方向/来源；导出转换按配置绑定，不覆盖源序列）。"""
        current_user()
        rows = [dict(r) for r in dbm.connect().execute(
            "SELECT * FROM barcode_pair ORDER BY id DESC LIMIT 100")]
        return jsonify({"pairs": rows})

    @app.get("/api/lab/qc")
    def api_qc_list():
        """定量测量记录（多次测量可保存；采用哪次需给理由）。"""
        current_user()
        rows = [dict(r) for r in dbm.connect().execute(
            "SELECT * FROM qc_measurement ORDER BY id DESC LIMIT 100")]
        return jsonify({"records": rows})

    @app.get("/api/lab/plates")
    def api_plates():
        current_user()
        rows = dbm.connect().execute("SELECT * FROM plate ORDER BY id DESC").fetchall()
        return jsonify({"plates": [dict(r) for r in rows]})

    @app.post("/api/lab/plates")
    def api_plate_create():
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").strip()
        spec = str(body.get("spec") or "96")
        if not code:
            abort(400, "code 必填")
        if spec not in PLATE_SPECS:
            abort(400, "spec 需为 " + "/".join(PLATE_SPECS))
        conn = dbm.connect()
        try:
            cur = conn.execute("INSERT INTO plate(code,spec,batch_id,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                               (code, spec, body.get("batch_id"), body.get("note"),
                                user["username"], _srv_mod().now()))
            conn.commit()
            return jsonify({"ok": True, "id": cur.lastrowid})
        except sqlite3.IntegrityError:
            conn.rollback()
            abort(409, "板编号已存在")

    @app.get("/api/lab/plates/<int:pid>/wells")
    def api_plate_wells(pid):
        user = _srv_mod().current_user()
        p = _plate(pid)
        conn = dbm.connect()
        wells = [dict(r) for r in conn.execute(
            "SELECT * FROM plate_well WHERE plate_id=? ORDER BY row, col", (pid,))]
        spec = PLATE_SPECS[p["spec"]]
        occupied = {(w["row"], w["col"]): w for w in wells}
        grid = [{"row": r, "col": c,
                 "state": ("occupied:" + occupied[(r, c)]["kind"]) if (r, c) in occupied else "empty",
                 "well": occupied.get((r, c))}
                for r in spec["rows"] for c in spec["cols"]]
        return jsonify({"plate": p, "wells": wells, "grid": grid,
                        "legend": "empty=空孔 sample=样本 control=对照 blank=空白；"
                                  "状态文字+符号（●样本 ◆对照 ○空白 ！异常）与颜色共同表达"})

    @app.post("/api/lab/plates/<int:pid>/wells/preview")
    def api_plate_wells_preview(pid):
        """批量占孔/移动预览（dry-run）：显示将写入/冲突/覆盖的孔。"""
        user = _srv_mod().current_user()
        p = _plate(pid)
        body = request.get_json(silent=True) or {}
        conn = dbm.connect()
        plan = []
        for w in body.get("wells") or []:
            row, col = w.get("row"), w.get("col")
            _check_well(p["spec"], row, col)
            ex = conn.execute("SELECT * FROM plate_well WHERE plate_id=? AND row=? AND col=?",
                              (pid, row, col)).fetchone()
            plan.append({"row": row, "col": col, "kind": w.get("kind") or "sample",
                         "obj_table": w.get("obj_table"), "obj_rid": w.get("obj_rid"),
                         "action": "overwrite" if ex else "assign",
                         "existing_kind": ex["kind"] if ex else None})
        conflicts = [x for x in plan if x["action"] == "overwrite"]
        return jsonify({"plan": plan, "assign": len(plan) - len(conflicts),
                        "overwrite": len(conflicts),
                        "note": "预览不写库；确认后调用 /wells/commit 事务提交"})

    @app.post("/api/lab/plates/<int:pid>/wells/commit")
    def api_plate_wells_commit(pid):
        """批量占孔/移动事务提交：同板同孔重复占用 409；对象类型清楚记录。"""
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        p = _plate(pid)
        conn = dbm.connect()
        n = 0
        try:
            for w in body.get("wells") or []:
                row, col = w.get("row"), w.get("col")
                _check_well(p["spec"], row, col)
                kind = w.get("kind") or "sample"
                if kind not in WELL_KINDS:
                    abort(400, "kind 需为 " + "/".join(WELL_KINDS))
                ex = conn.execute("SELECT id FROM plate_well WHERE plate_id=? AND row=? AND col=?",
                                  (pid, row, col)).fetchone()
                if ex and not body.get("allow_overwrite"):
                    abort(409, f"孔 {row}{col} 已被占用（预览后发生变化），需 allow_overwrite 显式覆盖")
                if ex:
                    conn.execute("UPDATE plate_well SET kind=?, obj_table=?, obj_rid=?, label=?, status=?, note=? "
                                 "WHERE id=?", (kind, w.get("obj_table"), w.get("obj_rid"),
                                                w.get("label"), w.get("status"), w.get("note"), ex["id"]))
                else:
                    conn.execute("INSERT INTO plate_well(plate_id,row,col,kind,obj_table,obj_rid,label,status,note,"
                                 "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (pid, row, col, kind, w.get("obj_table"), w.get("obj_rid"),
                                  w.get("label"), w.get("status"), w.get("note"),
                                  user["username"], _srv_mod().now()))
                n += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return jsonify({"ok": True, "written": n})

    @app.post("/api/lab/pools")
    def api_pool_create():
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").strip()
        if not code:
            abort(400, "code 必填")
        conn = dbm.connect()
        try:
            cur = conn.execute("INSERT INTO pool(code,note,created_by,created_at) VALUES(?,?,?,?)",
                               (code, body.get("note"), user["username"], _srv_mod().now()))
            conn.commit()
            return jsonify({"ok": True, "id": cur.lastrowid})
        except sqlite3.IntegrityError:
            conn.rollback()
            abort(409, "混池编号已存在")

    @app.post("/api/lab/pools/<int:pid>/members")
    def api_pool_add_member(pid):
        """加入混池成员：同池 UDI 组合冲突/长度/碱基按实验室规则校验。"""
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        u5, u7 = str(body.get("udi5") or "").strip().upper(), str(body.get("udi7") or "").strip().upper()
        _validate_barcodes(u5, u7, same_pool=pid)
        conn = dbm.connect()
        cur = conn.execute("INSERT INTO pool_member(pool_id,udi5,udi7,plate_id,well_id,created_by,created_at) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (pid, u5 or None, u7 or None, body.get("plate_id"), body.get("well_id"),
                            user["username"], _srv_mod().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})

    @app.post("/api/lab/barcode-pairs")
    def api_barcode_pair_create():
        """i5/i7 保存原序列、方向、来源；不覆盖源序列（导出转换只在导出时按配置绑定）。"""
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        # 源序列照录（保留原始大小写，不覆盖）；校验用大写副本
        i5 = str(body.get("i5_seq_raw") or "").strip()
        i7 = str(body.get("i7_seq_raw") or "").strip()
        _validate_barcodes(i5.upper(), i7.upper())
        i5d = body.get("i5_dir") or LAB_BARCODE_RULES["direction_i5"]
        i7d = body.get("i7_dir") or LAB_BARCODE_RULES["direction_i7"]
        conn = dbm.connect()
        cur = conn.execute("INSERT INTO barcode_pair(udi5,udi7,i5_seq_raw,i7_seq_raw,i5_dir,i7_dir,source,"
                           "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (body.get("udi5"), body.get("udi7"), i5, i7, i5d, i7d,
                            body.get("source"), user["username"], _srv_mod().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid,
                        "export_binding": "导出转换需绑定 config.LAB_EXPORT_CONVERT 中的仪器/软件/试剂配置；源序列不覆盖"})

    @app.post("/api/lab/qc")
    def api_qc_add():
        """定量测量：多次测量可保存；单位不明不自动换算；采用哪次需给理由。"""
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        if not body.get("obj_table") or not body.get("value"):
            abort(400, "obj_table/value 必填")
        conn = dbm.connect()
        cur = conn.execute("INSERT INTO qc_measurement(obj_table,obj_rid,value,unit,method,instrument,"
                           "adopted,adopt_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (body["obj_table"], body.get("obj_rid"), str(body["value"]),
                            body.get("unit"), body.get("method"), body.get("instrument"),
                            1 if body.get("adopted") else 0, body.get("adopt_reason"),
                            user["username"], _srv_mod().now()))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})

    @app.post("/api/lab/batches")
    def api_batch_create():
        user = _srv_mod().current_user()
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").strip()
        if not code:
            abort(400, "code 必填")
        conn = dbm.connect()
        try:
            cur = conn.execute("INSERT INTO experiment_batch(code,note,created_by,created_at) VALUES(?,?,?,?)",
                               (code, body.get("note"), user["username"], _srv_mod().now()))
            conn.commit()
            return jsonify({"ok": True, "id": cur.lastrowid})
        except sqlite3.IntegrityError:
            conn.rollback()
            abort(409, "批次编号已存在")

    @app.post("/api/lab/batches/<int:bid>/send")
    def api_batch_send(bid):
        """送测：绑定批次版本（sent_version 递增）；已发送后更改保留修订（新版本行）。"""
        user = _srv_mod().current_user()
        conn = dbm.connect()
        b = conn.execute("SELECT * FROM experiment_batch WHERE id=?", (bid,)).fetchone()
        if not b:
            abort(404, "批次不存在")
        ver = conn.execute("SELECT sent_version FROM experiment_batch WHERE id=?", (bid,)).fetchone()[0] + 1
        conn.execute("UPDATE experiment_batch SET sent_at=?, sent_version=? WHERE id=?",
                     (now(), ver, bid))
        _srv_mod().log_edit(_srv_mod().conn_get() if False else conn, user["username"], "uce", None, None, "update",
                 {"_summary": f"批次 {b['code']} 标记已发送（送测版本 v{ver}）；此后更改保留修订记录"})
        conn.commit()
        return jsonify({"ok": True, "sent_version": ver})
