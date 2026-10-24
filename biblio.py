# -*- coding: utf-8 -*-
"""文献引用与材料引证 API（v3.59）。不自动抓取全文；无 DOI 老文献不虚构标识；
原文引用与当前接受名并列；双向入口（文献→材料 / 标本→被引文献）。"""
import json

from flask import abort, jsonify, request

import db as dbm
from tables_def import TABLES

_srv_mod = None


def _srv():
    global _srv_mod
    if _srv_mod is None:
        import server
        _srv_mod = server
    return _srv_mod


def _visible(user):
    from server import visible_tables
    return visible_tables(user)


def register(app):
    @app.get("/api/refs-bib")
    def api_bib_list():
        user = _srv().current_user()
        conn = _srv().get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM bibliographic_reference ORDER BY id DESC")]
        for r in rows:
            r["citations"] = conn.execute(
                "SELECT COUNT(*) FROM material_citation WHERE ref_id=?", (r["id"],)).fetchone()[0]
        return jsonify({"references": rows})

    @app.post("/api/refs-bib")
    def api_bib_create():
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        body = request.get_json(silent=True) or {}
        title = str(body.get("title") or "").strip()
        if not title:
            abort(400, "题名必填（无 DOI 老文献保留完整书目信息，不虚构标识）")
        conn = _srv().get_db()
        cur = conn.execute(
            "INSERT INTO bibliographic_reference(stable_id,doi,title,authors,year,pages,plates,"
            "journal,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(body.get("stable_id") or "").strip() or None,
             str(body.get("doi") or "").strip() or None,
             title, str(body.get("authors") or ""), str(body.get("year") or ""),
             str(body.get("pages") or ""), str(body.get("plates") or ""),
             str(body.get("journal") or ""), str(body.get("note") or ""),
             user["username"], _srv().now()))
        conn.commit()
        _srv().invalidate("specimens")
        return jsonify({"ok": True, "id": cur.lastrowid})

    @app.get("/api/refs-bib/<int:bid>/materials")
    def api_bib_materials(bid):
        """从文献查看材料：引证标本（含原文材料编号/原文引用 + 当前接受名并列）。"""
        user = _srv().current_user()
        _srv().ensure_visible(user, "specimens")
        conn = _srv().get_db()
        ref = conn.execute("SELECT * FROM bibliographic_reference WHERE id=?", (bid,)).fetchone()
        if not ref:
            abort(404, "文献不存在")
        items = []
        for r in conn.execute("SELECT * FROM material_citation WHERE ref_id=? ORDER BY id", (bid,)):
            d = dict(r)
            row = conn.execute("SELECT data FROM records WHERE id=? AND tname=?",
                               (r["rid"], r["tname"])).fetchone()
            if row:
                d2 = json.loads(row["data"])
                d["current_key"] = d2.get("MHBU #") or d2.get("Tempt #")
                d["current_accepted"] = d2.get("FinalSpeciesName") or d2.get("TemporarySpeciesName")
            else:
                d["current_key"] = "(已删除)"
                d["current_accepted"] = None
            items.append(d)
        return jsonify({"reference": dict(ref), "materials": items,
                        "note": "原文引用与当前接受名并列保留；当前值为实时解析"})

    @app.get("/api/rows/<tname>/<int:rid>/citations")
    def api_row_citations(tname, rid):
        """从标本查看被哪些文献引用（双向入口）。"""
        user = _srv().current_user()
        _srv().ensure_visible(user, tname)
        conn = _srv().get_db()
        items = []
        for r in conn.execute(
                "SELECT mc.*, br.title, br.authors, br.year, br.doi, br.stable_id "
                "FROM material_citation mc JOIN bibliographic_reference br ON br.id=mc.ref_id "
                "WHERE mc.tname=? AND mc.rid=? ORDER BY mc.id DESC", (tname, rid)):
            items.append(dict(r))
        return jsonify({"citations": items})

    @app.post("/api/refs-bib/<int:bid>/cite")
    def api_bib_cite(bid):
        """登记材料引证：保存原文材料编号/名称、当前行、冻结时点（标本版本+采集摘要）。"""
        user = _srv().current_user()
        _srv().require_writer(user, "specimens")
        conn = _srv().get_db()
        if not conn.execute("SELECT 1 FROM bibliographic_reference WHERE id=?", (bid,)).fetchone():
            abort(404, "文献不存在")
        body = request.get_json(silent=True) or {}
        rid = int(body.get("rid") or 0)
        tname = body.get("tname") or "specimens"
        row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
        if not row:
            abort(404, "标本不存在")
        # 冻结时点：标本当前版本 + 采集信息摘要
        fv = row["row_version"]
        evnum = str((json.loads(row["data"]) or {}).get("CollectionEventNumber") or "")
        import hashlib
        ed = ""
        if evnum:
            h = hashlib.sha256()
            for e in conn.execute("SELECT data FROM records WHERE tname='events' AND deleted=0"):
                h.update(e[0].encode())
            ed = h.hexdigest()[:16]
        try:
            cur = conn.execute(
                "INSERT INTO material_citation(ref_id,tname,rid,orig_text,orig_number,"
                "evidence_att,frozen_spec_version,frozen_event_digest,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (bid, tname, rid, str(body.get("orig_text") or ""),
                 str(body.get("orig_number") or ""), body.get("evidence_att"),
                 fv, ed or None, user["username"], _srv().now()))
            conn.commit()
            _srv().invalidate(tname)
            return jsonify({"ok": True, "id": cur.lastrowid, "frozen_version": fv})
        except Exception as e:
            if "UNIQUE" in str(e):
                abort(409, "该文献已引用此标本")
            raise

    @app.post("/api/rows/<tname>/<int:rid>/type-status")
    @app.post("/api/rows/<tname>/<int:rid>/type-status")
    def api_type_status(tname, rid):
        """模式标本身份：依据/确认人/确认时间/状态来源。读改写模式确保可靠落库。"""
        user = _srv().current_user()
        _srv().require_writer(user, tname)
        body = request.get_json(silent=True) or {}
        allowed = ("type_basis", "type_confirmed_by", "type_confirmed_at", "type_status_source")
        conn = _srv().get_db()
        r0 = conn.execute("SELECT data, row_version FROM records WHERE id=? AND tname=? AND deleted=0",
                          (rid, tname)).fetchone()
        if not r0:
            abort(404, "记录不存在")
        d = json.loads(r0["data"])
        changed = {}
        for k in allowed:
            if k in body:
                nv = str(body[k] or "")
                if str(d.get(k) or "") != nv:
                    d[k] = nv
                    changed[k] = nv
        if not changed:
            return jsonify({"ok": True, "noop": True})
        cur = conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
            "WHERE id=? AND tname=? AND row_version=?",
            (json.dumps(d, ensure_ascii=False), _srv().now(), user["username"],
             rid, tname, r0["row_version"]))
        if cur.rowcount != 1:
            abort(409, "并发修改")
        _srv().log_edit(conn, user["username"], tname, rid, None, "update",
                        {"_summary": "模式标本身份更新",
                         **{k: [None, changed[k]] for k in changed}},
                        batch_id=_srv()._new_batch("T"))
        conn.commit()
        _srv().invalidate(tname)
        return jsonify({"ok": True, "changed": changed})