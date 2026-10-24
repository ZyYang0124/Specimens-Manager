# -*- coding: utf-8 -*-
"""标本信息管理系统 —— Flask 后端

功能：
  * 账号登录（首个注册用户为管理员），所有操作绑定账号
  * 三张数据表：specimens 标本信息 / events 采集地信息 / taxonomy 分类信息
  * 标本表的关联列读取时按 CollectionEventNumber 实时从采集地表取值（替代 VLOOKUP）
  * 所有增删改写入 edits 变更日志（谁、何时、改了什么、旧值->新值）
  * 版本快照：可创建、预览、恢复、删除；任何版本/当前数据均可导出 xlsx
  * Excel 导入（合并/替换两种模式，导入前自动建快照）
  * 管理员可下载数据库备份、管理用户
"""
import argparse
import difflib
import hashlib
import sqlite3
import io
import json
import os
import re
import secrets
import subprocess
import math
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
import sys
import backup
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta

import xml.etree.ElementTree as ET

import openpyxl
from flask import Flask, abort, g, jsonify, request, Response, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

import geo
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import config
import db as dbm
import validation as V
import checkup_rules as CR
import tasks as TASKS
import biblio as BIB
import stats_scope as SS
import projects as PRJ
import collab as COLLAB
import export_templates as ET
import field_catalog as FC
import lab as LAB
import tables_def as TD
from tables_def import TABLES, TABLES_REV, linked_columns, own_columns, COLUMN_DOCS
import field_registry as FR
from field_registry import COUNT_FIELDS_ALL

# 首页图表的主表（tables_def 中 primary=True 的表）
PRIMARY_TABLE = next((t for t, c in TABLES.items() if c.get("primary")), next(iter(TABLES)))



APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 名录文件目录（wsc_species.json / wsc_genera.json / wac_*.json）。
# 默认程序目录；SPM_WSC_DIR 可指向其他目录（隔离测试模拟“名录不可用”时使用；
# 生产环境不要设置——名录更新脚本仍写程序目录）。
CATALOG_DIR = os.environ.get("SPM_WSC_DIR") or APP_DIR
# 拥有鉴定历史模型的表（与 db.DET_TABLES 一致）
DET_TABLES = ("specimens", "mygalomorph", "dali", "swu")
EXCEL_ERRORS = {"#REF!", "#N/A", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!"}

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False  # JSON 键保持声明顺序（页签/表格顺序依赖）


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 基础设施
def get_db():
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = dbm.connect()
        g._db = conn
    return conn


@app.teardown_appcontext
def _close_db(exc):
    conn = getattr(g, "_db", None)
    if conn is not None:
        conn.close()


@app.errorhandler(HTTPException)
def on_http_error(e):
    return jsonify({"error": e.description}), e.code


@app.errorhandler(Exception)
def on_error(e):
    app.logger.exception(e)
    return jsonify({"error": f"服务器内部错误：{e}"}), 500


def clean_val(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s in EXCEL_ERRORS:
            return None
        return s
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def same(a, b):
    return clean_val(a) == clean_val(b)


# ---------------------------------------------------------------- 认证
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f"{salt}${h.hex()}"


def check_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
        return secrets.compare_digest(hash_pw(pw, salt), stored)
    except Exception:
        return False


def build_default_perms(tables_list, group):
    """tables 清单 → 三态 perms：rw/r/none。
    授权以显式清单为准：列出的表 rw；共享来源表（taxon_group=shared，如采集地）r；
    其余一律 none——不再按「同类群」推导可读（权限矩阵审计修复）。"""
    allowed = tables_list if isinstance(tables_list, list) else []
    star = "*" in allowed
    out = {}
    for t, c in TABLES.items():
        tg = c.get("taxon_group", "spider")
        if star:
            out[t] = "rw"
        elif t in allowed:
            out[t] = "rw"
        elif tg == "shared":
            out[t] = "r"
        else:
            out[t] = "none"
    return out


def parse_perms(raw, tables_list=None, group=""):
    """解析 users.perms；缺键按类群推断（不给 rw），保证每表都有三态值。"""
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        v = {}
    if not isinstance(v, dict):
        v = {}
    out = build_default_perms(tables_list or [], group)
    for t in list(out.keys()):
        if t in v and v[t] in ("rw", "r", "none"):
            out[t] = v[t]
    return out


def perms_to_tables(perms):
    """三态 → 旧 tables 清单（rw 集合），保持与旧字段/脚本的兼容。"""
    rw = [t for t, p in (perms or {}).items() if p == "rw"]
    if len(rw) == len(TABLES):
        return ["*"]
    return rw


def _parse_tables(raw):
    """解析用户/邀请码的表权限清单；"*"=全部；空=只读。"""
    try:
        v = json.loads(raw or '["*"]')
    except (ValueError, TypeError):
        return ["*"]
    if not isinstance(v, list):
        return ["*"]
    out = [x for x in v if isinstance(x, str) and (x == "*" or x in TABLES)]
    return out or []


def current_user(required=True):
    conn = get_db()
    tok = request.headers.get("X-Auth", "") or request.args.get("token", "")
    row = None
    if tok:
        row = conn.execute(
            "SELECT u.id, u.username, u.is_admin, u.disabled, u.role, u.tables, u.taxon_group, u.can_boxes, u.can_composite, u.prefs, u.perms FROM sessions s "
            "JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (tok,),
        ).fetchone()
        if row is None:  # API Token（给脚本用，长期有效）
            row = conn.execute(
            "SELECT u.id, u.username, u.is_admin, u.disabled, u.role, u.tables, u.taxon_group, u.can_boxes, u.can_composite, u.prefs, u.perms FROM api_tokens t "
            "JOIN users u ON u.username=t.username WHERE t.token=?",
                (tok,),
            ).fetchone()
    if row is None or row["disabled"]:
        if required:
            abort(401, "未登录或登录已失效，请重新登录")
        return None
    admin = bool(row["is_admin"])
    tables_list = ["*"] if admin else _parse_tables(row["tables"])
    perms = build_default_perms(["*"], "") if admin else \
        parse_perms(row["perms"], tables_list, row["taxon_group"] or "")
    return {"id": row["id"], "username": row["username"], "admin": admin,
            "role": row["role"] or "editor",
            "tables": ["*"] if admin else perms_to_tables(perms),
            "perms": perms,
            "group": row["taxon_group"] or "",
            "can_boxes": admin or bool(row["can_boxes"]),
            "can_composite": admin or bool(row["can_composite"]),
            "prefs": row["prefs"] or "{}",
            "token": tok}


def ensure_visible(u, tname):
    """三态权限：perm 为 r/rw 可见，none 不可见；admin 不限。"""
    cfg = chk_table(tname)
    if u["admin"]:
        return cfg
    if (u.get("perms") or {}).get(tname, "none") not in ("r", "rw"):
        label = cfg["label"]
        abort(403, f"您的账号无权访问「{label}」（该表对您的账号不可见），请联系管理员调整权限")
    return cfg


def visible_tables(u):
    if u["admin"]:
        return TABLES
    perms = u.get("perms") or {}
    return {t: c for t, c in TABLES.items() if perms.get(t, "none") in ("r", "rw")}


def require_writer(u, tname=None):
    """三态写入拦截：admin 全部可写；该表 perm=rw 才可写；r=只读；none=不可见。"""
    if u["admin"]:
        return
    if tname is not None:
        ensure_visible(u, tname)
        if (u.get("perms") or {}).get(tname, "none") != "rw":
            label = TABLES[tname]["label"] if tname in TABLES else tname
            abort(403, f"您的账号对「{label}」只有查看权限（只读），如需编辑请联系管理员调整权限")
        return
    if not any(p == "rw" for p in (u.get("perms") or {}).values()):
        abort(403, "只读账号无权修改数据，请联系管理员调整权限")


INVITE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 去掉易混淆的 0/O/1/I
VALID_ROLES = ("admin", "group_admin", "editor", "specimen_editor", "viewer")


def _gen_invite_code():
    return "-".join("".join(secrets.choice(INVITE_ALPHABET) for _ in range(4)) for _ in range(2))


@app.get("/api/invites")
def api_invite_list():
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可管理邀请码")
    if me["admin"]:
        rows = get_db().execute(
            "SELECT code, role, note, max_uses, used, tables, taxon_group, created_by, created_at, disabled "
            "FROM invite_codes ORDER BY created_at DESC").fetchall()
    else:
        rows = get_db().execute(
            "SELECT code, role, note, max_uses, used, tables, taxon_group, created_by, created_at, disabled "
            "FROM invite_codes WHERE created_by=? ORDER BY created_at DESC", (me["username"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tables"] = _parse_tables(d.get("tables"))
        out.append(d)
    return jsonify({"invites": out, "viewer_is_ga": _is_ga(me)})


def _norm_tables(v):
    """校验表权限清单：允许 ["*"] 或 TABLES 键的子集。"""
    if not isinstance(v, list):
        abort(400, "tables 需为表名数组")
    out = []
    for x in v:
        if x == "*":
            return ["*"]
        if x in TABLES and x not in out:
            out.append(x)
    return out


@app.post("/api/invites")
def api_invite_create():
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可生成邀请码")
    d = request.get_json(force=True)
    role = d.get("role")
    if role is not None and role not in VALID_ROLES:
        abort(400, "无效的权限级别")
    if _is_ga(me) and not me["admin"]:
        if role in ("admin", "group_admin"):
            abort(403, "类群管理员不能发放管理员邀请码")
    tables = _norm_tables(d.get("tables")) if "tables" in d else         {"admin": ["*"], "editor": ["*"], "specimen_editor": ["specimens"], "viewer": []}.get(role, ["*"])
    if role is None:  # 新式：不传角色，由表清单推导显示标签
        role = "editor" if "*" in tables else "viewer" if not tables else "specimen_editor"             if tables == ["specimens"] else "custom"
    try:
        max_uses = max(0, int(d.get("max_uses") or 0))
    except (TypeError, ValueError):
        abort(400, "可用次数需为数字（0 表示不限）")
    code = _gen_invite_code()
    conn = get_db()
    while conn.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone():
        code = _gen_invite_code()
    if _is_ga(me) and not me["admin"]:
        group = me["group"] or "spider"  # 类群管理员邀请码强制本类群
        tables = [t for t in (tables if isinstance(tables, list) else []) if TABLES.get(t, {}).get("taxon_group", "spider") in (group, "shared")]
    else:
        group = (d.get("taxon_group") or "spider").strip()
    _valid_groups = {c.get("taxon_group", "spider") for c in TABLES.values()} | {"spider", ""}
    if group not in _valid_groups:
        abort(400, f"未知的类群标识 {group}，可选值：{'、'.join(sorted(g for g in _valid_groups if g))}")
    conn.execute(
        "INSERT INTO invite_codes(code,role,note,max_uses,tables,taxon_group,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (code, role, (d.get("note") or "").strip(), max_uses, json.dumps(tables), group, me["username"], now()))
    conn.commit()
    return jsonify({"ok": True, "code": code, "role": role, "tables": tables, "taxon_group": group})


@app.patch("/api/invites/<code>")
def api_invite_patch(code):
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可管理邀请码")
    d = request.get_json(force=True)
    conn = get_db()
    row = conn.execute("SELECT code, created_by FROM invite_codes WHERE code=?", (code,)).fetchone()
    if not row:
        abort(404, "邀请码不存在")
    if _is_ga(me) and not me["admin"] and row["created_by"] != me["username"]:
        abort(403, "类群管理员只能管理自己生成的邀请码")
    conn.execute("UPDATE invite_codes SET disabled=? WHERE code=?",
                 (1 if d.get("disabled") else 0, code))
    conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/invites/<code>")
def api_invite_delete(code):
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可管理邀请码")
    conn = get_db()
    row = conn.execute("SELECT created_by FROM invite_codes WHERE code=?", (code,)).fetchone()
    if not row:
        abort(404, "邀请码不存在")
    if _is_ga(me) and not me["admin"] and row["created_by"] != me["username"]:
        abort(403, "类群管理员只能管理自己生成的邀请码")
    conn.execute("DELETE FROM invite_codes WHERE code=?", (code,))
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/auth/register")
def api_register():
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    invite = (d.get("invite") or "").strip().upper()
    if not config.ALLOW_REGISTER:
        abort(403, "本系统已关闭自助注册，请联系管理员添加账号")
    if not (1 <= len(username) <= 30):
        abort(400, "用户名需为 1-30 个字符")
    if len(password) < 4:
        abort(400, "密码至少 4 位")
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        abort(400, "该用户名已存在")
    user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if user_count == 0:
        # 初始设置：系统还没有任何账号时，第一个注册者自动成为管理员
        role, first = "admin", True
        tables = ["*"]
        group = "spider"
        reg_perms = build_default_perms(tables, group)
    else:
        if not invite:
            abort(400, "注册需要邀请码，请向管理员索取")
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (invite,)).fetchone()
        if not row or row["disabled"]:
            abort(400, "邀请码无效或已停用")
        if row["max_uses"] and row["used"] >= row["max_uses"]:
            abort(400, "该邀请码可用次数已用完，请联系管理员")
        role, first = row["role"], False
        tables = _parse_tables(row["tables"])
        if role == "admin":
            tables = ["*"]
        group = row["taxon_group"] or "spider"
        reg_perms = build_default_perms(_parse_tables(row["tables"]), group)
        conn.execute("UPDATE invite_codes SET used=used+1 WHERE code=?", (invite,))
    display_role = "admin" if (first or role == "admin") else (
        "editor" if "*" in tables else "viewer" if not tables else "specimen_editor"
        if tables == ["specimens"] else "custom")
    cur = conn.execute(
        "INSERT INTO users(username,pw_hash,is_admin,role,tables,taxon_group,perms,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (username, hash_pw(password), 1 if (first or role == "admin") else 0,
         display_role, json.dumps(tables), group, json.dumps(reg_perms), now()),
    )
    tok = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (tok, cur.lastrowid, now()))
    conn.commit()
    return jsonify({"token": tok, "username": username, "admin": bool(first or role == "admin"),
                    "role": display_role, "tables": tables, "group": group})


# 登录失败锁定：按 用户名+来源IP 计数，5 次失败锁 10 分钟（进程内状态，重启即清零）
_LOGIN_FAILS = {}
_LOGIN_LOCK_SEC = 600
_LOGIN_MAX_FAILS = 5


def _login_key(username):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    return f"{username}|{ip}"


@app.post("/api/auth/login")
def api_login():
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    key = _login_key(username)
    st = _LOGIN_FAILS.get(key)
    if st and st["lock_until"] > time.time():
        remain = int((st["lock_until"] - time.time()) / 60) + 1
        print(f"  [登录被拒-锁定期] user={username!r} ip={key.split('|')[-1]} 剩余约{remain}分钟", flush=True)
        abort(429, f"登录失败次数过多，账号已临时锁定，请约 {remain} 分钟后再试")
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not check_pw(d.get("password") or "", u["pw_hash"]):
        rec = _LOGIN_FAILS.setdefault(key, {"count": 0, "lock_until": 0})
        rec["count"] += 1
        print(f"  [登录失败] user={username!r} ip={key.split('|')[-1]} 原因={'用户不存在' if not u else '密码错误'} 第{rec['count']}次", flush=True)
        if rec["count"] >= _LOGIN_MAX_FAILS:
            rec["lock_until"] = time.time() + _LOGIN_LOCK_SEC
            rec["count"] = 0
            print(f"  [登录锁定] user={username!r} 10分钟", flush=True)
            abort(429, f"连续 {_LOGIN_MAX_FAILS} 次登录失败，已锁定 10 分钟")
        abort(401, f"用户名或密码错误（再错 {_LOGIN_MAX_FAILS - rec['count']} 次将锁定 10 分钟）")
    _LOGIN_FAILS.pop(key, None)
    print(f"  [登录成功] user={username!r} ip={key.split('|')[-1]}", flush=True)
    if u["disabled"]:
        abort(403, "该账号已被停用，请联系管理员")
    tok = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (tok, u["id"], now()))
    conn.commit()
    return jsonify({"token": tok, "username": u["username"], "admin": bool(u["is_admin"])})


@app.post("/api/auth/logout")
def api_logout():
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token=?", (request.headers.get("X-Auth", ""),))
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/workbench")
def api_workbench():
    """工作台待办清单：待鉴定 / 缺采集信息 / DNA 凭证断链 / cf. 待复查。每类返回计数与前 100 条。"""
    me = current_user()
    vis = visible_tables(me)
    conn = get_db()
    limit = 100

    ev_map = None

    def get_ev_map():
        nonlocal ev_map
        if ev_map is None:
            ev_map = {}
            for r2 in conn.execute("SELECT data FROM records WHERE tname='events' AND deleted=0"):
                d2 = json.loads(r2["data"])
                ev_map[str(d2.get("CollectionEventNumber") or "").strip()] = d2
        return ev_map

    def items_specimens(where, args=()):
        rows = conn.execute(
            "SELECT id, data FROM records WHERE tname='specimens' AND deleted=0 AND " + where +
            " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        rids = [r["id"] for r in rows]
        att = {}
        if rids:
            for x in conn.execute(
                    "SELECT rid, COUNT(*) c FROM attachments WHERE tname='specimens' AND rid IN (%s) GROUP BY rid"
                    % ",".join("?" * len(rids)), rids).fetchall():
                att[x["rid"]] = x["c"]
        evm = get_ev_map()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            e = evm.get(str(d.get("CollectionEventNumber") or "").strip()) or {}
            loc = "·".join([str(p) for p in (e.get("Province") or "", e.get("City") or "", e.get("Locality") or "") if str(p).strip()])[:40]
            y, m, dd = e.get("YYYY1"), e.get("MM1"), e.get("DD1")
            date = ".".join(str(x) for x in (y, m, dd) if x not in (None, "")) if (y or m or dd) else ""
            sex_bits = []
            for f, mark in (("AdultMale", "♂"), ("AdultFemale", "♀"), ("Subadult", "亚"), ("Juvenile", "幼")):
                try:
                    n2 = int(float(d.get(f) or 0))
                except (TypeError, ValueError):
                    n2 = 0
                if n2 > 0:
                    sex_bits.append(f"{n2}{mark}")
            tv = str(d.get("TypeSpecimens") or "").strip()
            out.append({"rid": r["id"], "key": d.get("MHBU #") or f"#{r['id']}",
                        "temp": d.get("TemporarySpeciesName") or "",
                        "genus": d.get("Genus") or "",
                        "event": d.get("CollectionEventNumber") or "",
                        "loc": loc, "date": date, "sex": " ".join(sex_bits),
                        "type": tv if tv and tv.lower() != "no" else "",
                        "att": att.get(r["id"], 0)})
        return out

    out = {"identify": {"n": 0, "items": [], "rule": "DET_PENDING"},
           "no_event": {"n": 0, "items": [], "rule": "MISSING_EVENT"},
           "dna_gap": {"n": 0, "items": [], "rule": "DNA_ORPHAN"},
           "cf_review": {"n": 0, "items": [], "rule": "CF_REVIEW"}}
    if "specimens" in vis:
        n = conn.execute("SELECT COUNT(*) FROM records WHERE tname='specimens' AND deleted=0 "
                         "AND json_extract(data,'$.TemporarySpeciesName')!='' "
                         "AND (json_extract(data,'$.FinalSpeciesName') IS NULL OR json_extract(data,'$.FinalSpeciesName')='')").fetchone()[0]
        out["identify"] = {"n": n, "items": items_specimens(
            "json_extract(data,'$.TemporarySpeciesName')!='' "
            "AND (json_extract(data,'$.FinalSpeciesName') IS NULL OR json_extract(data,'$.FinalSpeciesName')='')")}
        n2 = conn.execute("SELECT COUNT(*) FROM records WHERE tname='specimens' AND deleted=0 "
                          "AND (json_extract(data,'$.CollectionEventNumber') IS NULL OR json_extract(data,'$.CollectionEventNumber')='')").fetchone()[0]
        out["no_event"] = {"n": n2, "items": items_specimens(
            "(json_extract(data,'$.CollectionEventNumber') IS NULL OR json_extract(data,'$.CollectionEventNumber')='')")}
        n3 = conn.execute("SELECT COUNT(*) FROM records WHERE tname='specimens' AND deleted=0 "
                          "AND lower(json_extract(data,'$.TemporarySpeciesName')) LIKE 'cf.%'").fetchone()[0]
        out["cf_review"] = {"n": n3, "items": items_specimens(
            "lower(json_extract(data,'$.TemporarySpeciesName')) LIKE 'cf.%'")}
    # DNA 断链：UCE 有 Sample Code，但 DNA 提取台账无对应记录（DNAVoucherID）
    if "dna_extract" in vis and "uce" in vis:
        known = {}
        for r in conn.execute("SELECT data FROM records WHERE tname='dna_extract' AND deleted=0").fetchall():
            d = json.loads(r["data"])
            v = (d.get("DNAVoucherID") or "").strip()
            if v:
                known[v.upper()] = v
        gap = []
        for r in conn.execute("SELECT id, data FROM records WHERE tname='uce' AND deleted=0").fetchall():
            d = json.loads(r["data"])
            sc = (d.get("Sample Code") or "").strip()
            if not sc:
                continue
            if sc.upper() not in known:
                gap.append({"rid": r["id"], "key": d.get("Sample Key") or f"#{r['id']}",
                            "sample": sc, "species": d.get("Species name") or ""})
        out["dna_gap"] = {"n": len(gap), "items": gap[:limit]}
    conn.close()
    return jsonify(out)


@app.get("/api/search-suggest")
def api_search_suggest():
    """搜索无结果时的模糊建议：对属名与种加词做相近匹配（容错错别字）。"""
    current_user()
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"items": []})
    ql = q.lower()
    import difflib as _df
    pairs = [(g["g"], g["g"].lower()) for g in _WSC_LIST]
    close = _df.get_close_matches(ql, [p[1] for p in pairs], n=3, cutoff=0.72)
    out = []
    for low in close:
        if low == ql:
            continue
        orig = next((p[0] for p in pairs if p[1] == low), low)
        out.append({"text": orig, "kind": "属"})
    return jsonify({"items": out})


@app.get("/api/backups")
def api_backups():
    """管理员：备份包列表（完整恢复包 + 旧版单文件）与留存/容量/异地/RPO·RTO 政策。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可查看备份")
    pkgs = backup.list_packages()
    legacy = []
    bdir = os.path.join(APP_DIR, config.BACKUP_DIR)
    if os.path.isdir(bdir):
        for n in sorted(os.listdir(bdir), reverse=True):
            if n.startswith("data_") and n.endswith(".db"):
                p = os.path.join(bdir, n)
                legacy.append({"name": n, "size_mb": round(os.path.getsize(p) / 1048576, 1),
                               "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                      time.localtime(os.path.getmtime(p)))})
    return jsonify({
        "packages": pkgs,
        "legacy_files": legacy,
        "policy": {"keep": config.BACKUP_KEEP, "max_gb": config.BACKUP_MAX_GB,
                   "offsite_dir": config.BACKUP_OFFSITE_DIR,
                   "offsite_configured": bool(config.BACKUP_OFFSITE_DIR),
                   "rpo_target_min": config.RPO_TARGET_MIN,
                   "rto_target_sec": config.RTO_TARGET_SEC,
                   "note": ("异地副本已配置：" + config.BACKUP_OFFSITE_DIR)
                           if config.BACKUP_OFFSITE_DIR else
                           "未配置异地副本目录——当前备份仅存本机，不构成可靠的异地备份"},
    })


@app.post("/api/backups/create")
def api_backups_create():
    """管理员：立即创建完整恢复包（DB+uploads+manifest+配置说明，原子发布）。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可创建备份")
    body = request.get_json(silent=True) or {}
    kind = body.get("kind") if body.get("kind") in ("manual", "predeploy") else "manual"
    try:
        m = backup.create_package(kind=kind, app_version=APP_VERSION)
    except backup.BackupError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not m:
        abort(404, "数据库文件不存在")
    return jsonify({"ok": True, "name": m["name"], "manifest": {
        k: m.get(k) for k in ("created_at", "app_version", "schema_version",
                              "table_counts", "db_size", "uploads", "offsite",
                              "duration_sec")}})


@app.get("/api/backup")
def api_backup():
    """管理员：下载数据库备份。name=pkg-* 时下载完整恢复包 zip（剥离会话与 API 令牌）；
    未指定时下载当前库一致性副本。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可下载数据库备份")
    name = request.args.get("name") or ""
    import tempfile
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _sanitize(db_path, out_dir):
        """剥离会话与 API 令牌（访问令牌不随备份导出）；口令仅存哈希本就不导出明文。"""
        dst = os.path.join(out_dir, "data-sanitized.db")
        src = sqlite3.connect(db_path)
        try:
            d2 = sqlite3.connect(dst)
            with d2:
                src.backup(d2)
            d2.execute("DELETE FROM sessions")
            d2.execute("DELETE FROM api_tokens")
            d2.commit()
            d2.close()
        finally:
            src.close()
        return dst

    if name:
        try:
            pkg_dir = backup.package_path(name)
        except backup.BackupError as e:
            abort(400, str(e))
        if not os.path.isdir(pkg_dir):
            abort(404, "备份包不存在")
        out_dir = tempfile.mkdtemp(prefix="dl-")
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.close()
        import zipfile as _zf
        with _zf.ZipFile(tmp.name, "w", _zf.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(pkg_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, pkg_dir)
                    if rel.replace("\\", "/") == "data.db":
                        safe = _sanitize(full, out_dir)
                        z.write(safe, "data.db")
                        os.remove(safe)
                    else:
                        z.write(full, rel)
            mf_path = os.path.join(pkg_dir, "manifest.json")
            mf = json.load(open(mf_path, encoding="utf-8")) if os.path.exists(mf_path) else {}
            z.writestr("DOWNLOAD-NOTE.txt",
                       "本下载副本已剥离会话与 API 令牌（访问令牌不导出）；"
                       "口令仅存加盐哈希，恢复后可直接登录。\n"
                       "表计数: " + json.dumps(mf.get("table_counts", {}), ensure_ascii=False))
        resp = send_file(tmp.name, as_attachment=True,
                         download_name=name + "_剥离令牌_" + stamp + ".zip",
                         mimetype="application/zip")
        threading.Timer(120.0, lambda: os.path.exists(tmp.name) and os.remove(tmp.name)).start()
        return resp

    # 当前库一致性副本（原行为）
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    src = dbm.connect()
    try:
        dst = sqlite3.connect(tmp.name)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    resp = send_file(tmp.name, as_attachment=True,
                     download_name=config.SYSTEM_NAME + "_备份_" + stamp + ".db",
                     mimetype="application/octet-stream")
    threading.Timer(120.0, lambda: os.path.exists(tmp.name) and os.remove(tmp.name)).start()
    return resp


@app.post("/api/backups/drill")
def api_backup_drill():
    """管理员：恢复演练（优先最新完整恢复包）。独立临时目录解包 → integrity →
    必要表/manifest 计数/关键引用/附件摘要 → 临时启动隔离实例验证登录/查询/附件读取。
    全程不触碰生产库与生产端口；测量实际恢复耗时并与 RTO 目标对比。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可运行恢复演练")
    import tempfile as _tf
    bdir = os.path.join(APP_DIR, config.BACKUP_DIR)
    name = (request.get_json(silent=True) or {}).get("name") or ""
    if name:
        try:
            src_path = backup.package_path(name)
        except backup.BackupError as e:
            abort(400, str(e))
        if not os.path.isdir(src_path):
            abort(404, "备份包不存在")
    else:
        pkgs = backup.list_packages()
        legacy = sorted((n for n in os.listdir(bdir)
                         if n.startswith("data_") and n.endswith(".db")), reverse=True) \
            if os.path.isdir(bdir) else []
        if pkgs:
            src_path = os.path.join(bdir, pkgs[0]["name"])
        elif legacy:
            src_path = os.path.join(bdir, legacy[0])
        else:
            abort(404, "尚无备份（服务启动/每日计划/管理员手动均可创建）")
    t0 = time.time()
    work = _tf.mkdtemp(prefix="drill-")
    report = {"backup": os.path.basename(src_path), "steps": [], "failures": []}

    def fail(reason):
        report["failures"].append(reason)
        shutil.rmtree(work, ignore_errors=True)
        report["restore_seconds"] = round(time.time() - t0, 1)
        report["rto_target_sec"] = config.RTO_TARGET_SEC
        report["rto_meet"] = report["restore_seconds"] <= config.RTO_TARGET_SEC
        report["ok"] = False
        return jsonify(report), 500

    if os.path.isdir(src_path):
        restored = os.path.join(work, "restore")
        shutil.copytree(src_path, restored)
        report["steps"].append("解包完整恢复包")
    else:
        restored = os.path.join(work, "restore")
        os.makedirs(restored)
        shutil.copyfile(src_path, os.path.join(restored, "data.db"))
        report["steps"].append("解包旧版单文件备份")

    # 结构迁移（旧结构恢复：init_db 补齐缺失表/列）
    old_sv = ""
    try:
        d0 = sqlite3.connect(os.path.join(restored, "data.db"))
        r0 = d0.execute("SELECT value FROM schema_info WHERE key='schema_version'").fetchone()
        old_sv = r0[0] if r0 else ""
        d0.close()
    except Exception:
        old_sv = ""
    dbm.init_db_path(os.path.join(restored, "data.db"))
    report["steps"].append("结构迁移（%s → %s）" % (old_sv or "未知", dbm.SCHEMA_VERSION))

    ok_rest, rest_report = backup.verify_restored(restored)
    report["steps"].append("恢复产物校验：" + ("全部通过" if ok_rest else "存在失败"))
    report["verify"] = rest_report
    if not ok_rest:
        return fail("；".join(rest_report.get("failures", [])[:3]) or "恢复产物校验失败")

    # 临时启动隔离实例：登录 / 查询 / 附件列表
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    probe_port = sock.getsockname()[1]
    sock.close()
    d1 = sqlite3.connect(os.path.join(restored, "data.db"))
    try:
        d1.execute("INSERT OR REPLACE INTO users(username,pw_hash,is_admin,role,tables,taxon_group,perms,created_at) "
                   "VALUES('drill_admin',?,'1','admin','[\"*\"]','spider','{}',?)",
                   (hash_pw("drill-pass-1234"), now()))
        d1.commit()
    finally:
        d1.close()
    env = dict(os.environ)
    env.update({"SPM_DB_FILE": os.path.join(restored, "data.db"),
                "SPM_UPLOADS_DIR": os.path.join(restored, "uploads"),
                "SPM_PORT": str(probe_port),
                "SPM_BACKUP_DIR": os.path.join(work, "pkg")})
    logp = open(os.path.join(work, "probe.log"), "w", encoding="utf-8")
    probe = subprocess.Popen([sys.executable, os.path.join(APP_DIR, "server.py")],
                             cwd=APP_DIR, env=env, stdout=logp, stderr=subprocess.STDOUT)
    probe_ok = False
    probe_detail = {}
    try:
        up = False
        for _ in range(24):
            time.sleep(0.5)
            if probe.poll() is not None:
                break
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/" % probe_port, timeout=2)
                up = True
                break
            except Exception:
                pass
        if not up:
            return fail("隔离实例未启动")
        def _ureq(method, url, d=None, tok=None):
            h = {"Content-Type": "application/json"}
            if tok:
                h["X-Auth"] = tok
            rr = urllib.request.Request(url, data=json.dumps(d).encode() if d is not None else None,
                                        headers=h, method=method)
            try:
                resp2 = urllib.request.urlopen(rr, timeout=10)
                return resp2.status, json.loads(resp2.read())
            except urllib.error.HTTPError as e2:
                return e2.code, {}
        st_l, login = _ureq("POST", "http://127.0.0.1:%d/api/auth/login" % probe_port,
                            {"username": "drill_admin", "password": "drill-pass-1234"})
        hh = login.get("token")
        probe_detail["login_status"] = st_l
        st_q, _q = _ureq("GET", "http://127.0.0.1:%d/api/rows/specimens?per=1" % probe_port,
                         None, hh)
        probe_detail["query_status"] = st_q
        probe_ok = (st_l == 200 and st_q == 200)
        try:
            st_e, _e = _ureq("GET", "http://127.0.0.1:%d/api/rows/events?per=1" % probe_port,
                             None, hh)
            probe_detail["events_query_status"] = st_e
        except Exception:
            pass
    finally:
        probe.terminate()
        try:
            probe.wait(timeout=10)
        except Exception:
            probe.kill()
        logp.close()
    report["steps"].append("隔离实例验证（登录/查询）：" + ("通过" if probe_ok else "失败"))
    report["probe"] = {"ok": probe_ok, **probe_detail}
    if not probe_ok:
        return fail("隔离实例验证失败")

    shutil.rmtree(work, ignore_errors=True)
    restore_seconds = round(time.time() - t0, 1)
    return jsonify({"ok": True, "backup": report["backup"],
                    "steps": report["steps"], "verify": report.get("verify"),
                    "probe": report["probe"],
                    "restore_seconds": restore_seconds,
                    "rto_target_sec": config.RTO_TARGET_SEC,
                    "rto_meet": restore_seconds <= config.RTO_TARGET_SEC,
                    "note": "演练全程使用独立临时目录与隔离端口，未触碰生产库"})


def _is_ga(u):
    """u 为 users 行（Row/dict）：是否类群管理员。"""
    role = u["role"] if not isinstance(u, dict) else u.get("role")
    return role == "group_admin"


def _group_admin_perms(group):
    """类群管理员数据权限：本类群全部表可编辑，共享表（events）只读，其他类群不可见。"""
    out = {}
    for t, c in TABLES.items():
        tg = c.get("taxon_group", "spider")
        if tg == "shared":
            out[t] = "r"
        elif tg == group:
            out[t] = "rw"
        else:
            out[t] = "none"
    return out


def _can_manage_members(me):
    return bool(me["admin"] or _is_ga(me))


@app.get("/api/users")
def api_users():
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可查看成员")
    my_group = me["group"] or ""
    rows = get_db().execute(
        "SELECT u.id, u.username, u.is_admin, u.role, u.tables, u.taxon_group, u.can_boxes, u.can_composite, u.disabled, u.created_at, u.perms, "
        " (SELECT MAX(e.ts) FROM edits e WHERE e.user=u.username) last_edit "
        "FROM users u ORDER BY u.id"
    ).fetchall()
    users = []
    for r in rows:
        d = dict(r)
        if _is_ga(me) and not me["admin"]:
            if d["is_admin"] or d["role"] == "group_admin" or (d["taxon_group"] or "") != my_group:
                continue
        d["perms"] = build_default_perms(["*"], "") if d["is_admin"] else parse_perms(d.get("perms"), _parse_tables(d.get("tables")), d.get("taxon_group") or "")
        d["tables"] = ["*"] if d["is_admin"] else perms_to_tables(d["perms"])
        d["can_boxes"] = bool(d["is_admin"] or d["can_boxes"])
        d["can_composite"] = bool(d["is_admin"] or d["can_composite"])
        users.append(d)
    return jsonify({"users": users, "allow_register": config.ALLOW_REGISTER,
                    "viewer_is_ga": _is_ga(me)})


@app.post("/api/users/batch_delete")
def api_users_batch_delete():
    """批量删除用户（含其会话与邀请码关联清理）；编辑日志保留（审计不可删）。
    类群管理员仅能删除本类群的普通成员。"""
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可删除成员")
    d = request.get_json(force=True)
    ids = [int(x) for x in (d.get("ids") or []) if str(x).lstrip("-").isdigit()]
    if not ids:
        abort(400, "未选择要删除的用户")
    conn = get_db()
    users = conn.execute(
        f"SELECT id, username, is_admin, role, taxon_group FROM users WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    removed, skipped = [], []
    for u in users:
        if u["is_admin"]:
            skipped.append({"username": u["username"], "reason": "管理员不可删除"})
            continue
        if _is_ga(me) and not me["admin"] and (u["role"] == "group_admin" or (u["taxon_group"] or "") != (me["group"] or "")):
            skipped.append({"username": u["username"], "reason": "非本类群成员或类群管理员"})
            continue
        if u["id"] == me["id"]:
            skipped.append({"username": u["username"], "reason": "不能删除自己"})
            continue
        conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        removed.append(u["username"])
    conn.commit()
    return jsonify({"ok": True, "removed": removed, "skipped": skipped,
                    "removed_n": len(removed), "skipped_n": len(skipped)})


@app.post("/api/users")
def api_user_create():
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可添加成员")
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    role = d.get("role") if d.get("role") in VALID_ROLES else "editor"
    if _is_ga(me) and not me["admin"]:
        if role in ("admin", "group_admin"):
            abort(403, "类群管理员不能创建管理员或类群管理员")
        d["taxon_group"] = me["group"] or "spider"  # 强制本类群
    if not (1 <= len(username) <= 30) or len(password) < 4:
        abort(400, "用户名 1-30 字符，密码至少 4 位")
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        abort(400, "该用户名已存在")
    tables = _norm_tables(d.get("tables")) if "tables" in d else         {"admin": ["*"], "editor": ["*"], "specimen_editor": ["specimens"], "viewer": []}.get(role, ["*"])
    taxon_group = d.get("taxon_group") if d.get("taxon_group") in ("", "spider", "pseudo", "mygalomorph") else "spider"
    perms = parse_perms(json.dumps(d.get("perms") or {}), tables, taxon_group)
    tables = perms_to_tables(perms)
    new_role = ("admin" if role == "admin"
                else "group_admin" if role == "group_admin"
                else "viewer" if not any(p == "rw" for p in perms.values()) else "custom")
    conn.execute("INSERT INTO users(username,pw_hash,is_admin,role,tables,taxon_group,perms,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (username, hash_pw(password), 1 if role == "admin" else 0, new_role,
                  json.dumps(tables), taxon_group, json.dumps(perms), now()))
    log_edit(conn, me["username"], "users", None, username, "perm_create",
             {"role": new_role, "tables": tables, "taxon_group": taxon_group,
              "perms": perms, "_scrubbed": "凭据不入审计"},
             actor_type="human", reason="创建成员账号")
    conn.commit()
    return jsonify({"ok": True})


@app.patch("/api/users/<int:uid>")
def api_user_patch(uid):
    me = current_user()
    if not _can_manage_members(me):
        abort(403, "仅管理员或类群管理员可修改成员")
    d = request.get_json(force=True) if request.data else {}
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        abort(404, "用户不存在")
    if _is_ga(me) and not me["admin"]:
        if u["is_admin"] or u["role"] == "group_admin" or (u["taxon_group"] or "") != (me["group"] or ""):
            abort(403, "类群管理员只能管理本类群的成员")
        if "taxon_group" in d or d.get("role") == "group_admin" or "group_admin" in d:
            abort(403, "类群管理员/类群归属只能由系统管理员调整")
    # 类群管理员指定/撤销（仅系统管理员）。
    # 类群管理员 = 成员管理能力（本类群邀请码/学生账号），不决定数据权限——
    # 逐表权限永远以 perms 网格为准：已有显式权限的账号升级时原样保留（绝不覆盖）；
    # 从未设过权限的账号升级时用类群模板做一次性初始值（随后仍可逐表调整）。
    if "group_admin" in d and me["admin"] and not u["is_admin"]:
        if d["group_admin"]:
            has_explicit = bool(json.loads(u["perms"] or "{}")) if u["perms"] else False
            if not has_explicit:
                grp = u["taxon_group"] or "spider"
                conn.execute("UPDATE users SET role='group_admin', perms=?, tables=? WHERE id=?",
                             (json.dumps(_group_admin_perms(grp)), json.dumps(["*"]), uid))
            else:
                conn.execute("UPDATE users SET role='group_admin' WHERE id=?", (uid,))
            conn.commit()
            return jsonify({"ok": True, "role": "group_admin", "perms_kept": has_explicit})
        conn.execute("UPDATE users SET role='custom' WHERE id=?", (uid,))
        conn.commit()
        return jsonify({"ok": True, "role": "custom"})
    if "disabled" in d:
        if u["id"] == me["id"]:
            abort(400, "不能停用自己")
        conn.execute("UPDATE users SET disabled=? WHERE id=?", (1 if d["disabled"] else 0, uid))
        if d["disabled"]:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if d.get("new_password"):
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(d["new_password"]), uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if "taxon_group" in d and d.get("taxon_group") in ("", "spider", "pseudo", "mygalomorph"):
        conn.execute("UPDATE users SET taxon_group=? WHERE id=?", (d["taxon_group"], uid))
    if "perms" in d and isinstance(d["perms"], dict) and not u["is_admin"]:
        base = parse_perms(u["perms"] if "perms" in u.keys() else "{}",
                           _parse_tables(u["tables"]), u["taxon_group"] or "")
        merged = dict(base)
        for t, p in d["perms"].items():
            if t in TABLES and p in ("rw", "r", "none"):
                merged[t] = p
        rw = perms_to_tables(merged)
        # v4.36：类群管理员身份不随权限调整丢失——role 只在非 GA 间按 rw 推导
        # （此前任何一次逐表权限修改都会把 role 从 group_admin 改写成 custom/viewer，
        # 该成员的成员管理/邀请码能力静默消失）
        if u["role"] == "group_admin":
            new_role = "group_admin"
        else:
            new_role = "viewer" if not any(p == "rw" for p in merged.values()) else "custom"
        conn.execute("UPDATE users SET perms=?, tables=?, role=? WHERE id=?",
                     (json.dumps(merged), json.dumps(rw), new_role, uid))
    if "can_boxes" in d and not u["is_admin"]:
        conn.execute("UPDATE users SET can_boxes=? WHERE id=?", (1 if d["can_boxes"] else 0, uid))
    if "can_composite" in d and not u["is_admin"]:
        conn.execute("UPDATE users SET can_composite=? WHERE id=?", (1 if d["can_composite"] else 0, uid))
        conn.execute(
            "INSERT INTO edits(ts,user,tname,rid,rkey,action,changes,source,reason,evidence) "
            "VALUES(?,?,?,?,?,?,?,?,?,'full')",
            (now(), me["username"], "users", uid, u["username"], "perm_update",
             json.dumps({"_summary": f"{'授予' if d['can_composite'] else '收回'}「允许组合表构建器」权限（{u['username']}）",
                         "can_composite": [bool(u["can_composite"]), bool(d["can_composite"])]},
                        ensure_ascii=False),
             "user-patch", "组合表构建器功能权限变更（记录操作人/对象/前后值）"))
    if "tables" in d and not u["is_admin"]:
        tables = _norm_tables(d.get("tables"))
        new_role = "editor" if "*" in tables else "viewer" if not tables else "specimen_editor"             if tables == ["specimens"] else "custom"
        conn.execute("UPDATE users SET tables=?, role=? WHERE id=?",
                     (json.dumps(tables), new_role, uid))
    if d.get("role") in VALID_ROLES and "tables" not in d:
        legacy = {"admin": ["*"], "editor": ["*"], "specimen_editor": ["specimens"], "viewer": []}
        conn.execute("UPDATE users SET role=?, tables=?, is_admin=? WHERE id=?",
                     (d["role"], json.dumps(legacy[d["role"]]), 1 if d["role"] == "admin" else 0, uid))
    conn.commit()
    return jsonify({"ok": True})


# ---------------- API Token（供脚本批量回填等场景） ----------------
@app.get("/api/tokens")
def api_token_list():
    me = current_user()
    conn = get_db()
    if me["admin"]:
        rows = conn.execute("SELECT token, username, note, created_at FROM api_tokens ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT token, username, note, created_at FROM api_tokens WHERE username=? ORDER BY created_at DESC",
                            (me["username"],)).fetchall()
    return jsonify({"tokens": [dict(r) for r in rows]})


@app.post("/api/tokens")
def api_token_create():
    me = current_user()
    require_writer(me)
    d = request.get_json(force=True) if request.data else {}
    target = d.get("username") or me["username"]
    if target != me["username"] and not me["admin"]:
        abort(403, "只能为自己的账号创建 Token")
    tok = "sk_" + secrets.token_hex(24)
    conn = get_db()
    conn.execute("INSERT INTO api_tokens(token,username,note,created_at) VALUES(?,?,?,?)",
                 (tok, target, (d.get("note") or "").strip(), now()))
    conn.commit()
    return jsonify({"ok": True, "token": tok})


@app.delete("/api/tokens/<tok>")
def api_token_delete(tok):
    me = current_user()
    conn = get_db()
    row = conn.execute("SELECT * FROM api_tokens WHERE token=?", (tok,)).fetchone()
    if not row:
        abort(404, "Token 不存在")
    if row["username"] != me["username"] and not me["admin"]:
        abort(403, "只能删除自己的 Token")
    conn.execute("DELETE FROM api_tokens WHERE token=?", (tok,))
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 数据读取（带缓存）
_cache = {}


def _stamp(conn, tname):
    r = conn.execute("SELECT MAX(id) m FROM edits WHERE tname=?", (tname,)).fetchone()
    return r["m"] or 0


def load_rows(tname, include_deleted=True):
    """返回 [{id, deleted, data}]，带简单缓存（写入后自动失效）。"""
    conn = get_db()
    cfg = TABLES.get(tname, {})
    src = cfg.get("linked_source")
    stamp = (_stamp(conn, tname), _stamp(conn, src) if src else 0)
    hit = _cache.get(tname)
    if hit and hit[0] == stamp:
        return hit[1]
    recs = conn.execute(
        "SELECT id, deleted, data, updated_at, updated_by, row_version FROM records WHERE tname=? ORDER BY id", (tname,)
    ).fetchall()
    rows = [{"id": r["id"], "deleted": r["deleted"], "data": json.loads(r["data"]),
             "updated_at": r["updated_at"], "updated_by": r["updated_by"],
             "row_version": r["row_version"]} for r in recs]
    _cache[tname] = (stamp, rows)
    return rows


def invalidate(tname):
    _cache.pop(tname, None)
    for t, c in TABLES.items():
        if c.get("linked_source") == tname:
            _cache.pop(t, None)


def source_index(src_table):
    """关联来源表的 key -> 行数据 索引（重复 key 取第一条，等价原 VLOOKUP）。
    另注册 number_aliases 的历史编号/合并重定向 → 原行数据（改号与合并后，
    引用旧编号的行仍能解析关联展示字段；稳定引用契约）。"""
    idx = {}
    if not src_table:
        return idx
    key_col = TABLES[src_table]["key"]
    for r in load_rows(src_table):
        if r["deleted"]:
            continue
        k = r["data"].get(key_col)
        if k and k not in idx:
            idx[k] = r["data"]
    try:
        conn = get_db()
        for a in conn.execute("SELECT alias, rid FROM number_aliases WHERE tname=?", (src_table,)):
            if a["alias"] in idx:
                continue
            r = conn.execute("SELECT data, deleted FROM records WHERE id=?", (a["rid"],)).fetchone()
            if r and not r["deleted"]:
                d = json.loads(r["data"])
                k = d.get(key_col)
                if k and k not in idx:
                    idx[k] = d
                # 别名键必须独立注册：当前键通常已被主循环注册，若嵌套在
                # 「k not in idx」内则改名/合并后旧编号永远解析不到
                idx[a["alias"]] = d
    except Exception:
        pass
    return idx


def linked_eidx(tname):
    return source_index(TABLES[tname].get("linked_source"))


def display_row(tname, row, eidx):
    """自有数据 + 关联列合成完整展示行（任意带 linked 列的表通用）。
    关联列优先取来源表实时值；来源缺失时回退到本行存量值（历史导入数据）。
    返回 (data, miss)。"""
    data = row["data"]
    cfg = TABLES[tname]
    cols = cfg["columns"]
    if not eidx or not any(c.get("linked") for c in cols):
        return dict(data), False
    lkey = cfg.get("linked_key", "CollectionEventNumber")
    ce = data.get(lkey)
    src = eidx.get(ce) if ce else None
    out, miss = {}, False
    for col in cols:
        n = col["name"]
        if col.get("linked"):
            out[n] = (src or {}).get(col["linked"])
            if out[n] is None:
                out[n] = data.get(n)  # 回退：本行历史存量
        else:
            out[n] = data.get(n)
    if ce and src is None:
        miss = True
    return out, miss


# ---------------------------------------------------------------- 数据接口
# 系统应用版本（与数据库结构版本 SCHEMA_VERSION 相互独立；每次发布功能更新递增）
APP_VERSION = "4.39"
APP_CHANGELOG = [
                {"ver": "4.39", "date": "2026-10-10", "note": "修复导出工作簿在用户 Office 中被「修复」、工作表改名「已恢复_SheetN」（用户三次反馈「导出里那么多已恢复」）：排障确认导出文件本身完全合规（zip 完整、XML 全部可解析、无非法控制字符、无重复表名，原始 workbook.xml 层逐名核验），表名「跳蛛：河北大学标本信息」等用的全角冒号也符合 OOXML 规范——但用户的 Office（WPS/Excel 某些版本）打开含全角冒号表名的文件仍触发「修复」，把恰好 7 张带冒号表全部改名「已恢复_SheetN」。不再依赖各家对全角字符的实现一致性：_sheet_title 把全/半角冒号一律转安全连字符「-」（导出工作表名与下载文件名变为「跳蛛-河北大学标本信息」），导出模板路径用户自定义 sheet_name 同口径净化；界面显示名（「跳蛛: 河北大学标本信息」等带冒号）不变，导入按表头映射不认工作表名，重导入零影响。test_export_scope/test_export_ranges/selftest 表名断言同步更新"},
                {"ver": "4.38", "date": "2026-10-09", "note": "修复弹窗内容「显示不全」（用户两次反馈：导出向导最后一行被切半行、其它弹窗也提过）。两处根因，均在呈现层：①弹窗底部操作条是 position:sticky bottom:0 + margin-bottom:-18px（长表单滚动时保存/取消常驻底部的设计）——但内容不滚动时 sticky 约束仍会把操作条向上顶出约 20px，白底+上边框直接盖住最后一行文字（实测 hit test：提示行下半命中 DIV.form-actions），看起来就是「内容被截断」；现去掉负 bottom margin，操作条自然衔接内容，滚动时长表单仍粘底常驻。②弹窗体滚动条为 Windows 11 悬浮式、平时不可见，长内容（导出向导/成员与邀请码/详情抽屉）需要滚动时用户无从得知；现给 .modal-body 与 .drawer-body 加常显滚动条（10px 圆角灰条+轨道底色、Firefox 同款）并补 min-height:0（flex 列容器内 overflow-y 生效的必要条件）。纯呈现层修复，无任何数据/接口变更"},
                {"ver": "4.37", "date": "2026-10-09", "note": "修复「导出全库」点击无反应（用户反馈）：该菜单项自 v3.81 初始源码起一直未绑定点击事件（git 全历史可证），而后端 /api/export/all 一直存在且被测试使用——按钮是死的。现接线为 downloadFile('/api/export/all')（按账号可见表导出全部数据+README+数据字典，文件名随 Content-Disposition），点击即提示「正在生成全库工作簿」、完成下载后再提示（全库 3 万余行生成与下载需等待）"},
                {"ver": "4.36", "date": "2026-10-07", "note": "修复类群管理员身份被逐表权限调整静默抹掉（用户反馈「类群管理员账号看不到发本类群邀请码和管理本类群人员的选项」——排查发现生产库尚无任何 role=group_admin 账号属正常（类群管理员不是独立账号，须管理员在「成员与邀请码」里给成员勾选），但实测确认一个真缺陷：给已勾选类群管理员的成员调整任何一次逐表权限，api_user_patch 的 perms 分支会无条件把 role 重写为 custom/viewer，成员管理与邀请码能力静默消失。现 role 只在非 GA 间按 rw 推导，group_admin 保持不变（撤销类群管理员仍走既有勾选通道）。_perm_tests 增 4 项：设 GA→调权限 role 保持、GA 登录后 me.group_admin=true（前端菜单可见性依据）、GA 可生成邀请码且强制本类群"},
                {"ver": "4.35", "date": "2026-10-07", "note": "修复表格改一个字符也要全删重打（用户反馈：更改内容时需要删除所有原有内容才能继续编辑）：①点击已处于编辑态的单元格内部不再重启编辑——此前这一下点击会被网格委托当成重新进入编辑，提交并重建输入框再次全选，用户永远无法把光标定位到文字中间；现交给输入框原生行为（点击定位光标、双击选词）。②点击/F2/回车进入编辑时光标落在末尾而非全选——退格直接删最后一个字符，点文字中间可改单字符；整格替换 = 输入框内 Ctrl+A，或方向键导航到目标格后直接打字（替换语义不变，Excel 同款）。锁定枚举列（select 编辑器）无光标概念不受影响"},
                {"ver": "4.34", "date": "2026-10-07", "note": "修复首页「保存液浓度分布」把浓度画成 0.95%/1%/0.8%（用户截图反馈，应为 95%/85%/75%/80%/100%）：生产库 specimens 8,979 行与伪蝎表 2,331 行的浓度是早期 Excel 导入的小数写法（0.95/0.85/0.75/1/0.8），而仪表盘聚合直接 float(原值) 分桶拼 %。三处修复：①仪表盘聚合先 normalize_alcohol 再分桶——0.95→95、1→100、0.8→80、「75%ethanol」→75，带中文描述的原文（95%（死后泡）/80%（石下），按既定语义保留原文）进「其他」；②合并导入执行器 _apply_merge 补 _normalize_row_alcohol（此前只有替换/粘贴/兼容路径归一，合并导入把 0.75 原样入库——伪蝎表重导入后小数仍在即此因），与单行保存同口径；③存量数据治理：浓度小数批量归一为百分比（生产 specimens 8,979 行 + 伪蝎 2,331 行，按值分组 batch_update 执行、逐行留痕、批次可撤销；「75%ethanol」一并归一，「95%（死后泡）」「80%（石下）」等含鉴定现场信息的原文按既定语义保留）。"},
                {"ver": "4.33", "date": "2026-10-07", "note": "修复导入弹窗「拖入文件」后点开始导入报「未收到文件」（用户以拖入方式导入合并 Excel 实际触发；v3.81 起长期存在）：导入弹窗的拖放处理只更新了提示文字并启用按钮，从未把拖入的文件赋给 <input type=file>，点开始导入时 fileInput.files 为空、请求里没有文件，服务端按未收到文件 400 拒绝；点选择路径不受影响。现拖入时同步赋值 fileInput.files，并在开始导入前加文件存在性兜底（缺失时提示重新选择、不发空请求）。附件弹窗的拖入/粘贴链路（v4.28）直接用拖放的 FileList 上传，无此问题"},
                {"ver": "4.32", "date": "2026-10-07", "note": "伪蝎标本表新增「目」列（用户在 普通标本-伪蝎数据.xlsx 表头加入「目」并反馈导入后库里没有）：列插在 标本盒位置 与 科 之间（与用户文件列序一致），归入行详情「分类」分组（目/科/属/种），新增行表单同步加入；数据字典补说明。连带：①导入表头别名机制（tables_def.HEADER_ALIASES，精确/归一化/显示名都未命中时按表查别名，命中在预览 renames 中明示）——本次把伪蝎文件表头「活体照编号」映射到存储列 LivingPhotoNumber（此前该表头一直被当未知列忽略，文件中 7 行照片/相机信息从未入库，本次重导入一并落库）；②TABLES_REV 递增为 2026-10-07.1（列结构变更）。存量数据无结构迁移（记录为 JSON 文档，新列空缺即不显示），生产重导入该文件后 2,825 行全部补上「目」（值均为 Pseudoscorpiones）"},
                {"ver": "4.31", "date": "2026-10-07", "note": "鉴定联动：UCE 文库的物种鉴定以 DNA 提取台账为准（用户需求：UCE 文库表物种鉴定以 DNA 提取表为准，有差异要标出；任一表改物种名/属名都要带动另一表分类信息同步更新）。两表经 UCE 行 Sample Code（界面名 DNAVoucherID）= 提取行 DNAVoucherID 关联（值相等才联动，与关系注册表口径一致）。①写时联动：任一表修改 属/暂定名/最终名，把改动推给关联行——目标行走 _update_record 完整通道（属名变→分类链按分类信息表级联带出、最终名→属名回写、审计日志、行版本、乐观锁全部生效；同事务同批次可整批撤销；_ident_sync 内部标记防回环且最终名按权威值原样复制不过 WSC 严格校验——台账存量 646/1055 个最终名含 cf./aff. 限定名，重新校验会把联动整体卡死）；只推非空值：权威侧清空不抹掉对方已填信息，两侧为空的差异由标记呈现；DNA 提取行的一次改动推给其全部 UCE 文库行（同凭证可多次建库）。②读时标差：UCE 行的属/种名（Species name）/最终名与关联提取行不一致（含本侧为空而权威侧有值；权威侧为空不算差异——没有可依据的鉴定）时随行下发 dna_diff={列: 权威值}，前端在差异单元格挂「异」徽标（悬停显示权威值，点击该行即以 DNA 提取表为准同步）；差异总数随 /api/rows 下发 dna_diff_total。③新增拉取/推送：新增 UCE 行 Sample Code 对得上台账时自动补齐留空的鉴定字段（用户显式键入的值不覆盖，放在严格解析之后——键入名仍按原口径校验，拉取的权威值不过校验，按属带出阶元列只补仍空缺的）；新增 DNA 提取行把非空鉴定字段推给已引用本凭证的 UCE 行。④一键同步：POST /api/rows/<表>/sync_from_authority（非权威侧→权威侧；可传 rids 限行；单事务单批次 S 可整批撤销），存量 1283 行可联动 UCE 中 1282 行最终名差异一键补齐。⑤权限口径：联动会改到对方行而用户在对方表无编辑权限 → 单行/组合表编辑 403 拦下（联动是本次修改语义的一部分，不写半截留暗差异）、批量按行报错（原子整批不执行/partial 该行失败）、新增时的推送/拉取只提示不阻断；与对方值一致的修改不拦。实现：tables_def.IDENT_LINK 配置（fields 三元组解析两表异名列名 Temporary Name↔Species name，ident_link() 随 /api/meta 以 ident_sync 下发供前端消费），server 鉴定联动辅助函数组+_update_record 挂钩（api_update/by-key/组合表行编辑同享）+batch_update 阶段一预检阶段二应用+api_create 拉取/推送；导入执行器不联动（批量场景一键同步兜底，差异标记持续可见）。连带修三处既有缺陷（全量联动实测暴露）：①check_species_name 空值提前返回——v4.30 起清空最终名会被当成「未在 WSC 收录」400 拦下，留空本应合法；②force 路径属名取首词——「cf. Jotus sp.」等限定词开头的名称首词被当属名回写（隔离实例全量同步把 26 行属名写成了「cf.」），现限定词开头不下发属名；③联动回填禁用「最终名首词→属名」改写（属名只来自权威行的属名字段）——权威行存在「属名列 Phintella + 最终名 Lechia squamata」这类内部不一致，首词改写会在回填最终名时凭空制造与权威行的属名差异（读时标差正确捕获，但一键同步修不掉自己制造的差异）。无数据库结构变更，TABLES_REV 不变"},
                {"ver": "4.30", "date": "2026-10-06", "note": "跳蛛 DNA 提取台账与 UCE 文库补齐「属名联想 + 填属带出上级分类」（用户反馈：标本表填属名会自动联想、填了属会带出前面的分类单元，DNA 提取表两个功能都失效）：根因两处。① 表定义缺开关——dna_extract 与 uce 从未声明 genus_lookup/cascade_from，前后端都不为它们启用联想与带出；② 科学名列名写死——即便启用，写层与前端都按标本表列名找列（FinalSpeciesName/TemporarySpeciesName），而 DNA 台账存 Temporary Name/Final Name、UCE 文库存 Species name/Final Name，一条也匹配不上。现新增 tables_def.taxon_cols(tname) 作为唯一列名解析（新表选项 genus_col/temp_species_col/final_species_col，缺省即标本表列名），随 /api/meta 逐表下发 taxon_cols；后端 check_species_name(cols=)、validation.resolve_taxonomy、更新接口的「最终名→属名」回写、合并导入与旧导入执行器一律经它取列，前端 taxCols()/isFinalNameCol()/isTempNameCol() 替换散落的硬编码（网格单元格编辑、新增与以此行新增表单、批量编辑、详情抽屉标题科学名、鉴定值就地编辑、cf. 按钮挂载均按本表列名；四处无参 taxCols() 调用按当前表解析），学名斜体列集合补 Temporary Name。口径与标本表一致（用户选定）：最终名只接受 WSC 正式名，异名即时转为接受名，未收录 400 且名录同步滞后时 force_name 逃生口保留，报错文案指到本表实际字段（「Final Name」而非标本表列名）；暂定名保留 cf. 切换与未收录提醒语义；批量导入仍走非严格归一（species_normalize），历史数据不因未收录名卡死导入。分类链带出新增 _keep_taxon 按自有列过滤——UCE 只有 Order/Family/Subfamily/Tribe/Genus，绝不注入本表没有的 Infraorder/Clade1-3/Subtribe（否则留下用户看不到也改不掉的字段并混进变更历史）。连带修 field_catalog「暂定鉴定名」对 dna_extract 的内部键写错（Species name→Temporary Name，UCE 的兼容表头不变）。另修同秒两次打包撞包名（本次全套回归真实触发过一次假失败）：包名精确到秒，同一秒内两次备份同名，旧逻辑先 rmtree 已存在的目标再 os.replace——Windows 上刚删掉的目录常被扫描器/索引器短暂持有，紧接着替换就是 [WinError 5] 拒绝访问，且该秒原有包已被删（备份静默丢失）；现打包前预留包名（同秒冲突自动 -2/-3 递增）并在发布时二次避让，已发布的包一律不覆盖、不删除，test_backup_restore 增冻结时钟的同秒两包场景（旧代码必失败）。测试：_edge_tests 增 16 项（三表 taxon_cols 与 genus_lookup 随 meta 下发、DNA 新增即带出整链/改属换链/最终名带出属名与 force_name 逃生/未收录报错文案含 Final Name 且不含标本表列名且值不入库、UCE 带链+自有列过滤（无 Infraorder/Clade1-3/Subtribe）+UCE Final Name 生效）。无数据库结构变更（未增删列，仅新增表选项，TABLES_REV 保持 2026-10-06.1）；伪蝎系两表不启 WSC 联想（名录为 WAC，未接入），行为不变"},
                {"ver": "4.29", "date": "2026-10-06", "note": "修复附件弹窗上传/粘贴后要关两次（用户反馈：粘贴完关闭还会出现上一份弹窗）：openModal 每次调用都把当前内容存为「返回层」，而附件列表刷新走 openFiles→openModal 再入栈——上传/粘贴一次就多压一层，关闭时逐层恢复旧快照（老版单文件上传即存在，粘贴高频化后每次都要关 N+1 次）。现上传/删除后就地刷新：openFiles 检测附件弹窗已打开（modal 可见且 att-list 存在）时只重渲染列表不再压层，列表渲染拆出 renderAttList（含弹窗已被关闭的竞态守卫），删除后就地刷新并顺带修正跨表删除后按默认表重开的隐患；弹窗嵌套语义（表单守卫确认层等）不受影响"},
                {"ver": "4.28", "date": "2026-10-06", "note": "附件上传支持直接拖入与粘贴图片（用户需求）：附件弹窗的文件按钮升级为三合一投放区——把文件拖进虚线框（可一次多文件，dragenter/leave 计数高亮）、直接 Ctrl+V 粘贴剪贴板截图（文档级 paste 监听仅附件弹窗打开期间生效，弹窗重挂/关闭自动失效）、或点击选择文件（input 多选）；多文件逐个上传带进度（N/M），部分失败逐个报错不中断；剪贴板截图通常叫 image.png，上传时自动改名为「粘贴_日期_时间」（后端再统一加「标本编号_」前缀与重名序号），便于区分多次粘贴。所有入口（表格行回形针/详情抽屉/鉴定工作台/跨表共享行）共用同一弹窗自动受益；上传权限判定由 canWrite() 收紧为 canWriteTable(tname)（此前跨表打开时判定口径不一致）。帮助文案与使用说明同步"},
                {"ver": "4.27", "date": "2026-10-06", "note": "关于页头版式微调（用户截图反馈小字不协调）：标题与「多人协作 · 全程可追溯 · 版本可回退」小字同行基线对齐——login-sub 原带登录页语境的下边距 20px，在 hero 的垂直居中行里把小字顶高悬空；现包进 about-hero-txt 子行（baseline 对齐、可换行）并清零该上下文杂散边距，登录页自身版式不变"},
                {"ver": "4.26", "date": "2026-10-06", "note": "修复统计页按属/按科对伪蝎表恒为「暂无数据」（用户截图反馈）：_tally_table 的分类分布写死英文列名（Family/Genus/Tribe），伪蝎表列名为中文 科/属/种，一条都匹配不上；已鉴定口径（FinalSpeciesName/Final Name）同样漏掉伪蝎的 种 列。现按数量列 _SEX_MAP 的既有中英文兼容模式，分类分布与种名读取改为多候选列名回退（Family/科、Genus/属、Tribe、已鉴定种名 FinalSpeciesName/Final Name/种，与 server.GROUP_COLS 同口径），跳蛛/原蛛/借入/UCE/DNA 提取等英文列表不受影响。test_stats_scope 增伪蝎中文列名计入按属/按科两断言"},
                {"ver": "4.25", "date": "2026-10-06", "note": "修复替换导入丢失与现存键重叠的行（用户导入伪蝎表真实触发、连续三次，每次少 229 行）：v3.41 计划契约按「清空前」的表给文件行分类（键已存在→update/无变化→noop），但替换执行器只应用 action==add 的行——先清空再跳过 update/noop，这些行被静默丢弃（无计划兼容层无此问题，它本就全量重建）。现替换模式对 add/update/noop 三类行一律按文件值重建新行（清空后全量落库，与「清空后导入」语义一致）；blocked/ambiguous 仍不应用。连带修撤销预检：恢复旧行时「同批次撤销新增将移除的行」不再算编号占用（替换重建行与被删旧行同键，原子撤销曾被误判冲突整批 409，挡住用户撤销替换批次的恢复路径）。test_import_plan 增场景 11：表内已有键与文件重叠时替换导入，重叠行与新增行都必须在替换后存在且为文件值，撤销还原旧值。提醒：替换导入会清空全表（旧行进回收站可恢复），与现有数据有重叠时用「合并」更稳妥"},
                {"ver": "4.24", "date": "2026-10-06", "note": "修复导入预览前端永远卡「解析中…」（用户重新上传伪蝎表实际触发）：服务端计划构建与落库其实每次都成功（import_plans 可见 8 条完整计划），但 v3.41 统一计划器重写预览响应时把 unknown/renames 两键并入 mapping、漏发旧契约顶层键——前端预览弹窗读 pv.unknown.length 对 undefined 抛 TypeError，await 已返回而弹窗渲染中断，按钮永远停在「解析中…」且无任何报错提示；这是 v3.41 以来 UI 路径 Excel 导入的通病（此前大理/西南大学导入走脚本未经过该弹窗，伪蝎组今天首次 UI 导入即踩中）。修复：预览响应补回顶层 unknown/renames 旧键（mapping 内仍保留），前端弹窗双来源兜底（顶层优先、mapping 回退）不再可能 undefined；test_import_plan 增响应契约断言"},
                {"ver": "4.23", "date": "2026-10-06", "note": "新增伪蝎系统「伪蝎等: DNA 提取信息」表（用户以跳蛛 DNA 提取表导出的表头模板提出需求，模板另加了 4 列）：结构为跳蛛 DNA 提取表的等价克隆——主键 DNAVoucherID、分类链、组织、浓度定量、建库信息等 31 列原样沿用；「Specimen #」改「凭证标本位置」（对应伪蝎标本 标本位置编号），另加 凭证标本盒位置/DNA保存位置/Sanger Data/Geniomic Data 四列（用户模板新增），共 35 列；存储名与用户模板表头逐一相同，导入按名直映射零改名。采集相关列（CollectingData/Country 等）同跳蛛表为快照文本列（events 无同名列，不做实时关联）；不带 genus_lookup（伪蝎名录 WAC 联想未接入此表，属名相关列手填）。组合表关系注册表新增 伪蝎⇄伪蝎DNA提取（凭证标本位置=标本位置编号，双向）；任务视图新增「伪蝎 DNA 提取」预设；taxon_group=pseudo 仅伪蝎系统可见——存量伪蝎账号按显式授权契约默认 none，需管理员在权限矩阵勾选后可见。连带修复导入选表：系统模板/导出工作簿把 README 插在首位，而 _read_xlsx 固定读第一个工作表，按模板填好的数据导回会被当成 README 解析（说明页「本表与示例表不会被导入」从未兑现）——现按「表头能否映射到目标表自有列」从前往后选首个命中工作表（README/说明零映射自动跳过；tname 为空或全部零映射回退首表，兼容任意旧文件），公式无缓存扫描同步按同一选表规则对位行号。表定义版本 TABLES_REV 2026-10-06.1，无数据库结构变更"},
                {"ver": "4.22", "date": "2026-10-06", "note": "修复 Excel 导入含日期型单元格 500（用户导入伪蝎信息表实际触发，报错 Object of type datetime is not JSON serializable）：Excel 会把形如 1-21/1.21 的编号等文本自动存成日期单元格（如采集编号列出现 2025-01-21），v3.41 统一计划器在单元格入口只处理了 dict/list、漏了 datetime/date，计划落库 json.dumps 即崩 500——旧导入路径每格过 clean_val 所以没事，属计划器接入时的遗漏。现计划器入口对 datetime/date 按显示值转字符串（与兼容路径 clean_val 同规则：纯日期→YYYY-MM-DD，带时间→YYYY-MM-DD HH:MM:SS），转换出的可疑值由既有 _looks_like_date 疑似日期警告逐行提示核对；粘贴/API 导入本就无日期对象不受影响。test_import_plan 增场景 9（datetime/date 单元格预览不 500、按显示值转换、疑似日期警告、计划提交成功、date 同样转换）"},
                {"ver": "4.21", "date": "2026-10-06", "note": "数据录入表改名带类群前缀（用户截图反馈：侧边栏三张「河北大学标本信息…」完全无法分辨；且后续要为其它类群加 FDNA 提取表，需早做区分）：跳蛛「跳蛛: 河北大学标本信息」「跳蛛: DNA 提取信息」「跳蛛: 大理大学标本信息」「跳蛛: 西南大学标本信息」、伪蝎「伪蝎等: 河北大学标本信息」、原蛛「原蛛: 河北大学标本信息」，UCE文库不变，后续新类群沿用「类群: 机构/用途」惯例。半角冒号是 Excel 工作表名非法字符：新增 _sheet_title 统一净化（「: 」转全角「：」，其余非法字符兜底替换，截断 31 字符），单表/全库导出的工作表名与下载文件名都走它（文件名保留原非法字符替换作兜底）——「跳蛛：河北大学标本信息.xlsx」。改名只动显示名：tname、权限、组合表、项目快照一律存 tname 不受影响，导入按工作表首表读取不认名字，历史版本名与旧导出文件里的旧称不回改。新增 tables_def.display_label() 作同名守卫随 /api/meta 下发（当前六表名唯一、原样返回；未来同名表自动按类群加后缀），跨表清单（导航与面包屑、导出全库工作表名、变更历史与打印/导出留痕摘要、全局搜索分组、统计范围与按表分布、实体解析命中与「不在本范围」提示、组合表来源表与身份列、权限矩阵/我的权限/邀请勾选）统一消费。纯显示层，不动数据与结构；另修关于页「系统版本」卡两处写死值（同一截图反馈）：服务地址改取 location.host（原硬编码 10.51.42.96:8000，本机或隔离实例打开时显示错误），脚注不再钉死「数据库结构版本（3.17）」而与上一行实际版本一致——3.17 仅作为 pending_review 列的起始版本保留"},
                {"ver": "4.20", "date": "2026-10-05", "note": "跳蛛表成熟经验铺陈到其它类群（用户反馈）：①网格直接编辑——伪蝎「采集编号」是本表存储的关联键列，此前被 linked 过滤既进不了新增表单（前端已修，未单独发版）也在网格里被误报「由采集地信息带出」不可编辑，现双击/键盘进入编辑放行关联键列（真实关联列仍指向采集地信息页修改）；②新增/以此行新增表单为关联键列挂上采集事件号前缀联想（此前只有网格与详情有）；③详情抽屉改属名后整链刷新从 specimens 放宽到全部 genus_lookup 表（原蛛/大理/西南大学）；④任务视图铺陈——伪蝎新增「伪蝎鉴定」「采集信息」、原蛛新增「原蛛鉴定」「保存与组织」四个预设视图（列集合/固定列/默认排序），原蛛 add_first 增补采集事件号（新增行排序与跳蛛一致）；另收拢两项未单独发版的前端修复随本条一并上线：柱状图数值标签（原记 v4.18）、新增表单星号版式 lb-head 与关联键列入表单（原记 v4.19）——正式环境此前停在 4.17"},
                {"ver": "4.17", "date": "2026-10-05", "note": "环形图卡排版修复（用户反馈不协调）：图卡被统计网格拉高等高后，环形图固定小尺寸靠上、下方大片留白、图例还带滚动条。现环形卡改 flex 纵向布局——内容随卡高自适应并垂直居中，环形放大（clamp 150-250px），图例去滚动条、在富余空间内纵向居中舒展（min-height 22px/行、--fs-md 字号）；卡类名 dg-card 仅在当前类型=环形时挂载，切回条形/柱状自动还原常规布局。纯前端样式"},
                {"ver": "4.16", "date": "2026-10-05", "note": "设计语言统一（用户反馈：统计筛选的框/按钮风格不一致）：根因是全站 88 处 class=input 控件从未在 CSS 定义——导出向导/统计筛选/组合表构建器等控件一直渲染为浏览器原生样式。本次一处定义 .input 全局控件类（边框/圆角/高度对齐 .btn 的 --border-strong/--radius，焦点环对齐工具栏搜索：主色边+柔和外圈，禁用态置灰，select 微调与 .btn 行高精确对齐），并为组合表构建器/筛选弹窗/派生配置/批量修改等新增界面的裸输入框补挂该类；v4.06+ 新增块内硬编码颜色与字号全部归位设计 token（--fs 字号阶、--c-pending/--c-linked 语义色、--radius-lg、--primary-soft），清掉一次性灰值；统计筛选空态提示去重。纯前端样式，不改行为"},
                {"ver": "4.15", "date": "2026-10-05", "note": "统计图表类型可选（用户反馈：不能只有一种条形图）：四类渲染器离线 SVG 手绘（不引外部库）——条形/环形（占比+图例+中心总计，超 8 项自动归「其他」）/柱状（数值标注+斜排标签）/折线（面积趋势+数据点悬停）；每张图卡片头部类型切换（按图适配：属/族/科/省份/国家/模式类型=条形|环形|柱状，年份=柱状|折线|条形，性别构成=环形|条形），选择按浏览器记忆（localStorage sm_chart_types，刷新保留）；新增「按国家」「个体数量·按性别」两张图（共 8 张）；纯前端，数据管道与筛选不变"},
                {"ver": "4.14", "date": "2026-10-05", "note": "统计筛选多条件与权限边界（用户反馈）：①「添加条件」可连续添加多个不同字段的条件（每次追加下一个未用字段，行内可改字段；同一字段重复添加/改重时明确提示只保留一条），多条件为「并且」语义（全部满足才计入统计），筛选卡明示示例；②权限边界在界面明示——数据范围只包含用户有权限的数据表（范围下拉本就只列可见表，联合范围只含可见标本实体表；无权限表作范围 403 在案），筛选卡加注说明。test_stats_scope 增 2 断言（多条件 AND 交集、联合统计不越权）"},
                {"ver": "4.13", "date": "2026-10-05", "note": "统计页排版优化（用户反馈）：图表网格（属/族/科/省份/模式类型/年份六张）提前到筛选卡之后；物种与个体数量分解两张紧凑数字卡紧随图表；空间精度三级随后；「三种计数口径」详细定义卡移到页面最后（口径元数据卡次后），标题改为「各口径的定义、分母与排除」——先看结果图，口径说明收尾。纯前端排版调整"},
                {"ver": "4.12", "date": "2026-10-05", "note": "导出向导修复（用户报告）：①解除导出行数上限——稳定计划 resolve=ids 增 cap 参数（默认 5000 仍用于批量操作防失控；导出向导传 cap=200000，筛选全部 9145 行不再被「超出导出计划上限 5000」挡住；非法/超界 cap 明确 400 绝不静默截断）；②修绑定失败死循环——范围绑定失败后不再「失败→重渲染→自动再绑定」无限重试刷屏（bindTried 每「换范围」重置一次），错误改为内联显示在范围步骤（红字提示+可换范围重试），不再只靠 toast 堆叠遮挡向导内容；test_query_model 增 cap 三断言（不截断/小 cap 截断/非法 400）"},
                {"ver": "4.11", "date": "2026-10-05", "note": "可筛选统计（统计页）：数据范围可选——默认主表 / 任一可见表 / 类群联合（group=该类群下所有「标本实体表」即带采集关联键的表，跳蛛系统=标本+大理+西南大学三张台账并集，事件跨表去重）；叠加 搜索词 + 列筛选（字段-条件-值：属于[逗号分隔多值]/包含/为空/不为空，与表格筛选同一语义，作用于关联解析后的展示行；值输入带该列去重值联想；筛选字段须为范围内各表共有列，否则 400 具体指出）；统计输出新增 属/族/国家 Top15、物种数（最终名去重+暂定名去重单列+物种 Top 清单）、性别与个体数量分解（各表数量列名不同按统一性别键归并：跳蛛 AdultMale/伪蝎 雄性数量…，空值不当 0）、每表行数明细；图表数据下载（CSV）携带同一筛选并记录条件与性别/属/族/物种段；无权限表作为范围 403。前端统计页新增筛选卡（范围下拉+搜索+条件行+应用/清空，条件在会话内保留）与新图表卡片；test_stats_scope 增 11 断言（筛选/搜索/未知字段 400/联合/带筛选下载/403）"},
                {"ver": "4.10", "date": "2026-10-05", "note": "①详情抽屉附件轮播高度自适应：去掉固定 4:3 框，宽度占满抽屉行、高度随图片自然比例——竖版/长图完整显示不再被压进框内裁切（抽屉本身可滚动）；PDF 按 A4 纵向（210:297）比例显示首页。②导出向导不再强制选模板：没有适用模板或未选择时「下一步」始终可用，默认按表定义全部列导出（顶部摘要显示「默认全部列」，文件名回落「全部列导出」；高级配置在无模板时展示全部列可增删调整）；帮助文案同步。纯前端调整"},
                {"ver": "4.09", "date": "2026-10-05", "note": "详情抽屉附件预览改为轮播：一次显示一个附件（图片整幅 contain 不裁切、PDF 用内嵌查看器显示首页），左右圆形箭头循环切换 + 右上角「当前/总数」计数 + 底部文件名与动作条（图片=下载原图、PDF=预览/新标签打开，跨表共享附件标注来源）；单附件时不显示箭头与计数；点击图片仍进入大图灯箱；非图片/PDF 文件保持文件名清单。纯前端调整"},
                {"ver": "4.08", "date": "2026-10-05", "note": "详情抽屉附件预览条调大：网格最小列宽 200→340px（宽抽屉一行 4 张小图 → 2 张大图）；只有一张图片时占满整行（预览条在抽屉末尾，标本照大图更易读，点击仍可放大看原图）；纯前端样式调整，不改数据与接口"},
                {"ver": "4.07", "date": "2026-10-05", "note": "自定义组合表（SCHEMA 3.18，composite.py）：管理员像开放标本盒功能一样在成员管理勾选「允许组合表构建器」（users.can_composite，管理员默认，授权/收回入审计）；被授权者经可视化构建器（选主数据源→添加字段与关联→配置显示和编辑→预览→保存/发布）把 标本/采集信息/DNA 提取/UCE 文库 等已有字段组合成新表（如系统发育样本总表），无需改代码；每张表由管理员逐用户授权 不可见/只读/可编辑（composite_tables.permissions，前后值入审计），普通用户使用已发布表不要求构建器权限；两层权限独立——收回构建器权限不删除定义与源数据。数据权限=组合表授权×源表三态权限×记录可见性的交集，服务器逐请求实时计算（收回即时 403，不依赖重新登录），组合表「可编辑」不扩展源表权限。关系注册表（关系字典已确认语义：事件号×7 表、样本链 标本↔提取↔文库）之外不建关系；行粒度显式选择，一对多显式选展开/保留主记录，预览报告输入记录数→输出行数的膨胀与未匹配/多候选（绝不静默取第一条）；行身份由实际源实体（表+行id）组成，编辑/批量按源记录归并走 row_version/统一校验/幂等/批次审计（撤销作用于真实源记录，共享采集信息改动复用既有影响预览）；派生字段白名单（文本连接/数量合计[未知不补0]/关联计数/条件标签）默认只读；定义=草稿→发布→停用状态机，修订留痕可恢复（只恢复定义），配置并发修改 409 保护；查询/筛选/排序/分页/勾选/导出独立实现且遵守同一权限交集，导出绑定修订与行粒度（README 载明范围/展开规则/缺失口径，前导零与公式防护沿用 Excel 契约）；12 场景隔离测试（test_composite.py）+ 全套回归。"},
                {"ver": "3.81", "date": "2026-10-04", "note": "键盘/缩放/加载体验统一（v3.81，纯前端）：对话框语义与焦点管理——modal/drawer 加 role=dialog+aria-modal+aria-labelledby，打开时 app-view 置 inert（背景不可误操作）、初始焦点移至表单首控件、Tab 在容器内循环（焦点约束）、全关后焦点恢复至打开前元素；toast-box 加 role=status aria-live=polite；缩放与窄屏（≤760px，覆盖 200% 缩放）——长表单两列网格收单列、列定义行收两列、筛选行收单列、弹窗与抽屉全宽、动作换行，宽表容器保持局部滚动；渲染防护——协作冲突审阅字段项（行×列无上界）渲染上限 200 条并明示「命中 N 项仅渲染前 200，请用筛选缩小」，首页 recents 加载失败从静默改为显式错误卡（可刷新重试）；协作建包失败错误持久显示于弹窗内（已保留所选内容可修正重试）；流转登记失败错误写入表单内联错误区。加载状态均绑定实际请求生命周期（await 后渲染），无固定时长假加载；任务进度仅显示 tasks 注册表真实阶段。全套 36 套 767 项 rc=0 零泄漏；浏览器 webview 故障沿用降级记录（交互复验待面板恢复），记录 ui_review/v3.81；本轮不部署"},
                {"ver": "3.80", "date": "2026-10-04", "note": "文字与动作层级统一（v3.80，四高频页+最近操作；不改字段键与 API）：新增 rowMenu 行内更多菜单（键盘/读屏可达：aria-haspopup/expanded、菜单项均为 button、Esc 与点外关闭）；模板页行动作收敛（行内=生成文件[主]+编辑，预览/收藏/复制/删除入更多）；协作页行动作收敛（行内=回传合并[主]+下载工作簿，详情/收回入更多）；项目页行动作收敛（行内=成员与详情[主]，导出/固定入更多）；流转回退按钮统一用语「回退」（title 说明有后续修改会被阻止），确认文案改「回退这条流转的改动…应登记纠正事件」；最近操作批次按钮改「查看明细」，明细内改「回退此批次的修改…（仅回退该批次改动，非整表恢复）」，回退预览标题与说明同步（其他人之后做的修改会全部保留）；协作回传 409 冲突接入字段级审阅界面（renderPendingReview：业务编号/中文列名/基线/当前/回传三值[空/0/未知 disp 原样区分]、关键字与只看未决筛选、逐项保留当前/接受回传切换、批量先展示范围再确认执行、提交逐项重检后部分应用、结果分卡展示应用/保留/回冲突与剩余数）——修复 pending 响应解包丢失 field_items 的 bug；全套 36 套 767 项 rc=0 零泄漏；浏览器验收（四页动作层级/深链直开/审阅决策提交→服务器值=回传值）记录入 ui_review/v3.80；本轮不部署"},
                {"ver": "3.79", "date": "2026-10-04", "note": "协作回传冲突审阅与撤销逐项选择（v3.79，SCHEMA 3.17：edit_packages 增 pending_review 列，保留既有安全约束）：回传 409 时冲突展开为字段级审阅项持久化到 pending_review（业务编号/中文字段名/基线/服务器当前/回传值三值并列，空值/0/未知以 disp 结构原样区分不合并；不在允许范围的列不生成项）；新增 GET pending（字段级项+状态 undecided/applied/kept）与 POST resolve 决策端点——逐项重检（记录存在与删除态、行版本==审阅时版本、字段当前值==审阅时服务器值、列在允许范围、并发 WriteConflict 捕获）通过后 accept 以当前行版本条件更新应用（留 CR 审计批次）、keep 保留当前值记账；同记录多字段决策分组合并为一次条件更新（前一字段应用不使同记录其余字段失效）；部分合并明确设计=已应用与待处理分开记账、包保持 active 保留后续处理（未决项/未应用修改留在 pending 可继续决策或重新回传），全部字段项已决策才置 merged；失效项回冲突携带最新服务器值——审阅期间新修改不被旧决策覆盖。撤销逐项回退：undo/redo 接受 items=[记录ID]（只回退勾选的可安全项，勾选不可安全项 400 逐条说明原因，冲突行后续修改全部保留），skip_conflicts 兼容保留；前端后续接 pending 审阅界面。test_review_flow.py 十一组入套件；全套 36 套 767 项 rc=0 零泄漏；本轮不部署"},
                {"ver": "3.78", "date": "2026-10-04", "note": "首页最近使用与下一步动作（v3.78，简洁列表不建可配置仪表盘）：偏好白名单增 recents/pinned（按账号服务端存储，容量 5，权限失效条目渲染时隐藏并静默修剪）；首页顶部「继续处理」区——固定项目（项目页★固定最多5个）与最近项目（项目详情打开即埋记）一键打开成员与详情、收藏模板（复用收藏机制）点击直接进导出向导、我的保存视图点击应用（切换到对应表并套用视图）；表格勾选动作条（勾选≥1 行即出现）——加入项目（逐条写成员/失败行单独提示）/创建协作包（明确记录集合直接建包，保留来源标注）/模板导出（向导范围=勾选集合）/清除选择，导出菜单同步新增「从当前筛选创建动态项目」；项目详情下一步——成员清单 CSV、创建协作包（当前成员）、冻结版本数量与名称展示（导出向导可指定）；使用点埋记=项目详情打开、向导生成成功。test 无服务端行为变更（偏好白名单兼容既有快照），全套 35 套 754 项 rc=0 零泄漏；浏览器验收（一键继续/固定/勾选到导出零粘贴/范围=勾选集合）记录入 ui_review/v3.78；本轮不部署"},
                {"ver": "3.77", "date": "2026-10-04", "note": "统一关闭契约（v3.77）：✕按钮/遮罩/Esc/切模块/上一条下一条/浏览器导航/退出登录全部收敛到统一 closeGate——详情字段编辑阻塞提示、表格单元格编辑走既有 leaveGuardConfirm 三选一（保存/保留草稿/放弃，结果明确才切换）、长表单弹窗走 formGuardConfirm（继续编辑/保存并离开[仅当表单校验通过且等待提交结果]/保留草稿并关闭/放弃）。弹窗改弹层栈：嵌套弹窗只关闭当前层（批次明细→撤销预览取消后回明细），带守卫层关闭经确认；遮罩不再绕过守卫（drawer/modal overlay 与 Esc 全部走同一门）。四个长表单接入本机草稿（复用 sm_drafts 存储：账号|form:类型|目标 隔离、7 天 TTL 过期自动清除、登出与清除草稿入口统一处理、脱敏剔除 token/password/secret 键、不含文件二进制）：模板编辑器（列定义含未支持结构一并保存）、协作包创建（标本选择+可编辑列）、项目创建（动态筛选器状态）、流转登记（类型/数量/对方/位置/实体——恢复时经 GET /api/rows/<t>/<rid> 重新核验身份/权限/删除状态，不可用即移除并提示重新选择）；恢复时展示保存时间并询问/可丢弃。刷新/关标签走 beforeunload 原生提示（草稿 500ms 防抖已落盘可恢复）。无修改时所有路径直接关闭不弹确认。node 断言草稿契约五项（账号隔离/脱敏/TTL 过期自动清除/目标隔离/丢弃——执行 app.js 真实实现代码）；全套 35 套 754 项 rc=0 零泄漏；本轮内置浏览器 webview 故障（guest not attached，需重开面板），交互验收按降级以真实实现代码断言+语法/回归覆盖替代并如实记录于 ui_review/v3.77；本轮不部署"},
                {"ver": "3.76", "date": "2026-10-04", "note": "统一最近操作/任务结果入口（v3.76，复用既有事实存储不另建事实源）：新增 GET /api/activity/recent（edits 按 batch_id 聚合的批次卡+导入分区[action=import 汇总]+catalog_update 名录更新+稳定引用迁移[summary 匹配]+导出记录[新增审计]；普通用户仅见本人发起且触及可见表的条目、管理员全部）与 GET /api/activity/batch/<bid>（批次明细 rid/rkey/action/变更字段，按可见表与本人过滤，他人批次 404 不泄露）；导出操作首次留审计（模板生成/表导出/全库导出各一条 action=export 汇总，含范围与行数，修复合并事务缺 commit 与误插位置两处缺陷）；导入成功响应补 batch 号（前端/最近操作定位用）。前端「最近操作」页（质量与追溯组）：后台任务卡（复用 tasks 注册表；取消四态=请求已发送/正在停止/已取消或已完成，轮询读服务器真实状态，关闭面板≠取消）；待处理协作包卡（继续处理跳协作页）；批次卡「查看明细/撤销」（明细弹窗行内点击编号→深链定位原记录、复制编号清单、导出问题/明细清单 CSV、撤销走既有 undo preview→确认）；「按请求 ID 确认写入结果」（复用 GET /api/idempotency——已成功提示无需重试、无记录提示可安全重试）；刷新后全部从服务器真实状态读取。test_activity.py 十断言入套件；全套 35 套 764 项 rc=0 零泄漏；浏览器验收（错过提示/刷新后仍可找到批次：明细 4 行、定位原记录、复制/CSV、撤销入口、req_id 未知可安全重试）记录入 ui_review/v3.76；本轮不部署"},
                {"ver": "3.75", "date": "2026-10-04", "note": "详情抽屉连续处理与导航（v3.75，不重复建立鉴定/样本链模块）：抽屉新增固定头（编号/学名/DetStatus 状态徽标/模式星标/回收站标记 + 上一条·下一条[显示对方编号] + 范围内位置「第 i/N 条」）；上一条/下一条范围来自当前明确记录集合或查询计划（表页进入=当前搜索+列筛选+预设+排序+回收站；深链/无集合=全表自然排序），服务端新增 GET /api/rows/<t>/<rid>（单行读取：无权限 403 与其他 API 一致且响应不含记录内容、不存在 404、已删除返回 deleted 标记）与 GET /api/rows/<t>/<rid>/neighbors（与列表同一 preset/列筛选/q/两段排序语义全量定位，跨页按全量排序取相邻——不用当前页行号冒充全量位置；范围外返回 in_scope=false+建议定位首条）；范围外明确提示并一键「定位下一条」；hash 深链 #/r/<表>/<rid>：刷新/浏览器前进后退/有权限用户直接打开均可用（hashchange 驱动抽屉切换，URL 不含令牌）；无权限 403、不存在 404、已删除带标记打开；处理返回原任务位置：关闭抽屉清 hash 并回网格定位原行（滚动居中+高亮闪烁）；修改历史改为抽屉视图栈（详情→历史→←返回，恢复详情滚动位置与焦点），历次鉴定/附件/样本链 modal 关闭即返回且抽屉滚动位置保留（openModal/closeModal 钩子）；上一条/下一条与跨模块均走现有未保存守卫（网格编辑 leaveGuardConfirm 三选一结果明确后切换；抽屉字段编辑中阻塞并提示先提交或 Esc 放弃）；键盘 PageUp/PageDown（输入框/弹窗/组合输入边界内不触发）；test_query_model 增邻接定位/边界/范围外建议/单行/404/已删除标记/无权限深链七断言；全套 34 套 743 项 rc=0 零泄漏；浏览器验收（筛选后连续 20 条不重搜、历史返回恢复、深链刷新重开、未提交守卫阻塞-Esc 放行、关闭清链回网格、浏览器后退回上一条）记录入 ui_review/v3.75；本轮不部署"},
                {"ver": "3.74", "date": "2026-10-04", "note": "可视化配置编辑器（v3.74，复用查询模型列条件/字段目录与数据字典/export_templates columns_def/collab allowed_cols 契约，全程无 JSON）：①动态项目筛选器「字段—条件—值」——条件类型与查询模型真正允许的组合一一对应（属于以下值 in[支持（空）]/包含文本 contains/为空 empty/不为空 notempty）+关键词行，实时匹配计数（新增 GET /api/projects/dynamic-count，与固化成员 from-dynamic 完全同一匹配语义，未知条件/不存在列的 400 具体到列名与条件并显示在对应行旁）；「从当前筛选创建动态项目」入表页管理菜单（预填当前搜索词与列条件）；动态成员随数据变化在构建器中明示。②模板列编辑器——字段搜索覆盖中文名/原字段名/定义（数据字典+字段目录）；列顺序 ↑↓+拖拽；类型控件全中文名（原文/标识编号/学名/日期三分量[年月日字段+缺失表示]/数值[小数位+缺失]/复合字段[地点组合/材料描述/文本连接/取第一个非空+组成字段增删]）；不支持的结构（未知类型/未知选项键）以「未支持配置（原样保留）」卡片呈现并可原样往返，高级 JSON 与基础模式互转时校验转换、非法 JSON 控件旁报错且输入保留；接入模板库编辑器与导出向导高级配置。③协作可编辑列勾选列表——可搜索，身份字段（行身份/回传核对）与关联只读字段（采集事件带出）自动禁选并注明原因，选中结果为可读标签（不再手填 Notes,DetBy,DetDate）；接入协作包手动建包与向导式筛选建包两处。test_projects 增动态计数四断言；全套 34 套 735 项 rc=0 零泄漏；浏览器验收（无 JSON 建动态项目/勾选限定协作列/列编辑器类型控件与未支持往返/控件旁报错）记录入 ui_review/v3.74；本轮不部署"},
                {"ver": "3.73", "date": "2026-10-04", "note": "统一导出向导（v3.73，复用导出模板/生成器/稳定查询计划/项目冻结版本）：表页导出菜单收敛为「导出向导」——范围三来源（勾选记录/当前筛选全部[resolve=ids 计划固化，命中 0 与超上限明确报错]/全表[必须主动选择，绝不因输入为空悄悄退回]）；项目页新增「导出」入口——项目确认成员（已移除/排除不导出）+ 数据状态 当前/指定冻结版本（新增 GET /api/projects/<pid>/versions）；顶部固定范围摘要（所属表·项目/范围来源/原记录数/输出行数/模板修订/当前冻结状态/格式）；模板步收藏与适用模板优先+搜索，字段编辑收进高级配置（仅本次生成不改动已存模板）；检查步问题分级（阻断[需显式确认才可生成]/需确认/信息）按记录与列定位，缺失统计全量逐列显示具体缺项字段与输出策略（不笼统宣称合格）；预览/检查/生成同一份范围+模板+版本+格式（任一变化即作废预览并禁生成直到重检）；上一步不丢输入、生成失败可重试、进行中幂等锁定按钮；完成面板含文件名（服务端按模板名 Content-Disposition UTF-8）/输出行数/生成时间/再次下载；preview 响应新增 severity 与 missing（input_rows 区分原记录数与展开后输出行数）；test_export_templates 增分级/缺失统计/冻结版本端点/404 断言；全套 34 套 731 项 rc=0 零泄漏；浏览器验收（筛选/项目进入无需重贴编号、预览=下载同配置、返回保真、失效禁生成）记录入 ui_review/v3.73；本轮不部署"},
                {"ver": "3.72", "date": "2026-10-04", "note": "可复用实体选择器（v3.72）：服务端 POST /api/resolve/entities——输入编号/名称列表+允许表范围，逐条分类 matched（唯一精确/唯一前缀自动）/multi（同编号跨表、同表重复、前缀多条、名称检索——必须人工确认绝不默认取第一条）/not_found/duplicate_input/out_of_scope（存在于可见但范围外的表仅提示中文表名不含内容）；用户不可见的表完全不参与解析（查无结果与无权限不可区分，不泄露存在性）；匹配携带稳定 tname+rid，提交仍由各业务端点核对身份/权限/行版本。前端 renderEntityPicker 组件——批量粘贴（换行/Tab/中英文逗号/分号）、分组呈现（已匹配/多候选/未找到保留原输入/重复/不在范围）、多候选点选人工确认、已选计数/逐项移除/清空/身份只读查看、输入或表范围变化立即失效旧解析（解析未完成禁止宿主提交）、接收表格勾选与项目成员来源；接入：模板导出行范围（替换粘贴记录 ID）、项目纳入成员（含从其他项目导入成员）、文献引证（预填带出）、流转（选择器覆盖标本/借入台账，跨表同号人工确认所属表，替代原所在表下拉）、实验孔位关联标本、协作包手动建包、文献页 Material examined；test_entity_resolver.py 十一组（精确/跨表/前缀/名称/重复/未找到/范围外/权限不泄露/边界）入套件；全套 34 套 724 项 rc=0 零泄漏；浏览器验收：Excel 粘贴全分支分组、跨表候选点选、失配修正、失效禁提交、项目成员导入、孔位内联、viewer 权限不泄露（验证记录入 ui_review/v3.72）；本轮不部署"},
                {"ver": "3.71", "date": "2026-10-04", "note": "显示与操作流程优化（纯前端编排，服务端零改动；复用查询计划/稳定记录引用/唯一约束/幂等请求/权限，不改科学事实、空值不补造、不自动建关系）：详情抽屉新增「被引文献」区块与从抽屉直接登记引证（预填记录 ID/编号，重复引证唯一约束友好拦截——补齐 v3.59 双向入口的标本侧）；表格导出菜单新增「协作编辑包（当前筛选）」与「生成 Material examined（当前筛选）」——经 resolve=ids 查询计划固化行集合（0 命中/超上限明确报错，固化后筛选变化不扩大范围），不再手工粘贴编号；项目页 prompt() 原生弹窗替换为应用内模态（冻结命名/成员备注+任务状态）；孔位图内联编辑（占孔表单嵌入板弹窗、逐孔原地刷新不再整弹窗重开）；协作包回传合并后一键刷新列表；模板编辑器切换目标表刷新字段联想；浏览器典型任务验证 T1-T5（筛选建包/材料文本/抽屉引证与重复拦截失败恢复/项目模态/孔位内联）截图与记录入 ui_review/v3.71/；回归全套 33 套 711 项 rc=0 零泄漏；本轮不部署"},
        {"ver": "3.70", "date": "2026-10-04", "note": "界面缺口清查补全（审计：服务端全部路由 vs 前端调用差集，修正「只有 API 没有界面」的同类问题）：四大页面——「流转与借出」页（借出台账/近期流转/登记流转对话框[编号解析/类型联动/移盒盒孔/借出期限/force]/行级撤销 + 详情抽屉流转记录区块与登记入口，新增 GET /api/movements 全局台账）；「实验板」页（批次/送测版本、板与孔位图 96/384 占孔[预览→提交/覆盖确认/标本编号关联]、混池与成员[同池 UDI 冲突提示]、条码对源序列、定量 QC；补 GET batches/pools/barcode-pairs/qc 四个只读列表端点）；「文献与引证」页（文献库/新增无 DOI 老文献/材料清单双向/登记引证冻结版本/Material examined 文本生成[material-examined 新增 rids 参数]）；「项目工作集」页（固定/动态、纳入成员编号解析、项目内备注与移出留痕、动态固化成员、冻结受保护版本）。七处小接线——统计页口径元数据+三种计数口径+空间精度三级+图表数据下载；变更历史「校验审计链」+checkpoint 导出；首页健康卡「立即备份」；数据体检工具菜单新增 字段目录/合并重复采集事件[merge-preview 补 base_versions]/稳定引用体检与审阅式迁移/字段类型扫描/刷新 WSC 名录（任务进度+取消）/附件诊断/压缩数据库；导出菜单「打印清单（留痕）」接 print-log；浏览器端到端烟测：登录后逐页核验新侧边栏组/流转台账与登记弹窗/孔位图占孔闭环/文献列表/项目纳入成员闭环/模板导出起始模板标记/统计口径/审计链校验 全部通过；全套隔离测试 33 套 rc=0 零泄漏"},
        {"ver": "3.69", "date": "2026-10-03", "note": "模板导出与协作编辑包界面补全（v3.66/v3.62 此前仅有 API，无前端入口）：侧边栏新增「成果输出」任务组——「模板导出」页展示三类内置通用起始模板（系统发育样本信息表/演化分析元数据表/分类学 Material examined，明确标记 rules_source=personal 未核实期刊规范·通用/作者自定义）与个人/团队模板列表（规范核实状态三档徽标、期刊元数据、收藏/复制/修订），模板编辑器含列定义网格（字段联想/来源类型/白名单复合函数 JSON）与 preview 前 20 行+全量问题检查、generate XLSX/CSV/TSV（新增 rids 行范围收敛）；「协作编辑包」页补齐文件闭环——新增 GET /api/collab/packages 列表、POST /api/collab/resolve 编号→稳定 rid（缺失明示）、GET 工作簿下载（编号列+可编辑列当前值，下载留审计）、POST receive-file 回传 xlsx/csv/tsv（表头须含编号列，编号→rid 服务端解析，未命中编号逐条明示，文件值经 clean_val 归一后走同一三方合并）；同字段双方修改审阅支持 force=1 回传值为准（逐处留审计 _forced），行版本不符/越范围/不在包内 force 不放行（防丢更新）；编号列禁止入可编辑范围；test_collab 增包列表/解析/工作簿往返/CSV 回传/行版本保护不被 force 绕过等项、test_export_templates 增 rids 收敛，全套 32 套 rc=0 零泄漏"},
        {"ver": "3.67", "date": "2026-10-03", "note": "预发布验证修复（正式数据 35,853 条实测）：模板导出在真实数据上 500——数值格式化遇非数字文本（月份区间 25-26、自由文本）int() 崩溃，改为原样保留不猜不崩；日期格式化同类问题（非数字月/日原样）；确认修复后在预发布副本上模板导出全量成功、16 项断言全过"},
        {"ver": "3.66", "date": "2026-10-03", "note": "自定义/期刊模板导出（SCHEMA 3.16，export_templates.py，先核对实现复用字段目录/查询计划/版本/权限/导出代码）：export_templates 表（scope=personal|team、团队修改生成 template_revisions 修订、favorite 收藏、project_default_for 项目默认、template_kind=phylo|evolution|material|custom 三类通用起始模板[未核实期刊规范明确标记通用/作者自定义 rules_source=personal|lab|official]）+ journal/article_type/author_guide_url/rules_checked_at 期刊元数据；字段绑定稳定身份（field/field_catalog field_id）输出别名只影响导出列名；复合字段白名单函数（join/locality/material_desc/first_non_empty——白名单外 400 拒绝不执行用户代码/SQL/脚本）；格式化只影响导出：日期三分量保留部分日期（YYYY/YYYY-MM 不补造月日）、缺失值 dash/unknown/custom、数值小数位、复合空项自动省略；行身份显式声明 record/one_to_many=expand|aggregate|representative（禁止静默取第一条/自动去重）；POST preview 前 20 行+全量问题检查（重复标识/来源断链/渲染错误）+rids 收敛；POST generate XLSX/CSV/TSV + 独立伴随说明（README 不强制插入数据表）；公式注入防护（=+@ 前缀撇号）；预览与实际文件一致；CSV/TSV 明确不保留富文本；test_export_templates.py 十六项（顺序别名/前导零/公式注入/部分日期/学名/多文库/权限/模板复制修订/预览=文件/TSV 富文本说明）入隔离套件，全套 30 套 rc=0 零泄漏"},
        {"ver": "3.65", "date": "2026-10-03", "note": "范围一致性与统计口径校正：api_export 重写范围解析——ids 显式集合/搜索 q/列筛选 filters/预设 preset 按统一规则传递（可叠加），ids 无效值/行不存在/空集合明确报错不静默扩大，cols 严格按用户指定顺序输出（_write_sheet 按请求顺序 pick，不再按表定义顺序），未知列/空列集合 400；项目冻结重写——只冻结项目确认纳入的成员（排除 status=excluded 与有 exclude_reason 的行），支持多表成员（每表独立受保护版本），不再冻结整张 specimens 表；冻结时采集地/分类依赖【内容】随版本冻结（snap.dep_content），deps 摘要只作校验，修改当前采集地后旧冻结结果不变；project_members 加 exclude_reason 列、add member 改 upsert（移除后重新纳入）；stats_scope 语义校正——「最终种名已填记录数 named_records」与「已记录个体数 known_individuals[数量字段求和]」分开，明确全 0 不算未知（zero_qty 单独计数）、部分缺失显示完整性不推断总数、无事件号记录单独统计 no_event_records 不当新事件；新增 test_export_ranges.py 十三项验收（搜索属导出仅该范围/列顺序 C·A/未知列报错/冻结仅 2 条成员/依赖内容冻结/修改采集地旧冻结不变/全0≠未知/无名有量计入个体数/无事件单独统计）+实际打开文件核对；更新 test_projects.py 冻结语义；全套 30 套 rc=0 零泄漏"},
        {"ver": "3.64", "date": "2026-10-03", "note": "偏好历史与恢复（完善 v3.63 局部合并协议的可审阅找回方案）：每次保存/重置前自动留存最近 10 份偏好快照（prefs_history，含完整 views/colorder/colw 等——历史已丢失且不在快照中的视图仍无法凭空恢复，但新丢失可审阅找回）；GET /api/prefs/history 列出快照时间/标签/包含键/视图预览；POST /api/prefs/restore-history 恢复指定快照（恢复前自动留存当前状态）；POST /api/prefs/reset 恢复全部偏好（需 confirm=RESET，重置前自动留存快照，范围=hidden/colw/views/colorder/freeze/view_sel/density 全部恢复默认）；修复 _snapshot_prefs_history 曾用旧库状态覆盖合并结果的 bug（快照改为基于传入的合并后 prefs 只追加 history 键）；GET/PUT 回执过滤内部 history 键、回执带 prefs_version（乱序保护：服务器确认的最后更新单调递增）；前端用户菜单新增「偏好历史（找回）」与「恢复全部偏好」（确认对话框注明范围与找回途径）；test_prefs_merge.py 扩至十六项（重置需确认/历史含 views/找回/乱序 version 单调递增）入隔离套件，全套 29 套 rc=0 零泄漏"},
        {"ver": "3.63", "date": "2026-10-03", "note": "偏好局部合并协议（修复列宽保存丢失视图的确认路径）：PUT /api/prefs 改为 PATCH 语义——未提供的顶层键保持原值（复现「保存 views→setColw 只传 hidden/colw→views 被清空」的丢失路径并修复）；嵌套表级键（hidden/colw/colorder/freeze）按 {表名:值} 浅合并（两标签页更新不同表的列宽互不丢失）；删除必须显式 __delete__（支持深层路径如 colw.specimens.MHBU #——「删除一个保存视图」「重置当前表布局」分别实现）；未知顶层键拒绝（防旧客户端清空未知设置）；prefs_version 递增+回执基于服务器实际保存内容（乱序保护）；前端统一偏好保存队列 savePrefs（防抖+失败重试+乱序丢弃+失败保留本地待保存状态轻量提示），全部 5 处整包 PUT 调用点收敛到统一路径；test_prefs_merge.py 十项（丢失路径复现修复/表级合并/未知键拒绝/显式删除/版本递增）入隔离套件，全套 29 套 rc=0 零泄漏"},
        {"ver": "3.62", "date": "2026-10-03", "note": "协作编辑包（SCHEMA 3.15，collab.py）：edit_packages（pkg_id/创建者/表/列白名单/状态）+ edit_package_rows（稳定记录标识/导出时基础值/基础 row_version）；权威基线存服务器——Excel 只带包 ID+数据（不含令牌/身份/版本，隐藏列不被信任）；POST /api/collab/packages 创建 + GET 详情 + POST receive 三方比较回传（基础值 vs 服务器当前值 vs 回传值——非冲突自动应用、同字段双方修改 409 审阅、行版本不符/不在包内/列越范围均明确冲突）；删行不代表删除记录、新增需导入流程；viewer 回传 403 不继承管理员权限；包收回后阻止自动合并；同包重复回传幂等；审计含 包创建者/实际提交者/协作者署名未认证；POST revoke 收回；test_collab.py 十项（基础值/非冲突/服务器已改/不在包内/列越界/权限/收回/幂等/审计署名）入套件，全套 28 套 rc=0 零泄漏"},
        {"ver": "3.61", "date": "2026-10-03", "note": "项目工作集（SCHEMA 3.14，projects.py）：projects 表（名称/kind=fixed|dynamic/purpose 目的/owner 负责人/filters 动态条件/stage 项目内阶段）+ project_members 表（稳定记录标识引用主库不复制事实；join/remove 原因/时间/操作者；project_note/task_status/sort_order/exclude_reason 与主库字段分开存 member 行）；一个记录可属多个项目；移除成员不删主库记录；动态工作集 from-dynamic 可『纳入本次研究』固化成员版本；项目内备注验证不写主库（records.data 未改动断言）；详情按接收者可见性过滤（无权限成员不返回详情）；冻结导出复用版本机制（protected 版本+deps 摘要，不走 HTTP 回环直接调内部函数）；POST /api/projects CRUD + members/note/from-dynamic/freeze；新增 test_projects.py 十项（多项目归属/备注不污染主库/移除不删/动态固化/受限可见性/冻结）入隔离套件，全套 27 套 rc=0 零泄漏"},
        {"ver": "3.60", "date": "2026-10-03", "note": "统一统计/地图范围表达（stats_scope.py）：GET /api/stats/enhanced 明确范围元数据（统计对象=第一可见主表、分母=未删除行、排除条件=回收站+无权限表、可见表清单、类群范围、查询指纹）；三种计数口径可切换（标本记录数/独立采集事件数[按事件号去重，无事件号按行独立计]/已知个体数[FinalSpeciesName 非空]），每种附定义、分母与排除条件；缺少个体数量不当 0（unknown_quantity 指标如实呈现）；不做综合得分；空间精度三级（precise=有经纬度[含事件源关联回退]/province=仅知省份按省名区域聚合不用省会坐标冒充/unknown=完全未知不计入地图）；GET /api/stats/chart-download 图表数据 CSV 下载附数据子集、指标定义和排除数量；Latitude1/Longitude1 经关联解析（事件表值回退）；新增 test_stats_scope.py（范围元数据/三种口径/同事件多标本去重/空间三级/省份标签/隐藏表分母/图表下载）入隔离套件，全套 26 套 rc=0 零泄漏"},
        {"ver": "3.59", "date": "2026-10-03", "note": "文献引用与材料引证（SCHEMA 3.13，biblio.py）：bibliographic_reference 表（稳定标识/DOI 可空/题名/作者/年份/页码图版/期刊——无 DOI 老文献保留完整书目不虚构标识）；material_citation 表（文献→标本引证：原文材料引用/原文材料编号含历史编号/证据附件/冻结标本版本/采集信息摘要——原文引用与当前接受名并列保留，引证时冻结标本版本）；模式标本身份扩展（type_basis 依据/type_confirmed_by 确认人/type_confirmed_at 确认时间/type_status_source 状态来源[文献确认/标签记载/待核实/历史导入]——术语按实验室规范配置，程序不自动裁定命名学地位）；POST /api/refs-bib 创建文献/POST cite 登记材料引证（唯一约束防重复）/GET materials 从文献看材料/GET citations 从标本看被引文献（双向）；api_type_status 读改写+行版本条件更新+invalidate；新增 test_biblio.py（无DOI/多论文引用/旧编号别名/双向入口/模式争议/清单后修改）入套件，全套 25 套 rc=0 零泄漏"},
        {"ver": "3.58", "date": "2026-10-03", "note": "实验批次/板/孔/混池/条码最小关系模型（SCHEMA 3.12，lab.py 注册于 app；先核对实际流程：udi_i5/udi_i7 各 96 行、8bp ACGTN、无非法碱基与重复 UDI#——阈值与组合策略来自实测与实验室规则配置 LAB_BARCODE_RULES，不自造通用标准）：experiment_batch（送测清单绑定批次版本 sent_version 递增，已发送后更改保留修订）/ plate（规格配置化 96/384）/ plate_well（统一行列坐标，同板同孔唯一索引，对象类型 sample/control/blank 与异常状态清楚记录）/ pool+pool_member（同池 UDI 组合冲突 409 池作用域校验，跨池合理复用允许）/ barcode_pair（i5/i7 保存原序列不覆盖、方向、来源；导出转换绑定 config 仪器/软件/试剂配置）/ qc_measurement（定量多次测量：值/单位/仪器/方法/采用理由，单位不明不自动换算）；板位图 API 空孔/样本/对照/空白+异常状态（文字+符号+颜色共同表达）；批量占孔先预览 assign/overwrite 后事务提交；方向/长度/碱基校验明确报错；test_lab.py 十九项入隔离套件，全套 24 套 rc=0 零泄漏"},
        {"ver": "3.57", "date": "2026-10-03", "note": "共享来源修改影响预览（先覆盖 events）：GET/POST /api/events/<rid>/impact-preview 按稳定引用（事件号）计算受影响记录——有权限的表给出数量/样例/连带展示字段，无权限的表只提示「存在受限依赖」不泄露标识与数量；变更按语义分类（纠正拼写=宽松归一相同/展示格式=官方地名归一相同/修改源事实），事实变更标记高影响；事件更新的审计记录源操作与影响范围（受影响表计数+受限表清单+「动态展示变化不逐条伪造编辑」），响应携带影响摘要；提交以 base_version 验证计划仍有效（并发源修改/标本改引用 → 409 重新预览）；普通备注改动不高影响不添烦；已发布/受保护版本与历史导出不随活数据静默改写（deps 摘要可比对、关联列排除，来源更新后可重新生成新版本）；修复 SQL 字符串内未转义引号吞掉占位符的问题；新增 test_impact_preview.py（多关联计数/变更分类/权限受限/并发计划失效/已发布提示）入隔离套件，全套 24 套 rc=0 零泄漏"},
        {"ver": "3.56", "date": "2026-10-03", "note": "字段语义目录（field_catalog.py，按业务含义建档不按相似字符串合并）：八项核心字段（标本编号/采集事件号/DNA 凭证/文库编号/暂定名/最终名/采集人/保存条件）定义稳定 field_id、各表内部键、显示中英文名、定义、例子、数据类型、单位、空值状态、原始/推导属性、来源、可编辑条件与兼容表头（表作用域）；写明 uce.Sample Code=建库样本的 DNA 提取凭证（界面名 DNAVoucherID）与 dna_extract.Specimen # 的具体意义（含合作方说明文字非编号）；GET /api/field-catalog 带使用情况（所在表+关联体检规则）；导入映射复用目录：兼容表头唯一命中自动映射、碰撞停止自动映射交人工（旧表头如 MHBU_Number→Specimen # 实测）；导出数据字典 sheet 增补 field_id/英文名（列设置/详情/录入/历史复用同一元数据——视图/列序存内部键，改显示名不改数据身份、不使保存视图失效）；新增 test_field_catalog.py（目录完备/Sample Code 与 Specimen # 语义/旧表头导入/别名碰撞/视图与历史按内部键）入隔离套件，全套 23 套 rc=0 零泄漏"},
        {"ver": "3.55", "date": "2026-10-03", "note": "标本流转事件（SCHEMA 3.11，最小范围=跳蛛盒位与借出台账）：经生产数据核实「记录≠个体≠管」——数量列（雄/雌/亚成/幼）表明一条记录可含多个体，管数无独立字段，故模型以「记录 + 数量（可未知）」表达流转，不假设一对一；movements 表记录 入库/移盒/组织取样/借出/归还/转赠/损耗（对象、数量[可未知]、起止位置、对方、期限、原因、凭据、操作人、撤销链接）；POST /api/movements：移盒事务内盒孔唯一性双重检查（应用查询+部分唯一索引 idx_records_hole 兜底，并发同孔 409 明确提示）并以行版本条件更新兼容字段（Box #/Position 仍为权威存储）；借出记录对方/期限/数量（未知保持未知不当 1/0），未归还重复借出阻止；归还允许部分数量，GET /api/loans 借出台账显示仍在外数量（未知=null）；借出未归还时移盒 409（force 可带原因）；撤销有后续依赖流转时 409 阻止并提示生成纠正事件（不回写覆盖现状）；历史存量不伪造移动（无事件行 movements 为空，迁移现状即现有字段值）；GET /api/rows/<t>/<rid>/movements 行级流转查询；新增 test_movements.py（同时占位/部分归还/借出后移位/撤销冲突/迁移不伪造）入隔离套件，全套 22 套 rc=0 零泄漏"},
        {"ver": "3.54", "date": "2026-10-03", "note": "测试隔离与拒绝运行保护 + 性能基线 + 长任务化：testenv.py 强制测试默认临时环境（独立库/附件/端口），检测到生产 data.db 路径或 8000 固定端口即拒绝运行（显式 SPM_ALLOW_PROD=1 才放行），run_tests 在环境变量就绪后复查；高价值回归已覆盖并映射（并发保存/选择性撤销/精确恢复/导入计划/统一校验/权限泄露/备份损坏/附件一致性 21 套）；性能基线 perf_baseline 文档（合成 1 万标本：500 行渲染 p50 165/p95 222ms、保存 93/113ms、恢复计划 294/365ms、稳定计划 155/168ms；证据=全表 JSON 扫描为首批优化候选、invalidate 整表失效放大器；SQLite 保留，迁移需并发/容量证据）；长任务注册表 tasks.py（阶段进度=真实阶段不伪造、取消边界=阶段切换点、幂等键防重复创建、结果/错误保留），名录更新接入 /api/tasks（查询/取消，管理员），导入/SRA 批量后续接入；服务拆分保持接口兼容（validation/checkup_rules/lineage/backup/field_registry 已独立模块，不一次性改行为）"},
        {"ver": "3.53", "date": "2026-10-03", "note": "版本元数据与压缩快照（SCHEMA 3.10，实测选型）：先在生产只读副本量测——66 个版本共 373MB、最大 9.2MB；gzip 压缩比 35×、解压 42ms；相邻版本差异行多于总行数（导入重写）→ 增量策略实测无收益被否决，采用「压缩完整快照 v3 格式」（gzip+base64 信封带 fmt/enc/SHA-256 校验和/字节数，旧格式 legacy 照常读写）；versions 新增 category/description/protected/source_op_id/schema_ver/tables_rev/rules_ver/content_sha256/size_bytes/format/deps（分类/采集地内容摘要，发布版本冻结依赖引用）；保存命名版本（如「论文 A 投稿前」）可带描述与保护标记；受保护版本普通删除 409（管理员 force 例外）；PATCH 维护元数据；留存政策 GET cleanup-plan（dry-run 候选清单+可回收容量，受保护/发布/手动永不入选）与 POST cleanup-apply（管理员单事务执行，审计留痕）；快照损坏返回 400 明确校验错误（解压失败与校验和不匹配分开报告）绝不假恢复；POST /api/admin/vacuum 数据库压缩整理仅限计划维护（管理员 confirm=VACUUM，建议先备份）；新增 test_version_meta.py（元数据落库/依赖摘要/v3 解码与恢复/压缩损坏/legacy 读写/保护删除/清理计划）入套件，全套 21 套 rc=0 零泄漏"},
        {"ver": "3.52", "date": "2026-10-03", "note": "详情抽屉渐进增强与附件生命周期：抽屉顶部统一身份区（表名/业务键/行 ID/版本/复核状态/模式标本）；关联回退明确标注——来源记录缺失但本行留有导入时缓存值的单元格显示「历史缓存」徽章并悬停说明「历史缓存，关联未匹配」，绝不标成当前来源值；SCHEMA 3.9 附件扩展 sha256/mime/拍摄或来源说明/deleted·deleted_at；上传改 暂存→校验（摘要）→发布（原子改名，附件行与文件同批落库），失败自动清理暂存与半发布文件可重试；删除改软删除（文件保留可还原，30 天留存期后每日维护物理清理），新增还原端点；下载/预览按真实归属表鉴权并拦截软删项；跨表共享附件标注实际归属（src_table/src_rid，删除/还原只作用于归属那份，对端不显示软删项）；新增 /api/attachments/diagnostics 缺失与孤儿文件诊断（管理员）；新增 test_attachments.py（上传摘要/重复上传/软删还原/跨表归属/丢文件诊断）入隔离套件，全套 20 套 rc=0 零泄漏"},
        {"ver": "3.51", "date": "2026-10-03", "note": "导出统一范围表达与 Excel 契约：当前表/全库/历史版本/审计清单四类导出共用同一范围表达（表、类群、搜索、过滤、选中记录、列、数量、导出时点、版本；范围三模式=本页/筛选全部/勾选记录，由调用端显式给出，不继承不明确状态）；api/export 支持 q/preset 与列表查询同参，README 记录范围元数据（不含令牌/口令）；每次导出附带 README（导出人/服务器时点/应用·结构·名录版本/范围/数量口径/缺失值口径/关联回退说明/业务编号说明）与 数据字典 sheet（字段含义/类型/关联列标注）；业务编号按文本保存保护前导零，=+ - @ 开头文本加撇号前缀防公式执行且原文可追溯；历史版本导出排除关联列（快照不含关联表数据，避免用今天的采集地信息冒充历史时点），README 说明关联回退；新增 /api/metrics 六项计量（标本记录数/已知个体数[FinalSpeciesName 非空]/数量未知记录数[四类数量列全空]/提取数/文库数/不重复凭证数[提取∪UCE 大小写不敏感去重]），每项带分母与排除条件并随首页展示；修复 README 块曾误注入 export/all 与 edits/export 的构建错误；新增 test_export_scope.py（同版本重复导出一致/前导零/公式防护/部分日期原样/多建库不合并/ids·q 范围/历史版本无关联列/敏感信息）入隔离套件；全套 19 套 rc=0 零泄漏"},
        {"ver": "3.50", "date": "2026-10-03", "note": "样本链关系引擎（lineage.py）与关系字典：先对生产库只读核查 DNAVoucherID/Specimen #/Sample Code/Sample Key/UCE library Code/SRR 的实际语义与重复（提取主键 1954 全唯一、UCE 同凭证重复建库 100、同 accession 多行、外部说明文字非编号等），输出 关系字典_样本链_2026-10-03.md（未知含义不凭列名猜测）；保留 UCE—DNA 提取按凭证关联与标本—采集地规则，不恢复已解除的标本—UCE 直接关联；GET /api/lineage/voucher/<vid> 呈现 提取→同标本其他提取（一对多实验过程）→文库→SRR accession 与标本侧引用——引用只建立在精确值相等或已确认别名上；UCE 未匹配五分类（可追溯/编号差异候选/待补历史记录/外部来源/未明，实测外部来源≈1249），候选绝不自动选择，POST /api/lineage/alias 人工确认别名写入 number_aliases（原文/规则/确认人/批次留痕可撤销），确认后别名参与匹配；详情抽屉新增「样本链」面板（UCE/DNA 提取表），区分「记录可追溯」与「暂未匹配」；SRR 显示样本号/物种/匹配依据（缓存与故障提示沿用 SRA 自动匹配通道）；跨表连接不暴露无权限实体（伪蝎账号查 JXZ 凭证节点为空）；新增 test_lineage.py（重复提取/重复建库/外部来源/大小写差异候选→确认/非法别名/跨类群访问）入隔离套件，全套 18 套 rc=0 零泄漏"},
        {"ver": "3.49", "date": "2026-10-03", "note": "统一查询状态模型：查询表达=表+搜索词+列筛选+预设+排序+方向，服务端为每次查询计算指纹 query_digest（计数/分页/导出/批量共用同一表达）；resolve=ids 把当前筛选解析为带版本的稳定计划（显式行集合+样例+计数+上限 5000 截断标记，同一代码路径），批量操作与「导出筛选全部」先固化计划——之后筛选变化或新记录加入都不会扩大操作范围（计划指纹与当前不符时明确提示可重新固化）；选择三模式：本页勾选/明确记录集合/筛选全部（经计划固化）；请求取消（AbortController 取消上一笔未完成查询）+ 序号防乱序 + 查询失败独立呈现（重试按钮，绝不把请求失败显示为空表）；搜索标注范围（全部列含关联带出），编号查询精确/前缀命中优先排序；筛选条含命中数/全表数/可清除条件标签/分享视图入口；视图分享只分享定义（筛选+列集合+列序+列宽），导入方按自身权限与可见列重算生效；无结果/查询失败/无权限三种状态分开；新增 test_query_model.py（指纹一致性/编号优先/计划稳定性[新行不扩大]/ids 导出/权限拒绝）入隔离套件，浏览器实测慢请求乱序（晚到 2s 的旧响应不覆盖新查询）；全套 17 套 rc=0 零泄漏"},
        {"ver": "3.48", "date": "2026-10-03", "note": "编辑状态机（表格/表单）：草稿→提交→成功/失败/冲突/结果待确认 显式状态；提交中阻止同一编辑重复提交（状态位+确定性请求 ID=表|行|字段|新值|基础版本，重试复用同一幂等键，同键重发返回原结果并带 idempotent_replay 标记，同键不同请求 422）；成功以服务器值与版本为准并一次原位应用全部连带字段（属/科级联、复核状态等，不整表刷新）；失败保留本人输入与服务器旧值并提供 重试/复制草稿/放弃；响应丢失显示「结果待确认」并经 GET /api/idempotency/<req_id>（本人作用域）查询确认，绝不标记未保存后重复新增；Enter/Tab/方向键在提交结果明确后才移动（提交中排队不重发）；中文输入法组合输入期间不触发提交与移动；切表/关详情/登出/刷新遇未提交值给出 保存/保留草稿/放弃 三选；草稿按 账号|表|记录|字段|基础版本 隔离仅存本机（留存 7 天、不上传），恢复前核对服务器值且仅同基础版本可恢复、陈旧草稿绝不自动覆盖，登出清除本账号草稿、用户菜单可手动清除；网络不可用保留草稿恢复联网核对后再提交，不做无提示后台离线同步；多行备注 textarea 内 Enter 换行（既有设计）、Ctrl+Enter 或失焦提交；新增 test_edit_state_machine.py（幂等重放/422/查询/用户隔离/失败不留痕）入隔离套件，另以浏览器实测 输入法/连续Tab/断网重试/切表守卫/两账号隔离 全部通过"},
        {"ver": "3.47", "date": "2026-10-03", "note": "任务视图与表格语义（不改业务数据）：tables_def 新增 VIEWS 预设任务视图（跳蛛鉴定/采集信息/保存与盒位/DNA 提取/UCE 文库/借入管理×2），每条定义列集合与顺序、固定列、默认排序、解释文字与权限（沿用本表三态权限），经 /api/meta 下发；工具栏「任务视图」下拉一键应用/恢复整表默认；固定业务主键（视图可加属/种列）横向滚动仍可见身份，冻结列表头/表体统一宽度、层级正确（表头冻结 z12 > 行号 z11 > 表体冻结 z7）；表头可拖拽重排列并记忆；隐藏主键/固定身份列需明确确认；「列显示→显示全部」升级为恢复默认（清列序/固定）；单元格语义：缺失以「—」+悬停说明表示（不当作 0），学名按字段语义斜体（属名+种加词斜体，cf./aff./sp. nov. 等限定词与命名人正体），cf./sp./aff. 显示「待确认」文字徽章、日期分量越界显示「错误」、事件失配显示「失配」，关联列表头带来源图标；长文本默认单行省略、详情抽屉展开；数字右对齐；我的视图升级为保存筛选+列集合+列序+列宽+密度并整体还原；刷新后恢复滚动/勾选/焦点位置；无数据/无匹配（含相近建议）/无权限三种空态分别呈现；清理历史合并错位的搜索建议代码块；1366×768 与 800px 窄屏、500 行、65 列、长文本、冻结列叠加截图核查"},
        {"ver": "3.46", "date": "2026-10-03", "note": "界面重构（不改业务数据）：CSS 设计 token 体系——语义色（主动作/次动作/风险/只读/关联/待确认/已保存）+ 字号/间距/圆角/边框/焦点环统一变量，保留低饱和灰蓝气质与 logo，提升次要文字与风险色对比度；主导航按任务重组为可折叠侧边栏（数据录入/质量与追溯/更多数据/系统，仅显示类群与逐表权限可见的内容），顶栏新增当前任务组/当前表面包屑；表格工具栏重组：新增保持主按钮，导入/粘贴归「导入」，导出/表头模板归「导出」，回收站/版本管理/列显示/SRA 归「管理」（菜单内置检索，低频功能可搜索发现）；状态一律文字+图标表达（可编辑/只读徽章、待确认/已保存），颜色仅辅助；只读账号的新增/导入禁用并显示可见原因（不依赖悬停）；删除确认改为具体文案（如「将「MHBU-ARA-00000001」移入回收站？」）；工具栏菜单点选后自动收起；存量自定义列宽钳制最小 84px 并修复冻结列几何（行号列不再覆盖 Specimen # 表头）；新增紧凑/舒适密度切换（记忆）；快捷键保留且输入框内不触发应用级撤销；全局键盘焦点环；1366×768 与 200% 缩放核查；提交工作台/表格/详情/导入/冲突五种状态截图审查（ui_review/）全部通过"},
        {"ver": "3.45", "date": "2026-10-03", "note": "数据体检/纠错/工作台统一规则注册表（checkup_rules，SCHEMA 3.8 issues 工单表）：24 条规则带 rule_id/定义版本/分类（阻断错误·事实待复核·缺失资料·格式待解析·允许的历史例外）/理由/适用表/修复方式，体检页、一键修复、工作台队列共用同一份定义；问题以 稳定记录标识+规则 去重（UNIQUE dedup），状态机 未处理/处理中/已解决/暂缓/例外（暂缓与例外必须写明理由），记录负责人/处理备注/时间/复核人/重开计数；复核扫描：已解决复发自动重开、暂缓与例外只记检出时间不重复生成工单、未再检出的未处理项按规则口径自动关闭（仅限扫描者可见表，不越权关闭他人范围的工单）；阻断错误按固定优先级置顶（重复事件号→非空失配→空业务键→危险日期→编辑冲突），缺失坐标/日期/事件进「资料完善队列」；不提供综合质量得分——按维度分别展示计数/分母/例外数，解释权在研究者；一键修复先预览 before/after/规则/影响，应用走统一写入（行版本条件更新、单批次审计、可撤销、修复行同步解决工单），歧义坐标默认绝不自动转换；工单列表支持定位/状态流转/认领并原位刷新；权限控制覆盖计数（按可见表隔离）、分派与修复（写权限）；新增 test_checkup_rules.py 十五组场景入隔离套件 rc=0"},
        {"ver": "3.44", "date": "2026-10-03", "note": "鉴定历史/证据模型：determinations 表按行累积历次鉴定（名称原文/规范接受名/属科级/cf.·aff.·sp. 限定符/鉴定人/日期/依据/人工确认 confirmed_by/名录版本 checklist_ver/名录收录状态 checklist_status[accepted·synonym·not_found·service_unavailable]/来源 source），新增/修改/批量/导入/校准各写入路径自动追加，详情抽屉可查看历次鉴定（GET /api/rows/<t>/<rid>/determinations）；「名录收录」与「复核状态」两维度分离：标本表新增 DetStatus（待鉴定/暂定/已复核/有争议，改为已复核自动登记确认人）与 DetLocked（人工锁定）；存量行一次性回填（source=backfill，一律暂定、确认人为空——不推定其经过专家复核）；服务不可用（名录文件缺失/读取失败/在线核对不可达）与「未收录」分开报告，FinalSpeciesName 在服务不可用时返回 503 明确提示而非 400 分类错误；鉴定变更自动复位「暂定」，人工锁定或已确认/有依据的鉴定不被后台覆盖；WSC 校准改为预览→逐条/成批接受（/api/wsc/sync_species 预览 + /apply，受保护行跳过并列出原因，失败保留旧名录版本及其时间），接受行可随批次撤销（撤销同步移除该批鉴定记录）；SCHEMA 3.7"},
        {"ver": "3.43", "date": "2026-10-03", "note": "字段注册表 field_registry.py 集中定义日期/坐标/保存浓度/个体数量的类型/单位/范围/缺失语义/解析与显示规则（VERSION 随规则递增与 raw 原文一起落库 field_raw 表）；日期三分量精度解析（完整日/年月/仅年/未知，闰日合法，月13/日越界拒绝，严禁补1月1日）；坐标 DMS/十进制/NSEW/半球解析（歧义如 30.03.965′N 置信度 ambiguous 入人工队列，范围检查只提示）；浓度 0.95→95 归一并 raw 留存；数量非负整数或「未知」不当 0；/api/fields/scan 只读扫描 + /api/fields/apply 人工确认写入 + /api/rows/<t>/<rid>/field-raws 原文端点；validation.validate_constraints 接线日期与数量规则"},
        {"ver": "3.42", "date": "2026-10-03", "note": "备份/恢复体系统一：启动/计划任务/管理员手动/部署前统一走完整恢复包（data.db 一致性快照 + uploads 附件 + manifest [完成时点/表计数/大小与 SHA-256/附件清单] + config_notes，不含口令明文，下载剥离会话与 API 令牌）；临时目录写入→integrity_check→原子发布，未完成 .tmp 不进可恢复列表；附件一致性窗口（备份期间暂停附件写入/删除）；恢复演练升级：独立临时目录恢复→integrity/必要表/manifest 计数/附件摘要→临时隔离实例验证登录与查询，测量实际恢复耗时并对比 RTO 目标；损坏库/漏附件/半完成/磁盘不足/旧结构迁移均返回失败原因（绝不 integrity 失败仍 ok）；留存数量/容量上限/异地副本目录/RPO·RTO 目标移入 config（未配置异地时如实标注）；新增 test_backup_restore.py 八场景"},
        {"ver": "3.41", "date": "2026-10-03", "note": "统一导入计划契约：Excel/粘贴/API 批量导入统一解析-规范化-校验-计划（_build_import_plan），预览生成不可变 import_plan_id（绑定文件摘要/映射/空值策略/规则版本/目标行基础版本），提交按同一计划执行不再重新猜测解析，计划过期或目标行变更 409 重新预览；逐行显示源行号/主键/动作/旧值新值/自动带出/错误警告；检查文件内重复主键、重复表头、空主键（阻断）、公式无缓存值（警告）与疑似日期；空值契约：空单元格保持原值、NULL 字面量明确清空；替换预览展示回收行数与附件/引用影响；错误清单 CSV 可下载；映射按来源模板保存并自动应用；模板工作簿新增说明与示例独立工作表（示例不会被导入）；粘贴导入两段式（plan_only/plan_id）；计划导入幂等（req_id=plan_id，重复提交返回原结果）；前端导入改为预览→确认→提交；新增 test_import_plan.py 七场景"},
        {"ver": "3.40", "date": "2026-10-03", "note": "采集事件与标本稳定引用：number_aliases 别名表（历史编号/合并重定向 → 行 id）与 ref_mismatch 失配处置表；/api/refs/audit 只读体检（重复/空号/失配+引用计数+软删占用政策，不直接修复）；/api/refs/migrate 审阅式迁移（唯一匹配自动绑定别名，重复/空号/软删进待确认清单，幂等）；事件改号不改变实体身份、旧编号经别名继续解析关联字段并显示改号历史；重复合并：预览字段差异与受影响标本数→受影响标本逐条改指保留事件（可随批次撤销，后来修改的其他字段保留）→被合并事件软删重定向；失配三处置（匹配现有/建立待完善事件/声明历史来源未知，不猜填地点）；导入文件内重复合并键显式阻断不再静默 last-wins；source_index 关联解析支持别名（改号/合并后旧编号继续带出省份等关联字段）；新增 test_stable_refs.py 全场景入隔离套件"},
        {"ver": "3.39", "date": "2026-10-03", "note": "统一审计事件模型：edits 升级为唯一审计出口（op_id/req_id/batch_id/parent_op_id 统一标识，记录操作者与当时权限快照、服务器时间、来源入口、行版本 before/after、人工或自动变更、规则与名录版本、理由；敏感凭据不入审计）；prev_hash→hash 摘要链 + 触发器强制事件不可修改/删除 + /api/audit/verify 校验与 /api/audit/checkpoint 独立校验清单导出（文档明确同库哈希的局限）；替换导入保留逐行可定位变化（旧行含完整 before 数据）；无变化保存不产生事件；历史接口按事件标注证据完整度（旧日志 legacy 不回填）；权限变化/自动修复（WSC 校准、坐标转换）入审计；新增 test_audit_trail.py 八组断言入隔离套件"},
        {"ver": "3.38", "date": "2026-10-03", "note": "版本恢复快照契约化：快照 v2 信封（schema 版本/表定义版本 TABLES_REV/关联依赖/行级 row_version，区分字段缺失/null/空串）；完整恢复按快照对应结构精确还原全部自有字段（缺失字段按迁移规则置空或填枚举默认，不再仅合并快照出现的字段）；新增选择字段恢复（fields 模式）；恢复前自动保存保护快照并生成结构化计划（restore-preview：逐行动作与原因+计划版本），提交携带 base_versions、计划过期 409 重新预览；身份契约：仅按行 id 匹配保留原身份，编号撞车标记冲突不做业务编号强行匹配；软删除状态按契约精确恢复；关联列边界：单表恢复不还原采集地历史值并提示依赖版本选择；数据/保护快照/审计同事务；新增 test_restore_contract.py 九场景入隔离套件；修复恢复循环计数损坏与 row_version 双重递增残留"},
        {"ver": "3.37", "date": "2026-10-03", "note": "撤销/重做/整批回滚重构为批次增量逆操作：基于原操作 before/after、记录 id 与行版本逐行回退，不再用整表快照恢复模拟撤销；字段仅在其当前值仍符合该操作产生的状态时自动逆转，被后续修改的同字段标记冲突（只改其他字段的后来操作保留）；撤销新增前检查后续编辑与附件（不直接软删），撤销删除前重查编号冲突与必填；新增只读预览 API（可应用/冲突/缺失计数+逐条原因）；默认整批原子撤销，可选跳过冲突模式并单独记录；重做以实际完成的撤销事件为依据（防重复撤销/重复重做/状态跳跃）；整批回滚改为增量逆操作，整表恢复保留为版本管理独立功能；撤销事件与原批次双向链接（_undo_of/_redo_of + _inverse_batch）"},
        {"ver": "3.36", "date": "2026-10-02", "note": "权限矩阵审计修复：stats/dashboard 改按统一授权服务 visible_tables 取可见主表（不再按 taxon_group 推导、无可见主表返回空状态不回退默认表）；行历史/批次明细/历史导出按表可见性拦截；跨表附件共享对端表不可见时不建立关联（附件不能跨权读取）；首页动态流按可见表过滤；导出发送前复核权限（长任务期间权限变更立即生效）；授权语义收紧：显式清单 rw + 共享表 r + 其余 none（不再同类群默认可读）；修复类群管理员创建即降级 custom 的角色 bug 与并发同编号唯一索引兜底；新增 test_perm_matrix.py（30 格覆盖矩阵 + 四项负面测试）"},
        {"ver": "3.35", "date": "2026-10-02", "note": "写入层统一重构：字段白名单/权限/必填/主键/枚举/单位/盒号/关联/分类规则提取为可复用校验服务 validation.py，所有写入入口共用同一层；显式例外「历史数据兼容（导入）」「精确历史恢复（版本恢复/撤销）」并在审计日志标注，不再静默；底层单行写入不自行 commit，由调用方管理事务（锁超时 503 可重试、异常整体回滚不泄漏半次写入）；批量编辑改为两阶段——整批校验+单事务提交（不再默认 force，携带每行 base_versions），显式 partial 模式用独立 SAVEPOINT 给出成功/失败/未执行明细；新增幂等键（X-Request-Id，作用域=用户+请求ID+操作类型+请求摘要）：同键同请求返回原结果、同键异请求 422 拒绝，幂等结果与数据/日志同事务落库，覆盖提交成功但响应丢失；新增 test_write_layer.py（第50行失败/重复提交/响应丢失/并发编号/未知字段/中途异常）入隔离套件"},
        {"ver": "3.34", "date": "2026-10-02", "note": "行级修订号 row_version 与并发控制：所有修改/删除/还原/导入/撤销/恢复路径统一递增（存量行迁移为版本 1）；写入携带 base_version 做带版本条件的更新并核对受影响行数（杜绝检查与提交之间的竞争）；过期版本按字段三方合并（服务器未改的字段安全合并，双方同改返回 409，附 base/当前值/拟改值/最后修改人）；「仍要覆盖」必须携带本次看到的版本并记录覆盖原因与被覆盖修订；缺少版本的旧客户端显式拒绝（脚本可 allow_overwrite 并留痕）；前端改用服务器返回的 row_version 与 updated_at；修复 edits 导出与首页动态遇内部键崩溃"},
    {"ver": "3.33", "date": "2026-10-02", "note": "测试/开发隔离与两处全新库修复：SPM_PORT/SPM_DB_FILE/SPM_UPLOADS_DIR 环境变量覆盖（默认行为不变），新增 run_tests.py 隔离编排器（独立库/端口/附件+种子+零泄漏断言）；修复全新部署首个账号注册 500（reg_perms 未赋值）与标本盒表为空时盒列表 500（box_usage_map 解包不一致）；selftest 断言相对化（不再钉死旧库规模）"},
    {"ver": "3.32", "date": "2026-10-02", "note": "修复：工作台四个队列（待鉴定/cf. 待复查/缺采集信息/DNA 凭证断链）的「去处理/查看」改为按键值定位——目标行不在当前分页时也能直达详情（原逻辑只对前 100 条有效）；含登录审计日志"},
    {"ver": "3.31", "date": "2026-10-02", "note": "新增「导出表头」：表格工具栏一键下载本表表头模板（仅列名的空 Excel），在 Excel 按列名填好数据后用「导入 Excel → 合并」按主键入库；导入对话框恢复「下载导入模板」按钮"},
    {"ver": "3.30", "date": "2026-10-02", "note": "权限体系重塑为三级：管理员 → 类群管理员 → 学生。管理员在成员管理中勾选「类群管理员」完成指定；类群管理员可为本类群生成邀请码、管理学生账号（停用/重置密码/逐表权限/批量删除），不可跨类群、不可触及管理员"},
    {"ver": "3.29", "date": "2026-10-02", "note": "工作台「DNA 凭证断链」队列改为指向 DNA 提取台账：UCE 已建库但 DNAVoucherID 在台账无对应记录的列为断链（原口径为比对标本表）"},
    {"ver": "3.28", "date": "2026-10-02", "note": "关联关系调整：跳蛛标本表不再与 UCE 文库关联（仅通过采集事件号关联采集地信息），编号带出物种仅剩原蛛表；UCE 文库改为与 DNA 提取台账按 DNA 凭证编号互相关联，两表附件跨表共享"},
    {"ver": "3.27", "date": "2026-10-02", "note": "跳蛛系统新增「DNA Extract」表（DNA 提取台账）：DNAVoucherID 主键、分类链、组织/浓度/建库信息；已从 JXZDNAVouchers-2026.10.2.xlsx 导入 1,954 行；酒精归一修复：纯整数与 “1” 比例写法正确转百分比"},
    {"ver": "3.26", "date": "2026-10-02", "note": "UCE 表头重构：Sample Code 显示为 DNAVoucherID、Species name 显示为 Temporary Name；新增 Final Name 与 CollectionEventNumber 列；列序按要求重排；移除 remove<200bp 列（数据保留）；导入/编号带出/SRA 匹配/跨表附件全部兼容"},
    {"ver": "3.25", "date": "2026-10-02", "note": "跳蛛表表头统一：MHBU # → Specimen #，PreserveCondition(75%, 85%, 95%, 100%) → PreserveCondition（界面与导出用新名；存储键不变，历史数据/查询/日志完全兼容；导入同时识别新旧两种表头）"},
    {"ver": "3.25", "date": "2026-10-02", "note": "跳蛛表表头统一：MHBU # → Specimen #，PreserveCondition(75%, 85%, 95%, 100%) → PreserveCondition（界面与导出用新名；存储键不变，历史数据/查询/日志完全兼容；导入同时识别新旧两种表头）"},
    {"ver": "3.24", "date": "2026-10-02", "note": "跳蛛表新增 Position 列（Box 后）：按编号在盒内顺序自动推算 [行,列]，新增/改盒/改编号自动重算、可手工修正；体检页一键补算（本地已回填 9,185 行）"},
    {"ver": "3.24", "date": "2026-10-02", "note": "跳蛛表新增 Position 列（Box 后）：按编号在盒内顺序自动推算 [行,列]，新增/改盒/改编号自动重算、可手工修正；体检页一键补算（本地已回填 9,185 行）"},
    {"ver": "3.23", "date": "2026-10-02", "note": "新增全局搜索（Ctrl+K 或页头「搜索」按钮）：一次关键字跨所有有权限的表查找（含省份等关联带出的展示字段），按表分组显示命中数与片段，点「查看」直达该表并打开行详情"},
    {"ver": "3.22", "date": "2026-10-02", "note": "详情页新增标本盒示意图：按盒号段自动画正方形孔位图（6×6 / 10×10），其余孔淡灰、本标本淡绿高亮并标注行列——一眼定位取管位置"},
    {"ver": "3.21", "date": "2026-10-01", "note": "新增表单标本盒默认选中当前有空余的盒（编号顺延的下一盒），编号同步预填——打开即录"},
    {"ver": "3.20", "date": "2026-10-01", "note": "表格/详情页键入属名即时核对：未收录于 WSC 时在编辑处弹出持续浮动警告（含相近属建议），改对自动消失——与新增表单同口径"},
    {"ver": "3.19", "date": "2026-10-01", "note": "修复：改属名时分类链不再停留旧值——科/亚科/族/Clade 随新属整链覆盖更新（新增与以此行新增表单）；克隆表单补齐属名联想/核对/链联动"},
    {"ver": "3.18", "date": "2026-10-01", "note": "新增标本盒先行：选盒后自动预填该盒下一个可用编号，编号只能用所选盒号段内（前端实时提示 + 保存双层校验）；新增/克隆时属名未收录于 WSC 即时弹出核对警告（附相近属建议）"},
    {"ver": "3.17", "date": "2026-10-01", "note": "数据体检新增「属名未收录」：属名在 WSC 与本地分类表都查不到的行列为问题数据，附相近属名建议（覆盖跳蛛/原蛛），可一键定位修改"},
    {"ver": "3.16", "date": "2026-10-01", "note": "筛选值列表改为字母序（数字列按数值升序），条数仍显示——按名称查找更直观"},
    {"ver": "3.15", "date": "2026-10-01", "note": "鉴定工作台改版：每行显示鉴定辅助信息（模式标记·产地·日期·性别）与图片附件直达按钮；定名输入框定宽不再整行铺开"},
    {"ver": "3.14", "date": "2026-10-01", "note": "酒精浓度归一补全：Excel 导入与粘贴导入的 0.95 等小数比例自动转为 95%（与单行保存同口径）；显示层统一为“95%”格式（原蛛表此前缺百分号）"},
    {"ver": "3.13", "date": "2026-10-01", "note": "系统接入实验室徽标（页头/登录页/关于页）"},
    {"ver": "3.12", "date": "2026-10-01", "note": "关于页按权限分层：普通用户见我的数据表与个人权限卡；结构变更与部署运维仅管理员可见"},
    {"ver": "3.11", "date": "2026-10-01", "note": "关于页新增系统版本卡与完整更新日志"},
    {"ver": "3.10", "date": "2026-10-01", "note": "详情页「以此行新增」按钮图标缺失修复；图标渲染加兜底"},
    {"ver": "3.9", "date": "2026-10-01", "note": "回收站定时维护在多进程下的上下文修复"},
    {"ver": "3.8", "date": "2026-10-01", "note": "管理员系统健康卡（备份/演练/版本）；体检管理动作收进「数据工具」菜单；按 ? 查看快捷键面板；搜索错别字容错建议"},
    {"ver": "3.6", "date": "2026-10-01", "note": "鉴定工作台：待鉴定 / cf. 待复查 / 缺采集信息 / DNA 凭证断链四类待办队列，行内快速定名（联想 + WSC 校验 + 自动移出）"},
    {"ver": "3.5", "date": "2026-10-01", "note": "借出视图「未归还」标记；借出字段语义修正"},
    {"ver": "3.4", "date": "2026-10-01", "note": "保存时疑似重复录入检测（同采集事件 + 同属/种，可确认后强制保存）"},
    {"ver": "3.3", "date": "2026-10-01", "note": "备份列表与一键恢复演练（不触碰生产库）；撤销栈保留 30 天/500 批；回收站超 90 天自动清理"},
    {"ver": "3.2", "date": "2026-10-01", "note": "登录失败锁定：连续 5 次失败锁定 10 分钟"},
    {"ver": "3.1", "date": "2026-10-01", "note": "成员批量删除；清理历史测试账号与测试邀请码"},
    {"ver": "3.0", "date": "2026-09-30", "note": "三态表级权限；UCE/原蛛编号自动带出物种；导入预览确认；数据库结构版本追踪；详情页字段定位"},
]


@app.get("/api/meta")
def api_meta():
    me = current_user()
    conn = get_db()
    tables = {}
    visible = visible_tables(me)
    visible_sorted = dict(sorted(visible.items(), key=lambda kv: kv[1].get("sort", 99)))
    for t, cfg in visible_sorted.items():
        total = conn.execute("SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=0", (t,)).fetchone()["c"]
        deleted = conn.execute("SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=1", (t,)).fetchone()["c"]
        ver = conn.execute("SELECT COUNT(*) c FROM versions WHERE tname=?", (t,)).fetchone()["c"]
        last = conn.execute(
            "SELECT ts, user FROM edits WHERE tname=? AND action!='version' ORDER BY id DESC LIMIT 1", (t,)
        ).fetchone()
        tables[t] = {
            "label": cfg["label"], "display_label": TD.display_label(t),
            "key": cfg["key"], "columns": cfg["columns"],
            "total": total, "deleted": deleted, "versions": ver,
            "last_edit": dict(last) if last else None,
            "groups": cfg.get("groups"), "genus_lookup": bool(cfg.get("genus_lookup")),
            "wac_lookup": bool(cfg.get("wac_lookup")),
            "taxon_cols": TD.taxon_cols(t),
            "loan_view": bool(cfg.get("loan_view")), "nav": cfg.get("nav", "main"),
            "linked_key": cfg.get("linked_key"),
            "add_first": cfg.get("add_first"),
            "taxon_group_label": cfg.get("taxon_group_label"),
            "taxon_group": cfg.get("taxon_group", "spider"),
            "required_fields": cfg.get("required_fields") or [],
            "require_one_of": cfg.get("require_one_of") or [],
            "type_field": cfg.get("type_field"),
            "perm": "rw" if (me["admin"] or (me.get("perms") or {}).get(t) == "rw") else "r",
            # 预设任务视图（列集合/顺序/固定列/默认排序/解释；权限沿用本表三态）
            "views": TD.table_views(t),
        }
        # 鉴定联动（v4.31）：只给参与联动的表下发，其余表不带该键
        _ident = TD.ident_link(t)
        if _ident:
            tables[t]["ident_sync"] = _ident
    taxon_groups = []
    for t, c in TABLES.items():
        g = c.get("taxon_group")
        lbl = c.get("taxon_group_label")
        if g and g != "shared" and lbl and not any(x["key"] == g for x in taxon_groups):
            taxon_groups.append({"key": g, "label": lbl})
    return jsonify({
        "system_name": config.SYSTEM_NAME,
        "taxon_groups": taxon_groups,
        "docs": COLUMN_DOCS,
        "app_version": APP_VERSION,
        "app_changelog": APP_CHANGELOG,
        "schema": {"version": dbm.SCHEMA_VERSION,
                   "updated": dict(conn.execute("SELECT key, value FROM schema_info").fetchall()).get("schema_updated_at", ""),
                   "changes": dbm.SCHEMA_CHANGELOG},
        "tables": tables,
        "username": None,  # 前端从 /api/auth/me 获取
    })


@app.get("/api/auth/me")
def api_me():
    u = current_user(required=False)
    conn = get_db()
    needs_setup = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
    return jsonify({"user": u["username"] if u else None, "admin": u["admin"] if u else False,
                    "role": u["role"] if u else None,
                    "group_admin": (u["role"] == "group_admin") if u else False,
                    "tables": u["tables"] if u else [], "group": u["group"] if u else "",
                    "can_boxes": u["can_boxes"] if u else False,
                    "can_composite": u["can_composite"] if u else False,
                    "allow_register": config.ALLOW_REGISTER, "needs_setup": needs_setup})


@app.post("/api/auth/change_password")
def api_change_pw():
    me = current_user()
    d = request.get_json(force=True)
    if len(d.get("new_password") or "") < 4:
        abort(400, "新密码至少 4 位")
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
    if not check_pw(d.get("old_password") or "", u["pw_hash"]):
        abort(400, "原密码不正确")
    conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(d["new_password"]), me["id"]))
    conn.commit()
    return jsonify({"ok": True})


def _apply_col_filters(disp, filters):
    """列级筛选：in=勾选值多选（"（空）"代表空值）、contains=包含文本、empty/notempty。"""
    if not filters:
        return disp
    out = []
    for r in disp:
        ok = True
        for col, f in filters.items():
            if not isinstance(f, dict):
                continue
            v = r["data"].get(col)
            sv = "" if v is None else str(v).strip()
            mode = f.get("mode")
            if mode == "in":
                vals = set(str(x).strip() for x in (f.get("values") or []))
                if "（空）" in vals:
                    if sv != "" and sv not in vals:
                        ok = False
                        break
                elif sv not in vals:
                    ok = False
                    break
            elif mode == "contains":
                if str(f.get("text") or "").lower() not in sv.lower():
                    ok = False
                    break
            elif mode == "empty":
                if sv != "":
                    ok = False
                    break
            elif mode == "notempty":
                if sv == "":
                    ok = False
                    break
        if ok:
            out.append(r)
    return out


@app.get("/api/rows/<tname>")
def api_rows(tname):
    user = current_user()
    cfg = ensure_visible(user, tname)
    q = (request.args.get("q") or "").strip().lower()
    page = max(1, int(request.args.get("page", 1)))
    per = min(500, max(10, int(request.args.get("per", 100))))
    sort = request.args.get("sort") or ""
    direction = request.args.get("dir", "asc")
    natural = request.args.get("natural", "") == "1"
    show_deleted = request.args.get("deleted") == "1"
    preset = (request.args.get("preset") or "").strip()
    type_field = cfg.get("type_field")
    filters = {}
    raw = request.args.get("filters") or ""
    if raw:
        try:
            filters = json.loads(raw)
            if not isinstance(filters, dict):
                filters = {}
        except ValueError:
            filters = {}
    eidx = linked_eidx(tname)
    # 鉴定联动差异标记（非权威侧，当前即 UCE 文库）：逐行比对本表属/暂定名/最终名
    # 与关联权威行（DNA 提取台账）的现值，差异（含本侧为空、权威侧有值）随行下发
    # dna_diff={列: 权威值}，前端徽标呈现并可一键以权威值为准同步。
    _ident = TD.ident_link(tname)
    _ident_idx = None
    if _ident and not _ident["authority"]:
        _ident_idx = _ident_peer_index(_ident)
    _diff_total = 0
    disp = []
    for r in load_rows(tname):
        if r["deleted"] != show_deleted:
            continue
        d, miss = display_row(tname, r, eidx)
        if preset == "loaned" and TABLES[tname].get("loan_view") and not show_deleted:
            loaned = (d.get("LoanSpecimenNumber") or d.get("LoanSpecimenFrom")
                      or d.get("SentDate")) and not d.get("ReturnDate")
            if not loaned:
                continue
        if preset == "types" and type_field:
            tv = str(d.get(type_field) or "").strip().lower()
            if not tv or tv == "no":
                continue
        item = {"id": r["id"], "data": d, "miss": miss,
                "updated_at": r.get("updated_at"), "updated_by": r.get("updated_by"),
                "row_version": r.get("row_version")}
        if type_field:
            tv = str(d.get(type_field) or "").strip()
            if tv and tv.lower() != "no":
                item["_type"] = tv
        if _ident_idx is not None:
            dd = _ident_diff_mark(_ident, d, _ident_idx)
            if dd:
                item["dna_diff"] = dd
                if not show_deleted:
                    _diff_total += 1
        disp.append(item)
    disp = _apply_col_filters(disp, filters)
    if q:
        disp = [r for r in disp if any(
            v is not None and q in str(v).lower() for v in r["data"].values())]
    total = len(disp)
    # 默认按主键列自然排序（文本+数字混合编号按数值排，如 A2 < A10）
    if not sort and cfg.get("key"):
        key_col = next((c for c in cfg["columns"] if c["name"] == cfg["key"]), None)
        if key_col and not key_col.get("numeric"):
            def nat_key(r):
                v = str(r["data"].get(cfg["key"]) or "")
                return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", v)]
            disp.sort(key=nat_key)
    # 显式排序：文本+数字混合编号按数值排（如 A2 < A10）
    if sort:
        colmeta = {c["name"]: c for c in cfg["columns"]}
        col = colmeta.get(sort)
        if col and not col.get("numeric"):
            def nat_key(r):
                v = str(r["data"].get(sort) or "")
                return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", v)]
            disp.sort(key=nat_key, reverse=(direction == "desc"))
            rows = disp[(page - 1) * per: page * per]
            resp = {"columns": cfg["columns"], "rows": rows, "total": total,
                    "page": page, "per": per, "users": []}
            if _ident_idx is not None:
                resp["dna_diff_total"] = _diff_total
            return jsonify(resp)
    colmeta = {c["name"]: c for c in cfg["columns"]}
    if sort in colmeta:
        numeric = colmeta[sort].get("numeric")

        def sk(r):
            v = r["data"].get(sort)
            if numeric or isinstance(v, (int, float)):
                try:
                    return (0, float(v))
                except (TypeError, ValueError):
                    return (1, 0)
            return (1, str(v) if v is not None else "")
        disp.sort(key=sk, reverse=(direction == "desc"))
    # 统一查询状态模型：查询指纹（所有计数/分页/导出/批量共用同一表达；
    # 客户端把它作为「带版本的稳定计划」凭据，筛选/数据变化后指纹即失效）
    qdigest = hashlib.sha256(json.dumps(
        {"t": tname, "q": q, "filters": filters, "preset": preset,
         "sort": sort, "dir": direction, "deleted": show_deleted},
        sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
    if request.args.get("resolve") == "ids":
        # 稳定计划：把当前筛选解析为显式行集合（批量操作/导出用，不随筛选变化扩大）。
        # cap 默认 5000（批量操作防失控）；导出向导传 cap=200000 —— 导出不应设限（用户要求：
        # 筛选全部 9145 行也要能固化导出）。非法/超界 cap 明确 400，绝不静默截断。
        ids = [r["id"] for r in disp]
        try:
            cap = int(request.args.get("cap") or 5000)
        except (TypeError, ValueError):
            abort(400, "cap 需为整数（默认 5000；导出向导用 200000）")
        if not 1 <= cap <= 200000:
            abort(400, "cap 超出允许范围（1-200000）")
        sample = [{"id": r["id"], "key": (r["data"].get(cfg.get("key"))
                                          if cfg.get("key") else None)} for r in disp[:5]]
        return jsonify({"ids": ids[:cap], "count": len(ids), "truncated": len(ids) > cap,
                        "cap": cap, "digest": qdigest, "sample": sample})
    # 编号优先：搜索词命中主键精确值或前缀的行排在最前（精确 > 前缀 > 其余）
    if q and cfg.get("key"):
        keyname = cfg["key"]

        def key_priority(r):
            v = str(r["data"].get(keyname) or "").lower()
            if v == q:
                return (0, 0)
            if v.startswith(q):
                return (1, 0)
            return (2, 0)
        disp.sort(key=key_priority)   # 稳定排序：组内保持原排序
    rows = disp[(page - 1) * per: page * per]
    resp = {"columns": cfg["columns"], "rows": rows, "total": total,
            "page": page, "per": per, "users": [], "query_digest": qdigest,
            "scope": "全部列（含采集地等关联带出列）"}
    if _ident_idx is not None:
        resp["dna_diff_total"] = _diff_total
    return jsonify(resp)


@app.get("/api/rows/<tname>/values")
def api_row_values(tname):
    """列去重值 + 计数（筛选面板用）。关联列按实时解析后的展示值统计。"""
    user = current_user()
    ensure_visible(user, tname)
    cfg = chk_table(tname)
    col = request.args.get("col") or ""
    if col not in {c["name"] for c in cfg["columns"]}:
        abort(400, f"未知列 {col}")
    show_deleted = request.args.get("deleted") == "1"
    eidx = linked_eidx(tname)
    counts = {}
    empty = 0
    for r in load_rows(tname):
        if r["deleted"] != show_deleted:
            continue
        d, _ = display_row(tname, r, eidx)
        v = d.get(col)
        sv = "" if v is None else str(v).strip()
        if sv == "":
            empty += 1
        else:
            counts[sv] = counts.get(sv, 0) + 1
    # 筛选值排序：默认按字母（大小写不敏感）便于查找；纯数字列按数值升序
    def _val_key(kv):
        try:
            return (0, float(kv[0]), "")
        except ValueError:
            return (1, 0.0, kv[0].lower())
    items = sorted(counts.items(), key=_val_key)
    return jsonify({"col": col, "values": items[:1000], "empty": empty,
                    "truncated": len(items) > 1000,
                    "total": sum(counts.values()) + empty})


def chk_table(tname):
    if tname not in TABLES:
        abort(404, f"未知数据表 {tname}")
    return TABLES[tname]


_RULES_VERSIONS_CACHE = {}


def _rules_versions():
    """当时的规则/名录版本：WSC 同步日期、WAC 文件版本、表定义与结构版本（缓存）。"""
    if not _RULES_VERSIONS_CACHE:
        wsc = ""
        try:
            wsc = open(LAST_SYNC_FILE, encoding="utf-8").read().strip()
        except OSError:
            pass
        if not wsc:
            try:
                wsc = str(int(os.path.getmtime(os.path.join(APP_DIR, "wsc_species.json"))))
            except OSError:
                wsc = ""
        _RULES_VERSIONS_CACHE.update({
            "schema": dbm.SCHEMA_VERSION, "tables_rev": TABLES_REV, "wsc_sync": wsc,
        })
    return _RULES_VERSIONS_CACHE


def _source_entry():
    """来源入口：API 路径+方法（+ 前端 X-Source 动作标注）。"""
    try:
        base = f"{request.method} {request.path}"
        tag = request.headers.get("X-Source") or ""
        return (base + (f" [{tag}]" if tag else ""))[:180]
    except RuntimeError:
        return "internal"


def _user_perms_snapshot(username):
    """操作者当时权限快照（逐表三态 + 角色/类群），落库前读取，不做推断。"""
    try:
        u = get_db().execute(
            "SELECT role, taxon_group, perms, is_admin FROM users WHERE username=?",
            (username,)).fetchone()
        if not u:
            return ""
        perms = parse_perms(u["perms"] or "{}", [], u["taxon_group"] or "")
        return json.dumps({"role": u["role"], "admin": bool(u["is_admin"]),
                           "group": u["taxon_group"] or "", "perms": perms},
                          ensure_ascii=False)
    except Exception:
        return ""


_SENSITIVE_RE = re.compile(r"password|pw_hash|token|invite|secret", re.I)


def _scrub_sensitive(obj):
    """敏感账号信息不入审计：detail 中命中敏感词的键剔除/遮蔽。"""
    if isinstance(obj, dict):
        return {k: ("***" if _SENSITIVE_RE.search(str(k)) else _scrub_sensitive(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_sensitive(v) for v in obj]
    return obj


_audit_chain_prev = {"hash": None}


def log_edit(conn, user, tname, rid, rkey, action, changes, batch_id=None,
             op_id=None, parent_op_id=None, req_id=None, source=None,
             actor_type="human", reason=None, rv_before=None, rv_after=None):
    """统一审计事件写入器：所有写入路径唯一出口。
    事件含稳定记录身份（rid/rkey）、表、操作者与当时权限、服务器时间、来源入口、
    行版本 before/after、字段 before/after（detail）、人工/自动、规则与名录版本、
    理由及父操作；与数据同事务提交；哈希链 prev_hash→hash 支持摘要校验。
    敏感账号信息不入日志（_scrub_sensitive）。"""
    ts = now()
    detail = json.dumps(_scrub_sensitive(changes), ensure_ascii=False, default=str)
    rkey = str(rkey) if rkey is not None else None
    perms_snap = _user_perms_snapshot(user)
    rules = json.dumps(_rules_versions(), ensure_ascii=False)
    if not op_id:
        op_id = batch_id or ("E" + secrets.token_hex(6))
    if not source:
        source = _source_entry()
    # 行版本 before/after：未显式给出时按当前行推断（写入后查询 → after；before=after-1）
    if rv_after is None and rid is not None and tname:
        r0 = conn.execute("SELECT row_version FROM records WHERE id=? AND tname=?",
                          (rid, tname)).fetchone()
        if r0:
            rv_after = r0["row_version"]
            rv_before = rv_before if rv_before is not None else max(1, rv_after - 1)
    prev = _audit_chain_prev.get("hash")
    if prev is None:
        r = conn.execute("SELECT hash FROM edits WHERE hash<>'' ORDER BY id DESC LIMIT 1").fetchone()
        prev = r["hash"] if r else ""
        _audit_chain_prev["hash"] = prev
    rv_b = str(rv_before) if rv_before is not None else ""
    rv_a = str(rv_after) if rv_after is not None else ""
    canon = json.dumps([prev, ts, user, tname, rid, rkey, action, detail, op_id,
                        parent_op_id or "", req_id or "", source, actor_type, perms_snap,
                        rv_b, rv_a, rules, reason or ""],
                       ensure_ascii=False, sort_keys=True, default=str)
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    _audit_chain_prev["hash"] = h
    conn.execute(
        "INSERT INTO edits(ts,user,tname,rid,rkey,action,changes,batch_id,"
        "op_id,parent_op_id,req_id,source,actor_type,user_perms,"
        "row_version_before,row_version_after,rules_versions,reason,prev_hash,hash,evidence) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, user, tname, rid, rkey, action, detail, batch_id,
         op_id, parent_op_id or "", req_id or "", source, actor_type, perms_snap,
         str(rv_before) if rv_before is not None else "",
         str(rv_after) if rv_after is not None else "",
         rules, reason or "", prev, h, "full"),
    )


# ---------------------------------------------------------------- 幂等键 / 写事务管理
class WriteConflict(Exception):
    """乐观锁冲突（409）：payload 为完整响应体。"""

    def __init__(self, payload):
        super().__init__(payload.get("error", "conflict"))
        self.payload = payload


def _is_locked(e):
    s = str(e).lower()
    return "lock" in s or "busy" in s


def _tx_rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


def _idem_ctx(user, op_type, body, force_req_id=None):
    """从请求头取 X-Request-Id 并计算请求摘要。未携带请求头 → None（不做幂等）。"""
    req_id = force_req_id or (request.headers.get("X-Request-Id") or "").strip()
    if not req_id or len(req_id) > 200:
        return None
    digest = hashlib.sha256(
        json.dumps(body or {}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return {"username": user["username"], "req_id": req_id, "op_type": op_type, "digest": digest}


def _idem_lookup(conn, ctx):
    """幂等查询：命中同键同请求 → 返回存储的原结果；同键不同请求 → 拒绝。
    查找与后续写入同连接，天然串行（SQLite 单写者）。"""
    if not ctx:
        return None
    row = conn.execute(
        "SELECT op_type, req_digest, status, response FROM idempotency_keys WHERE username=? AND req_id=?",
        (ctx["username"], ctx["req_id"])).fetchone()
    if not row:
        return None
    if row["op_type"] != ctx["op_type"] or row["req_digest"] != ctx["digest"]:
        return (jsonify({"error": f"请求 ID「{ctx['req_id']}」已被不同的请求使用，请更换 X-Request-Id 后重试",
                         "idempotency_conflict": True}), 422)
    payload = json.loads(row["response"])
    if isinstance(payload, dict):
        payload = {**payload, "idempotent_replay": True}   # 客户端可识别这是重放的原结果
    resp = jsonify(payload)
    resp.headers["X-Idempotent-Replay"] = "1"
    return resp


def _idem_store(conn, ctx, status, payload):
    """幂等结果入库：必须与数据写入同一事务，由调用方在 commit 前调用。
    覆盖「提交成功但响应丢失」——客户端携同一 ID 重试时返回存储的原结果。"""
    if not ctx:
        return
    conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys(username,req_id,op_type,req_digest,status,response,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (ctx["username"], ctx["req_id"], ctx["op_type"], ctx["digest"],
         status, json.dumps(payload, ensure_ascii=False, default=str), now()))
    conn.execute("DELETE FROM idempotency_keys WHERE created_at < datetime('now','localtime','-1 day')")


def _finish_write(conn, payload, idem=None, status=200):
    """提交并返回写入响应：幂等结果与数据/日志在同一事务中落库；
    锁超时 → 回滚并返回 503（可重试），不泄漏半次写入。"""
    try:
        _idem_store(conn, idem, status, payload)
        conn.commit()
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        if _is_locked(e):
            return jsonify({"error": "数据库正忙（写入锁超时），本次更改已整体回滚，请稍后重试",
                            "retryable": True}), 503
        raise
    resp = jsonify(payload)
    if idem:
        resp.headers["X-Request-Id"] = idem["req_id"]
    return resp


def _tx_fail(e):
    """写事务异常统一处理：回滚（不泄漏半次写入）；锁超时 → 503；其余原样抛出。"""
    if isinstance(e, sqlite3.OperationalError) and _is_locked(e):
        return jsonify({"error": "数据库正忙（写入锁超时），本次更改已整体回滚，请稍后重试",
                        "retryable": True}), 503
    raise e


TAXONOMY_CHAIN = [  # (标本表字段, 分类信息表字段)
    ("Order", "Order"), ("Infraorder", "Infraorder"), ("Family", "Family"),
    ("Subfamily", "SUBFAMILY"), ("Clade1", "CLADE1"), ("Clade2", "CLADE2"),
    ("Clade3", "CLADE3"), ("Tribe", "TRIBE"), ("Subtribe", "SUBTRIBE"),
]


def _load_wsc_species():
    path = os.path.join(CATALOG_DIR, "wsc_species.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    lowmap = {x["n"].lower(): x for x in data}
    return lowmap, data


_WSC_SPEC_LOW, _WSC_SPEC_LIST = _load_wsc_species()


def _load_wac():
    """加载 WAC 伪蝎名录（update_wac.py 从 GBIF Backbone 刷新）。"""
    def load(fname):
        path = os.path.join(CATALOG_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []
    gen = load("wac_genera.json")
    spc = load("wac_species.json")
    return ({x["g"].lower(): x for x in gen}, {x["n"].lower(): x for x in spc})


_WAC_GEN_LOW, _WAC_SPC_LOW = _load_wac()


def check_wac_species(genus, epithet):
    """属 + 种加词 → WAC 校验。返回 {in_wac, genus, epithet, family, corrected, warn}。"""
    genus = str(genus or "").strip()
    epithet = str(epithet or "").strip()
    if not genus or not epithet:
        gh = _WAC_GEN_LOW.get(genus.lower()) if genus else None
        return {"in_wac": False, "genus": genus, "epithet": epithet,
                "family": (gh or {}).get("f"), "corrected": False}
    full = (genus + " " + epithet).lower()
    hit = _WAC_SPC_LOW.get(full)
    corrected = False
    warn = None
    if hit:
        if hit.get("syn") and hit.get("acc"):
            acc = hit["acc"]
            ag = str(hit.get("g") or "")
            parts = acc.split()
            new_epithet = parts[1] if len(parts) > 1 else acc
            if ag:
                genus = ag
            epithet = new_epithet
            corrected = True
            warn = f"已按 WAC 将种名更新为接受名「{acc}」"
        return {"in_wac": True, "genus": genus, "epithet": epithet,
                "family": hit.get("f"), "corrected": corrected, "warn": warn}
    # 组合未收录：查属是否存在（属存在 + 加词未知 → 可能是暂定新种）
    gh = _WAC_GEN_LOW.get(genus.lower())
    if gh:
        if gh.get("syn") and gh.get("acc"):
            acc_gen = str(gh["acc"]).strip()
            if acc_gen and acc_gen.lower() != genus.lower():
                genus = acc_gen
                corrected = True
                warn = f"属名已按 WAC 更新为接受名「{acc_gen}」"
        return {"in_wac": False, "genus": genus, "epithet": epithet,
                "family": gh.get("f"), "corrected": corrected, "warn": warn,
                "genus_known": True}
    return {"in_wac": False, "genus": genus, "epithet": epithet,
            "family": None, "corrected": corrected, "warn": "属名未在 WAC 收录"}


def _wac_apply(tname, data, old=None):
    """伪蝎表新增/编辑的 WAC 联动。返回 (data, warn)。"""
    warn = None
    if tname != "pseudoscorpiones":
        return data, warn
    genus = str(data.get("属") or "").strip()
    epithet = str(data.get("种") or "").strip()
    if genus:
        gh = _WAC_GEN_LOW.get(genus.lower())
        if gh:
            if gh.get("syn") and gh.get("acc"):
                acc_gen = str(gh["acc"]).strip()
                if acc_gen and acc_gen.lower() != genus.lower():
                    # 接受属名可能带命名人与年份，取第一个词
                    genus = acc_gen.split()[0]
                    data["属"] = genus
                    warn = f"属名已按 WAC 更新为接受名「{genus}」"
            if data.get("科") in (None, "") and gh.get("f"):
                data["科"] = gh["f"]
    if genus and epithet:
        res = check_wac_species(genus, epithet)
        if res["corrected"]:
            data["属"] = res["genus"]
            data["种"] = res["epithet"]
            warn = res.get("warn") or warn
            if res.get("family") and data.get("科") in (None, ""):
                data["科"] = res["family"]
    return data, warn


@app.get("/api/wac-suggest")
def api_wac_suggest():
    """WAC 联想：无 genus → 属名前缀联想；有 genus → 该属下种加词联想。"""
    current_user()
    ensure_visible(current_user(), "pseudoscorpiones")
    q = (request.args.get("q") or "").strip().lower()
    genus = (request.args.get("genus") or "").strip().lower()
    if not q:
        return jsonify({"items": []})
    items = []
    if genus:
        for n, s in _WAC_SPC_LOW.items():
            if s.get("g", "").lower() != genus:
                continue
            full = s["n"]
            parts = full.split()
            ep = parts[1] if len(parts) > 1 and parts[0].lower() == genus else full
            if not ep.lower().startswith(q):
                continue
            items.append({"epithet": ep, "full": s.get("acc") or full,
                          "family": s.get("f"), "syn": bool(s.get("syn"))})
            if len(items) >= 12:
                break
        items.sort(key=lambda x: (x["syn"], x["epithet"].lower()))
    else:
        for n, gw in _WAC_GEN_LOW.items():
            if not n.startswith(q):
                continue
            items.append({"genus": gw["g"], "family": gw.get("f"), "syn": bool(gw.get("syn"))})
            if len(items) >= 12:
                break
        items.sort(key=lambda x: (x["syn"], x["genus"].lower()))
    return jsonify({"items": items[:12]})


@app.get("/api/wac-check")
def api_wac_check():
    current_user()
    ensure_visible(current_user(), "pseudoscorpiones")
    genus = request.args.get("genus") or ""
    epithet = request.args.get("epithet") or ""
    return jsonify(check_wac_species(genus, epithet))


def gbif_match_species(name):
    """GBIF 在线匹配种名（数据源即 WSC）。
    返回 (accepted_binomen, genus, family) 或 None；异名自动解析为当前接受名。
    网络异常时置 _GBIF_NET_ERROR（区别「服务不可用」与「确未收录」）；
    SPM_WSC_OFFLINE=1 时禁用在线回退（离线部署/隔离测试，名录只看本地文件）。"""
    global _GBIF_NET_ERROR
    if os.environ.get("SPM_WSC_OFFLINE") == "1":
        _GBIF_NET_ERROR = "离线模式（SPM_WSC_OFFLINE），在线核对已禁用"
        return None
    import requests as _rq
    _GBIF_NET_ERROR = None
    try:
        r = _rq.get("https://api.gbif.org/v1/species/match",
                    params={"name": name, "strict": "false", "kingdom": "Animalia", "phylum": "Arthropoda",
                            "class": "Arachnida", "order": "Araneae"}, timeout=12)
        j = r.json()
        if j.get("matchType") in ("EXACT", "HIGHERRANK", "FUZZY") and j.get("rank") in ("SPECIES", "SUBSPECIES")                 and j.get("canonicalName") and len(j.get("canonicalName", "").split()) >= 2:
            canonical, genus, family = j["canonicalName"], j.get("genus"), j.get("family")
            # 取详情确认当前状态；异名解析为当前接受名
            det = _rq.get(f"https://api.gbif.org/v1/species/{j['usageKey']}", timeout=12).json()
            if det.get("taxonomicStatus") == "SYNONYM" and det.get("acceptedKey"):
                acc = _rq.get(f"https://api.gbif.org/v1/species/{det['acceptedKey']}", timeout=12).json()
                canon = acc.get("canonicalName") or ""
                if canon and len(canon.split()) >= 2:
                    canonical, genus = canon, acc.get("genus")
            return canonical, genus, family
    except Exception as e:
        _GBIF_NET_ERROR = str(e)[:120]
    return None


def species_normalize(value):
    """FinalSpeciesName 归一（最终鉴定结果必须是 WSC 收录的名称）：
    返回 None = WSC 未收录（拒绝保存）；返回 dict = {"name": 接受名, "genus": 属, "family": 科,
    "corrected": 是否由异名/拼写修正}。优先离线种名录，其次 GBIF 在线匹配。"""
    s = str(value or "").strip()
    if not s:
        return None
    low = s.lower()
    rec = _WSC_SPEC_LOW.get(low)
    if rec:
        corrected = rec["syn"] or rec["n"] != s
        return {"name": rec["acc"], "genus": rec["acc"].split()[0],
                "family": rec.get("f"), "corrected": corrected or rec["n"].lower() != rec["acc"].lower()}
    online = gbif_match_species(s)
    if online:
        accepted, genus, fam = online
        return {"name": accepted, "genus": genus, "family": fam, "corrected": accepted.lower() != low}
    return None


_GBIF_NET_ERROR = None


def _wsc_unavailable():
    """名录服务不可用：本地种名录文件缺失/不可读，且在线核对不可达或未配置。
    「服务不可用」≠「未收录」——前者不构成任何分类结论。"""
    if _WSC_SPEC_LIST:
        return False
    return not os.path.exists(os.path.join(CATALOG_DIR, "wsc_species.json"))


def name_check_status(value):
    """名录核对（二维度之「收录维度」）：返回 {
      status: accepted（收录）/ synonym（异名，附接受名）/ not_found（未收录）/
              service_unavailable（服务不可用，不能当分类错误）,
      accepted, genus, family, corrected, detail}
    供鉴定历史落库使用；核对动作只读本地名录文件 + （必要时）在线查询，
    不占用数据库写事务（调用方须在写事务开始前调用）。"""
    s = str(value or "").strip()
    if not s:
        return None
    m = V.parse_det_name(s)
    core = m["core"]
    qualifier = m["qualifier"]
    if not core:
        return {"status": "not_found", "detail": "无法解析出可核对的学名", "qualifier": qualifier}
    if _wsc_unavailable():
        return {"status": "service_unavailable", "qualifier": qualifier,
                "detail": "WSC 种名录文件缺失或不可读，暂无法核对收录状态"}
    low = core.lower()
    rec = _WSC_SPEC_LOW.get(low)
    if rec:
        status = "synonym" if rec.get("syn") else "accepted"
        return {"status": status, "accepted": rec["acc"], "genus": rec["acc"].split()[0],
                "family": rec.get("f"), "corrected": bool(rec.get("syn")) or rec["n"] != core,
                "qualifier": qualifier, "detail": m["detail"]}
    if len(core.split()) >= 2:
        online = gbif_match_species(core)
        if online:
            accepted, genus, fam = online
            corrected = accepted.lower() != low
            return {"status": "synonym" if corrected else "accepted", "accepted": accepted,
                    "genus": genus, "family": fam, "corrected": corrected,
                    "qualifier": qualifier, "detail": m["detail"], "via": "gbif"}
        if _GBIF_NET_ERROR:
            return {"status": "service_unavailable", "qualifier": qualifier,
                    "detail": f"本地名录未收录且在线核对不可达（{_GBIF_NET_ERROR}）"}
    genus_only = core.split()[0]
    if wsc_known(genus_only):
        return {"status": "not_found", "genus_known": True, "genus": genus_only,
                "qualifier": qualifier, "detail": (m["detail"] + "；属已收录，种级名称未收录" if m["detail"]
                                                   else "属已收录，种级名称未收录（可能是暂定名/新种候选）")}
    return {"status": "not_found", "qualifier": qualifier,
            "detail": (m["detail"] + "；属与种均未收录" if m["detail"] else "WSC 未收录该名称")}


def _name_check_local(value):
    """仅离线本地名录的快速核对（不发起网络请求；写事务内的兜底同步用）。"""
    s = str(value or "").strip()
    if not s:
        return None
    if _wsc_unavailable():
        return {"status": "service_unavailable", "detail": "名录文件不可读"}
    rec = _WSC_SPEC_LOW.get(s.lower())
    if rec:
        return {"status": "synonym" if rec.get("syn") else "accepted",
                "accepted": rec["acc"], "genus": rec["acc"].split()[0], "family": rec.get("f")}
    genus_only = s.split()[0] if s.split() else ""
    return {"status": "not_found", "genus_known": wsc_known(genus_only), "genus": genus_only or None}


def check_species_name(value, force=False, cols=None):
    # cols = 本表的科学名列名（taxon_cols）：DNA 提取/UCE 台账的这一列叫 Final Name，
    # 报错文案必须指到用户实际看到的字段，不能一律写标本表的列名
    cols = cols or {"final": "FinalSpeciesName", "temp": "TemporarySpeciesName"}
    if force:
        # 名录同步滞后时的逃生口：确认是 WSC 正式名后允许保存（跳过转换与校验）。
        # 属名取首词——但限定词开头的名称（如「cf. Jotus sp.」）首词不是属名，
        # 此时不下发属名（v4.31 修：全量联动曾把 26 行属名回写成了「cf.」）
        s = str(value or "").strip()
        first = s.split()[0] if s.split() else ""
        if first.lower().rstrip(".").rstrip(",") in ("cf", "cfr", "aff", "sp", "gen"):
            first = ""
        return {"name": s, "genus": first, "family": None, "corrected": False}
    if not str(value or "").strip():
        # 清空不是填名：留空合法（v4.31 修——空值此前会被当成未收录名称 400 拦下）
        return None
    res = species_normalize(value)
    if res is None:
        st = name_check_status(value)
        if st and st["status"] == "service_unavailable":
            # 服务不可用不是分类结论：明确告知暂无法核对，而不是「未收录」
            abort(503, f"名录服务暂不可用（{st.get('detail') or 'WSC 名录文件不可读'}）。"
                       f"{cols['final']}「{value}」暂无法核对，服务恢复后重试，"
                       f"或先用 {cols['temp']} 保留该名称（不会记为未收录）")
        abort(400, f"「{value}」未在 WSC（World Spider Catalog）收录，{cols['final']} 不允许保存。"
                   f"暂定名请填写 {cols['temp']} 字段；如确认该种已发表，请先核对拼写或刷新名录")
    return res


def _load_wsc():
    """加载 WSC 属级名录（wsc_genera.json，由 update_wsc.py 刷新）。"""
    path = os.path.join(CATALOG_DIR, "wsc_genera.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    lowmap = {x["g"].lower(): x for x in data}
    return lowmap, data


_WSC_LOW, _WSC_LIST = _load_wsc()


def wsc_known(genus):
    return bool(genus) and str(genus).strip().lower() in _WSC_LOW


def genus_unknown_warn(genus):
    if not genus or wsc_known(genus):
        return None
    for r in load_rows("taxonomy"):
        if r["deleted"]:
            continue
        g = r["data"].get("GENUS")
        if g and str(g).strip().lower() == str(genus).strip().lower():
            return None
    return f"属名“{genus}”未在 WSC 属级名录与分类信息表中找到，请核对拼写"


# ---------------------------------------------------------------- 鉴定历史/证据模型（v3.44）
_DET_STATUS_VALUES = ("待鉴定", "暂定", "已复核", "有争议")


def _latest_determination(conn, tname, rid):
    return conn.execute(
        "SELECT * FROM determinations WHERE tname=? AND rid=? ORDER BY seq DESC, id DESC LIMIT 1",
        (tname, rid)).fetchone()


def _det_protected(conn, tname, rid, row_data=None):
    """行是否受保护（后台名录校准不得覆盖）：人工锁定，或最近一次鉴定
    已人工确认（confirmed_by）或留有依据（evidence）。"""
    d = row_data or {}
    if str(d.get("DetLocked") or "").strip().lower() in ("1", "yes", "true", "是", "y"):
        return "人工锁定（DetLocked）"
    lat = _latest_determination(conn, tname, rid)
    if lat and (lat["confirmed_by"] or ""):
        return f"最近鉴定已由 {lat['confirmed_by']} 确认"
    if lat and (lat["evidence"] or "").strip():
        return "最近鉴定附有依据（evidence）"
    return None


def det_check_for(name_raw, det_by=None, det_date=None, note=None):
    """写事务前的名录核对（外部数据源访问不占数据库写锁）。
    汇总限定符解析 + 收录状态核对 + 鉴定人/日期/依据，产出 record_determination 所需字段。"""
    st = name_check_status(name_raw)
    if st is None:
        return None
    return {"name_raw": str(name_raw).strip(), "name_accepted": st.get("accepted"),
            "genus": st.get("genus"), "family": st.get("family"),
            "det_detail": st.get("detail"), "checklist_status": st["status"],
            "checklist_ver": _rules_versions().get("wsc_sync") or None,
            "determiner": (str(det_by).strip() or None) if det_by is not None else None,
            "det_date": (str(det_date).strip() or None) if det_date is not None else None,
            "evidence": (str(note).strip() or None) if note else None}


def record_determination(conn, tname, rid, det, *, source, user=None, batch_id=None,
                         det_status=None, replace_latest_check=False):
    """追加一条鉴定历史（同事务，不 commit）。seq 自动递增；
    source ∈ manual/create/import/calibration/backfill。返回落库后的记录 dict。"""
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM determinations "
                       "WHERE tname=? AND rid=?", (tname, rid)).fetchone()["n"]
    created_by = user or "system"
    conn.execute(
        "INSERT INTO determinations(tname,rid,seq,name_raw,name_accepted,genus,family,"
        "det_status,det_detail,determiner,det_date,evidence,confirmed_by,"
        "checklist_ver,checklist_status,source,batch_id,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tname, rid, seq, det["name_raw"], det.get("name_accepted"), det.get("genus"),
         det.get("family"), det_status or None, det.get("det_detail"), det.get("determiner"),
         det.get("det_date"), det.get("evidence"), det.get("confirmed_by"),
         det.get("checklist_ver"), det.get("checklist_status"), source, batch_id,
         created_by, now()))
    return {"seq": seq, "name_raw": det["name_raw"], "name_accepted": det.get("name_accepted"),
            "det_status": det_status, "checklist_status": det.get("checklist_status"),
            "source": source}


def _det_sync_status(conn, tname, rid, new_status, user):
    """人工修改 DetStatus（复核维度）→ 同步到最近一次鉴定记录：
    「已复核」登记确认人；改回其他状态视为撤回确认。无鉴定记录且行有名称时补建一条。"""
    if new_status not in _DET_STATUS_VALUES:
        return None
    lat = _latest_determination(conn, tname, rid)
    if lat:
        conn.execute("UPDATE determinations SET det_status=?, confirmed_by=? WHERE id=?",
                     (new_status, user if new_status == "已复核" else None, lat["id"]))
        return {"seq": lat["seq"], "synced": True}
    row = conn.execute("SELECT data FROM records WHERE tname=? AND id=?", (tname, rid)).fetchone()
    if not row:
        return None
    d = json.loads(row["data"])
    raw = str(d.get("FinalSpeciesName") or "").strip() or str(d.get("TemporarySpeciesName") or "").strip()
    if not raw:
        return None
    det = det_check_for(raw, det_by=d.get("DetBy"), det_date=d.get("DetDate"),
                        note=d.get("TaxonomyComment"))
    if det is None:
        return None
    # 写事务内的兜底：只读本地名录文件，不发起网络请求
    if det["checklist_status"] is None or det["name_accepted"] is None:
        st = _name_check_local(raw)
        det.update({"name_accepted": st.get("accepted"), "genus": st.get("genus"),
                    "family": st.get("family"), "checklist_status": st["status"]})
    return record_determination(conn, tname, rid, det, source="manual", user=user,
                                det_status=new_status)


def _det_after_write(conn, tname, rid, old, new, changes, *, user, batch_id, source, det_pre=None):
    """行写入后的鉴定历史挂钩（与调用方同事务）：
    名称变更 → 追加鉴定记录；DetStatus 人工变更 → 同步最近一次鉴定（确认/撤回）。
    det_pre 为写事务前 det_check_for 的结果（核对不占写事务）。"""
    if tname not in DET_TABLES:
        return None
    fs_o, fs_n = old.get("FinalSpeciesName"), new.get("FinalSpeciesName")
    tp_o, tp_n = old.get("TemporarySpeciesName"), new.get("TemporarySpeciesName")
    raw = None
    if not same(fs_o, fs_n) and str(fs_n or "").strip():
        raw = str(fs_n).strip()
    elif not same(tp_o, tp_n) and str(tp_n or "").strip():
        raw = str(tp_n).strip()
    if raw:
        if det_pre is None:
            # 兜底（导入等未预核对路径）：写事务内仅离线本地核对，不联网
            st = _name_check_local(raw)
            det_pre = {"name_raw": raw, "name_accepted": st.get("accepted"),
                       "genus": st.get("genus"), "family": st.get("family"),
                       "det_detail": None, "checklist_status": st["status"],
                       "checklist_ver": _rules_versions().get("wsc_sync") or None}
        det = dict(det_pre)
        det["determiner"] = det.get("determiner") or _nz(new.get("DetBy")) or _nz(old.get("DetBy"))
        det["det_date"] = det.get("det_date") or _nz(new.get("DetDate")) or _nz(old.get("DetDate"))
        if not det.get("evidence"):
            note = new.get("TaxonomyComment")
            det["evidence"] = _nz(note) if note and not same(old.get("TaxonomyComment"), note) else None
        explicit = new.get("DetStatus")
        status = explicit if explicit in _DET_STATUS_VALUES else (
            old.get("DetStatus") if old.get("DetStatus") in _DET_STATUS_VALUES else "暂定")
        return record_determination(conn, tname, rid, det, source=source, user=user,
                                    batch_id=batch_id, det_status=status)
    if "DetStatus" in changes:
        return _det_sync_status(conn, tname, rid, str(new.get("DetStatus") or ""), user)
    return None


def _nz(v):
    s = str(v or "").strip()
    return s or None


def _det_map_for(tname, rows):
    """导入预核对：为各行名称生成鉴定核对（写事务前调用；同名只核对一次）。
    批量导入只用本地名录离线核对（避免逐行在线查询拖长导入），限定的
    checklist_ver 照常落库；单行保存路径仍可在线核对。"""
    if tname not in DET_TABLES:
        return {}
    out = {}
    for d in rows:
        n = (str(d.get("FinalSpeciesName") or "").strip()
             or str(d.get("TemporarySpeciesName") or "").strip())
        if not n or n.lower() in out:
            continue
        st = _name_check_local(n)
        q = V.parse_det_name(n)
        out[n.lower()] = {"name_raw": n, "name_accepted": st.get("accepted"),
                          "genus": st.get("genus"), "family": st.get("family"),
                          "det_detail": q.get("detail"), "checklist_status": st["status"],
                          "checklist_ver": _rules_versions().get("wsc_sync") or None,
                          "determiner": _nz(d.get("DetBy")), "det_date": _nz(d.get("DetDate")),
                          "evidence": _nz(d.get("TaxonomyComment"))}
    return out


def _keep_taxon(out, tname):
    """分类链只回填本表真实存在的列：DNA 提取/UCE 台账的阶元比标本表少，
    写入不存在的列会留下用户看不到也改不了的字段并混进变更历史。"""
    if not tname:
        return out
    own = set(own_columns(tname))
    return {k: v for k, v in out.items() if k in own}


def taxonomy_fill(data, tname=None):
    """按属名从分类信息表匹配上级分类。属名命中时返回 {标本字段: 值}（以分类表为准）；
    未命中返回 {}，不影响手填。属名对应多条记录时优先取 Family 与当前数据一致的。"""
    g = data.get("Genus")
    if not g or not str(g).strip():
        return {}
    g_low = str(g).strip().lower()
    matches = []
    for r in load_rows("taxonomy"):
        if r["deleted"]:
            continue
        gen = r["data"].get("GENUS")
        if gen and str(gen).strip().lower() == g_low:
            matches.append(r["data"])
    if not matches:
        # 分类信息表未覆盖的属：用 WSC 属名录带出科（属等阶元联动）
        rec = _WSC_LOW.get(g_low)
        if rec:
            out = {"Order": "Araneae", "Family": rec["f"]}
            if rec.get("sf"):
                out["Subfamily"] = rec["sf"]
            return _keep_taxon(out, tname)
        return {}
    fam = data.get("Family")
    pick = next((m for m in matches if fam and m.get("Family") == fam), matches[0])
    return _keep_taxon({field: pick.get(src) for field, src in TAXONOMY_CHAIN}, tname)


# 已知阶元从细到粗：亚族 > 族 > Clade3 > Clade2 > Clade1 > 亚科 > 科（初鉴可能只到其中某级）
_UPWARD_RANKS = (("Subtribe", "SUBTRIBE"), ("Tribe", "TRIBE"),
                 ("Clade3", "CLADE3"), ("Clade2", "CLADE2"), ("Clade1", "CLADE1"),
                 ("Subfamily", "SUBFAMILY"), ("Family", "Family"))


def taxonomy_fill_upward(data, tname=None):
    """属名未知时的向上补齐：按已知的最细阶元在分类信息表中匹配，
    只补「更高的空缺阶元」，不覆盖任何已填内容、不猜测属名。
    返回需补齐的 {字段: 值}；无可匹配或信息冲突时返回 {}。"""
    known = {}
    for field, src in TAXONOMY_CHAIN:
        v = str(data.get(field) or "").strip()
        if v:
            known[field] = v
    if not known or "Genus" in known:
        return {}
    rows = [r["data"] for r in load_rows("taxonomy") if not r["deleted"]]
    if not rows:
        return {}
    # 从最细的已知阶元向上匹配
    for rank_field, col in _UPWARD_RANKS:
        v = known.get(rank_field)
        if not v:
            continue
        hits = [m for m in rows
                if str(m.get(col) or "").strip().lower() == v.lower()]
        if not hits:
            continue
        out = {}
        for field, src in TAXONOMY_CHAIN:
            if field == "Genus" or field in known:
                continue  # 只补空缺的更高阶元
            val = hits[0].get(src)
            if val:
                out[field] = val
        # 一致性检查：若补齐结果与已知的更高阶元冲突，则放弃（说明匹配有歧义）
        for f2 in known:
            if f2 in out and str(out[f2]).lower() != known[f2].lower():
                return {}
        return _keep_taxon(out, tname)
    return {}


KEY_PREFIX_RE = {"specimens": ("MHBU-ARA-", "MHBU #"), "mygalomorph": ("KYUARA#", "KYUARA #")}


# ---------------------------------------------------------------- 鉴定联动（v4.31）
# UCE 文库的物种鉴定以 DNA 提取台账为权威（tables_def.IDENT_LINK）：任一表修改
# 属/暂定名/最终名，把改动推给关联行；UCE 侧读取时逐行标注与权威行的差异。
# 关联键：UCE 行 Sample Code（界面名 DNAVoucherID）= 提取行 DNAVoucherID。

def _warn_append(warn, note):
    """warn 在既有代码里可能是 list（resolve_taxonomy）/ str / None，统一追加。"""
    if warn is None:
        return note
    if isinstance(warn, list):
        warn.append(note)
        return warn
    return f"{warn}；{note}"


def _ident_push_payload(tname, diff):
    """联动推送内容：{对方表列: 新值}。只取本次修改过的联动字段且新值非空——
    权威侧清空不抹掉对方已填信息（清空后的不一致由差异标记继续呈现）。"""
    lnk = TD.ident_link(tname)
    if not lnk:
        return {}, []
    changed_own = [f for f in lnk["cols"] if f in diff]
    payload = {}
    for f in changed_own:
        v = diff[f][1]
        if str(v or "").strip():
            payload[lnk["cols"][f]] = v
    return payload, changed_own


def _ident_targets(tname, new, old, payload):
    """联动目标行：按本行（写入后）关联键的现值在对端表精确匹配（值相等才联动，
    与关系注册表口径一致）。返回 (peer_t, [row, ...])。"""
    lnk = TD.ident_link(tname)
    if not lnk or not payload:
        return None, []
    kval = str(new.get(lnk["self_key"], old.get(lnk["self_key"])) or "").strip()
    if not kval:
        return lnk["peer"], []
    return lnk["peer"], [r for r in load_rows(lnk["peer"]) if not r["deleted"]
                         and str(r["data"].get(lnk["peer_key"]) or "").strip() == kval]


def _ident_sync_permission_error(user, tname, old, new, diff):
    """联动写权限预检（写事务前）：本次修改的联动字段确实会改到对方行的值、
    而用户在对方表没有编辑权限 → 返回 403 文案。与对方值一致的修改不拦（无需写）。"""
    payload, changed_own = _ident_push_payload(tname, diff)
    if not payload or not changed_own:
        return None
    peer_t, targets = _ident_targets(tname, new, old, payload)
    if not targets:
        return None
    if user["admin"] or (user.get("perms") or {}).get(peer_t) == "rw":
        return None
    dirty = [r for r in targets
             if any(not same(r["data"].get(dst), v) for dst, v in payload.items())]
    if not dirty:
        return None
    return (f"修改 {'、'.join(changed_own)} 需联动同步到「{TD.display_label(peer_t)}」"
            f"（{len(dirty)} 行将随本次修改更新），但您在该表只有查看权限。"
            f"请联系管理员调整权限，或先在「{TD.display_label(peer_t)}」中修改对应鉴定信息")


def _ident_apply_sync(user, tname, old, diff, new, batch_id):
    """应用联动（与主写入同事务同批次）：对每条值确有差异的关联行调用
    _update_record 完整通道——级联带出/鉴定历史/审计日志/行版本全部生效；
    _ident_sync=True 防回环，最终名走 force 口径（权威值原样复制，不重复过
    WSC 严格校验：台账存量含 cf./aff. 限定名，重新校验会把联动整体卡死）。"""
    payload, _changed = _ident_push_payload(tname, diff)
    if not payload:
        return None
    peer_t, targets = _ident_targets(tname, new, old, payload)
    if not targets:
        return None
    lnk = TD.ident_link(tname)
    peer_label = TD.display_label(peer_t)
    synced = skipped_perm = 0
    if not (user["admin"] or (user.get("perms") or {}).get(peer_t) == "rw"):
        return {"peer": peer_t, "peer_label": peer_label, "rows": 0,
                "skipped_perm": len(targets)}
    for r in targets:
        ch = {dst: v for dst, v in payload.items() if not same(r["data"].get(dst), v)}
        if not ch:
            continue
        _update_record(user, peer_t, r["id"],
                       {"changes": ch, "allow_overwrite": True,
                        "overwrite_reason": f"鉴定联动：与「{TD.display_label(tname)}」的修改保持一致"},
                       batch_id=batch_id, _ident_sync=True)
        synced += 1
    out = {"peer": peer_t, "peer_label": peer_label, "rows": synced,
           "total_linked": len(targets)}
    return out


def _ident_diff_mark(lnk, data, peer_idx):
    """UCE 行与权威提取行的差异：{本表列: 权威值}。权威侧为空的字段不算差异
    （没有可依据的鉴定），本侧为空而权威侧有值算差异（鉴定待补齐）。"""
    peer = peer_idx.get(str(data.get(lnk["self_key"]) or "").strip())
    if not peer:
        return None
    out = {}
    for own_col, peer_col in lnk["cols"].items():
        pv = str(peer.get(peer_col) or "").strip()
        if pv and str(data.get(own_col) or "").strip() != pv:
            out[own_col] = peer.get(peer_col)
    return out or None


def _ident_peer_index(lnk):
    """权威表 主键值 -> 行数据（重复键取第一条；空键不入索引）。"""
    idx = {}
    for r in load_rows(lnk["peer"]):
        if r["deleted"]:
            continue
        k = str(r["data"].get(lnk["peer_key"]) or "").strip()
        if k and k not in idx:
            idx[k] = r["data"]
    return idx


def _ident_create_pull(tname, data, user):
    """新增非权威侧行（UCE 文库）时以权威行补齐【留空的】联动字段——
    「物种鉴定以 DNA 提取表为准」在录入时刻即生效。用户显式填写的值不动
    （与权威行的差异由差异标记呈现，不静默覆盖用户的输入）。"""
    lnk = TD.ident_link(tname)
    if not lnk or lnk["authority"]:
        return None
    if not (user["admin"] or (user.get("perms") or {}).get(lnk["peer"]) in ("r", "rw")):
        return None  # 看不到权威表：不阻断新增，也不拉取
    kval = str(data.get(lnk["self_key"]) or "").strip()
    if not kval:
        return None
    peer = _ident_peer_index(lnk).get(kval)
    if not peer:
        return None
    filled = []
    for own_col, peer_col in lnk["cols"].items():
        if not str(data.get(own_col) or "").strip():
            pv = str(peer.get(peer_col) or "").strip()
            if pv:
                data[own_col] = pv
                filled.append(own_col)
    return {"peer_label": lnk["peer_label"], "filled": filled} if filled else None


def _ident_create_push(user, tname, data, batch_id):
    """新增权威侧行（DNA 提取台账）时把非空联动字段推给已引用本凭证的 UCE 行
    （新增行即刻「以台账为准」；与修改联动同一非空规则）。反方向不推：新增
    非权威侧行走 _ident_create_pull 拉取，用户显式填写的值不覆盖权威行。
    对方表无写权限时跳过并在响应中说明，不阻断新增本身。"""
    lnk = TD.ident_link(tname)
    if not lnk or not lnk["authority"]:
        return None
    payload = {b: data.get(a) for _n, a, b in TD.IDENT_LINK["fields"]
               if str(data.get(a) or "").strip()}
    if not payload:
        return None
    kval = str(data.get(lnk["self_key"]) or "").strip()
    if not kval:
        return None
    targets = [r for r in load_rows(lnk["peer"]) if not r["deleted"]
               and str(r["data"].get(lnk["peer_key"]) or "").strip() == kval]
    if not targets:
        return None
    peer_label = TD.display_label(lnk["peer"])
    if not (user["admin"] or (user.get("perms") or {}).get(lnk["peer"]) == "rw"):
        return {"peer": lnk["peer"], "peer_label": peer_label, "rows": 0,
                "skipped_perm": len(targets)}
    synced = 0
    for r in targets:
        ch = {b: v for b, v in payload.items() if not same(r["data"].get(b), v)}
        if not ch:
            continue
        _update_record(user, lnk["peer"], r["id"],
                       {"changes": ch, "allow_overwrite": True,
                        "overwrite_reason": f"鉴定联动：新增「{TD.display_label(tname)}」行后保持一致"},
                       batch_id=batch_id, _ident_sync=True)
        synced += 1
    return {"peer": lnk["peer"], "peer_label": peer_label, "rows": synced,
            "total_linked": len(targets)}


def normalize_spec_key(v, kind="specimens"):
    """服务端编号归一：
    specimens: MHBU-ARA-1234 / mhbu ara 1234 / 1234 → MHBU-ARA-00001234
    mygalomorph: KYUARA#497 / kyuara 497 / 497 → KYUARA#0497
    其他格式原样保留。"""
    import re as _re
    if v is None:
        return v
    s = str(v).strip()
    if not s:
        return s
    if kind == "mygalomorph":
        up = s.upper().replace(" ", "")
        m = _re.match(r"^KYUARA[#-]?(.+)$", up)
        if m:
            rest = m.group(1)
            return "KYUARA#" + (rest.zfill(4) if rest.isdigit() else rest)
        if up.isdigit():
            return "KYUARA#" + up.zfill(4)
        return s
    up = s.upper().replace(" ", "")
    m = _re.match(r"^MHBU[- ]?ARA[- ]?(.+)$", up)
    if m:
        rest = m.group(1)
        return "MHBU-ARA-" + (rest.zfill(8) if rest.isdigit() else rest)
    if up.isdigit():
        return "MHBU-ARA-" + up.zfill(8)
    return s


def _box_for_number(mhbu_val):
    """按 8 位编号匹配已登记的标本盒（管理员在台账指定号段）。返回盒名或 None。"""
    import re as _re
    m = _re.match(r"^MHBU-ARA-(\d{8})$", str(mhbu_val or "").strip())
    if not m:
        return None
    num = int(m.group(1))
    for b in get_db().execute("SELECT * FROM boxes ORDER BY num_start").fetchall():
        if b["num_start"] <= num <= b["num_end"]:
            return b["name"]
    return None


def _get_required_fields(tname):
    """从表配置读取必填字段列表。"""
    return TABLES[tname].get("required_fields") or []


def _check_required(tname, data, exclude_rid=None, only_clearing=False):
    """按表配置的必填字段校验。
    创建时严格：必填字段全填。编辑时宽松：只拦截清空已有必填值的操作。"""
    required = _get_required_fields(tname)
    if not required:
        return
    if only_clearing:
        cleared = [f for f in required
                   if f in data and not str(data.get(f) or "").strip()
                   and str((data.get("_old") or {}).get(f) or "").strip()]
        if cleared:
            abort(400, f"不能清空必填字段：{'、'.join(cleared)}")
        return
    missing = [f for f in required if not str(data.get(f) or "").strip()]
    if missing:
        abort(400, f"必填字段缺失：{'、'.join(missing)}，请补齐后再保存")
    if tname == "pseudoscorpiones":
        count_fields = ("标本数量", "雄性数量", "雌性数量", "幼体数量")
        if not any(str(data.get(f) or "").strip() for f in count_fields):
            abort(400, "标本数量 / 雄性数量 / 雌性数量 / 幼体数量 至少需要填写一项")


def _compute_position(box_name, mhbu_val):
    """按编号在盒内的顺序推算位置坐标 [行,列]。
    容量为完全平方数按 √容量 列数（36→6×6，100→10×10），否则按每行 10 列。"""
    v = str(box_name or "").strip()
    b = get_db().execute("SELECT * FROM boxes WHERE name=?", (v,)).fetchone() if v else None
    if not b:
        return None
    m = re.match(r"^MHBU-ARA-(\d{8})$", str(mhbu_val or "").strip())
    if not m:
        return None
    num = int(m.group(1))
    if not (b["num_start"] <= num <= b["num_end"]):
        return None
    off = num - b["num_start"]
    cap = b["num_end"] - b["num_start"] + 1
    cols = math.isqrt(cap)
    if cols * cols != cap:
        cols = 10
    return f"[{off // cols + 1},{off % cols + 1}]"


def _check_box_number_range(box_value, mhbu_val):
    """手选盒 + 编号必须落在该盒登记号段内（盒先行的数据约束）。"""
    v = str(box_value or "").strip()
    if not v:
        return
    b = get_db().execute("SELECT * FROM boxes WHERE name=?", (v,)).fetchone()
    if not b:
        return  # 未登记的盒不拦（兼容历史值）
    m = re.match(r"^MHBU-ARA-(\d{8})$", str(mhbu_val or "").strip())
    if not m:
        abort(400, f"已选择标本盒「{v}」：MHBU 编号需为 8 位数字（如 MHBU-ARA-{b['num_start']:08d}）")
    num = int(m.group(1))
    if not (b["num_start"] <= num <= b["num_end"]):
        abort(400, f"编号 MHBU-ARA-{num:08d} 不在所选盒「{v}」号段内（{b['num_start']:08d} – {b['num_end']:08d}），请核对盒或编号")


def _check_box_available(box_value, exclude_rid=None):
    """盒号被登记且已用满时，拒绝再选（单行 UI 保存路径；导入不受限）。"""
    v = str(box_value or "").strip()
    if not v:
        return
    m = BOX_PAT.match(v)
    if not m:
        return
    conn = get_db()
    b = conn.execute("SELECT * FROM boxes WHERE name=?", (v,)).fetchone()
    if not b:
        return  # 未登记的盒不拦截（兼容历史/外部值）
    used = 0
    pat = re.compile(r"^MHBU-ARA-(\d{8})$")
    for r in load_rows("specimens"):
        if r["deleted"] or r["id"] == exclude_rid:
            continue
        mm = pat.match(str(r["data"].get("MHBU #") or ""))
        if mm and b["num_start"] <= int(mm.group(1)) <= b["num_end"]:
            used += 1
    total = b["num_end"] - b["num_start"] + 1
    if used >= total:
        abort(400, f"标本盒「{v}」的号段已用满（{used}/{total}），请选择其他标本盒")


def normalize_locked_enums(tname, data, apply_defaults=False):
    """锁定枚举归一：大小写不敏感匹配选项；不匹配则回退默认值。
    apply_defaults=True 时（新增行），空缺字段自动填默认值。"""
    colmeta = {c["name"]: c for c in TABLES[tname]["columns"]}
    for k, v in list(data.items()):
        col = colmeta.get(k)
        if not col or not col.get("enum_locked") or v is None:
            continue
        opts = col.get("options") or []
        match = next((o for o in opts if str(o).lower() == str(v).lower()
                      or (isinstance(o, (int, float)) and str(o) == str(v))), None)
        if match is None and isinstance(v, (int, float)):
            match = next((o for o in opts if isinstance(o, (int, float)) and abs(o - v) < 0.01), None)
        data[k] = match if match is not None else (col.get("default") or "")
    if apply_defaults:
        for c in TABLES[tname]["columns"]:
            if c.get("enum_locked") and c.get("default") and not data.get(c["name"]):
                data[c["name"]] = c["default"]


def check_key_unique(tname, value, exclude_rid=None):
    """业务主键查重：编号已存在（未删除的行）则报 400。exclude_rid 用于编辑自身。"""
    if value is None or str(value).strip() == "":
        return
    cfg = TABLES[tname]
    key = cfg["key"]
    if not key or cfg.get("key_not_unique"):
        return
    v = str(value).strip()
    for r in load_rows(tname):
        if r["deleted"] or r["id"] == exclude_rid:
            continue
        if str(r["data"].get(key) or "").strip() == v:
            abort(400, f"「{value}」已存在（与现有记录的 {key} 重复），请核对编号后重新输入")


def normalize_alcohol(v):
    """酒精浓度归一：0.75 / 75 / "75%" / "75%ethanol" → 统一数字 75（百分比）。
    带文字描述的值（如 "95%（死后泡）"）原样保留（历史信息不能丢）。"""
    if v is None:
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        if 0 < f <= 1.0001:  # 小数比例 → 百分比
            return round(f * 100) if abs(f * 100 - round(f * 100)) < 0.01 else round(f * 100, 1)
        # v4.34：整数百分比返回 int（此前返回 75.0 浮点，与字符串枚举选项
        # （伪蝎「保存条件」["75",...]）匹配失败，被锁定枚举回退成默认值 95）
        return int(round(f)) if abs(f - round(f)) < 0.001 else round(f, 1)
    s = str(v).strip()
    if not s:
        return v
    import re as _re
    # 裸小数（0.75）→ 乘 100；"数字+%"或"数字+英文"（75%ethanol）→ 取数字
    # 含中文描述的（如 95%（死后泡））保留原文
    if _re.fullmatch(r"0\.[0-9]+|1(\.0{0,2})?", s):
        # "1" / "1.0" / "0.95" 等比例写法 → 百分比（"1" 即 100%）
        f = float(s)
        if 0 < f <= 1.0001:
            return round(f * 100) if abs(f * 100 - round(f * 100)) < 0.01 else round(f * 100, 1)
    if _re.fullmatch(r"[0-9]{2,3}", s):
        # 纯整数（95 / 100）→ 直接百分比数字
        n = int(s)
        if 0 < n <= 100:
            return n
    m = _re.fullmatch(r"([0-9]+(?:[.][0-9]+)?)\s*(%|％)(.*)", s)
    if m and not _re.search(r"[一-鿿]", m.group(3) or ""):
        num = float(m.group(1))
        return round(num) if abs(num - round(num)) < 0.01 else num
    return s


def validate_field_values(tname, updates):
    """数字列严格校验（数量列允许「未知」标记，不当作 0）；枚举列给建议但不强制。"""
    colmeta = {c["name"]: c for c in TABLES[tname]["columns"]}
    for k, v in updates.items():
        col = colmeta.get(k)
        if not col or v is None:
            continue
        if col.get("numeric"):
            if k in FR.COUNT_FIELDS_ALL and isinstance(v, str)                     and v.strip() in FR.COUNT_UNKNOWN:
                continue  # 「未知」按缺失语义原样保留
            ok = isinstance(v, (int, float)) or (
                isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()))
            if not ok:
                abort(400, f"列“{k}”必须填数字或「未知」（当前输入：“{v}”）；如需留空请清空内容")


# ---------------------------------------------------------------- 手填采集信息建档与复用
EVENT_MATCH_FIELDS = ["Country", "Province", "City", "County", "Locality", "Collector",
                      "DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2"]


def _event_sig(data):
    """采集要素签名：地点 + 采集人 + 日期（地名标准化后比较）。"""
    out = []
    for f in EVENT_MATCH_FIELDS:
        v = str(data.get(f) or "").strip()
        if f == "Country":
            v = geo.norm_country(v)
        elif f == "Province":
            v = geo.norm_province(v)
        elif f == "City":
            v = geo.norm_city(v)
        out.append(v.lower())
    return tuple(out)


def _find_same_event(data):
    """查找采集要素完全相同的已有事件（供编号复用）；无地点要素时不匹配。"""
    sig = _event_sig(data)
    if not any(sig[:6]):  # 国家/省/市/县/地点/采集人全空 → 无可比性
        return None
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        if _event_sig(r["data"]) == sig:
            return r
    return None


LATLON_FIELDS = ("Latitude1", "Longitude1", "Latitude2", "Longitude2")


def _geo_convert_fields(data):
    """就地把度分(秒)/装饰小数度坐标转为规范小数度。返回 {字段: 原值}。"""
    changed = {}
    for f in LATLON_FIELDS:
        v = data.get(f)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        conv, kind = geo.normalize_coord_value(s)
        if conv is None:
            continue
        if kind == "decorated":
            try:
                if float(conv) == float(s):
                    continue  # 数值未变（仅空格差异），不落无意义改动
            except (TypeError, ValueError):
                pass
        changed[f] = v
        data[f] = conv
    return changed


def _normalize_row_alcohol(tname, data):
    """行级酒精浓度归一（导入/粘贴路径与单行保存同口径）：0.95 → 95、"95%" → 95。"""
    col = TABLES.get(tname, {}).get("alcohol_col")
    if col and col in data:
        data[col] = normalize_alcohol(data[col])
    return data


def _find_suspect_duplicate(tname, data, exclude_rid=None):
    """疑似重复录入：同采集事件 + 同属/种 + 同性别数量结构 的其他有效行。
    命中返回 {rid, key}；用于保存前提醒（可强制保存）。"""
    cfg = TABLES.get(tname)
    if not cfg:
        return None
    ev_col = cfg.get("linked_key") or "CollectionEventNumber"
    ev = str(data.get(ev_col) or "").strip()
    genus = str(data.get("Genus") or data.get("属") or "").strip()
    species = str(data.get("FinalSpeciesName") or data.get("Species name") or data.get("种") or "").strip()
    if not ev or not (genus or species):
        return None  # 缺关键锚点不做猜测
    conds, args = ["deleted=0", "tname=?"], [tname]
    conds.append("json_extract(data, '$." + ev_col + "') = ?")
    args.append(ev)
    if genus:
        gcol = "Genus" if "Genus" in [c["name"] for c in cfg["columns"]] else "属"
        conds.append("json_extract(data, '$." + gcol + "') = ?")
        args.append(genus)
    sp_col = next((c["name"] for c in cfg["columns"] if c["name"] in ("FinalSpeciesName", "Species name", "种")), None)
    if sp_col and species:
        conds.append("json_extract(data, '$." + sp_col + "') = ?")
        args.append(species)
    if exclude_rid:
        conds.append("id != ?")
        args.append(exclude_rid)
    try:
        row = get_db().execute(
            f"SELECT id, data FROM records WHERE {' AND '.join(conds)} ORDER BY id LIMIT 1", args).fetchone()
    except Exception:
        return None
    if not row:
        return None
    key = cfg.get("key")
    d = json.loads(row["data"])
    return {"rid": row["id"], "key": d.get(key) if key else f"#{row['id']}"}


@app.post("/api/rows/<tname>")

def api_create(tname):
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    body = request.get_json(force=True)
    conn0 = get_db()
    ctx = _idem_ctx(user, f"row_create:{tname}", body)
    hit = _idem_lookup(conn0, ctx)
    if hit:
        return hit
    data_in = body.get("data") or {}
    try:
        # 统一校验服务层：白名单（未知字段 400）+ 关联列指引
        data = V.whitelist(tname, data_in)
    except V.WriteValidationError as e:
        abort(400, e.message)
    if cfg.get("geo_normalize"):
        geo.normalize_geo(data)  # 地名标准化
        _geo_convert_fields(data)  # 度分秒坐标 → 小数度
    # 鉴定历史预核对（写事务前；name_raw 取键入原文，名录修正不改写原文）
    det_pre = None
    if cfg.get("genus_lookup") and tname in DET_TABLES:
        _typed = (str(data_in.get("FinalSpeciesName") or "").strip()
                  or str(data_in.get("TemporarySpeciesName") or "").strip())
        if _typed:
            det_pre = det_check_for(_typed, det_by=data_in.get("DetBy"),
                                    det_date=data_in.get("DetDate"),
                                    note=data_in.get("TaxonomyComment"))
    # 分类/名录解析（外部名录文件读取）在写事务之前完成；单位与枚举归一走服务层
    warn = None
    try:
        data, warn = V.resolve_taxonomy(tname, data,
                                        force_name=bool(body.get("force_name")))
    except ValueError:
        data = data
    # 鉴定联动·新增拉取（UCE 文库）：Sample Code 对得上提取台账时，以台账为准
    # 补齐留空的属/暂定名/最终名（权威值原样复制，不过 WSC 严格校验——台账存量
    # 含 cf./aff. 限定名）。放在严格解析之后：用户显式键入的名称仍按原口径校验。
    pull_info = _ident_create_pull(tname, data, user)
    if pull_info and pull_info.get("filled"):
        if "Genus" in pull_info["filled"]:
            for k, v in taxonomy_fill(data, tname).items():
                if not str(data.get(k) or "").strip():
                    data[k] = v  # 拉取得属名后按本表口径补齐仍空缺的阶元列
        warn = _warn_append(warn, "已按「%s」补齐空缺鉴定字段（%s）"
                            % (pull_info["peer_label"], "、".join(pull_info["filled"])))
    if det_pre and tname in DET_TABLES and not str(data.get("DetStatus") or "").strip():
        data["DetStatus"] = "暂定"  # 新记录的鉴定一律「暂定」，已复核需人工确认
    _alc_raw = None
    if cfg.get("alcohol_col") and cfg["alcohol_col"] in data:
        _alc_raw = data[cfg["alcohol_col"]]
        data[cfg["alcohol_col"]] = normalize_alcohol(data[cfg["alcohol_col"]])
    normalize_locked_enums(tname, data, apply_defaults=True)
    validate_field_values(tname, data)
    if tname in KEY_PREFIX_RE and data.get("MHBU #"):
        data["MHBU #"] = normalize_spec_key(data["MHBU #"], kind="specimens")
    if tname == "mygalomorph" and data.get("KYUARA #"):
        data["KYUARA #"] = normalize_spec_key(data["KYUARA #"], kind="mygalomorph")
    try:
        V.validate_constraints(tname, data)
    except V.WriteValidationError as e:
        abort(400, e.message)
    # 疑似重复录入提醒（同事件+同种）：未明确 force_dup 时拦截确认
    if not (request.get_json(silent=True) or {}).get("force_dup"):
        dup = _find_suspect_duplicate(tname, data)
        if dup:
            return jsonify({"error": f"疑似重复录入：同采集事件下已有标本「{dup['key']}」的属/种与本次相同。"
                                     f"若确为不同标本请选择继续保存", "suspect_dup": dup}), 409
    box_manual = "Box # (Start#-End#)" in data_in
    if tname == "specimens" and data.get("MHBU #"):
        box_auto = _box_for_number(data["MHBU #"])
        if box_auto and not data.get("Box # (Start#-End#)"):
            data["Box # (Start#-End#)"] = box_auto
            box_manual = False
    if tname == "specimens" and data.get("Box # (Start#-End#)") and box_manual:
        _check_box_available(data["Box # (Start#-End#)"])
        _check_box_number_range(data["Box # (Start#-End#)"], data.get("MHBU #"))
    if tname == "specimens" and not str(data.get("Position") or "").strip():
        pos = _compute_position(data.get("Box # (Start#-End#)"), data.get("MHBU #"))
        if pos:
            data["Position"] = pos
    # 主键查重已并入 V.validate_constraints
    if cfg.get("genus_lookup"):
        warn = genus_unknown_warn(data.get("Genus")) or warn
    if tname == "pseudoscorpiones":
        data, wac_warn = _wac_apply(tname, data)
        if wac_warn:
            warn = wac_warn  # resolve_taxonomy 已含 WAC，此分支兜底其它入口
    # 采集地信息：事件号留空 → 手填信息自动建档；已有完全相同的采集要素则提示复用编号
    if tname == "events" and not (data.get(cfg["key"]) or "").strip():
        force_new = bool((request.get_json(silent=True) or {}).get("force"))
        if not force_new:
            dup = _find_same_event(data)
            if dup:
                ev_cfg = TABLES["events"]
                dup_key = (dup["data"] or {}).get(ev_cfg["key"]) or f"#{dup['id']}"
                return jsonify({
                    "error": f"已存在完全相同的采集信息（编号 {dup_key}），建议直接复用该编号",
                    "duplicate": {"key": dup_key, "rid": dup["id"]},
                }), 409
        n = get_db()
        seq = n.execute("SELECT COUNT(*) c FROM records WHERE tname='events'").fetchone()["c"] + 1
        cand = f"M-{seq:04d}"
        used = {str(r["data"].get("CollectionEventNumber") or "").strip()
                for r in load_rows("events")}
        while cand in used:
            seq += 1
            cand = f"M-{seq:04d}"
        data[cfg["key"]] = cand
    conn = conn0
    try:
        cur = conn.execute(
            "INSERT INTO records(tname,data,updated_at,updated_by,deleted,row_version) VALUES(?,?,?,?,0,1)",
            (tname, json.dumps(data, ensure_ascii=False), now(), user["username"]),
        )
        key = cfg["key"]
        bid = _new_batch("C")
        log_edit(conn, user["username"], tname, cur.lastrowid,
                 data.get(key) if key else None, "create", {"_data": data}, batch_id=bid)
        if tname in DET_TABLES:
            _det_after_write(conn, tname, cur.lastrowid, {}, data, data, user=user["username"],
                             batch_id=bid, source="create", det_pre=det_pre)
        if _alc_raw is not None and str(_alc_raw) != str(data.get(cfg["alcohol_col"])):
            conn.execute("INSERT INTO field_raw(tname,rid,field,raw,canonical,rules_ver,created_by,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (tname, cur.lastrowid, cfg["alcohol_col"], str(_alc_raw),
                          str(data.get(cfg["alcohol_col"])), FR.VERSION, user["username"], now()))
        # 鉴定联动·新增推送（DNA 提取台账）：已有引用本凭证的 UCE 行即刻同步台账鉴定
        push_info = _ident_create_push(user, tname, data, bid)
        payload = {"ok": True, "id": cur.lastrowid, "warn": warn, "batch": bid,
                   "key": data.get(key), "row_version": 1}
        if push_info:
            payload["ident_sync"] = push_info
            if push_info.get("skipped_perm"):
                payload["warn"] = _warn_append(
                    warn, "%d 行「%s」因您在该表无编辑权限未联动同步"
                          % (push_info["skipped_perm"], push_info["peer_label"]))
    except sqlite3.IntegrityError:
        # 主键表达式唯一索引兜底：并发同编号时应用层查重存在竞态窗口，由索引拦截
        _tx_rollback(conn)
        abort(400, f"「{data.get(cfg['key'])}」已存在（另一请求刚写入同一编号），请勿重复提交")
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    invalidate(tname)
    return _finish_write(conn, payload, ctx)


_MISSING = object()


def _last_field_editors(conn, tname, rid, fields, limit=200):
    """从编辑日志反查每个字段最近一次修改人/时间（供冲突详情展示）。"""
    out, want = {}, set(fields)
    for e in conn.execute(
            "SELECT user, ts, changes FROM edits WHERE tname=? AND rid=? AND action IN ('update','import') "
            "ORDER BY id DESC LIMIT ?", (tname, rid, limit)):
        try:
            ch = json.loads(e["changes"] or "{}")
        except (ValueError, TypeError):
            continue
        for k in list(want):
            if k in ch:
                out[k] = (e["user"], e["ts"])
                want.discard(k)
        if not want:
            break
    return out


def _update_record(user, tname, rid, body, batch_id=None, _ident_sync=False):
    """单行修改：校验走统一服务层；本函数只做写入（UPDATE+日志），不 commit——
    事务由调用方管理（_finish_write / 批量编排）。冲突以 WriteConflict 抛出。
    _ident_sync=True 为鉴定联动的内部写入（对方表回填）：最终名按权威值原样
    复制（force 口径，不再过 WSC 严格校验），且不再触发下一轮联动（防回环）。"""
    changes_in = body.get("changes") or {}
    force = bool(body.get("force"))
    try:
        V.check_changes_writable(tname, changes_in)
    except V.WriteValidationError as e:
        abort(400, e.message)
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row or row["deleted"]:
        abort(404, "记录不存在或已删除")
    old = json.loads(row["data"])
    cur_ver = row["row_version"]

    # ---- 乐观锁：base_version 条件更新 + 字段级三方合并 ----
    # base_version == 当前版本        → 全部修改直接应用
    # base_version 过期（未 force）    → 逐字段合并：服务器未改的字段安全合并，双方同改 → 409
    # force 必须携带本次看到的版本：匹配 → 覆盖并留痕（原因+被覆盖修订）；不匹配 → 409 重新确认
    # 旧客户端（仅 base_updated_at）   → 沿用时间戳比对；两者都缺 → 400 拒绝，绝不静默覆盖
    base_version = body.get("base_version")
    if base_version is not None:
        try:
            base_version = int(base_version)
        except (TypeError, ValueError):
            abort(400, "base_version 需为整数（先读取行获取当前 row_version）")
    base_updated_at = body.get("base_updated_at")
    base_values = body.get("base_values") if isinstance(body.get("base_values"), dict) else None
    override_log = None
    conflict_fields = {}

    def _conflict_response():
        editors = _last_field_editors(conn, tname, rid, conflict_fields.keys())
        fields = {}
        for k, (cb, cur, _nv) in conflict_fields.items():
            by, at = editors.get(k, (row["updated_by"], row["updated_at"]))
            fields[k] = {"base": cb, "current": cur,
                         "proposed": clean_val(changes_in[k]), "by": by, "at": at}
        return {
            "error": f"该行刚被 {row['updated_by']} 于 {row['updated_at']} 修改过，请核对后选择合并或覆盖",
            "conflict": {"by": row["updated_by"], "at": row["updated_at"],
                         "row_version": row["row_version"],
                         "fields": fields,
                         "values": {k: old.get(k) for k in changes_in}},
        }

    if base_version is not None:
        if force:
            if base_version != cur_ver:
                raise WriteConflict(_conflict_response())  # 确认期间又被第三方修改：覆盖基准已失效
            override_log = {"reason": str(body.get("overwrite_reason") or "用户确认覆盖"),
                            "overridden_version": cur_ver}
        elif base_version != cur_ver:
            for k, v in changes_in.items():
                nv = clean_val(v)
                cur = old.get(k)
                cb = base_values.get(k, _MISSING) if base_values else _MISSING
                if cb is not _MISSING and same(cur, cb):
                    continue  # 服务器在我加载后未改过该字段 → 安全合并
                if same(cur, nv):
                    continue  # 拟改值已是服务器现值 → 无需写
                conflict_fields[k] = (cb if cb is not _MISSING else None, cur, nv)
            if conflict_fields:
                raise WriteConflict(_conflict_response())
            changes_in = {k: v for k, v in changes_in.items()
                          if not same(old.get(k), clean_val(v))}
    elif force:
        if base_updated_at:
            override_log = {"reason": "旧客户端 force（时间戳不一致仍覆盖）",
                            "overridden_version": cur_ver}
        else:
            abort(400, "force 覆盖必须携带 base_version（本次冲突时看到的 row_version）")
    elif not base_updated_at and not body.get("allow_overwrite"):
        abort(400, "缺少 base_version：为防覆盖他人修改，写入必须携带版本"
                   "（旧客户端可传 base_updated_at，脚本可传 allow_overwrite: true 并注明原因）")
    elif body.get("allow_overwrite"):
        override_log = {"reason": str(body.get("overwrite_reason") or "脚本 allow_overwrite 覆盖"),
                        "overridden_version": cur_ver}
    elif row["updated_at"] != base_updated_at:
        raise WriteConflict(_conflict_response())  # 旧客户端时间戳比对（行为与历史一致）

    merged_fields = [k for k in changes_in] if (base_version is not None and base_version != cur_ver and not force) else []
    diff = {}
    for k, v in changes_in.items():
        nv = clean_val(v)
        if not same(old.get(k), nv):
            diff[k] = [old.get(k), nv]
    if not diff:
        return {"ok": True, "noop": True, "row_version": cur_ver,
                "updated_at": row["updated_at"]}
    # 鉴定联动写权限预检（写事务前）：改联动字段且确实要改到对方行、而对方表无
    # 编辑权限 → 403 拦下（联动是本次修改语义的一部分，不能只写一半留下暗差异）
    if not _ident_sync and TD.ident_link(tname):
        _new_probe = dict(old)
        _new_probe.update({k: p[1] for k, p in diff.items()})
        _perm_err = _ident_sync_permission_error(user, tname, old, _new_probe, diff)
        if _perm_err:
            abort(403, _perm_err)
    # FinalSpeciesName 受控校验：改的必须是 WSC 收录名称，异名自动转接受名，属联动
    cfg = TABLES[tname]
    warn = None
    # 鉴定历史预核对（外部名录访问在写事务之前；name_raw 取用户键入原文）
    det_pre = None
    if cfg.get("genus_lookup") and tname in DET_TABLES:
        nf = ("FinalSpeciesName" if "FinalSpeciesName" in diff
              else ("TemporarySpeciesName" if "TemporarySpeciesName" in diff else None))
        if nf and str(diff[nf][1] or "").strip():
            det_pre = det_check_for(
                diff[nf][1],
                det_by=diff["DetBy"][1] if "DetBy" in diff else old.get("DetBy"),
                det_date=diff["DetDate"][1] if "DetDate" in diff else old.get("DetDate"),
                note=diff["TaxonomyComment"][1] if "TaxonomyComment" in diff else None)
            # 鉴定变更后复核状态复位为「暂定」（同请求显式给出的 DetStatus 优先）
            if det_pre and "DetStatus" not in diff and not same(old.get("DetStatus"), "暂定"):
                diff["DetStatus"] = [old.get("DetStatus"), "暂定"]
    tc = TD.taxon_cols(tname)
    if cfg.get("genus_lookup") and tc["final"] in diff:
        # 联动回填按 force 口径：权威行的存量值（含 cf./aff. 等限定名）原样复制，
        # 不再过 WSC 严格校验（校验会把联动整体卡死，且名录在线核对拖慢写入）
        sp = check_species_name(diff[tc["final"]][1],
                                force=bool(body.get("force_name")) or _ident_sync, cols=tc)
        if sp:
            nv_name, sp_gen = sp["name"], sp["genus"]
            if nv_name != diff[tc["final"]][1]:
                diff[tc["final"]] = [diff[tc["final"]][0], nv_name]
            # 联动回填时属名只来自权威行的属名字段（payload 带 Genus 则已在上）——
            # 绝不用学名首词改写：权威行可能是「属名列 Phintella + 最终名 Lechia
            # squamata」这类内部不一致数据，首词改写会凭空制造与权威行的差异
            if (sp_gen and not _ident_sync and not same(old.get(tc["genus"]), sp_gen)
                    and tc["genus"] not in diff
                    and diff[tc["final"]][1] != diff[tc["final"]][0]):
                diff[tc["genus"]] = [old.get(tc["genus"]), sp_gen]
            if sp["corrected"]:
                warn = f"已按 WSC 将物种名更新为接受名「{sp['name']}」"
    # 属名变了 → 上级分类整条链按分类信息表自动带出（同一日志条目内逐字段可溯）
    if cfg.get("genus_lookup") and "Genus" in diff:
        tentative = dict(old)
        tentative.update({k: p[1] for k, p in diff.items()})
        for k, v in taxonomy_fill(tentative, tname).items():
            if k not in diff and not same(old.get(k), v):
                diff[k] = [old.get(k), v]
        warn = genus_unknown_warn(diff["Genus"][1])
    # 族/亚科/Clade/科级修改（属名未动）→ 向上补齐更高阶元
    elif cfg.get("genus_lookup") and not old.get("Genus"):
        for f in ("Subtribe", "Tribe", "Clade3", "Clade2", "Clade1", "Subfamily", "Family"):
            if f in diff:
                tentative = dict(old)
                tentative.update({k: p[1] for k, p in diff.items()})
                for k, v in taxonomy_fill_upward(tentative, tname).items():
                    if k not in diff and not same(old.get(k), v):
                        diff[k] = [old.get(k), v]
                break
    # 盒或编号变更 → 位置坐标自动重算（本次未手工改 Position 时）
    if tname == "specimens":
        if ("Box # (Start#-End#)" in diff or "MHBU #" in diff) and "Position" not in diff:
            box_now = diff["Box # (Start#-End#)"][1] if "Box # (Start#-End#)" in diff else old.get("Box # (Start#-End#)")
            mhbu_now = diff["MHBU #"][1] if "MHBU #" in diff else old.get("MHBU #")
            pos = _compute_position(box_now, mhbu_now)
            new_pos = pos or ""
            if not same(old.get("Position"), new_pos):
                diff["Position"] = [old.get("Position"), new_pos]
    # 采集地信息的国家/省份/城市自动标准化为官方地名
    if cfg.get("geo_normalize"):
        for k, fn in (("Country", geo.norm_country), ("Province", geo.norm_province), ("City", geo.norm_city)):
            if k in diff and not same(diff[k][1], fn(diff[k][1])):
                diff[k] = [diff[k][0], fn(diff[k][1])]
        # 度分(秒)坐标自动转小数度（含装饰小数度净化；歧义值不猜，保持原样）
        for k in LATLON_FIELDS:
            if k in diff:
                conv, _kind = geo.normalize_coord_value(str(diff[k][1] or ""))
                if conv is not None and not same(diff[k][1], conv):
                    diff[k] = [diff[k][0], conv]
    # 乐观锁判定已前移到读取行之后（base_version 条件 + 字段级三方合并）
    # 伪蝎表：属/种按 WAC 规范（异名转接受名、属→科联动）
    if tname == "pseudoscorpiones" and ("属" in diff or "种" in diff):
        tent = {k: diff[k][1] for k in ("属", "种", "科") if k in diff}
        tent.setdefault("属", old.get("属"))
        tent.setdefault("种", old.get("种"))
        tent.setdefault("科", old.get("科"))
        fixed, wac_warn = _wac_apply(tname, dict(tent))
        for k in ("属", "种", "科"):
            if k in diff and not same(diff[k][1], fixed.get(k)):
                diff[k] = [diff[k][0], fixed.get(k)]
        if wac_warn:
            warn = wac_warn
    cfgu = TABLES[tname]
    if cfgu.get("alcohol_col") and cfgu["alcohol_col"] in diff:
        nv = normalize_alcohol(diff[cfgu["alcohol_col"]][1])
        if not same(diff[cfgu["alcohol_col"]][1], nv):
            diff[cfgu["alcohol_col"]] = [diff[cfgu["alcohol_col"]][0], nv]
    for k in list(diff.keys()):
        col = next((c for c in TABLES[tname]["columns"] if c["name"] == k), None)
        if col and col.get("enum_locked") and diff[k][1] is not None:
            opts = col.get("options") or []
            match = next((o for o in opts if str(o).lower() == str(diff[k][1]).lower()), None)
            diff[k][1] = match if match is not None else (col.get("default") or "")
    validate_field_values(tname, {k: p[1] for k, p in diff.items()})
    box_from_auto = False
    if tname == "specimens" and "MHBU #" in diff:
        box_auto = _box_for_number(diff["MHBU #"][1])
        if box_auto and "Box # (Start#-End#)" not in diff and not same(old.get("Box # (Start#-End#)"), box_auto):
            diff["Box # (Start#-End#)"] = [old.get("Box # (Start#-End#)"), box_auto]
            box_from_auto = True
    if tname == "specimens":
        final_data = dict(old)
        final_data.update({k: p[1] for k, p in diff.items()})
        final_data["_old"] = old
        _check_required(tname, final_data, exclude_rid=rid, only_clearing=True)
        final_data.pop("_old", None)
    if tname == "specimens" and "Box # (Start#-End#)" in diff and not box_from_auto:
        _check_box_available(diff["Box # (Start#-End#)"][1], exclude_rid=rid)
    # 主键查重：把编号改成已有编号时拦截
    keyu = cfgu["key"]
    if keyu and keyu in diff and not cfgu.get("key_not_unique"):
        if tname in KEY_PREFIX_RE:
            nv = normalize_spec_key(diff[keyu][1], kind="mygalomorph" if tname == "mygalomorph" else "specimens")
            if not same(diff[keyu][1], nv):
                diff[keyu] = [diff[keyu][0], nv]
        check_key_unique(tname, diff[keyu][1], exclude_rid=rid)
    new = dict(old)
    new.update({k: p[1] for k, p in diff.items()})
    ts = now()
    # 带版本条件的更新并核对受影响行数：读取与提交之间若被并发修改（row_version 变化），
    # 受影响行数为 0 → 返回最新状态的 409，绝不静默覆盖
    if override_log is None or base_version is not None:
        cur = conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
            "WHERE id=? AND row_version=?",
            (json.dumps(new, ensure_ascii=False), ts, user["username"], rid, cur_ver))
        if cur.rowcount != 1:
            fresh = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
            fd = json.loads(fresh["data"]) if fresh else {}
            raise WriteConflict({
                "error": "提交时发现该行已被并发修改，本次未写入；请刷新后重试",
                "conflict": {"by": fresh["updated_by"] if fresh else "?",
                             "at": fresh["updated_at"] if fresh else "?",
                             "row_version": fresh["row_version"] if fresh else None,
                             "fields": {}, "values": {k: fd.get(k) for k in diff}},
            })
    else:
        conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? WHERE id=?",
            (json.dumps(new, ensure_ascii=False), ts, user["username"], rid))
    cfg = TABLES[tname]
    if not batch_id:
        batch_id = _new_batch("U")
    diff_log = {**diff, "_override": override_log} if override_log else diff
    impact_summary = None
    if tname == "events" and any(k in diff for k in
                                 ("Province", "City", "County", "Locality", "Latitude1", "Longitude1",
                                  "CollectionEventNumber")):
        try:
            impact_summary = _event_impact(tname, rid, user=user)
            diff_log["_impact"] = {"affected_total": impact_summary["affected_total"],
                                   "tables": {a["table"]: a["count"] for a in impact_summary["affected"]},
                                   "restricted": impact_summary["restricted_tables"],
                                   "note": "源操作影响范围；动态展示变化不逐条伪造编辑"}
        except Exception:
            pass
    log_edit(conn, user["username"], tname, rid, new.get(cfg["key"]) if cfg["key"] else None, "update", diff_log,
             batch_id=batch_id)
    det = _det_after_write(conn, tname, rid, old, new, diff, user=user["username"],
                           batch_id=batch_id, source="manual", det_pre=det_pre)
    # 鉴定联动：把本次改动的属/暂定名/最终名推给关联行（同事务同批次，可随批次撤销；
    # 内部回填写入 _ident_sync=True 不再触发，防回环）
    sync_info = None
    if not _ident_sync and TD.ident_link(tname):
        sync_info = _ident_apply_sync(user, tname, old, diff, new, batch_id)
    resp = {"ok": True, "changes": diff, "warn": warn, "batch": batch_id,
            "row_version": cur_ver + 1, "updated_at": ts}
    if det:
        resp["determination"] = det
    if sync_info:
        resp["ident_sync"] = sync_info
    if impact_summary:
        resp["impact"] = {"affected_total": impact_summary["affected_total"],
                          "tables": {a["table"]: a["count"] for a in impact_summary["affected"]}}
    if merged_fields:
        resp["merged_fields"] = merged_fields
        resp["warn"] = (warn + "；" if warn else "") + f"已合并保存 {len(merged_fields)} 个字段（该行在您编辑期间有其他修改）"
    return resp


@app.put("/api/rows/<tname>/<int:rid>")
def api_update(tname, rid):
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    body = request.get_json(force=True)
    conn = get_db()
    ctx = _idem_ctx(user, f"row_update:{tname}:{rid}", body)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    try:
        payload = _update_record(user, tname, rid, body)
    except WriteConflict as c:
        _tx_rollback(conn)
        return jsonify(c.payload), 409
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    invalidate(tname)
    return _finish_write(conn, payload, ctx)


@app.put("/api/rows/<tname>/by-key")
def api_update_by_key(tname):
    """给脚本用的按主键修改：PUT /api/rows/specimens/by-key?key=MHBU-ARA-00020001  body={"changes":{...}}"""
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    key = cfg["key"]
    if not key:
        abort(400, "该表没有可用主键列")
    kval = (request.args.get("key") or "").strip()
    if not kval:
        abort(400, "缺少 key 参数")
    rid = None
    for r in load_rows(tname):
        if not r["deleted"] and str(r["data"].get(key) or "").strip() == kval:
            rid = r["id"]
            break
    if rid is None:
        abort(404, f"未找到 {key}={kval} 的记录")
    body = request.get_json(force=True)
    conn = get_db()
    ctx = _idem_ctx(user, f"row_update_key:{tname}:{kval}", body)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    try:
        payload = _update_record(user, tname, rid, body)
    except WriteConflict as c:
        _tx_rollback(conn)
        return jsonify(c.payload), 409
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    invalidate(tname)
    return _finish_write(conn, payload, ctx)


@app.delete("/api/rows/<tname>/<int:rid>")
def api_delete(tname, rid):
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row or row["deleted"]:
        abort(404, "记录不存在或已删除")
    conn.execute("UPDATE records SET row_version=row_version+1, deleted=1, updated_at=?, updated_by=? WHERE id=?", (now(), user["username"], rid))
    data = json.loads(row["data"])
    bid = _new_batch("D")
    log_edit(conn, user["username"], tname, rid, data.get(cfg["key"]) if cfg["key"] else None,
             "delete", {"_data": data}, batch_id=bid)
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True, "batch": bid})


@app.post("/api/rows/<tname>/<int:rid>/undelete")
def api_undelete(tname, rid):
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row:
        abort(404, "记录不存在")
    if not row["deleted"]:
        return jsonify({"ok": True, "noop": True})
    conn.execute("UPDATE records SET row_version=row_version+1, deleted=0, updated_at=?, updated_by=? WHERE id=?", (now(), user["username"], rid))
    data = json.loads(row["data"])
    log_edit(conn, user["username"], tname, rid, data.get(cfg["key"]) if cfg["key"] else None,
             "undelete", {"_summary": "从回收站还原"})
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True})


@app.delete("/api/rows/<tname>/<int:rid>/purge")
def api_purge(tname, rid):
    user = current_user()
    if not user["admin"]:
        abort(403, "彻底删除仅管理员可操作")
    cfg = chk_table(tname)
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row:
        abort(404, "记录不存在")
    data = json.loads(row["data"])
    conn.execute("DELETE FROM records WHERE id=?", (rid,))
    conn.execute("DELETE FROM attachments WHERE tname=? AND rid=?", (tname, rid))
    shutil.rmtree(os.path.join(UPLOAD_DIR, tname, str(rid)), ignore_errors=True)
    log_edit(conn, user["username"], tname, None, data.get(cfg["key"]) if cfg["key"] else None,
             "purge", {"_data": data, "_summary": f"彻底删除（原行ID {rid}）"})
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 变更历史

# ---------------------------------------------------------------- 批量编辑 / 撤销重做 / 活动统计
def _new_batch(tag="B"):
    return tag + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)


@app.post("/api/rows/<tname>/batch_update")
def api_batch_update(tname):
    """批量修改共享字段（两阶段）：
      阶段一（只读）整批校验：白名单/关联列（全局一次）+ 逐行存在性、必填清空、
        每行基础版本 base_versions（{rid: version} 或与 rids 对齐的数组）。
      阶段二（单事务）：
        默认原子模式——任一行失败则整批不执行；全部通过才一次提交。
        partial=true 显式部分成功模式——每行独立 SAVEPOINT，失败行回滚自身；
        响应含 成功(updated/noop)/失败(failed)/未执行(not_executed) 明细。
    不再默认 force：过期 base_version 即该行冲突；未提供 base_versions 时以
    执行时刻行版本做条件更新（无用户级三方合并，行为见 API 文档）。
    单位/枚举归一在阶段一之前完成，不占用写事务。"""
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    body = request.get_json(force=True)
    rids = body.get("rids") or []
    changes = body.get("changes") or {}
    partial = bool(body.get("partial"))
    base_versions = body.get("base_versions")
    if not isinstance(rids, list) or not rids:
        abort(400, "未选择任何行")
    if not isinstance(changes, dict) or not changes:
        abort(400, "没有要修改的字段")
    if len(rids) > 500:
        abort(400, "一次最多批量修改 500 行")
    try:
        V.check_changes_writable(tname, changes)
    except V.WriteValidationError as e:
        abort(400, e.message)
    validate_field_values(tname, changes)
    cfg = TABLES[tname]
    if cfg.get("alcohol_col") in changes:
        changes = {**changes, cfg["alcohol_col"]: normalize_alcohol(changes[cfg["alcohol_col"]])}
    normalize_locked_enums(tname, changes)

    conn = get_db()
    ctx = _idem_ctx(user, "batch_update:" + tname, body)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit

    # ---- 阶段一：整批校验（只读，不写库不占写锁） ----
    idlist = [int(r) for r in rids]
    ph = ",".join("?" * len(idlist))
    found = conn.execute(
        "SELECT * FROM records WHERE tname=? AND id IN (" + ph + ")", [tname] + idlist).fetchall()
    by_id = {r["id"]: r for r in found}
    plans, errors = [], []
    for i, rid in enumerate(idlist):
        row = by_id.get(rid)
        if not row or row["deleted"]:
            errors.append({"rid": rid, "error": "记录不存在或已删除"})
            continue
        old = json.loads(row["data"])
        if isinstance(base_versions, dict):
            bv = base_versions.get(str(rid), base_versions.get(rid))
        elif isinstance(base_versions, list) and i < len(base_versions):
            bv = base_versions[i]
        else:
            bv = None
        if bv is not None:
            try:
                bv = int(bv)
            except (TypeError, ValueError):
                abort(400, "base_versions 需为整数行版本")
            if bv != row["row_version"]:
                errors.append({"rid": rid,
                               "error": ("该行在您读取后已被 %s 于 %s 修改（行版本 %s，您基于 %s）"
                                         % (row["updated_by"], row["updated_at"],
                                            row["row_version"], bv)),
                               "conflict": {"row_version": row["row_version"],
                                            "values": {k: old.get(k) for k in changes}}})
                continue
        merged = dict(old)
        merged.update(changes)
        # 鉴定历史：名称变更预核对（写事务前），鉴定变更复位「暂定」
        det_pre = None
        if cfg.get("genus_lookup") and tname in DET_TABLES:
            nf = ("FinalSpeciesName" if "FinalSpeciesName" in changes
                  else ("TemporarySpeciesName" if "TemporarySpeciesName" in changes else None))
            if nf and str(changes.get(nf) or "").strip():
                det_pre = det_check_for(
                    changes[nf],
                    det_by=changes.get("DetBy") if "DetBy" in changes else old.get("DetBy"),
                    det_date=changes.get("DetDate") if "DetDate" in changes else old.get("DetDate"),
                    note=changes.get("TaxonomyComment"))
                if det_pre and "DetStatus" not in changes and not same(old.get("DetStatus"), "暂定"):
                    merged["DetStatus"] = "暂定"
        try:
            d2 = dict(merged)
            d2["_old"] = old
            _check_required(tname, d2, exclude_rid=rid, only_clearing=True)
        except HTTPException as e:
            errors.append({"rid": rid, "error": e.description})
            continue
        changed = {k: changes[k] for k in changes if not same(old.get(k), changes[k])}
        plan = {"rid": rid, "old": old, "new": merged, "changes": changed,
                "diff": {k: [old.get(k), v] for k, v in changed.items()},
                "cur_ver": row["row_version"], "noop": not changed,
                "det_pre": det_pre}
        # 鉴定联动写权限预检（与单行修改同口径）：联动会改到对方行而对方表无
        # 编辑权限 → 该行不通过（原子模式整批不执行，partial 模式该行失败）
        if changed and TD.ident_link(tname):
            perm_err = _ident_sync_permission_error(user, tname, old, merged, plan["diff"])
            if perm_err:
                errors.append({"rid": rid, "error": perm_err})
                continue
        plans.append(plan)
    if errors and not partial:
        return jsonify({
            "error": ("整批未执行：%d / %d 行未通过校验或存在版本冲突（原子模式不做部分写入）"
                      % (len(errors), len(idlist))),
            "executed": False, "errors": errors,
        }), 409

    # ---- 阶段二：单事务应用（原子一次提交；partial 逐行 SAVEPOINT） ----
    batch_id = _new_batch("M")
    updated = noop = 0
    sync_agg = {"peer_label": None, "rows": 0}   # 鉴定联动合计（响应汇总展示）
    # partial 模式：阶段一未通过的行即该行的失败明细（savepoint 循环只处理通过校验的行）
    failed, not_executed = (list(errors), []) if partial else ([], [])
    _ = not_executed

    def _apply_one(plan):
        """条件更新 + 日志（不 commit）。rowcount!=1 → 提交瞬间并发修改。"""
        cur = conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
            "WHERE id=? AND row_version=?",
            (json.dumps(plan["new"], ensure_ascii=False), now(), user["username"],
             plan["rid"], plan["cur_ver"]))
        if cur.rowcount != 1:
            raise WriteConflict({"error": "行 %s 在提交时被并发修改" % plan["rid"],
                                 "rid": plan["rid"]})
        log_edit(conn, user["username"], tname, plan["rid"],
                 plan["new"].get(cfg["key"]) if cfg.get("key") else None,
                 "update", plan["changes"], batch_id=batch_id)
        _det_after_write(conn, tname, plan["rid"], plan["old"], plan["new"], plan["changes"],
                         user=user["username"], batch_id=batch_id, source="manual",
                         det_pre=plan.get("det_pre"))
        # 鉴定联动：本行改动的属/暂定名/最终名推给关联行（同事务同批次）
        if plan.get("diff") and TD.ident_link(tname):
            si = _ident_apply_sync(user, tname, plan["old"], plan["diff"], plan["new"], batch_id)
            if si:
                sync_agg["peer_label"] = si.get("peer_label")
                sync_agg["rows"] += si.get("rows") or 0
                if si.get("skipped_perm"):
                    sync_agg["skipped_perm"] = sync_agg.get("skipped_perm", 0) + si["skipped_perm"]

    if not partial:
        try:
            for plan in plans:
                if plan["noop"]:
                    noop += 1
                    continue
                _apply_one(plan)
                updated += 1
        except WriteConflict as c:
            _tx_rollback(conn)
            return jsonify({"error": "整批未执行：提交时发现并发修改（%s）" % c.payload.get("error"),
                            "executed": False, "errors": errors + [c.payload]}), 409
        except sqlite3.OperationalError as e:
            _tx_rollback(conn)
            return _tx_fail(e)
        except Exception:
            _tx_rollback(conn)
            raise
    else:
        for i, plan in enumerate(plans):
            sp = "bw_%s_%d" % ("".join(ch if ch.isalnum() else "_" for ch in batch_id), i)
            conn.execute("SAVEPOINT " + sp)
            try:
                if plan["noop"]:
                    noop += 1
                else:
                    _apply_one(plan)
                    updated += 1
                conn.execute("RELEASE SAVEPOINT " + sp)
            except WriteConflict as c:
                conn.execute("ROLLBACK TO SAVEPOINT " + sp)
                conn.execute("RELEASE SAVEPOINT " + sp)
                failed.append({"rid": plan["rid"], "error": c.payload.get("error")})
            except sqlite3.OperationalError as e:
                conn.execute("ROLLBACK TO SAVEPOINT " + sp)
                conn.execute("RELEASE SAVEPOINT " + sp)
                if _is_locked(e):
                    failed.append({"rid": plan["rid"], "error": "数据库忙（锁超时），该行已回滚"})
                    for rest in plans[i + 1:]:
                        not_executed.append({"rid": rest["rid"],
                                             "reason": "前序行遇到数据库锁超时，为保持批次可预期已停止执行"})
                    break
                _tx_rollback(conn)
                return _tx_fail(e)
            except HTTPException as e:
                conn.execute("ROLLBACK TO SAVEPOINT " + sp)
                conn.execute("RELEASE SAVEPOINT " + sp)
                failed.append({"rid": plan["rid"], "error": e.description})
    invalidate(tname)
    payload = {"ok": True, "batch": batch_id, "updated": updated, "noop": noop,
               "failed": failed, "not_executed": not_executed, "partial": partial}
    if sync_agg["rows"] or sync_agg.get("skipped_perm"):
        payload["ident_sync"] = sync_agg
    return _finish_write(conn, payload, ctx)


@app.post("/api/rows/<tname>/sync_from_authority")
def api_sync_from_authority(tname):
    """鉴定联动·一键同步（非权威侧 → 权威侧，当前即 UCE 文库 ← DNA 提取台账）：
    把权威行的属/暂定名/最终名复制到本表关联行（权威侧为空的字段不动）。
    body.rids 可限定行（差异徽标单行同步即传单行）；缺省整表所有可联动行。
    单事务单批次（批量 S），可整批撤销；每行走 _update_record 完整通道——
    级联带出/审计/行版本与手工修改完全一致，联动不再级联触发（防回环）。"""
    user = current_user()
    require_writer(user, tname)
    lnk = TD.ident_link(tname)
    if not lnk or lnk["authority"]:
        abort(400, "该表没有以对方表为基准的鉴定联动，无需一键同步")
    body = request.get_json(silent=True) or {}
    rids = body.get("rids")
    if rids is not None and (not isinstance(rids, list) or not all(
            isinstance(x, int) or (isinstance(x, str) and x.isdigit()) for x in rids)):
        abort(400, "rids 需为行 ID 数组")
    rows = [r for r in load_rows(tname) if not r["deleted"]]
    if rids:
        want = {int(x) for x in rids}
        rows = [r for r in rows if r["id"] in want]
        if not rows:
            abort(404, "所选行不存在或已删除")
    if len(rows) > 20000:
        abort(400, "一次最多同步 20000 行，请用 rids 分批")
    conn = get_db()
    ctx = _idem_ctx(user, f"ident_sync:{tname}",
                    {"rids": sorted(int(x) for x in rids) if rids else None})
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    peer_idx = _ident_peer_index(lnk)
    batch_id = _new_batch("S")
    synced = noop = unlinked = 0
    failed = []
    for r in rows:
        d = r["data"]
        peer = peer_idx.get(str(d.get(lnk["self_key"]) or "").strip())
        if not peer:
            unlinked += 1
            continue
        ch = {}
        for own_col, peer_col in lnk["cols"].items():
            pv = peer.get(peer_col)
            if str(pv or "").strip() and not same(d.get(own_col), pv):
                ch[own_col] = pv
        if not ch:
            noop += 1
            continue
        try:
            _update_record(user, tname, r["id"],
                           {"changes": ch, "allow_overwrite": True,
                            "overwrite_reason": f"以「{lnk['peer_label']}」为准同步鉴定信息"},
                           batch_id=batch_id, _ident_sync=True)
            synced += 1
        except HTTPException as e:
            failed.append({"rid": r["id"], "error": str(e.description)})
        except WriteConflict as c:
            failed.append({"rid": r["id"], "error": str((c.payload or {}).get("error"))})
    invalidate(tname)
    payload = {"ok": True, "batch": batch_id, "synced": synced, "noop": noop,
               "unlinked": unlinked, "peer_label": lnk["peer_label"]}
    if failed:
        payload["failed"] = failed
    return _finish_write(conn, payload, ctx)


def _undo_stack_for(user, limit=10):
    """该用户最近的可撤销批次（create/update/delete），标注是否已被撤销。"""
    conn = get_db()
    marks = conn.execute(
        "SELECT changes FROM edits WHERE user=? AND action IN ('undo','redo') ORDER BY id DESC LIMIT 400",
        (user,)).fetchall()
    state = {}
    for m in reversed(marks):
        try:
            ch = json.loads(m["changes"])
        except ValueError:
            continue
        if not isinstance(ch, dict):
            continue
        if ch.get("_undo_of"):
            state[ch["_undo_of"]] = "undo"
        elif ch.get("_redo_of"):
            state[ch["_redo_of"]] = "redo"
    # 撤销栈保留策略：最近 30 天且最多 500 个批次（更早的操作仍留编辑日志，只是不再出现在撤销菜单）
    rows = conn.execute(
        "SELECT batch_id, MIN(id) hid, MIN(ts) ts, tname, COUNT(*) n FROM edits "
        "WHERE user=? AND batch_id IS NOT NULL AND action IN ('create','update','delete') "
        "AND ts >= datetime('now', 'localtime', '-30 days') "
        "GROUP BY batch_id ORDER BY hid DESC LIMIT 500", (user,)).fetchall()
    out = []
    for r in rows:
        t = r["tname"]
        out.append({"batch": r["batch_id"], "ts": r["ts"], "tname": t,
                    "label": TD.display_label(t),
                    "n": r["n"], "undone": state.get(r["batch_id"]) == "undo"})
    return out[:limit]


@app.get("/api/undostack")
def api_undostack():
    u = current_user()
    return jsonify({"items": _undo_stack_for(u["username"])})


def conn_exists_batch(batch_id):
    return bool(get_db().execute("SELECT 1 FROM edits WHERE batch_id=? LIMIT 1",
                                 (batch_id,)).fetchone())


def _inverse_last_state(batch_id):
    """批次撤销状态机：None=未撤销过 / "undone"=已撤销 / "redone"=撤销后又重做。
    取该批次最近一次 undo/redo 事件的方向。"""
    conn = get_db()
    for r in conn.execute(
            "SELECT changes FROM edits WHERE action IN ('undo','redo') ORDER BY id DESC LIMIT 400"):
        try:
            ch = json.loads(r["changes"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(ch, dict):
            continue
        if ch.get("_undo_of") == batch_id:
            return "undone"
        if ch.get("_redo_of") == batch_id:
            return "redone"
    return None


def plan_inverse(user, batch_id, mode="undo"):
    """只读计划器（多表）：基于原批次日志的 before/after 对逐行生成逆操作计划。
    行 item：{tname, rid, kind, cur_ver, fields, conflicts, reason}。
    仅按行 id 匹配、按行版本重新验证；不做任何写入。"""
    conn = get_db()
    head = conn.execute("SELECT user, tname FROM edits WHERE batch_id=? ORDER BY id LIMIT 1",
                        (batch_id,)).fetchone()
    if not head:
        abort(404, "批次不存在")
    if head["user"] != user["username"] and not user["admin"]:
        abort(403, "只能撤销/重做自己的操作")
    edits = conn.execute(
        "SELECT tname, id, rid, action, changes FROM edits WHERE batch_id=? "
        "AND action IN ('create','update','delete') ORDER BY id DESC", (batch_id,)).fetchall()
    if not edits:
        abort(400, "该批次没有可回退的行变更")
    # 逐表写权限
    for tn in {e["tname"] for e in edits}:
        if TABLES.get(tn):
            require_writer(user, tn)
        else:
            abort(400, "该批次所属数据表已不存在")
    tgt_i = 0 if mode == "undo" else 1
    produced_i = 1 - tgt_i
    field_targets, field_produced = {}, {}
    creates, deletes = [], []
    for e in edits:
        rid = e["rid"]
        if not rid:
            continue
        tn = e["tname"]
        try:
            ch = json.loads(e["changes"] or "{}")
        except (ValueError, TypeError):
            continue
        k = (tn, rid)
        if e["action"] == "update":
            d = field_targets.setdefault(k, {})
            pp = field_produced.setdefault(k, {})
            for f, pair in ch.items():
                if f.startswith("_"):
                    continue
                if isinstance(pair, list) and len(pair) == 2 and f not in d:
                    d[f] = pair[tgt_i]
                    pp[f] = pair[produced_i]
        elif e["action"] == "create":
            creates.append(k)
        elif e["action"] == "delete":
            deletes.append(k)

    items = []
    seen_k = set()

    def row_snapshot(tn, rid):
        r = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tn)).fetchone()
        if not r:
            return None
        return r, json.loads(r["data"]), r["row_version"]

    def emit(tn, rid, kind, cur_ver=None, fields=None, conflicts=None, reason=""):
        item = {"tname": tn, "rid": rid, "kind": kind, "cur_ver": cur_ver,
                "fields": fields or {}, "conflicts": conflicts or {}, "reason": reason}
        items.append(item)
        return item

    # ---- 更新类（跨表分组） ----
    for (tn, rid), olds in field_targets.items():
        kk = (tn, rid)
        if kk in seen_k:
            continue
        seen_k.add(kk)
        snap = row_snapshot(tn, rid)
        if not snap or snap[0]["deleted"]:
            emit(tn, rid, "update", reason="记录不存在或已删除", cur_ver=snap[2] if snap else None)
            continue
        row, data, cur_ver = snap
        produced = field_produced.get((tn, rid), {})
        apply_fields, conflicts = {}, {}
        for f, target in olds.items():
            cur = data.get(f)
            produced_val = produced.get(f)
            if same(cur, target):
                continue
            if produced_val is not None and same(cur, produced_val):
                apply_fields[f] = target
            else:
                conflicts[f] = {"produced": produced_val, "current": cur, "restore_to": target}
        kind = "update" if (apply_fields or conflicts) else "noop"
        if kind == "noop":
            reason = "该行已是目标状态"
        else:
            reason = ""
        emit(tn, rid, kind, cur_ver=cur_ver, fields=apply_fields, conflicts=conflicts, reason=reason)

    # ---- 新增类：撤销=软删（需无后续编辑/附件，跨表各自判定） ----
    for tn, rid in dict.fromkeys(creates):
        kk = (tn, rid)
        if kk in seen_k:
            continue
        seen_k.add(kk)
        snap = row_snapshot(tn, rid)
        if not snap:
            emit(tn, rid, "delete", reason="记录不存在（已被彻底删除）")
            continue
        row, data, cur_ver = snap
        if mode == "redo":
            if not row["deleted"]:
                emit(tn, rid, "noop", reason="该行本就未被撤销", cur_ver=cur_ver)
            else:
                emit(tn, rid, "undelete", cur_ver=cur_ver)
            continue
        if row["deleted"]:
            emit(tn, rid, "noop", reason="该行已在回收站", cur_ver=cur_ver)
            continue
        later = conn.execute(
            "SELECT COUNT(*) c FROM edits WHERE tname=? AND rid=? AND batch_id IS NOT NULL "
            "AND batch_id<>? AND action IN ('update','delete')",
            (tn, rid, batch_id)).fetchone()["c"]
        att = conn.execute("SELECT COUNT(*) c FROM attachments WHERE tname=? AND rid=?",
                           (tn, rid)).fetchone()["c"]
        if later or att:
            why = []
            if later:
                why.append(f"该行随后被编辑过 {later} 次")
            if att:
                why.append(f"挂有 {att} 个附件")
            emit(tn, rid, "delete", reason="、".join(why) + "，不能直接撤销新增", cur_ver=cur_ver)
            continue
        emit(tn, rid, "delete", cur_ver=cur_ver)

    # ---- 删除类：撤销=恢复（重查编号冲突与必填）；重做=再次移除 ----
    for tn, rid in dict.fromkeys(deletes):
        kk = (tn, rid)
        if kk in seen_k:
            continue
        seen_k.add(kk)
        snap = row_snapshot(tn, rid)
        if not snap:
            emit(tn, rid, "undelete", reason="记录不存在（已被彻底删除）")
            continue
        row, data, cur_ver = snap
        if mode == "redo":
            if row["deleted"]:
                emit(tn, rid, "noop", reason="该行已处于回收站", cur_ver=cur_ver)
            else:
                emit(tn, rid, "delete", cur_ver=cur_ver)
            continue
        if not row["deleted"]:
            emit(tn, rid, "noop", reason="该行已处于目标状态", cur_ver=cur_ver)
            continue
        cfg = TABLES.get(tn)
        key = cfg.get("key") if cfg else None
        kdup = ""
        missing_req = []
        if key:
            kval = str(data.get(key) or "").strip()
            if kval:
                # 同批次的撤销新增（本批创建、即将软删的行）不算占用：替换导入重建的行
                # 与被删旧行同键，撤销时新建行在本批内先行移除，恢复不产生编号冲突
                same_batch_creates = {(t, r) for t, r in creates}
                for r2 in load_rows(tn):
                    if not r2["deleted"] and r2["id"] != rid \
                       and (tn, r2["id"]) not in same_batch_creates \
                       and str(r2["data"].get(key) or "").strip() == kval:
                        kdup = f"编号「{kval}」已被行 {r2['id']} 占用，恢复将冲突"
                        break
        if cfg:
            missing_req = [f for f in (cfg.get("required_fields") or [])
                           if not str(data.get(f) or "").strip()]
        if kdup or missing_req:
            reason = kdup + (f"；必填缺失 {'、'.join(missing_req)}" if missing_req else "")
            emit(tn, rid, "undelete", reason=reason, cur_ver=cur_ver)
            continue
        emit(tn, rid, "undelete", cur_ver=cur_ver)

    return head["tname"], items, head


def _apply_inverse(user, batch_id, mode, skip_conflicts=False, only_rids=None):
    """应用逆操作计划（多表）：默认整批原子（任一冲突/缺失→409 整批不执行）；
    skip_conflicts=true 显式跳过冲突行并单独记录（兼容保留）；
    only_rids=[记录ID] 为界面逐项回退：只回退勾选的可安全项，冲突行不动
    （其后续修改全部保留并在响应中说明）；不在可安全清单内的勾选项 → 400 逐条说明。
    逐行以 row_version 条件更新重新验证；数据与审计同事务。"""
    conn = get_db()
    head_tname, items, head = plan_inverse(user, batch_id, mode)
    applicable = [it for it in items if it["kind"] in ("update", "delete", "undelete")
                  and not it["conflicts"] and not it["reason"]]
    conflicts = [it for it in items if it["conflicts"] or (it["reason"] and it["kind"] != "noop")]
    if only_rids is not None:
        safe_ids = {it["rid"] for it in applicable}
        requested = set(int(x) for x in only_rids)
        unsafe = sorted(requested - safe_ids)
        if unsafe:
            reasons = []
            for rid in unsafe[:5]:
                it = next((x for x in items if x["rid"] == rid), None)
                reasons.append({"rid": rid,
                                "reason": (it and (it["reason"] or "存在冲突（后续修改会丢失）")) or "不在该批次中"})
            return jsonify({"error": f"{len(unsafe)} 项不可安全回退：这些记录在批次之后有后续修改",
                            "unsafe": reasons, "executed": False}), 400
        applicable = [it for it in applicable if it["rid"] in requested]
        conflicts = [c for c in conflicts if c["rid"] not in requested]
    missing = [it for it in items if it["reason"] and it["kind"] == "noop"
               and "已在" not in it["reason"] and "已处于" not in it["reason"]]
    if conflicts and not skip_conflicts:
        return jsonify({
            "error": f"整批未执行：{len(conflicts)} 行存在冲突（原子模式），可用「跳过冲突」仅回退无冲突行",
            "executed": False,
            "conflicts": [{"tname": c["tname"], "rid": c["rid"],
                           "fields": c["conflicts"] or None, "reason": c["reason"]} for c in conflicts],
            "missing": [{"tname": m["tname"], "rid": m["rid"], "reason": m["reason"]} for m in missing],
            "applicable": len(applicable),
        }), 409

    inv_batch = _new_batch("X")
    marker = "_undo_of" if mode == "undo" else "_redo_of"
    applied = skipped = noop = 0
    skipped_log = []
    ts = now()

    for it in applicable:
        tn, rid, cur_ver = it["tname"], it["rid"], it["cur_ver"]
        cfg = TABLES.get(tn) or {}
        key = cfg.get("key")
        if it["kind"] == "noop":
            noop += 1
            continue
        if it["kind"] == "update":
            r0 = conn.execute("SELECT data FROM records WHERE id=? AND tname=?", (rid, tn)).fetchone()
            new = json.loads(r0["data"])
            new.update(it["fields"])
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
                "WHERE id=? AND tname=? AND row_version=?",
                (json.dumps(new, ensure_ascii=False), ts, user["username"], rid, tn, cur_ver))
            if cur.rowcount != 1:
                skipped += 1
                skipped_log.append({"tname": tn, "rid": rid, "error": "提交时被并发修改，该行未回退"})
                continue
            log_edit(conn, user["username"], tn, rid, new.get(key) if key else None, mode,
                     {**{k: [None, v] for k, v in it["fields"].items()}, marker: batch_id,
                      "_inverse_batch": inv_batch,
                      "_compat": "exact_restore（撤销/重做按批次日志精确回放）"},
                     batch_id=inv_batch, parent_op_id=batch_id)
            applied += 1
        elif it["kind"] == "delete":
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, deleted=1, updated_at=?, updated_by=? "
                "WHERE id=? AND tname=? AND row_version=?",
                (ts, user["username"], rid, tn, cur_ver))
            if cur.rowcount != 1:
                skipped += 1
                skipped_log.append({"tname": tn, "rid": rid, "error": "提交时被并发修改，该行未回退"})
                continue
            log_edit(conn, user["username"], tn, rid, None, mode,
                     {"_summary": "撤销新增：移入回收站", marker: batch_id,
                      "_inverse_batch": inv_batch, "_batch": inv_batch,
                      "_compat": "exact_restore（撤销/重做按批次日志精确回放）"},
                     batch_id=inv_batch, parent_op_id=batch_id)
            applied += 1
        elif it["kind"] == "undelete":
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, deleted=0, updated_at=?, updated_by=? "
                "WHERE id=? AND tname=? AND row_version=?",
                (ts, user["username"], rid, tn, cur_ver))
            if cur.rowcount != 1:
                skipped += 1
                skipped_log.append({"tname": tn, "rid": rid, "error": "提交时被并发修改，该行未回退"})
                continue
            log_edit(conn, user["username"], tn, rid, None, mode,
                     {"_summary": "恢复该行（撤销删除）", marker: batch_id,
                      "_inverse_batch": inv_batch, "_batch": inv_batch,
                      "_compat": "exact_restore（撤销/重做按批次日志精确回放）"},
                     batch_id=inv_batch, parent_op_id=batch_id)
            applied += 1

    # 鉴定历史联动：撤销批次时移除该批次追加的鉴定记录（严格回到操作前状态；
    # 重做不重建鉴定行——原批次审计中保留完整内容可查）
    det_removed = 0
    if mode == "undo":
        det_removed = conn.execute("DELETE FROM determinations WHERE batch_id=?",
                                   (batch_id,)).rowcount

    involved = {it["tname"] for it in applicable} | {head_tname}
    for tn in involved:
        if tn in TABLES:
            invalidate(tn)
    summary = ("撤销" if mode == "undo" else "重做") + f"批次 {batch_id}：回退 {applied} 行" + (
        f"、跳过冲突 {skipped + len(conflicts)} 行" if (skipped or conflicts) else "") + (
        f"、移除鉴定记录 {det_removed} 条" if det_removed else "")
    log_edit(conn, user["username"], head_tname, None, None, mode,
             {"_summary": summary, marker: batch_id, "_inverse_batch": inv_batch,
              "_batch": inv_batch, "_det_removed": det_removed,
              "_compat": "exact_restore（撤销/重做按批次日志精确回放）"}, batch_id=inv_batch)
    payload = {"ok": True, "applied": applied, "noop": noop, "mode": mode, "batch": batch_id,
               "inverse_batch": inv_batch, "determinations_removed": det_removed,
               "skipped_conflicts": [{"tname": c["tname"], "rid": c["rid"],
                                      "reason": c["reason"],
                                      "conflicts": c["conflicts"] or None} for c in conflicts],
               "skipped": skipped_log,
               "missing": [{"tname": m["tname"], "rid": m["rid"], "reason": m["reason"]} for m in missing]}
    return payload


@app.get("/api/undo/<batch_id>/preview")
def api_undo_preview(batch_id):
    """只读预览：可应用数/冲突数/缺失数与逐条原因（不做任何写入）。"""
    user = current_user()
    mode = request.args.get("mode", "undo")
    if mode not in ("undo", "redo"):
        abort(400, "mode 仅支持 undo / redo")
    tname, items, head = plan_inverse(user, batch_id, mode)
    applicable = [it for it in items if it["kind"] in ("update", "delete", "undelete")
                  and not it["reason"] and not it["conflicts"]]
    conflicts = [it for it in items if it["conflicts"] or (it["reason"] and it["kind"] != "noop")]
    missing = [it for it in items if it["reason"] and it["kind"] == "noop"
               and "已在" not in it["reason"] and "已处于" not in it["reason"]]
    det_removed = 0
    if mode == "undo":
        det_removed = get_db().execute(
            "SELECT COUNT(*) c FROM determinations WHERE batch_id=?", (batch_id,)).fetchone()["c"]
    return jsonify({
        "mode": mode, "table": tname, "batch": batch_id,
        "state": _inverse_last_state(batch_id),
        "applicable": len(applicable), "conflicts": len(conflicts),
        "missing": len(missing), "noop": len(items) - len(applicable) - len(conflicts) - len(missing),
        "determinations_removed": det_removed,
        "plan": [{"rid": it["rid"], "kind": it["kind"], "fields": it["fields"] or None,
                  "conflicts": it["conflicts"] or None, "reason": it["reason"] or None,
                  "row_version": it["cur_ver"]} for it in items],
    })


@app.post("/api/undo/<batch_id>")
def api_undo(batch_id):
    user = current_user()
    body = request.get_json(silent=True) or {}
    if not conn_exists_batch(batch_id):
        abort(404, "批次不存在")
    if _inverse_last_state(batch_id) == "undone":
        return jsonify({"error": "该批次已撤销过（不能重复撤销）；如需恢复请使用重做",
                        "state": "undone"}), 409
    conn = get_db()
    try:
        payload = _apply_inverse(user, batch_id, "undo",
                                 skip_conflicts=bool(body.get("skip_conflicts")),
                                 only_rids=body.get("items"))
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    if isinstance(payload, tuple):
        _tx_rollback(conn)
        return payload
    invalidate(payload.get("batch"))
    return _finish_write(conn, payload, _idem_ctx(user, f"undo:{batch_id}", body))


@app.post("/api/redo/<batch_id>")
def api_redo(batch_id):
    user = current_user()
    body = request.get_json(silent=True) or {}
    if not conn_exists_batch(batch_id):
        abort(404, "批次不存在")
    if _inverse_last_state(batch_id) != "undone":
        return jsonify({"error": "该批次当前不在「已撤销」状态，不能重做（防状态跳跃）",
                        "state": _inverse_last_state(batch_id)}), 409
    conn = get_db()
    try:
        payload = _apply_inverse(user, batch_id, "redo",
                                 skip_conflicts=bool(body.get("skip_conflicts")),
                                 only_rids=body.get("items"))
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    if isinstance(payload, tuple):
        _tx_rollback(conn)
        return payload
    invalidate(payload.get("batch"))
    return _finish_write(conn, payload, _idem_ctx(user, f"redo:{batch_id}", body))

@app.get("/api/activity")
def api_activity():
    me = current_user()
    try:
        days = min(365, max(1, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    since = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_db()
    ACT = "('create','update','delete')"
    vis_names = None if me["admin"] else list(visible_tables(me).keys())
    if vis_names is not None:
        _uwhere = f"AND tname IN ({','.join('?' for _ in vis_names)})"
        _uparams = [since] + vis_names
    else:
        _uwhere = ""
        _uparams = [since]
    by_user = [dict(r) for r in conn.execute(
        "SELECT user, COUNT(*) n, SUM(action='create') creates, SUM(action='delete') deletes, "
        "MAX(ts) last FROM edits WHERE ts>=? AND action IN " + ACT + _uwhere +
        " GROUP BY user ORDER BY n DESC LIMIT 12", _uparams).fetchall()]
    by_day = [dict(r) for r in conn.execute(
        "SELECT substr(ts,1,10) day, COUNT(*) n FROM edits WHERE ts>=? AND action IN " + ACT +
        " GROUP BY day ORDER BY day", (since,)).fetchall()]
    by_table = []
    vis = None if me["admin"] else set(visible_tables(me).keys())
    for r in conn.execute(
            "SELECT tname, COUNT(*) n FROM edits WHERE ts>=? AND action IN " + ACT +
            " GROUP BY tname ORDER BY n DESC", (since,)).fetchall():
        t = r["tname"]
        if vis is not None and t not in vis:
            continue
        by_table.append({"tname": t, "n": r["n"],
                         "label": TD.display_label(t)})
    return jsonify({"days": days, "total": sum(x["n"] for x in by_table),
                    "by_user": by_user, "by_day": by_day, "by_table": by_table})


@app.get("/api/species-fuzzy")
def api_species_fuzzy():
    """种名拼写近似提醒：给一个未收录的名称，找 WSC 里的相近名。"""
    current_user()
    name = (request.args.get("name") or "").strip()
    if len(name) < 4:
        return jsonify({"input": name, "in_wsc": False, "suggestions": []})
    low = name.lower()
    exact = _WSC_SPEC_LOW.get(low)
    if exact and not exact.get("syn"):
        return jsonify({"input": name, "in_wsc": True, "suggestions": []})
    close = difflib.get_close_matches(low, list(_WSC_SPEC_LOW.keys()), n=8, cutoff=0.72)
    sug, seen_acc = [], set()
    for c in close:
        s = _WSC_SPEC_LOW[c]
        acc = s.get("acc") or s["n"]
        if acc.lower() in seen_acc:
            continue
        seen_acc.add(acc.lower())
        sug.append({"name": s["n"], "accepted": acc, "genus": s.get("g"),
                    "family": s.get("f"), "syn": bool(s.get("syn"))})
        if len(sug) >= 5:
            break
    return jsonify({"input": name, "in_wsc": False, "suggestions": sug})



# ---------------------------------------------------------------- 坐标度分秒批量转换（管理员）
@app.get("/api/geo/convert-preview")
def api_geo_convert_preview():
    """全表扫描待转换的度分(秒)/装饰小数度坐标，返回逐行预览、核实结果与无法解析清单。"""
    me = current_user()
    ensure_visible(me, "events")
    if not me["admin"]:
        abort(403, "仅管理员可执行坐标整理")
    eidx = linked_eidx("events")
    out = []
    unparsed = []
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        d = r["data"]
        check = dict(d)
        convs = _geo_convert_fields(check)  # check 内的值已被替换为小数度
        # 无法自动识别的坐标（歧义书写等）单独成列，不猜、不改，交人工处理
        for f in LATLON_FIELDS:
            raw = str(d.get(f) or "").strip()
            if raw and re.search(r"\d", raw) and geo.classify_coord(raw)[1] is None:
                unparsed.append({"id": r["id"], "ce": d.get("CollectionEventNumber"),
                                 "field": f, "raw": raw})
        if not convs:
            continue
        # 核实：应用转换后重算合理性
        country = str(check.get("Country") or "")
        la, lo = check.get("Latitude1"), check.get("Longitude1")
        ok, why = geo.is_plausible(la, lo, country)
        out.append({
            "id": r["id"],
            "ce": d.get("CollectionEventNumber"),
            "fields": {k: str(v) for k, v in convs.items()},
            "converted": {k: check.get(k) for k in convs},
            "country": country,
            "plausible": ok,
            "why": why,
        })
        if len(out) >= 6000:
            break
    n_warn = sum(1 for x in out if not x["plausible"])
    return jsonify({"count": len(out), "warn": n_warn, "items": out[:500],
                    "unparsed_count": len(unparsed), "unparsed": unparsed[:300]})


@app.post("/api/geo/convert-apply")
def api_geo_convert_apply():
    """按预览结果批量应用转换（管理员，单批次可撤销）。"""
    me = current_user()
    ensure_visible(me, "events")
    if not me["admin"]:
        abort(403, "仅管理员可执行坐标整理")
    d_in = request.get_json(silent=True) or {}
    safe_only = bool(d_in.get("safe_only"))
    batch_id = _new_batch("G")
    conn = get_db()
    n_row = n_field = n_skip = 0
    field_cnt = {f: 0 for f in LATLON_FIELDS}
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        d = dict(r["data"])
        convs = _geo_convert_fields(d)
        if not convs:
            continue
        if safe_only:
            # 用转换后的值核实：超出国别常见范围的行为管理员保留原值待人工核对
            ok, _why = geo.is_plausible(d.get("Latitude1"), d.get("Longitude1"),
                                        str(d.get("Country") or ""))
            if not ok:
                n_skip += 1
                continue
        conn.execute("UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? WHERE id=?",
                     (json.dumps(d, ensure_ascii=False), now(), me["username"], r["id"]))
        log_edit(conn, me["username"], "events", r["id"], d.get("CollectionEventNumber") or None,
                 "autofix", {"_auto": "坐标转小数度", "fields": convs},
                 actor_type="automatic", reason="度分(秒)坐标自动转换为小数度")
        diff = {k: [r["data"].get(k), d.get(k)] for k in convs}
        log_edit(conn, me["username"], "events", r["id"],
                 d.get(TABLES["events"]["key"]), "update", diff, batch_id=batch_id)
        n_row += 1
        n_field += len(convs)
        for k in convs:
            field_cnt[k] += 1
    log_edit(conn, me["username"], "events", None, None, "update",
             {"_summary": f"坐标整理：{n_row} 行的度分(秒)坐标已转为小数度（{n_field} 个字段值）"
              + (f"，跳过核实异常 {n_skip} 行" if n_skip else ""),
              "_batch": batch_id}, batch_id=batch_id)
    conn.commit()
    invalidate("events")
    return jsonify({"ok": True, "rows": n_row, "fields": n_field, "by_field": field_cnt,
                    "skipped": n_skip, "batch": batch_id})


# ---------------------------------------------------------------- 按编号带出物种信息（UCE / 原蛛）
def _species_payload(d, source, tname="uce"):
    """从一行数据提取可带出的物种字段（键名按目标表列名归一，前端按 data-col 回填）。"""
    if source == "specimens":
        species = (d.get("FinalSpeciesName") or d.get("TemporarySpeciesName") or "").strip()
    elif source == "uce":
        species = (d.get("Species name") or "").strip()
    else:  # mygalomorph
        species = (d.get("FinalSpeciesName") or d.get("IdentifiedSpeciesName")
                   or d.get("TemporarySpeciesName") or "").strip()
    sp_col = "Species name" if tname == "uce" else "FinalSpeciesName"
    return {
        sp_col: species,
        "Genus": (d.get("Genus") or "").strip(),
        "Family": (d.get("Family") or "").strip(),
        "Subfamily": (d.get("Subfamily") or d.get("SUBFAMILY") or "").strip(),
        "Tribe": (d.get("Tribe") or d.get("TRIBE") or "").strip(),
        "Subtribe": (d.get("Subtribe") or d.get("SUBTRIBE") or "").strip(),
        "Infraorder": (d.get("Infraorder") or "").strip(),
        "Order": (d.get("Order") or "").strip(),
        "source": source,
    }


@app.get("/api/specimen-link")
def api_specimen_link():
    """按编号带出物种信息：原蛛按 MHBU #（UCE 与标本表的编号带出关联已移除）。"""
    me = current_user()
    tname = request.args.get("table") or ""
    if tname != "mygalomorph":
        abort(400, "table 仅支持 mygalomorph")
    ensure_visible(me, tname)
    code_col = "MHBU #"
    code = (request.args.get("code") or "").strip()
    if tname == "mygalomorph" and code:
        code = normalize_spec_key(code, kind="mygalomorph")
    code = code.upper()
    if not code:
        return jsonify({"found": False})
    vis = visible_tables(me)
    hits = []
    # 1) 本表内同编号行（同一样本多次建库 / 同一标本多管组织）
    for r in load_rows(tname):
        if r["deleted"]:
            continue
        if str(r["data"].get(code_col) or "").strip().upper() == code:
            hits.append(_species_payload(r["data"], tname, tname))
    # 2) 标本表（权威鉴定）：UCE 按 DNAVoucherID，原蛛按 MHBU #
    if "specimens" in vis:
        target = "DNAVoucherID" if tname == "uce" else "MHBU #"
        for r in load_rows("specimens"):
            if r["deleted"]:
                continue
            if str(r["data"].get(target) or "").strip().upper() == code:
                hits.append(_species_payload(r["data"], "specimens", tname))
    if not hits:
        return jsonify({"found": False})
    sp_col = "Species name" if tname == "uce" else "FinalSpeciesName"
    # 优先取有最终/完整物种信息的（ species 最完整者）
    hits.sort(key=lambda h: (not h[sp_col], not h["Genus"], not h["Family"]))
    best = hits[0]
    fill = {k: v for k, v in best.items()
            if k not in ("source",) and v}
    return jsonify({"found": True, "species": best[sp_col], "fill": fill,
                    "hits": len(hits), "sources": sorted({h["source"] for h in hits})})


# ---------------------------------------------------------------- 打印清单权限校验与留痕
@app.post("/api/print-log/<tname>")
def api_print_log(tname):
    """打印清单前调用：校验该表对当前账号可见，并把打印行为留痕。"""
    me = current_user()
    ensure_visible(me, tname)
    d = request.get_json(silent=True) or {}
    n = d.get("rows")
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = None
    scope = []
    if (d.get("q") or "").strip():
        scope.append("搜索「" + str(d["q"]).strip()[:30] + "」")
    if isinstance(d.get("filters"), dict) and d["filters"]:
        scope.append(str(len(d["filters"])) + " 列筛选")
    summary = (f"打印清单：{TD.display_label(tname)}"
               + (f" {n} 行" if n is not None else "")
               + ("（" + "、".join(scope) + "）" if scope else "（全表）"))
    conn = get_db()
    log_edit(conn, me["username"], tname, None, None, "print", {"_summary": summary})
    conn.commit()
    return jsonify({"ok": True, "summary": summary})


# ---------------------------------------------------------------- NCBI SRA 联动（UCE 文库）
_NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_sra_cache = {}  # 编号小写 -> {"ts":..., "cands":[...]}，缓存 24h


def _ncbi_get(path, params, retries=2, timeout=20):
    key = (getattr(config, "NCBI_API_KEY", "") or os.environ.get("NCBI_API_KEY") or "").strip()
    if key:
        params = {**params, "api_key": key}
    url = f"{_NCBI}/{path}?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "specimen-manager/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # 网络抖动重试
            last = e
            time.sleep(0.6)
    raise last


def _valid_species_name(name):
    """规范化种名：去 cf. 前缀、排除 sp./nov./gen./aff. 等开放命名；返回 None 表示无效。"""
    n = str(name or "").strip()
    n = re.sub(r"^cf\.?\s*", "", n, flags=re.I)
    if not n or " " not in n:
        return None
    if re.search(r"(gen\.|sp\.|ssp\.|nov\.?|aff\.?)", n, re.I):
        return None
    return n


def _genus_of(name):
    """从种名（可能带引号/cf./sp. 等）提取属名。"""
    n = re.sub(r"\b(cf|aff|sp|nov|gen)\.?\b", " ", str(name or ""), flags=re.I)
    n = n.replace('"', " ").replace("'", " ")
    toks = [t for t in n.split() if t]
    return toks[0] if toks else ""


def _code_in_text(code, text):
    """编号原文是否出现在文本中（忽略大小写；要求编号两侧不是字母数字，避免 JXZ418 误配 JXZ4180）。"""
    c = code.lower()
    t = (text or "").lower()
    i = t.find(c)
    while i != -1:
        b = t[i - 1] if i > 0 else ""
        a = t[i + len(c)] if i + len(c) < len(t) else ""
        if not (b and b.isalnum()) and not (a and a.isalnum()):
            return True
        i = t.find(c, i + 1)
    return False


def _sra_candidates(code):
    """按编号（如 JXZ418）在 NCBI SRA 检索，返回候选列表。"""
    low = str(code).strip().lower()
    if not low:
        return []
    hit = _sra_cache.get(low)
    if hit and time.time() - hit["ts"] < 86400:
        return hit["cands"]
    es = json.loads(_ncbi_get("esearch.fcgi",
                              {"db": "sra", "term": f"{code.strip()}[All Fields]",
                               "retmax": 15, "retmode": "json"}))
    ids = (es.get("esearchresult") or {}).get("idlist") or []
    cands = []
    if ids:
        sm = json.loads(_ncbi_get("esummary.fcgi",
                                  {"db": "sra", "id": ",".join(ids[:15]), "retmode": "json"}))
        res = sm.get("result", {})
        for uid in ids[:15]:
            item = res.get(uid)
            if not item:
                continue
            xml_txt = item.get("expxml") or ""
            runs_txt = item.get("runs") or ""
            runs = []
            if runs_txt.strip():
                try:
                    for r in ET.fromstring("<r>" + runs_txt + "</r>").iter("Run"):
                        runs.append(r.get("acc") or "")
                except ET.ParseError:
                    pass
            srx = srs = srp = study_name = organism = bioproj = biosample = title = platform = None
            try:
                root = ET.fromstring("<x>" + xml_txt + "</x>")  # expxml 为多根片段，需包一层
                title = root.findtext("Summary/Title")
                plat = root.find("Summary/Platform")
                platform = plat.get("instrument_model") if plat is not None else None
                ex = root.find("Experiment")
                srx = ex.get("acc") if ex is not None else None
                st = root.find("Study")
                srp = st.get("acc") if st is not None else None
                study_name = st.get("name") if st is not None else None
                org = root.find("Organism")
                organism = org.get("ScientificName") if org is not None else None
                sa = root.find("Sample")
                srs = sa.get("acc") if sa is not None else None
                bp = root.find("Bioproject")
                bioproj = bp.text if bp is not None else None
                bs = root.find("Biosample")
                biosample = bs.text if bs is not None else None
            except ET.ParseError:
                pass
            cand = {"uid": uid, "srx": srx, "srs": srs, "srp": srp, "study": study_name,
                    "organism": organism, "bioproject": bioproj, "biosample": biosample,
                    "title": title, "platform": platform, "runs": runs}
            # 编号原文核验：记录的标题/实验名/库名/样本名/Study 名等任一位置出现编号原文
            hay = " | ".join(str(x) for x in (title, srx, srs, srp, study_name,
                                              bioproj, biosample, xml_txt) if x)
            cand["code_hit"] = _code_in_text(code, hay)
            cands.append(cand)
    _sra_cache[low] = {"ts": time.time(), "cands": cands}
    return cands


@app.get("/api/sra-lookup")
def api_sra_lookup():
    me = current_user()
    ensure_visible(me, "uce")
    code = (request.args.get("sample") or "").strip()
    species = (request.args.get("species") or "").strip()
    if not code:
        abort(400, "缺少 sample 参数")
    cands = _sra_candidates(code)
    genus = _genus_of(species).lower()
    for c in cands:
        c["genus_match"] = bool(genus and c.get("organism")
                                and _genus_of(c["organism"]).lower() == genus)
    cands.sort(key=lambda c: (not c["code_hit"], not c["genus_match"]))
    return jsonify({"sample": code, "species": species, "genus": genus,
                    "count": len(cands), "candidates": cands})


@app.post("/api/sra-auto")
def api_sra_auto():
    """UCE 表批量联动：按 Sample Code 检索 NCBI SRA，唯一属名匹配者自动填 Accession number。"""
    user = current_user()
    chk_table("uce")
    require_writer(user, "uce")
    d = request.get_json(force=True, silent=True) or {}
    max_rows = min(30, max(1, int(d.get("max") or 30)))
    rids = d.get("rids") or []
    rows = [r for r in load_rows("uce") if not r["deleted"]]
    if rids:
        rows = [r for r in rows if r["id"] in set(rids)]
    todo = [r for r in rows
            if not (r["data"].get("Accession number") or "").strip()
            and (r["data"].get("Sample Code") or "").strip()]
    if not todo:
        return jsonify({"ok": True, "scanned": 0, "remaining": 0, "filled": [],
                        "ambiguous": [], "notfound": [], "errors": [], "batch": None})
    batch_id = _new_batch("S")
    has_key = bool((getattr(config, "NCBI_API_KEY", "") or os.environ.get("NCBI_API_KEY") or "").strip())
    filled, ambiguous, notfound, errors = [], [], [], []
    for r in todo[:max_rows]:
        code = r["data"]["Sample Code"].strip()
        species = r["data"].get("Species name") or ""
        try:
            cands = _sra_candidates(code)
        except Exception as e:
            errors.append({"rid": r["id"], "code": code, "error": str(e)[:160]})
            continue
        genus = _genus_of(species).lower()
        matches = [c for c in cands
                   if c.get("code_hit") and genus and c.get("organism")
                   and _genus_of(c["organism"]).lower() == genus]
        if len(matches) == 1:
            c = matches[0]
            acc = "; ".join(c["runs"]) or c.get("srx") or ""
            if not acc:
                errors.append({"rid": r["id"], "code": code, "error": "候选缺少 SRR/SRX 编号"})
                continue
            try:
                resp = _update_record(user, "uce", r["id"],
                                      {"changes": {"Accession number": acc}, "force": True},
                                      batch_id=batch_id)
                payload = json.loads(resp.get_data(as_text=True))
                if not payload.get("noop"):
                    filled.append({"rid": r["id"], "code": code, "species": species,
                                   "acc": acc, "organism": c.get("organism"),
                                   "srp": c.get("srp"), "study": c.get("study")})
            except Exception as e:
                errors.append({"rid": r["id"], "code": code, "error": str(e)[:160]})
        elif len(matches) > 1:
            for c in matches:
                c["genus_match"] = True
            ambiguous.append({"rid": r["id"], "code": code, "species": species,
                              "candidates": matches})
        else:
            for c in cands:
                c["genus_match"] = False
            notfound.append({"rid": r["id"], "code": code, "species": species,
                             "others": cands[:3]})
        time.sleep(0.15 if has_key else 0.45)  # NCBI 礼貌限速
    invalidate("uce")
    return jsonify({"ok": True, "batch": batch_id, "scanned": len(todo[:max_rows]),
                    "remaining": max(0, len(todo) - max_rows),
                    "filled": filled, "ambiguous": ambiguous,
                    "notfound": notfound, "errors": errors})


# ---------------------------------------------------------------- 名录自动月更（WSC / WAC）
CATALOG_MAX_AGE_DAYS = 31
_catalog_lock = threading.Lock()


def _catalog_age_days(fname):
    try:
        return (time.time() - os.path.getmtime(os.path.join(APP_DIR, fname))) / 86400
    except OSError:
        return 99999.0


def reload_catalogs():
    """名录文件更新后热重载内存缓存（联想/校验立即生效，无需重启）。"""
    global _WSC_SPEC_LOW, _WSC_SPEC_LIST, _WSC_LOW, _WSC_LIST, _WAC_GEN_LOW, _WAC_SPC_LOW
    _WSC_SPEC_LOW, _WSC_SPEC_LIST = _load_wsc_species()
    _WSC_LOW, _WSC_LIST = _load_wsc()
    _WAC_GEN_LOW, _WAC_SPC_LOW = _load_wac()
    print("  [名录更新] 内存缓存已热重载：WSC 属 %d 种 %d / WAC 属 %d 种 %d" % (
        len(_WSC_LOW), len(_WSC_SPEC_LOW), len(_WAC_GEN_LOW), len(_WAC_SPC_LOW)))


def run_catalog_update(trigger="auto"):
    """刷新 WSC（含种级）+ WAC 名录。阻塞调用（10-20 分钟），调用方放后台线程。"""
    if not _catalog_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有一次名录更新在进行中"}
    try:
        import update_wsc
        import update_wac
        print("  [名录更新] 开始（触发：%s）：WSC 属+种级拉取约 10-20 分钟…" % trigger)
        update_wsc.main(with_species=True)
        print("  [名录更新] WSC 完成，开始 WAC 伪蝎名录…")
        update_wac.main()
        reload_catalogs()
        summary = ("WSC/WAC 名录已自动更新：WSC 属 %d、种 %d；WAC 伪蝎属 %d、种 %d" % (
            len(_WSC_LOW), len(_WSC_SPEC_LOW), len(_WAC_GEN_LOW), len(_WAC_SPC_LOW)))
        with app.app_context():
            conn = get_db()
            log_edit(conn, "system", "specimens", None, None, "catalog_update",
                     {"_summary": summary + ("（手动触发）" if trigger != "auto" else "")})
            conn.commit()
        print("  [名录更新] 完成：", summary)
        return {"ok": True, "summary": summary}
    except Exception as e:
        print("  [名录更新] 失败：", e)
        return {"ok": False, "error": str(e)}
    finally:
        _catalog_lock.release()


RECYCLE_RETENTION_DAYS = 90  # 回收站保留天数：超期行在每日维护时物理删除（含附件）
ATTACHMENT_RETENTION_DAYS = 30  # 软删除附件保留天数：超期在每日维护时物理删除文件与行


def _attachment_maintenance(conn):
    """软删除附件永久清理：超过保留期才物理删除文件与行（保护期内可随时还原）。"""
    cutoff = (datetime.now() - timedelta(days=ATTACHMENT_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id, tname, rid, stored, fname FROM attachments WHERE CAST(deleted AS INTEGER)=1 AND deleted_at<?",
        (cutoff,)).fetchall()
    n = 0
    for r in rows:
        path = os.path.join(UPLOAD_DIR, r["tname"], str(r["rid"]), r["stored"])
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                continue
        conn.execute("DELETE FROM attachments WHERE id=?", (r["id"],))
        n += 1
    return n


def _recycle_maintenance():
    n_att = 0
    try:
        conn0 = get_db()
        n_att = _attachment_maintenance(conn0)
        conn0.commit()
        if n_att:
            print(f"[维护] 永久清理软删除附件 {n_att} 个（超过 {ATTACHMENT_RETENTION_DAYS} 天）")
    except Exception as e:
        print("[维护] 附件清理失败：", e)
    """回收站自动清理：物理删除删除时间超过保留期的行（含附件与目录）。
    updated_at 在移入回收站时刷新，作为保留期起点；动作整体留痕一条批次日志。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=RECYCLE_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id, tname, data FROM records WHERE deleted=1 AND updated_at < ?", (cutoff,)).fetchall()
    if not rows:
        conn.close()
        return 0
    bid = _new_batch("R")
    n = 0
    for r in rows:
        conn.execute("DELETE FROM records WHERE id=?", (r["id"],))
        conn.execute("DELETE FROM attachments WHERE tname=? AND rid=?", (r["tname"], r["id"]))
        shutil.rmtree(os.path.join(UPLOAD_DIR, r["tname"], str(r["id"])), ignore_errors=True)
        try:
            d = json.loads(r["data"])
        except ValueError:
            d = {}
        log_edit(conn, "system", r["tname"], None, d.get(TABLES[r["tname"]]["key"]) if TABLES.get(r["tname"], {}).get("key") else None,
                 "purge", {"_summary": f"回收站超 {RECYCLE_RETENTION_DAYS} 天自动清理（原行ID {r['id']}）", "_batch": bid}, batch_id=bid)
        n += 1
    conn.commit()
    invalidate("*")
    conn.close()
    return n


def _start_recycle_maintenance():
    def loop():
        time.sleep(300)  # 启动稳定后先执行一次
        while True:
            try:
                with app.app_context():
                    n = _recycle_maintenance()
                if n:
                    print(f"  [回收站] 自动清理 {n} 行（保留 {RECYCLE_RETENTION_DAYS} 天）")
            except Exception as e:
                print("  [回收站] 清理异常：", e)
            time.sleep(24 * 3600)
    threading.Thread(target=loop, daemon=True).start()


def _start_catalog_updater():
    def loop():
        time.sleep(120)  # 启动稳定后先检查一次
        while True:
            try:
                age = _catalog_age_days("wsc_species.json")
                if age > CATALOG_MAX_AGE_DAYS:
                    print("  [名录更新] wsc_species.json 已 %.0f 天未更新（阈值 %d 天）" % (
                        age, CATALOG_MAX_AGE_DAYS))
                    run_catalog_update("auto")
            except Exception as e:
                print("  [名录更新] 检查异常：", e)
            time.sleep(24 * 3600)
    threading.Thread(target=loop, daemon=True).start()


@app.post("/api/catalogs/refresh")
def api_catalogs_refresh():
    """管理员手动触发名录更新（后台执行，立即返回）。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可手动触发名录更新")
    if not _catalog_lock.acquire(blocking=False):
        abort(409, "已有一次名录更新在进行中")
    _catalog_lock.release()

    def job():
        time.sleep(0.2)
        run_catalog_update("manual:" + me["username"])

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "msg": "名录更新已开始（后台执行，约 10-20 分钟），完成后可在变更历史查看"})


# ---------------------------------------------------------------- 回收站定期自动清理
RETENTION_DAYS = 360


def autoclean_recycle_bin():
    """彻底删除回收站中删除超过 RETENTION_DAYS 天的行（含附件记录）。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    cleaned = {}
    try:
        for t, cfg in TABLES.items():
            rows = conn.execute(
                "SELECT id, data FROM records WHERE tname=? AND deleted=1 "
                "AND updated_at IS NOT NULL AND updated_at<?", (t, cutoff)).fetchall()
            if not rows:
                continue
            key = cfg.get("key")
            sample = []
            for r in rows:
                try:
                    sample.append(str((json.loads(r["data"]) or {}).get(key) or "#" + str(r["id"])))
                except ValueError:
                    sample.append("#" + str(r["id"]))
            conn.executemany("DELETE FROM records WHERE id=?", [(r["id"],) for r in rows])
            conn.executemany("DELETE FROM attachments WHERE tname=? AND rid=?",
                             [(t, r["id"]) for r in rows])
            log_edit(conn, "system", t, None, None, "autoclean",
                     {"_summary": "自动清理回收站 " + str(len(rows)) + " 行（删除超 " +
                      str(RETENTION_DAYS) + " 天）：" + "、".join(sample[:8]) +
                      ("…" if len(sample) > 8 else "")})
            cleaned[t] = len(rows)
        conn.commit()
        for t in cleaned:
            invalidate(t)
        if cleaned:
            print("  回收站自动清理:", cleaned)
    except Exception as e:
        print("  回收站自动清理失败:", e)


def _start_autoclean():
    def loop():
        time.sleep(25)
        while True:
            try:
                with app.app_context():
                    autoclean_recycle_bin()
            except Exception:
                pass
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True).start()


@app.get("/api/edits")
def api_edits():
    me = current_user()
    conn = get_db()
    where, params = ["1=1"], []
    if not me["admin"]:
        vis = list(visible_tables(me).keys())
        # 名录更新（catalog_update）是全局系统日志，所有类群可见
        where.append(f"(tname IN ({','.join('?' for _ in vis)}) OR action='catalog_update')")
        params.extend(vis)
    for field, col in (("t", "tname"), ("user", "user"), ("action", "action")):
        v = (request.args.get(field) or "").strip()
        if v:
            where.append(f"{col}=?")
            params.append(v)
    v = (request.args.get("from") or "").strip()
    if v:
        where.append("ts >= ?")
        params.append(f"{v} 00:00:00" if len(v) == 10 else v)
    v = (request.args.get("to") or "").strip()
    if v:
        where.append("ts <= ?")
        params.append(f"{v} 23:59:59" if len(v) == 10 else v)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(rkey LIKE ? OR changes LIKE ? OR user LIKE ?)")
        params += [f"%{q}%"] * 3
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) c FROM edits WHERE {w}", params).fetchone()["c"]
    limit = min(200, max(10, int(request.args.get("limit", 50))))
    offset = max(0, int(request.args.get("offset", 0)))
    rows = conn.execute(
        f"SELECT * FROM edits WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"] or "{}")
        except (ValueError, TypeError):
            pass
        d["evidence"] = "full" if r["hash"] else "legacy"  # 旧日志不回填新元数据，如实标注
        out.append(d)
    return jsonify({"edits": out, "total": total, "limit": limit, "offset": offset})


@app.get("/api/edits/row/<tname>/<int:rid>")
def api_edits_row(tname, rid):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    rows = get_db().execute(
        "SELECT * FROM edits WHERE tname=? AND rid=? ORDER BY id DESC LIMIT 200",
        (tname, rid),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"] or "{}")
        except (ValueError, TypeError):
            pass
        d["evidence"] = "full" if r["hash"] else "legacy"  # 旧日志不回填新元数据，如实标注
        out.append(d)
    return jsonify({"edits": out})


@app.get("/api/edit-users")
def api_edit_users():
    current_user()
    rows = get_db().execute("SELECT DISTINCT user FROM edits ORDER BY user").fetchall()
    return jsonify({"users": [r["user"] for r in rows]})


@app.get("/api/audit/verify")
def api_audit_verify():
    """审计链摘要校验（管理员）：按 id 序重算 prev_hash→hash 链，报告首个断点。
    说明：同库哈希无法抵抗拥有数据库完全写权限的攻击者（可重算整链）；
    更强证据请使用 /api/audit/checkpoint 将校验清单导出至独立介质保存。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可执行审计链校验")
    conn = get_db()
    prev = ""
    total = 0
    broken_at = None
    legacy = 0
    for r in conn.execute("SELECT * FROM edits ORDER BY id"):
        total += 1
        if not r["hash"]:
            legacy += 1
            continue
        # 与写入侧完全一致：changes 使用存储的原始字符串（不做解析重序列化）
        canon = json.dumps([r["prev_hash"], r["ts"], r["user"], r["tname"], r["rid"], r["rkey"],
                            r["action"], r["changes"], r["op_id"], r["parent_op_id"], r["req_id"],
                            r["source"], r["actor_type"], r["user_perms"],
                            r["row_version_before"], r["row_version_after"],
                            r["rules_versions"], r["reason"]],
                           ensure_ascii=False, sort_keys=True, default=str)
        h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        if r["prev_hash"] != prev or r["hash"] != h:
            broken_at = {"id": r["id"], "ts": r["ts"], "expect_prev": prev,
                         "stored_prev": r["prev_hash"], "hash_match": r["hash"] == h}
            break
        prev = r["hash"]
    return jsonify({"ok": broken_at is None, "total": total, "legacy": legacy,
                    "last_hash": prev, "broken_at": broken_at,
                    "limitation": "同库哈希链可检测意外损坏/局部篡改，但不能对抗拥有数据库完全写权限的攻击者；"
                                  "更强证据请定期 /api/audit/checkpoint 导出校验清单至独立介质"})


@app.post("/api/audit/checkpoint")
def api_audit_checkpoint():
    """导出审计链校验清单至独立目录 audit_chain/（可选方案：独立保存，脱离数据库写权限边界）。"""
    me = current_user()
    if not me["admin"]:
        abort(403, "仅管理员可导出审计校验清单")
    conn = get_db()
    last = conn.execute("SELECT id, hash, ts FROM edits WHERE hash<>'' ORDER BY id DESC LIMIT 1").fetchone()
    total = conn.execute("SELECT COUNT(*) c FROM edits").fetchone()["c"]
    payload = {"exported_at": now(), "by": me["username"], "total_events": total,
               "last_event_id": last["id"] if last else 0,
               "last_hash": last["hash"] if last else "",
               "note": "独立保存本文件与后续备份，可检测数据库内审计记录被篡改/删除"}
    os.makedirs(os.path.join(APP_DIR, "audit_chain"), exist_ok=True)
    fp = os.path.join(APP_DIR, "audit_chain", f"checkpoint-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    open(fp, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False, indent=1))
    return jsonify({"ok": True, "file": os.path.basename(fp), **payload})


@app.get("/api/edits/export")
def api_edits_export():
    """变更历史导出为 xlsx 审计清单（update 按字段展开）。
    非管理员只能导出自己可见表的历史；显式指向不可见表 → 403。"""
    me = current_user()
    conn = get_db()
    where, params = ["1=1"], []
    vis = visible_tables(me)
    if not me["admin"]:
        t_req = (request.args.get("t") or "").strip()
        if t_req and t_req not in vis:
            abort(403, f"您没有「{TABLES[t_req]['label'] if t_req in TABLES else t_req}」的查看权限，不能导出其变更历史")
        allowed = list(vis) or ["__none__"]
        where.append(f"tname IN ({','.join('?' * len(allowed))})")
        params.extend(allowed)
    for field, col in (("t", "tname"), ("user", "user"), ("action", "action")):
        v = (request.args.get(field) or "").strip()
        if v:
            where.append(f"{col}=?")
            params.append(v)
    v = (request.args.get("from") or "").strip()
    if v:
        where.append("ts >= ?")
        params.append(f"{v} 00:00:00" if len(v) == 10 else v)
    v = (request.args.get("to") or "").strip()
    if v:
        where.append("ts <= ?")
        params.append(f"{v} 23:59:59" if len(v) == 10 else v)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(rkey LIKE ? OR changes LIKE ? OR user LIKE ?)")
        params += [f"%{q}%"] * 3
    rows = conn.execute(f"SELECT * FROM edits WHERE {' AND '.join(where)} ORDER BY id ASC LIMIT 30000",
                        params).fetchall()
    action_txt = {"create": "新增", "update": "修改", "delete": "删除", "import": "导入",
                  "restore": "恢复", "version": "版本", "undelete": "还原", "purge": "彻底删除",
                  "file": "附件"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "变更历史"
    ws.append(["时间", "操作人", "数据表", "操作", "主键", "行ID", "字段", "旧值", "新值", "详情"])
    for c in ws[1]:
        c.font = HDR_FONT
        c.fill = HDR_FILL
    for e in rows:
        label = TD.display_label(e["tname"])
        act = action_txt.get(e["action"], e["action"])
        try:
            ch = json.loads(e["changes"] or "{}")
        except (ValueError, TypeError):
            ch = {}
        if e["action"] == "update" and not ch.get("_summary"):
            for f, pair in ch.items():
                if f.startswith("_"):
                    continue  # 内部键（_batch/_override 等）非字段对，入详情列
                ws.append([e["ts"], e["user"], label, act, e["rkey"], e["rid"], f,
                           str(pair[0]) if pair[0] is not None else "",
                           str(pair[1]) if pair[1] is not None else "", ""])
            meta = {k: v for k, v in ch.items() if k.startswith("_")}
            if meta:
                ws.append([e["ts"], e["user"], label, act, e["rkey"], e["rid"], "", "", "",
                           json.dumps(meta, ensure_ascii=False)[:500]])
        else:
            detail = ch.get("_summary") or ""
            if ch.get("_file"):
                detail += f"（{ch['_file']}）"
            ws.append([e["ts"], e["user"], label, act, e["rkey"], e["rid"], "", "", "",
                       detail or json.dumps(ch, ensure_ascii=False)[:500]])
    for col, w in zip("ABCDEFGHIJ", (20, 10, 12, 8, 22, 8, 24, 28, 28, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    # ---- README（审计清单导出；元数据不含令牌、口令等敏感信息） ----
    ws0 = wb.create_sheet("README", 0)
    rv = _rules_versions()
    for row in [
        ["项目", "值"],
        ["导出人", me["username"]],
        ["服务器时点（导出时点）", now()],
        ["应用版本 / 结构版本 / 名录版本", f"{APP_VERSION} / {rv['schema']} / WSC {rv.get('wsc_sync') or '未同步'}"],
        ["导出范围", f"变更历史审计清单（筛选：user={request.args.get('user') or '全部'}、表={request.args.get('tname') or '全部'}）"],
        ["数量口径", "每行=一条审计事件；update 事件按字段展开"],
        ["敏感性", "本文件不含访问令牌、账号口令或会话信息；动作详情中敏感字段已在写入时脱敏"],
    ]:
        ws0.append(row)
    for c in ws0[1]:
        c.font = HDR_FONT
    ws0.column_dimensions["A"].width = 30
    ws0.column_dimensions["B"].width = 110

    wb.properties.creator = "Z.ai"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"变更历史_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- 版本快照
@app.get("/api/versions/<tname>")
def api_versions(tname):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    rows = get_db().execute(
        "SELECT id, name, note, created_at, created_by, total FROM versions WHERE tname=? ORDER BY id DESC",
        (tname,),
    ).fetchall()
    return jsonify({"versions": [dict(r) for r in rows]})


@app.post("/api/versions/<tname>")
def api_version_create(tname):
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    d = request.get_json(force=True)
    rv = _rules_versions()
    name = (d.get("name") or "").strip() or f"快照 {now()}"
    note = (d.get("note") or "").strip()
    rows = load_rows(tname)
    snap = _snapshot_envelope(tname, [{"id": r["id"], "deleted": r["deleted"],
                                       "data": r["data"], "row_version": r["row_version"]}
                                      for r in rows])
    total = sum(1 for r in rows if not r["deleted"])
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO versions(tname,name,note,created_at,created_by,snapshot,total,"
        "category,description,protected,source_op_id,schema_ver,tables_rev,rules_ver,"
        "content_sha256,size_bytes,format,deps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tname, name, note, now(), user["username"], json.dumps(_pack_snapshot(snap), ensure_ascii=False),
         total, "manual", str(d.get("description") or ""), 1 if d.get("protected") else 0,
         "", rv["schema"], TABLES_REV, rv.get("wsc_sync") or "",
         hashlib.sha256(json.dumps(snap, ensure_ascii=False).encode()).hexdigest(),
         len(json.dumps(snap, ensure_ascii=False)), "v3", json.dumps(_version_deps(tname), ensure_ascii=False)),
    )
    log_edit(conn, user["username"], tname, None, None, "version",
             {"_summary": f"保存版本“{name}”（{total} 行）", "_note": note})
    conn.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "total": total})


@app.get("/api/versions/<tname>/<int:vid>/rows")
def api_version_rows(tname, vid):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    v = get_db().execute("SELECT * FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not v:
        abort(404, "版本不存在")
    _env, snap = _load_snapshot(v)
    q = (request.args.get("q") or "").strip().lower()
    page = max(1, int(request.args.get("page", 1)))
    per = min(500, max(10, int(request.args.get("per", 100))))
    eidx = linked_eidx(tname)
    disp = []
    for r in snap:
        if r["deleted"]:
            continue
        d, miss = display_row(tname, {"data": r["data"]}, eidx)
        disp.append({"id": r["id"], "data": d, "miss": miss})
    if q:
        disp = [r for r in disp if any(v is not None and q in str(v).lower() for v in r["data"].values())]
    total = len(disp)
    return jsonify({"columns": TABLES[tname]["columns"], "rows": disp[(page - 1) * per: page * per],
                    "total": total, "page": page, "per": per,
                    "version": {"id": v["id"], "name": v["name"], "note": v["note"],
                                "created_at": v["created_at"], "created_by": v["created_by"]}})


def _restore_plan(tname, env, mode="full", fields=None):
    """结构化恢复计划（只读）。
    身份契约：仅按行 id 匹配（保留原始行身份）；快照行 id 已不存在时不按业务编号
    强行匹配现存行——若编号撞车则该行标记 conflict。
    字段契约（full 完整恢复）：恢复到「快照对应结构的完整状态」——快照中存在的字段
    精确取值（区分 null/空串），快照中不存在的字段按显式迁移规则处理（锁定枚举填
    默认值，其余置 None），绝不保留恢复前的现值（修复仅合并快照字段导致的遗漏）。
    fields 模式（选择字段恢复）：仅恢复所选字段，语义独立、计划中显式标注。
    软删除契约：快照行 deleted 状态参与恢复（当前可见→soft_delete，已删→noop）。
    关联边界：linked 列不写入（实时取自来源表）；notices 提示依赖表版本选择。"""
    cfg = TABLES[tname]
    own = [c["name"] for c in cfg["columns"] if not c.get("linked")]
    colmeta = {c["name"]: c for c in cfg["columns"]}
    key = cfg["key"]
    sel = sorted(set(fields)) if (mode == "fields" and fields) else None
    snap_rows = env["rows"]
    current = {r["id"]: r for r in load_rows(tname)}
    rows_out = []
    counts = {"update": 0, "create": 0, "soft_delete": 0, "undelete": 0, "noop": 0, "conflict": 0}
    absent_total = 0
    matched = set()

    for s in snap_rows:
        sid = s["id"]
        sdata = s.get("data") or {}
        sdel = bool(s.get("deleted"))
        cur = current.get(sid)
        item = {"rid": sid, "kind": None, "cur_ver": cur["row_version"] if cur else None,
                "fields": {}, "absent_fields": [], "conflict": "", "snap_deleted": sdel}
        if cur is None:
            item["kind"] = "create"
            k = str(sdata.get(key) or "").strip() if key else ""
            if key and k:
                for r2 in current.values():
                    if not r2["deleted"] and str(r2["data"].get(key) or "").strip() == k:
                        item["kind"] = "conflict"
                        item["conflict"] = (f"快照行原 id {sid} 已不存在，而编号「{k}」与现存行 "
                                            f"{r2['id']} 重复：无法确定原始行身份，不做业务编号强行匹配")
                        break
            if item["kind"] == "create" and mode == "fields":
                item["kind"] = "conflict"
                item["conflict"] = "选择字段恢复只对当前已存在的行生效（快照行不在当前表中）"
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
            if item["kind"] == "conflict":
                rows_out.append(item)
                continue
            target = {}
            for col in own:
                if col in sdata:
                    target[col] = sdata[col]
                else:
                    target[col] = _absent_field_target(colmeta.get(col), item)
                    item["absent_fields"].append(col)
            absent_total += len(item["absent_fields"])
            item["fields"] = target
            rows_out.append(item)
            continue
        matched.add(sid)
        row, data, cur_ver = cur, cur["data"], cur["row_version"]
        if sdel:
            item["kind"] = "soft_delete" if not row["deleted"] else "noop"
            if item["kind"] == "noop":
                item["reason"] = "快照中该行已删除，当前也已删除"
            counts[item["kind"]] += 1
            rows_out.append(item)
            continue
        apply_cols = [c for c in own if (sel is None or c in sel)]
        for col in apply_cols:
            if col in sdata:
                target = sdata[col]
            else:
                target = _absent_field_target(colmeta.get(col), item)
                item["absent_fields"].append(col)
            cur_v = data.get(col)
            if not same(cur_v, target):
                item["fields"][col] = [cur_v, target]
        absent_total += len(item["absent_fields"])
        if row["deleted"]:
            item["kind"] = "undelete"
        elif item["fields"]:
            item["kind"] = "update"
        else:
            item["kind"] = "noop"
            item["reason"] = "与快照状态一致"
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        rows_out.append(item)

    for r in current.values():
        if r["id"] not in matched and not r["deleted"]:
            item = {"rid": r["id"], "kind": "soft_delete", "cur_ver": r["row_version"],
                    "fields": {}, "absent_fields": [], "conflict": "",
                    "reason": "该行在快照之后新增（恢复后移入回收站）"}
            counts["soft_delete"] += 1
            rows_out.append(item)

    notices = []
    if cfg.get("linked_source"):
        dep_label = TABLES[cfg["linked_source"]]["label"] if cfg["linked_source"] in TABLES else cfg["linked_source"]
        notices.append(f"关联列（如省份/采集地等）实时取自「{dep_label}」：单表恢复不会恢复其历史值；"
                       f"如需一并回退，请先在「{dep_label}」选择恢复其对应历史版本（依赖版本选择），再恢复本表")
    notices.append("快照仅含数据与软删除状态，不含附件与版本历史（不是整库/附件备份）")
    if env.get("snapshot_version", 1) < 2:
        notices.append("旧格式快照（未记录表定义版本）：缺失字段一律按迁移规则置空/默认")
    if absent_total:
        notices.append(f"迁移规则：快照中不存在的 {absent_total} 个字段位按规则置空（锁定枚举填默认值），"
                       "不保留恢复前的现值")
    if sel is not None:
        notices.append(f"选择字段恢复：仅处理 {len(sel)} 个所选字段，其余字段保持现状")
    return {"table": tname, "mode": mode, "fields": sel,
            "snapshot": {"snapshot_version": env.get("snapshot_version", 1),
                         "schema_version": env.get("schema_version"),
                         "table_rev": env.get("table_rev"), "saved_at": env.get("saved_at"),
                         "deps": env.get("deps") or {}},
            "counts": counts, "rows": rows_out, "notices": notices}


def _absent_field_target(col, item=None):
    """迁移规则：快照中不存在的字段——锁定枚举带默认值 → 默认值；其余 → None。"""
    if col and col.get("enum_locked") and col.get("default") is not None:
        return col["default"]
    return None


@app.get("/api/versions/<tname>/<int:vid>/diff")
def api_version_diff(tname, vid):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    v = get_db().execute("SELECT * FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not v:
        abort(404, "版本不存在")
    env, _rows = _load_snapshot(v)
    plan = _restore_plan(tname, env)
    return jsonify({"updates": plan["counts"]["update"], "creates": plan["counts"]["create"],
                    "deletes": plan["counts"]["soft_delete"],
                    "undeletes": plan["counts"]["undelete"], "noop": plan["counts"]["noop"],
                    "conflicts": plan["counts"]["conflict"], "notices": plan["notices"]})


def _apply_restore(user, tname, v, mode="full", fields=None, base_versions=None):
    """按计划恢复（full=完整恢复 / fields=选择字段恢复）。
    恢复前自动保存当前状态的保护快照；逐行以 row_version 条件更新重新验证
    （计划过期→该行冲突，整体 409 要求重新预览）；数据、保护快照与审计同事务提交。
    返回 (payload, protective_snapshot_id, plan)。不 commit——事务由调用方管理。"""
    conn = get_db()
    env, _rows = _load_snapshot(v)
    plan = _restore_plan(tname, env, mode, fields)
    print('[DEBUG restore] apply plan:', [(r['rid'], r['kind'], r['cur_ver'], list((r.get('fields') or {}).keys())) for r in plan['rows']],
          'base_versions:', base_versions, flush=True)
    if not isinstance(base_versions, dict) or not base_versions:
        abort(400, "请先调用 restore-preview 获取恢复计划，并在提交时携带 base_versions"
                   "（计划过期需重新预览）")
    protective_id = _pre_snapshot(conn, tname, user["username"],
                                  f"恢复到“{v['name']}”前自动保护快照（当前状态可随时恢复）",
                                  name=f"恢复前保护快照 {now()}")
    own = [c["name"] for c in TABLES[tname]["columns"] if not c.get("linked")]
    colmeta = {c["name"]: c for c in TABLES[tname]["columns"]}
    key = TABLES[tname].get("key")
    ts = now()
    n = {"update": 0, "create": 0, "soft_delete": 0, "undelete": 0, "noop": 0}
    conflicts = []
    for item in plan["rows"]:
        rid, kind, cur_ver = item["rid"], item["kind"], item["cur_ver"]
        if kind in ("noop", "conflict"):
            if kind == "noop":
                n["noop"] += 1
            else:
                conflicts.append({"rid": rid, "reason": item["conflict"]})
            continue
        bv = base_versions.get(str(rid), base_versions.get(rid))
        if kind == "create":
            # create（按原 id 重建）无现行可对照：豁免计划版本核对；身份冲突已在计划阶段标注
            pass
        else:
            if bv is None:
                conflicts.append({"rid": rid, "reason": "缺少该行的计划版本（请重新预览）"})
                continue
            try:
                bv = int(bv)
            except (TypeError, ValueError):
                conflicts.append({"rid": rid, "reason": "计划版本格式错误"})
                continue
            if bv != cur_ver:
                conflicts.append({"rid": rid,
                                  "reason": f"计划过期：该行已变化（当前版本 {cur_ver}，计划基于 {bv}）"})
                continue
        if kind == "update":
            row = conn.execute("SELECT data FROM records WHERE id=? AND tname=?",
                               (rid, tname)).fetchone()
            new = json.loads(row["data"])
            new.update({k: pair[1] for k, pair in item["fields"].items()})
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
                "WHERE id=? AND row_version=?",
                (json.dumps(new, ensure_ascii=False), ts, user["username"], rid, cur_ver))
        elif kind == "undelete":
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, deleted=0, updated_at=?, updated_by=? "
                "WHERE id=? AND row_version=?",
                (ts, user["username"], rid, cur_ver))
        elif kind == "soft_delete":
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, deleted=1, updated_at=?, updated_by=? "
                "WHERE id=? AND row_version=?",
                (ts, user["username"], rid, cur_ver))
        elif kind == "create":
            srow = next(s for s in env["rows"] if s["id"] == rid)
            sdata = srow.get("data") or {}
            target = {}
            for col in own:
                target[col] = sdata.get(col, _absent_field_target(colmeta.get(col)))
            try:
                cur = conn.execute(
                    "INSERT INTO records(id,tname,data,updated_at,updated_by,deleted,row_version) "
                    "VALUES(?,?,?,?,?,0,1)",
                    (rid, tname, json.dumps(target, ensure_ascii=False), ts, user["username"]))
            except sqlite3.IntegrityError:
                conflicts.append({"rid": rid, "reason": "该行 id 已被占用（无法保留原始行身份恢复）"})
                continue
        else:
            continue
        if cur.rowcount != 1 and kind != "create":
            conflicts.append({"rid": rid, "reason": "提交时被并发修改（行版本已变化），该行未恢复"})
        if conflicts:
            # 逐行版本不符或 create 身份冲突：整批原子回滚（恢复不部分执行）
            _tx_rollback(conn)
            raise WriteConflict({
                "error": f"恢复未执行：{len(conflicts)} 行冲突；请核对后重新预览再提交",
                "executed": False, "conflicts": conflicts,
                "protective_snapshot_id": None,
            })
        n[kind] += 1
    if conflicts:
        _tx_rollback(conn)
        raise WriteConflict({
            "error": f"恢复计划已过期：{len(conflicts)} 行与计划不符，本次未恢复；请重新预览后再提交",
            "executed": False,
            "conflicts": conflicts,
            "protective_snapshot_id": None,
        })
    summary = (f"恢复到版本“{v['name']}”（{mode}模式）：更新 {n['update']}、重建 {n['create']}、"
               f"移入回收站 {n['soft_delete']}、恢复可见 {n['undelete']}、无变化 {n['noop']}")
    log_edit(conn, user["username"], tname, None, None, "restore",
             {"_summary": summary,
              "_compat": "exact_restore（完整恢复按快照结构精确还原；缺失字段按迁移规则处理）",
              "_protective_snapshot": protective_id,
              "_fields": sorted(fields) if fields else None,
              "_batch": None}, batch_id=None)
    payload = {"ok": True, "updates": n["update"], "creates": n["create"],
               "deletes": n["soft_delete"], "undeletes": n["undelete"], "noop": n["noop"],
               "summary": summary, "protective_snapshot_id": protective_id,
               "notices": plan["notices"]}
    print(f'[DEBUG restore] response: {payload}', flush=True)
    return payload, protective_id, plan


@app.post("/api/versions/<tname>/<int:vid>/restore")
def api_version_restore(tname, vid):
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    conn = get_db()
    v = conn.execute("SELECT * FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not v:
        abort(404, "版本不存在")
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "full")
    if mode not in ("full", "fields"):
        abort(400, "mode 仅支持 full（完整恢复）/ fields（选择字段恢复）")
    fields = body.get("fields") if isinstance(body.get("fields"), list) else None
    if mode == "fields" and not fields:
        abort(400, "选择字段恢复需提供 fields 列表")
    ctx = _idem_ctx(user, f"version_restore:{tname}:{vid}", body)
    print(f'[DEBUG restore-route] body={body!r}', flush=True)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    try:
        payload, protective_id, plan = _apply_restore(user, tname, v, mode, fields,
                                                      body.get("base_versions"))
    except WriteConflict as c:
        _tx_rollback(conn)
        return jsonify(c.payload), 409
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    invalidate(tname)
    payload["notices"] = plan["notices"]
    return _finish_write(conn, payload, ctx)


# 恢复计划只读预览（含逐行原因、依赖提示、行版本）
@app.get("/api/versions/<tname>/<int:vid>/restore-preview")
def api_restore_preview(tname, vid):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    v = get_db().execute("SELECT * FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not v:
        abort(404, "版本不存在")
    mode = request.args.get("mode", "full")
    fields = [f for f in (request.args.get("fields") or "").split(",") if f.strip()]
    env, _rows = _load_snapshot(v)
    plan = _restore_plan(tname, env, mode if mode in ("full", "fields") else "full",
                         fields if fields else None)
    resp = dict(plan)
    resp["plan"] = [dict(it, row_version=it.get("cur_ver")) for it in plan["rows"]]  # 计划行明细（含行版本）
    return jsonify(resp)


# ---------------------------------------------------------------- 样本链（采集事件→标本→提取→文库→SRR）
@app.get("/api/lineage/voucher/<vid>")
def api_lineage_voucher(vid):
    """样本链查询：按 DNA 凭证号呈现 提取→文库→accession 与标本侧引用。
    只基于明确值匹配（含已确认别名）建立实体引用；未匹配分类呈现
    （traced/alias_candidate/pending/external/unknown），候选绝不自动选择。
    跨表连接不暴露调用者无权限的表节点。"""
    user = current_user()
    visible = visible_tables(user)
    conn = get_db()
    import lineage as LN
    ch = LN.chain_for_voucher(conn, vid, visible)
    ch["unmatched_stats"] = LN.unmatched_stats(conn, visible)
    return jsonify(ch)


@app.post("/api/lineage/alias")
def api_lineage_alias():
    """确认编号别名（原文 → 规范值）：留原文/规则/确认人（number_aliases），单批次可审计。
    仅当 规范值 在提取台账唯一存在时接受（非唯一候选禁止自动选择，必须先消歧）。"""
    user = current_user()
    require_writer(user, "uce")
    body = request.get_json(silent=True) or {}
    original = str(body.get("original") or "").strip()
    canonical = str(body.get("canonical") or "").strip()
    reason = str(body.get("reason") or "").strip() or "样本链人工确认（UCE Sample Code → DNA 提取凭证）"
    if not original or not canonical:
        abort(400, "original/canonical 必填")
    conn = get_db()
    hits = [json.loads(r["data"]) for r in conn.execute(
        "SELECT data FROM records WHERE tname='dna_extract' AND deleted=0")]
    hits = [str(d.get("DNAVoucherID") or "").strip() for d in hits]
    uniq = {h.lower(): h for h in hits}
    if canonical.lower() not in uniq:
        abort(400, f"规范值「{canonical}」在 DNA 提取台账不存在，不能确认为别名")
    if sum(1 for h in hits if h.lower() == canonical.lower()) != 1:
        abort(409, "该规范值在提取台账非唯一，先消歧再确认别名")
    if original == canonical:
        abort(400, "原文与规范值相同（无需别名）")
    batch_id = _new_batch("A")
    tgt = conn.execute(
        "SELECT id, data FROM records WHERE tname='dna_extract' AND deleted=0").fetchall()
    tgt_id = None
    for r0 in tgt:
        d0 = json.loads(r0["data"])
        if str(d0.get("DNAVoucherID") or "").strip() == uniq[canonical.lower()]:
            tgt_id = r0["id"]
            break
    conn.execute("INSERT OR REPLACE INTO number_aliases(tname,alias,canonical,rid,reason,batch_id,created_by,created_at) "
                 "VALUES('uce',?,?,?,?,?,?,?)",
                 (original, uniq[canonical.lower()], tgt_id,
                  reason + "（原文留痕：" + original + "）",
                  batch_id, user["username"], now()))
    log_edit(conn, user["username"], "uce", None, original, "update",
             {"_summary": f"确认编号别名：{original} → {uniq[canonical.lower()]}（样本链候选人工确认）",
              "_original": original, "_canonical": uniq[canonical.lower()], "_rule": reason}, batch_id=batch_id)
    conn.commit()
    invalidate("uce")
    return jsonify({"ok": True, "original": original, "canonical": uniq[canonical.lower()], "batch": batch_id})


# ---------------------------------------------------------------- 用户偏好
@app.get("/api/idempotency/<req_id>")
def api_idempotency_query(req_id):
    """结果待确认查询：客户端网络超时/响应丢失时，凭 X-Request-Id 查询该次写入
    是否已经落库（仅限本人名下的请求，按 幂等键=用户+请求ID 作用域）。
    返回 found=true 时 response 为当时的完整响应体（客户端据此以服务器值与版本为准）。"""
    user = current_user()
    if not req_id or len(req_id) > 200:
        abort(400, "req_id 无效")
    conn = get_db()
    row = conn.execute(
        "SELECT op_type, status, response, created_at FROM idempotency_keys "
        "WHERE username=? AND req_id=?", (user["username"], req_id)).fetchone()
    if not row:
        return jsonify({"found": False})
    return jsonify({"found": True, "op_type": row["op_type"], "status": row["status"],
                    "response": json.loads(row["response"] or "{}"),
                    "created_at": row["created_at"]})


@app.get("/api/prefs")
def api_prefs_get():
    u = current_user()
    row = get_db().execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        prefs = json.loads(row["prefs"] or "{}")
    except (ValueError, TypeError):
        prefs = {}
    if isinstance(prefs, dict):
        prefs = {k: v for k, v in prefs.items() if k != "prefs_history"}
    return jsonify({"prefs": prefs})


PREF_TOP_KEYS = ("hidden", "colw", "views", "colorder", "freeze", "view_sel", "density", "groups", "recents", "pinned")
PREF_NESTED_KEYS = ("hidden", "colw", "colorder", "freeze")   # {表名: ...} 结构


@app.put("/api/prefs")
def api_prefs_put():
    """局部合并协议（PATCH 语义）：未提供的顶层键保持原值；
    删除用 {"__delete__": [key]} 显式表达。嵌套表级键（hidden/colw/colorder/freeze）
    按 {表名: 值} 浅合并（表内整键替换），未知顶层键拒绝（防旧客户端清空未知设置）。"""
    u = current_user()
    d = request.get_json(force=True)
    if not isinstance(d, dict):
        abort(400, "prefs 需为 JSON 对象")
    conn = get_db()
    row = conn.execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        cur = json.loads(row["prefs"] or "{}") if row else {}
    except (ValueError, TypeError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    deletes = d.pop("__delete__", None)
    unknown = [k for k in d if k not in PREF_TOP_KEYS and k != "__delete__"]
    if unknown:
        abort(400, f"未知偏好键：{'、'.join(map(str, unknown))}（旧客户端请走局部合并协议）")
    # 顶层浅合并：同一路径冲突按服务器最后确认更新（避免两浏览器整包覆盖）
    for k, v in d.items():
        if k in PREF_NESTED_KEYS and isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update(v)          # 表级浅合并：更新某表的 colw 不丢其他表
        else:
            cur[k] = v
    if isinstance(deletes, list):
        for k in deletes:
            if isinstance(k, str) and "." in k:
                parts = k.split(".")
                node = cur
                for part in parts[:-1]:
                    if not isinstance(node.get(part), dict):
                        node = None
                        break
                    node = node[part]
                if isinstance(node, dict):
                    node.pop(parts[-1], None)   # 支持深层路径（colw.specimens.MHBU #）
            else:
                cur.pop(k, None)
    cur["prefs_version"] = cur.get("prefs_version", 0) + 1
    cur["prefs_updated_at"] = now()
    cur = _snapshot_prefs_history(conn, u, cur)
    conn.execute("UPDATE users SET prefs=? WHERE id=?",
                 (json.dumps(cur, ensure_ascii=False), u["id"]))
    conn.commit()
    return jsonify({"ok": True,
                    "prefs": {k: cur.get(k) for k in PREF_TOP_KEYS if k in cur},
                    "prefs_version": cur.get("prefs_version", 0),
                    "saved_keys": list(d.keys())})


PREF_HISTORY_KEY = "prefs_history"
PREF_HISTORY_MAX = 10


def _snapshot_prefs_history(conn, u, prefs_snapshot, tag="自动留存"):
    """可审阅找回方案：每次保存/重置前留存最近 N 份偏好快照（含完整 views 等）。
    历史已丢失的视图不能凭空恢复，但本次修复起的新丢失可从这里审阅找回。
    基于【传入的合并后 prefs】生成快照条目并只追加 history 键——不覆盖调用方的合并结果。"""
    row = conn.execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        old = json.loads(row["prefs"] or "{}") if row else {}
    except (ValueError, TypeError):
        old = {}
    hist = old.get(PREF_HISTORY_KEY) or []
    hist.insert(0, {"tag": tag, "at": now(),
                    "snapshot": {k: v for k, v in (prefs_snapshot or {}).items()
                                 if k != PREF_HISTORY_KEY}})
    prefs_snapshot[PREF_HISTORY_KEY] = hist[:PREF_HISTORY_MAX]
    return prefs_snapshot


@app.get("/api/prefs/history")
def api_prefs_history():
    """偏好历史（可审阅）：最近 N 份快照的时间/标签与顶层键清单（不直接返回全量避免刷屏）。"""
    u = current_user()
    row = get_db().execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        cur = json.loads(row["prefs"] or "{}") if row else {}
    except (ValueError, TypeError):
        cur = {}
    items = []
    for h in (cur.get(PREF_HISTORY_KEY) or []):
        snap = h.get("snapshot") or {}
        items.append({"tag": h.get("tag"), "at": h.get("at"),
                      "keys": sorted(snap.keys()),
                      "views_count": sum(len(v or []) for v in (snap.get("views") or {}).values()),
                      "views_preview": [{"table": t, "names": [x.get("name") for x in (v or [])]}
                                        for t, v in (snap.get("views") or {}).items()]})
    return jsonify({"history": items, "max": PREF_HISTORY_MAX,
                    "note": "找回方案：审阅后用 /api/prefs/restore-history 恢复指定快照"})


@app.post("/api/prefs/restore-history")
def api_prefs_restore_history():
    """从历史快照恢复（可审阅找回）：index 指定快照；只恢复除 history 外的偏好键。"""
    u = current_user()
    body = request.get_json(silent=True) or {}
    idx = body.get("index")
    conn = get_db()
    row = conn.execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        cur = json.loads(row["prefs"] or "{}") if row else {}
    except (ValueError, TypeError):
        cur = {}
    hist = cur.get(PREF_HISTORY_KEY) or []
    if not isinstance(idx, int) or idx < 0 or idx >= len(hist):
        abort(404, "历史快照不存在")
    snap = hist[idx].get("snapshot") or {}
    _snapshot_prefs_history(conn, u, cur, tag=f"恢复历史 {hist[idx].get('at')} 前")
    restored = {k: v for k, v in snap.items() if k != PREF_HISTORY_KEY}
    for k in PREF_TOP_KEYS:
        if k in restored:
            cur[k] = restored[k]
        else:
            cur.pop(k, None)
    cur["prefs_version"] = cur.get("prefs_version", 0) + 1
    conn.execute("UPDATE users SET prefs=? WHERE id=?",
                 (json.dumps(cur, ensure_ascii=False), u["id"]))
    conn.commit()
    return jsonify({"ok": True, "prefs": {k: cur.get(k) for k in PREF_TOP_KEYS},
                    "restored_from": hist[idx].get("at")})


@app.post("/api/prefs/reset")
def api_prefs_reset():
    """恢复全部偏好（范围明确）：清空全部偏好键（含 hidden/colw/views/colorder/freeze/view_sel/density），
    重置前自动留存快照（可从 /api/prefs/history 审阅找回）。需 confirm=RESET。"""
    u = current_user()
    if (request.get_json(silent=True) or {}).get("confirm") != "RESET":
        abort(400, "恢复全部偏好需 confirm=RESET（重置前自动留存快照，可审阅找回）")
    conn = get_db()
    row = conn.execute("SELECT prefs FROM users WHERE id=?", (u["id"],)).fetchone()
    try:
        cur = json.loads(row["prefs"] or "{}") if row else {}
    except (ValueError, TypeError):
        cur = {}
    cur2 = _snapshot_prefs_history(conn, u, cur, tag="恢复全部偏好 前")
    fresh = {PREF_HISTORY_KEY: cur2.get(PREF_HISTORY_KEY) or [],
             "prefs_version": cur.get("prefs_version", 0) + 1,
             "prefs_updated_at": now()}
    conn.execute("UPDATE users SET prefs=? WHERE id=?",
                 (json.dumps(fresh, ensure_ascii=False), u["id"]))
    conn.commit()
    return jsonify({"ok": True, "prefs": {}, "history_kept": len(fresh[PREF_HISTORY_KEY]),
                    "note": "全部偏好已恢复默认（历史快照保留，可审阅找回）"})


# ---------------------------------------------------------------- 全局搜索
@app.get("/api/global_search")
def api_global_search():
    """全局搜索：在当前用户可见的所有表中查找关键字。
    含关联列解析后的展示值（如标本表的省份/采集地来自采集地表）。
    按表分组返回：命中总数 + 前 8 条（键字段优先），供头部全局搜索框使用。"""
    user = current_user()
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"results": []})
    out = []
    for tname, cfg in visible_tables(user).items():
        key = cfg["key"]
        cols = cfg["columns"]
        eidx = linked_eidx(tname) if any(c.get("linked") for c in cols) else None
        hits, total = [], 0
        for r in load_rows(tname):
            if r["deleted"]:
                continue
            d, _ = display_row(tname, r, eidx) if eidx else (r["data"], False)
            match = None
            kv = str(d.get(key) if d.get(key) is not None else "")
            if q in kv.lower():
                match = (key, kv)
            else:
                for k, v in d.items():
                    if k == key:
                        continue
                    s = str(v if v is not None else "")
                    if q in s.lower():
                        match = (k, s)
                        break
            if not match:
                continue
            total += 1
            if len(hits) < 8:
                k, s = match
                low = s.lower()
                pos = low.find(q)
                start = max(0, pos - 18)
                end = min(len(s), pos + len(q) + 32)
                snippet = ("…" if start > 0 else "") + s[start:end] + ("…" if end < len(s) else "")
                hits.append({"rid": r["id"], "key": str(d.get(key) or f"#{r['id']}"),
                             "field": k, "snippet": snippet})
        if total:
            out.append({"table": tname, "label": TD.display_label(tname), "total": total, "hits": hits})
    return jsonify({"results": out})


# ---------------------------------------------------------------- 标本盒管理
BOX_PAT = re.compile(r"^Box\s*(\d+)\s*[（(]\s*(\d+)\s*[-–]\s*(\d+)\s*[)）]\s*$")


def box_usage_map():
    """标本编号 → 每个已注册盒的占用数（一次扫描）。"""
    out = {}
    all_boxes = get_db().execute("SELECT * FROM boxes ORDER BY num_start").fetchall()
    if not all_boxes:
        return {}, all_boxes  # 与有盒分支同形（调用方按二元组解包）
    usage = {b["id"]: 0 for b in all_boxes}
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        m = re.match(r"^MHBU-ARA-(\d{8})$", str(r["data"].get("MHBU #") or ""))
        if not m:
            continue
        num = int(m.group(1))
        for b in all_boxes:
            if b["num_start"] <= num <= b["num_end"]:
                usage[b["id"]] += 1
                break
    return {b["id"]: usage[b["id"]] for b in all_boxes}, all_boxes


@app.get("/api/boxes/next")
def api_box_next_number():
    """某盒内最小可用编号（新增标本时按盒预填）。"""
    current_user()
    name = (request.args.get("name") or "").strip()
    conn = get_db()
    b = conn.execute("SELECT * FROM boxes WHERE name=?", (name,)).fetchone()
    if not b:
        return jsonify({"num": None})
    pat = re.compile(r"^MHBU-ARA-(\d{8})$")
    used = set()
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        m = pat.match(str(r["data"].get("MHBU #") or ""))
        if m:
            used.add(int(m.group(1)))
    for n in range(b["num_start"], b["num_end"] + 1):
        if n not in used:
            return jsonify({"num": n, "key": f"MHBU-ARA-{n:08d}"})
    return jsonify({"num": None, "key": None, "full": True})


@app.get("/api/boxes")
def api_boxes_list():
    current_user()
    usage, all_boxes = box_usage_map()
    out = []
    for b in all_boxes:
        total = b["num_end"] - b["num_start"] + 1
        used = usage.get(b["id"], 0)
        out.append({"id": b["id"], "name": b["name"], "num_start": b["num_start"],
                    "num_end": b["num_end"], "note": b["note"], "total": total,
                    "used": used, "free": max(0, total - used), "full": used >= total})
    pat = re.compile(r"^MHBU-ARA-(\d{8})$")
    used_nums = set()
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        m = pat.match(str(r["data"].get("MHBU #") or ""))
        if m:
            used_nums.add(int(m.group(1)))
    suggested_box, suggested_num = None, None
    for b in sorted(all_boxes, key=lambda x: x["num_start"]):
        for n in range(b["num_start"], b["num_end"] + 1):
            if n not in used_nums:
                if suggested_num is None or n < suggested_num:
                    suggested_box, suggested_num = b["name"], n
                break
    return jsonify({"boxes": out, "can_manage": bool(current_user()["admin"] or current_user()["can_boxes"]),
                    "suggested_box": suggested_box,
                    "suggested_key": f"MHBU-ARA-{suggested_num:08d}" if suggested_num is not None else None})


def _require_box_manager(u):
    if u["admin"]:
        return
    if not u["can_boxes"]:
        abort(403, "设置标本盒需要专门的标本盒管理权限，请联系管理员")
    grp = u.get("group") or ""
    if grp and grp != "spider":
        abort(403, "标本盒为跳蛛系统专属功能，其他类群无权操作")


@app.post("/api/boxes")
def api_box_create():
    user = current_user()
    _require_box_manager(user)
    d = request.get_json(force=True)
    try:
        start, end = int(d["num_start"]), int(d["num_end"])
    except (KeyError, TypeError, ValueError):
        abort(400, "起止编号需为数字")
    if start > end:
        abort(400, "起始编号不能大于结束编号")
    name = (d.get("name") or f"Box{start}-{end}").strip()
    conn = get_db()
    if conn.execute("SELECT 1 FROM boxes WHERE name=?", (name,)).fetchone():
        abort(400, f"标本盒「{name}」已存在")
    conn.execute("INSERT INTO boxes(name,num_start,num_end,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                 (name, start, end, (d.get("note") or "").strip(), user["username"], now()))
    conn.commit()
    return jsonify({"ok": True, "name": name})


@app.patch("/api/boxes/<int:bid>")
def api_box_patch(bid):
    user = current_user()
    _require_box_manager(user)
    d = request.get_json(force=True)
    conn = get_db()
    if not conn.execute("SELECT 1 FROM boxes WHERE id=?", (bid,)).fetchone():
        abort(404, "标本盒不存在")
    if "note" in d:
        conn.execute("UPDATE boxes SET note=? WHERE id=?", ((d.get("note") or "").strip(), bid))
    conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/boxes/<int:bid>")
def api_box_delete(bid):
    user = current_user()
    _require_box_manager(user)
    conn = get_db()
    conn.execute("DELETE FROM boxes WHERE id=?", (bid,))
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/boxes/import_from_data")
def api_boxes_import():
    """从现有标本数据的 Box 列扫描登记盒（管理员或盒管理员）。"""
    user = current_user()
    _require_box_manager(user)
    conn = get_db()
    added = 0
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        v = str(r["data"].get("Box # (Start#-End#)") or "").strip()
        m = BOX_PAT.match(v)
        if not m:
            continue
        num, start, end = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if conn.execute("SELECT 1 FROM boxes WHERE name=?", (v,)).fetchone():
            continue
        conn.execute("INSERT INTO boxes(name,num_start,num_end,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                     (v, start, end, "存量导入", user["username"], now()))
        added += 1
    conn.commit()
    return jsonify({"ok": True, "added": added})


# ---------------------------------------------------------------- 跨表关联附件
@app.get("/api/attachments/diagnostics")
def api_attachment_diagnostics():
    """附件缺失/孤儿诊断（管理员）：DB 有行但文件缺失、磁盘有文件但无 DB 行。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员")
    conn = get_db()
    missing, ok = [], 0
    for r in conn.execute("SELECT id, tname, rid, fname, stored, deleted FROM attachments"):
        p = os.path.join(UPLOAD_DIR, r["tname"], str(r["rid"]), r["stored"])
        if os.path.exists(p):
            ok += 1
        else:
            missing.append({"id": r["id"], "tname": r["tname"], "rid": r["rid"],
                            "fname": r["fname"], "soft_deleted": bool(r["deleted"])})
    known = set()
    for r in conn.execute("SELECT tname, rid, stored FROM attachments"):
        known.add(os.path.join(UPLOAD_DIR, r["tname"], str(r["rid"]), r["stored"]))
    orphans = []
    for root, _dirs, files in os.walk(UPLOAD_DIR):
        for fn in files:
            if fn.startswith(".staging-"):
                continue
            p = os.path.join(root, fn)
            if p not in known:
                orphans.append(p)
    return jsonify({"ok_count": ok, "missing": missing[:200], "missing_count": len(missing),
                    "orphan_files": orphans[:200], "orphan_count": len(orphans)})


@app.get("/api/linked-files/<tname>/<int:rid>")
def api_linked_files(tname, rid):
    """跨表共享附件：同一凭证号在 标本/原蛛(DNAVoucherID)、DNA 提取(DNAVoucherID)、
    UCE 文库(Sample Code) 之间的行互相可见对方附件（JXZ… 等提取凭证贯穿三表）。
    只建立在精确值相等上，不猜测格式变体；对端表不可见时不建立关联
    （附件共享不能跨权读取）。返回 {own: [...], linked: [...]}，
    linked 项带 src_table/src_rid（实际归属，删除/还原只作用于归属行）。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    conn = get_db()
    row = conn.execute("SELECT data FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row:
        abort(404, "记录不存在")
    d = json.loads(row["data"])

    VOUCHER_COL = {"specimens": "DNAVoucherID", "mygalomorph": "DNAVoucherID",
                   "dna_extract": "DNAVoucherID", "uce": "Sample Code"}
    links = []  # [(other_table, other_key_field, key_val)]
    vis = visible_tables(user)
    if tname in VOUCHER_COL:
        kv = str(d.get(VOUCHER_COL[tname]) or "").strip()
        if kv:
            for other, ocol in VOUCHER_COL.items():
                if other != tname and other in vis:
                    links.append((other, ocol, kv))

    own_files = conn.execute(
        "SELECT id, fname, size, uploaded_by, uploaded_at, sha256, mime, note, deleted, deleted_at "
        "FROM attachments WHERE tname=? AND rid=? ORDER BY uploaded_at DESC", (tname, rid)).fetchall()
    own_list = []
    for f in own_files:
        d0 = dict(f)
        d0["deleted"] = int(d0["deleted"] or 0)           # TEXT '0' 历史行归一（类型坑）
        d0["owner_table"], d0["owner_rid"] = tname, rid   # 实际归属：删除/还原只影响这一份
        own_list.append(d0)

    linked_tables = []  # 跨表附件
    for other_table, other_key, key_val in links:
        # 找对端表中匹配的行
        matching_rids = []
        for r in load_rows(other_table):
            if r["deleted"]:
                continue
            other_d = r["data"]
            other_val = str(other_d.get(other_key) or "").strip()
            if other_val == key_val:
                matching_rids.append(r["id"])
        for mrid in matching_rids:
            fs = conn.execute(
                "SELECT id, fname, size, uploaded_by, uploaded_at, sha256, mime, note, deleted "
                "FROM attachments WHERE tname=? AND rid=? AND deleted=0 "
                "ORDER BY uploaded_at DESC", (other_table, mrid)).fetchall()
            for f in fs:
                d0 = dict(f)
                d0["deleted"] = int(d0["deleted"] or 0)   # TEXT '0' 历史行归一
                # 实际归属：文件物理存放在 src_table/src_rid（删除/还原只作用于归属行）
                linked_tables.append({**d0, "src_table": other_table, "src_rid": mrid})
    return jsonify({"own": own_list, "linked": linked_tables})


# ---------------------------------------------------------------- 稳定引用（采集事件/标本）
def _canon_key(tname, val):
    """规范化业务编号（标本/原蛛沿用既有归一，其余去除首尾空白）。"""
    if val is None:
        return ""
    v = str(val).strip()
    if tname == "specimens":
        return normalize_spec_key(v, kind="specimens")
    if tname == "mygalomorph":
        return normalize_spec_key(v, kind="mygalomorph")
    return v


def _event_by_number(num):
    """编号 → 稳定事件：精确主键匹配优先，其次别名重定向；返回 (rid, via) 或 (None, None)。"""
    conn = get_db()
    n = _canon_key("events", num)
    if not n:
        return None, None
    for r in load_rows("events"):
        if not r["deleted"] and _canon_key("events", r["data"].get("CollectionEventNumber")) == n:
            return r["id"], "exact"
    a = conn.execute("SELECT rid FROM number_aliases WHERE tname='events' AND alias=?",
                     (n,)).fetchone()
    if a:
        r = conn.execute("SELECT deleted FROM records WHERE id=? AND tname='events'",
                         (a["rid"],)).fetchone()
        if r and not r["deleted"]:
            return a["rid"], "alias"
    return None, None


@app.get("/api/events/by-number/<path:num>")
def api_event_by_number(num):
    """事件号（含别名）→ 事件行（标本侧写穿透编辑的目标行查询）。
    仅对 events 可见用户开放；不存在返回 404（前端据此提供「补建采集事件」）。"""
    user = current_user()
    ensure_visible(user, "events")
    rid, via = _event_by_number(str(num).strip())
    if rid is None:
        abort(404, "事件「%s」不存在（含别名）" % num)
    r = get_db().execute(
        "SELECT id, data, row_version FROM records WHERE id=? AND tname='events'", (rid,)).fetchone()
    return jsonify({"rid": r["id"], "row_version": r["row_version"],
                    "data": json.loads(r["data"]), "number": num, "via": via})


def _key_index_present(tname):
    conn = get_db()
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                             ("idx_records_key_" + tname,)).fetchone())


def _ref_count(tname, key_val):
    """引用计数：指向该键的可见引用行数（跨以本表为关联来源的表）。"""
    n = 0
    ck = _canon_key(tname, key_val)
    if not ck:
        return 0
    for t2, c2 in TABLES.items():
        if c2.get("linked_source") != tname:
            continue
        lk = c2.get("linked_key") or "CollectionEventNumber"
        for r in load_rows(t2):
            if not r["deleted"] and _canon_key(tname, r["data"].get(lk)) == ck:
                n += 1
    return n


@app.get("/api/refs/audit/<tname>")
def api_refs_audit(tname):
    """只读引用体检（不修复）：重复 / 空号 / 失配及其引用计数。
    政策口径：软删除保留编号占用（回收站可还原），彻底清理后释放。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    cfg = TABLES[tname]
    key = cfg["key"]
    rows = load_rows(tname)
    vis = [r for r in rows if not r["deleted"]]
    by_key = {}
    empty = []
    for r in vis:
        k = _canon_key(tname, r["data"].get(key))
        if not k:
            empty.append({"rid": r["id"], "key_hint": str(r["data"].get(key) or "")[:40],
                          "updated_at": r["updated_at"]})
            continue
        by_key.setdefault(k, []).append(r)
    duplicates = [{"key": k, "rids": [r["id"] for r in rs],
                   "referencing": _ref_count(tname, k)} for k, rs in by_key.items() if len(rs) > 1]
    mismatches = []
    if cfg.get("linked_source") == "events":
        conn = get_db()
        for r in vis:
            ce = str(r["data"].get("CollectionEventNumber") or "").strip()
            if not ce:
                continue
            rid_ev, _via = _event_by_number(ce)
            if rid_ev is None:
                row_st = conn.execute(
                    "SELECT status FROM ref_mismatch WHERE tname=? AND rid=?",
                    (tname, r["id"])).fetchone()
                mismatches.append({"rid": r["id"], "key": str(r["data"].get(key) or ""),
                                   "event_number": ce,
                                   "status": row_st["status"] if row_st else "unresolved"})
    aliases = [{"alias": a["alias"], "rid": a["rid"], "canonical": a["canonical"],
                "reason": a["reason"], "created_at": a["created_at"]}
               for a in get_db().execute(
                   "SELECT alias, rid, canonical, reason, created_at FROM number_aliases "
                   "WHERE tname=? ORDER BY id DESC LIMIT 200", (tname,))]
    return jsonify({"table": tname,
                    "policy": {"soft_delete_reuse": "软删除保留编号占用（回收站可还原）；彻底清理后释放",
                               "unique_enforced": _key_index_present(tname)},
                    "duplicates": duplicates, "empty": empty[:200], "empty_total": len(empty),
                    "mismatches": mismatches[:200], "mismatch_total": len(mismatches),
                    "aliases": aliases})


@app.post("/api/refs/migrate/<tname>")
def api_refs_migrate(tname):
    """审阅式迁移：唯一匹配自动建立稳定别名绑定（编号 → 行 id）；
    重复 / 空号 / 软删除行进入待确认清单（不自动处理）。幂等、留痕。"""
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    if TABLES[tname].get("key_not_unique"):
        abort(400, "该表主键允许重复（注册表例外），无稳定编号可绑定")
    rows = load_rows(tname)
    conn = get_db()
    bound, already = 0, 0
    pending = []
    have = {a["alias"]: a["rid"] for a in conn.execute(
        "SELECT alias, rid FROM number_aliases WHERE tname=?", (tname,))}
    batch_id = _new_batch("R")
    key = TABLES[tname]["key"]
    vis_by_key = {}
    for r in rows:
        if r["deleted"]:
            continue
        k = _canon_key(tname, r["data"].get(key))
        if k:
            vis_by_key.setdefault(k, []).append(r)
    for r in rows:
        k = _canon_key(tname, r["data"].get(key))
        if not k:
            pending.append({"rid": r["id"], "reason": "空号"})
            continue
        if k in have:
            already += 1
            continue
        same = vis_by_key.get(k, [])
        if len(same) != 1 or r["deleted"]:
            pending.append({"rid": r["id"], "key": k,
                            "reason": "重复编号" if len(same) > 1 else "软删除行（按策略保留占用）"})
            continue
        conn.execute("INSERT OR IGNORE INTO number_aliases(tname,alias,rid,canonical,reason,batch_id,created_by,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (tname, k, r["id"], k, "迁移绑定（唯一匹配自动建立）", batch_id,
                      user["username"], now()))
        bound += 1
    log_edit(conn, user["username"], tname, None, None, "ref_bind",
             {"_summary": f"稳定引用迁移：绑定 {bound}、已存在 {already}、待确认 {len(pending)}",
              "_batch": batch_id}, batch_id=batch_id)
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True, "bound": bound, "already": already,
                    "pending": pending[:200], "pending_total": len(pending)})


@app.post("/api/events/<int:rid>/rename")
def api_event_rename(rid):
    """事件改号：实体身份（行 id）不变；旧编号入别名表继续解析；
    审计记录 原编号→新编号 与理由。"""
    user = current_user()
    require_writer(user, "events")
    chk_table("events")
    body = request.get_json(force=True)
    new_key = _canon_key("events", body.get("new_key"))
    reason = str(body.get("reason") or "事件改号").strip()
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname='events'", (rid,)).fetchone()
    if not row or row["deleted"]:
        abort(404, "事件不存在或已删除")
    if not new_key:
        abort(400, "新编号不能为空")
    old_key = _canon_key("events", json.loads(row["data"]).get("CollectionEventNumber"))
    if new_key == old_key:
        return jsonify({"ok": True, "noop": True, "old": old_key, "new": new_key})
    clash = conn.execute("SELECT 1 FROM records WHERE tname='events' AND id<>? AND deleted=0 "
                         "AND json_extract(data,'$.\"CollectionEventNumber\"')=?",
                         (rid, new_key)).fetchone()
    if clash:
        abort(400, f"新编号「{new_key}」已被其他事件占用")
    batch_id = _new_batch("N")
    conn.execute("INSERT OR IGNORE INTO number_aliases(tname,alias,rid,canonical,reason,batch_id,created_by,created_at) "
                 "VALUES('events',?,?,?,'事件改号',?,?,?)",
                 (old_key, rid, new_key, batch_id, user["username"], now()))
    conn.execute("UPDATE records SET row_version=row_version+1, "
                 "data=json_set(data,'$.\"CollectionEventNumber\"',json(?)) WHERE id=? AND row_version=?",
                 (json.dumps(new_key), rid, row["row_version"]))
    log_edit(conn, user["username"], "events", rid, new_key, "update",
             {"CollectionEventNumber": [old_key, new_key], "_rename": True,
              "_aliases": [old_key], "reason": reason},
             batch_id=batch_id, parent_op_id=batch_id)
    conn.commit()
    invalidate("events")
    return jsonify({"ok": True, "rid": rid, "old": old_key, "new": new_key,
                    "identity": "events#%d" % rid, "note": "实体身份不变；旧编号经别名继续解析"})


@app.get("/api/events/merge-preview")
def api_event_merge_preview():
    """重复合并预览：两事件字段差异 + 受影响标本数（不写入）。"""
    user = current_user()
    ensure_visible(user, "events")
    keep, dup = int(request.args.get("keep", 0)), int(request.args.get("dup", 0))
    rows = {r["id"]: r for r in load_rows("events") if not r["deleted"]}
    if keep not in rows or dup not in rows or keep == dup:
        abort(400, "保留/被合并事件不存在或相同")
    dk, dd = rows[keep]["data"], rows[dup]["data"]
    dup_num = _canon_key("events", dd.get("CollectionEventNumber"))
    affected, bvs = [], {}
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        if _canon_key("events", r["data"].get("CollectionEventNumber")) == dup_num:
            affected.append(r["id"])
            bvs[str(r["id"])] = r.get("row_version")
    diff = [{"field": f, "keep": dk.get(f), "dup": dd.get(f)}
            for f in sorted(set(dk) | set(dd))
            if not same(dk.get(f), dd.get(f))]
    return jsonify({"keep": {"rid": keep, "number": dk.get("CollectionEventNumber")},
                    "dup": {"rid": dup, "number": dd.get("CollectionEventNumber")},
                    "diff": diff, "affected_specimens": len(affected),
                    "affected_sample": affected[:20], "base_versions": bvs,
                    "notes": ["合并后被合并编号将重定向到保留事件（别名），可随批次撤销",
                              "引用该编号的标本会逐条改为保留编号（逐条留痕）"]})


@app.post("/api/events/merge")
def api_event_merge():
    """重复合并：受影响标本逐条改指保留事件（逐条留痕）→ 被合并事件软删（重定向标记）
    → 别名重定向 → 汇总审计；整批原子、可随批次撤销；携带受影响标本 base_versions。"""
    user = current_user()
    require_writer(user, "events")
    chk_table("events")
    body = request.get_json(force=True)
    keep, dup = int(body.get("keep", 0)), int(body.get("dup", 0))
    reason = str(body.get("reason") or "重复合并").strip()
    bvs = body.get("base_versions") or {}
    if keep == dup:
        abort(400, "保留与被合并不能是同一行")
    conn = get_db()
    ctx = _idem_ctx(user, "event_merge:%d:%d" % (keep, dup), body)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    rows = {r["id"]: r for r in load_rows("events") if not r["deleted"]}
    if keep not in rows or dup not in rows:
        abort(404, "事件不存在或已删除")
    dk, dd = rows[keep]["data"], rows[dup]["data"]
    keep_num = _canon_key("events", dk.get("CollectionEventNumber"))
    dup_num = _canon_key("events", dd.get("CollectionEventNumber"))
    if not keep_num or not dup_num or keep_num == dup_num:
        abort(400, "两事件编号无效或相同")
    affected = [r for r in load_rows("specimens")
                if not r["deleted"]
                and _canon_key("events", r["data"].get("CollectionEventNumber")) == dup_num]
    conflicts = []
    for r in affected:
        bv = bvs.get(str(r["id"]), bvs.get(r["id"]))
        if bv is not None and str(bv) != str(r["row_version"]):
            conflicts.append({"rid": r["id"],
                              "reason": "标本在合并前已被修改（版本 %s，基于 %s）"
                                        % (r["row_version"], bv)})
    if conflicts:
        _tx_rollback(conn)
        return jsonify({"error": "合并未执行：%d 个引用标本版本与计划不符（原子模式）" % len(conflicts),
                        "executed": False, "conflicts": conflicts}), 409
    batch_id = _new_batch("M")
    ts = now()
    for r in affected:
        conn.execute(
            "UPDATE records SET row_version=row_version+1, "
            "data=json_set(data,'$.\"CollectionEventNumber\"',json(?)), updated_at=?, updated_by=? "
            "WHERE id=? AND row_version=?",
            (json.dumps(keep_num), ts, user["username"], r["id"], r["row_version"]))
        log_edit(conn, user["username"], "specimens", r["id"],
                 r["data"].get("MHBU #") or None, "update",
                 {"CollectionEventNumber": [dup_num, keep_num],
                  "_merge_of": batch_id, "_reason": reason}, batch_id=batch_id)
    conn.execute(
        "UPDATE records SET row_version=row_version+1, deleted=1, "
        "data=json_set(data,'$.\"_merged_into\"',json(?)), updated_at=?, updated_by=? "
        "WHERE id=? AND row_version=?",
        (json.dumps(keep), ts, user["username"], dup, rows[dup]["row_version"]))
    log_edit(conn, user["username"], "events", dup, dup_num, "delete",
             {"_summary": "重复合并：软删被合并事件并重定向到 %s" % keep_num,
              "_merged_into": keep, "_alias": dup_num, "_reason": reason,
              "_compat": "exact_restore（合并可随批次撤销恢复）"}, batch_id=batch_id)
    log_edit(conn, user["username"], "events", None, keep_num, "update",
             {"_summary": "重复合并头：%d 个标本改指 %s" % (len(affected), keep_num),
              "_merged_into": keep, "_aliases": [dup_num], "_reason": reason,
              "_batch": batch_id}, batch_id=batch_id)
    conn.execute("INSERT OR IGNORE INTO number_aliases(tname,alias,rid,canonical,reason,batch_id,created_by,created_at) "
                 "VALUES('events',?,?,?,?,?,?,?)",
                 (dup_num, keep, keep_num, reason, batch_id, user["username"], now()))
    payload = {"ok": True, "batch": batch_id, "keep": keep_num, "merged": dup_num,
               "affected_specimens": len(affected)}
    invalidate("events")
    invalidate("specimens")
    return _finish_write(conn, payload, ctx)


@app.post("/api/refs/resolve-mismatch")
def api_refs_resolve_mismatch():
    """失配引用处置（三选一，不猜填地点）：
    match=关联到现有事件（需显式给出事件编号）；pending=建立待完善事件（仅调用方提供字段，
    编号留空自动生成）；unknown=声明历史来源未知（仅记录状态）。"""
    user = current_user()
    require_writer(user, "specimens")
    body = request.get_json(force=True)
    rid = int(body.get("rid") or 0)
    act = body.get("action")
    note = str(body.get("note") or "").strip()
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname='specimens'", (rid,)).fetchone()
    if not row or row["deleted"]:
        abort(404, "标本不存在或已删除")
    data = json.loads(row["data"])
    ce = str(data.get("CollectionEventNumber") or "").strip()
    if act == "unknown":
        conn.execute("INSERT INTO ref_mismatch(tname,rid,event_number,status,note,created_by,created_at) "
                     "VALUES('specimens',?,?,?,?,?,?)",
                     (rid, ce or None, "unknown", note or "历史来源未知", user["username"], now()))
        log_edit(conn, user["username"], "specimens", rid, data.get("MHBU #"), "ref_status",
                 {"status": "unknown", "event_number": ce, "note": note,
                  "_note2": "不猜填地点"}, actor_type="human")
        conn.commit()
        return jsonify({"ok": True, "status": "unknown"})
    if act == "match":
        ev_num = _canon_key("events", body.get("event_key"))
        rid_ev, via = _event_by_number(ev_num)
        if rid_ev is None:
            abort(400, "事件「%s」不存在（含别名），请核对后重试" % ev_num)
        conn.execute("UPDATE records SET row_version=row_version+1, "
                     "data=json_set(data,'$.\"CollectionEventNumber\"',json(?)), updated_at=?, updated_by=? "
                     "WHERE id=? AND row_version=?",
                     (json.dumps(ev_num), now(), user["username"], rid, row["row_version"]))
        log_edit(conn, user["username"], "specimens", rid, data.get("MHBU #"), "update",
                 {"CollectionEventNumber": [ce, ev_num],
                  "_reason": note or "失配引用匹配现有事件"}, actor_type="human",
                 reason=note or None)
        conn.commit()
        invalidate("specimens")
        return jsonify({"ok": True, "status": "matched", "event_rid": rid_ev,
                        "event_number": ev_num, "via": via})
    if act == "pending":
        prov = body.get("event_data") or {}
        if not isinstance(prov, dict):
            abort(400, "event_data 需为对象")
        seq = conn.execute("SELECT COUNT(*) c FROM records WHERE tname='events'").fetchone()["c"] + 1
        cand = "M-%04d" % seq
        used = {str(r["data"].get("CollectionEventNumber") or "").strip() for r in load_rows("events")}
        while cand in used:
            seq += 1
            cand = "M-%04d" % seq
        ev_data = {k: v for k, v in prov.items() if v not in (None, "")}
        ev_data["CollectionEventNumber"] = cand
        cur = conn.execute(
            "INSERT INTO records(tname,data,updated_at,updated_by,deleted,row_version) VALUES(?,?,?,?,0,1)",
            ("events", json.dumps(ev_data, ensure_ascii=False), now(), user["username"]))
        conn.execute("UPDATE records SET row_version=row_version+1, "
                     "data=json_set(data,'$.\"CollectionEventNumber\"',json(?)), updated_at=?, updated_by=? "
                     "WHERE id=? AND row_version=?",
                     (json.dumps(cand), now(), user["username"], rid, row["row_version"]))
        log_edit(conn, user["username"], "events", cur.lastrowid, cand, "create",
                 {"_data": ev_data, "_reason": note or "失配引用建立待完善事件（未提供字段留空，不猜填）"},
                 actor_type="human")
        log_edit(conn, user["username"], "specimens", rid, data.get("MHBU #"), "update",
                 {"CollectionEventNumber": [ce, cand], "_reason": "失配引用建立待完善事件"},
                 actor_type="human")
        conn.commit()
        invalidate("events")
        invalidate("specimens")
        return jsonify({"ok": True, "status": "pending", "event_number": cand,
                        "event_rid": cur.lastrowid,
                        "note": "事件已建立但字段可能不完整（待完善），未猜填任何地点/日期"})
    abort(400, "action 仅支持 match / pending / unknown")



# ---------------------------------------------------------------- 统一导入计划
IMPORT_PLAN_TTL_MIN = 60          # 计划有效期（分钟）
CLEAR_SENTINEL = "NULL"           # 单元格填 NULL = 明确清空该字段；空单元格 = 保持原值


def _file_digest(data):
    import hashlib as _h
    return _h.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()


def _pick_import_sheet(wb, tname=None):
    """选数据工作表：系统模板/导出工作簿把 README 插在首位（说明页承诺 README/说明/示例
    不作为数据来源），按「表头能否映射到目标表自有列」从前往后选首个命中工作表；
    tname 为空或全部零映射时回退首个工作表（由计划器按未知列处理，兼容任意旧文件）。"""
    first = None
    for sn in wb.sheetnames:
        rows = list(wb[sn].iter_rows(values_only=True))
        if not rows:
            continue
        hdr = [clean_val(h) for h in rows[0]]
        if first is None:
            first = (sn, hdr, rows[1:])
            if not tname:
                break
        idx_map, _, _ = _map_columns([str(h or "").strip() for h in hdr], tname)
        if idx_map:
            return sn, hdr, rows[1:]
    return first


def _read_xlsx_formula_cells(fobj, tname=None):
    """第二遍解析：返回公式单元格（data_only 下无缓存值）的 (行号0基, 列号0基) 集合。
    选表规则与 _read_xlsx 一致，保证公式警告落在实际导入的工作表上。"""
    import openpyxl as _oxl
    wb = _oxl.load_workbook(fobj, data_only=False, read_only=True)
    picked = _pick_import_sheet(wb, tname)
    out = set()
    if picked:
        _, _, rows = picked
        # 行号契约与旧实现一致：含表头的工作表绝对行号（0 基）→ 计划器按 ri+1 对位数据行
        for ri, row in enumerate(rows, start=1):
            for ci, v in enumerate(row):
                if isinstance(v, str) and v.startswith("="):
                    out.add((ri, ci))
    wb.close()
    return out


def _build_import_plan(tname, mode, header, raw_rows, source, file_name,
                       file_digest, user, formula_cells=None):
    """统一计划器：解析/规范化/校验/逐行动作。返回 plan 字典（未落库）。
    空值契约：源中空单元格 = 空白保持（不写入该字段）；字面量 NULL = 明确清空。
    关键异常（空主键/文件内重复主键）→ 该行 blocking，整批阻止提交。"""
    import openpyxl as _oxl
    cfg = TABLES[tname]
    key = cfg["key"]
    own = set(own_columns(tname))
    label_map = {c["label"]: c["name"] for c in cfg["columns"] if c.get("label") and not c.get("linked")}
    colmeta = {c["name"]: c for c in cfg["columns"]}

    # ---- 映射：表头 → 自有字段（与 _map_columns 同规则；重复表头检测） ----
    hdr_norm = []
    seen_hdr = {}
    dup_hdr = []
    for i, h in enumerate(header):
        hh = str(h or "").strip()
        if hh and hh in seen_hdr:
            dup_hdr.append(hh)
        seen_hdr[hh] = i
        hdr_norm.append(hh)
    idx_map, unknown, renames = _map_columns(hdr_norm, tname)
    mapping = {"idx_map": {str(k): v for k, v in idx_map.items()},
               "renames": renames, "unknown": unknown[:30]}

    notices = []
    if unknown:
        notices.append("以下列不在本表字段内，已忽略并记录：" + "、".join(unknown[:12])
                       + (" 等" if len(unknown) > 12 else ""))
    if dup_hdr:
        notices.append("文件内存在重复表头：" + "、".join(sorted(set(dup_hdr))) + "（按第一个匹配列处理）")
    notices.append("空值契约：空单元格=保持原值；填 NULL=明确清空该字段")

    # ---- 公式无缓存检测 ----
    formula_rows = set()
    if formula_cells:
        for (ri, ci) in formula_cells:
            formula_rows.add(ri)

    # ---- 日期疑似检测（非日期列出现日期型单元格 → 警告） ----
    date_like = {}

    # ---- 逐行 ----
    plan_rows = []
    seen_keys = {}
    blocking = 0
    warn_total = 0
    counts = {"add": 0, "update": 0, "noop": 0, "ambiguous": 0, "error": 0}
    current_by_key = {}
    if key and not cfg.get("key_not_unique"):
        for r in load_rows(tname):
            if r["deleted"]:
                continue
            kv = str(r["data"].get(key) or "").strip()
            if kv:
                current_by_key.setdefault(kv.upper(), r)

    for ri, raw in enumerate(raw_rows):
        srow = ri + 2  # Excel 数据从第 2 行开始（含表头）
        errors, warnings = [], []
        d = {}
        for i, name in idx_map.items():
            v = raw[i] if i < len(raw) else None
            if isinstance(v, (dict, list)):
                v = str(v)
            elif isinstance(v, (datetime, date)):
                # Excel 会把形如 1-21/1.21 的编号等文本自动存成日期单元格：按显示值
                # 转字符串（与兼容路径 clean_val 同规则），否则计划落库 json.dumps 即 500；
                # 转换出的可疑值随后由 _looks_like_date 逐行警告提示核对
                v = clean_val(v)
            if v == CLEAR_SENTINEL:
                d[name] = "__CLEAR__"     # 明确清空 → 应用阶段置 None
            elif v is not None:
                d[name] = v
        if not d:
            continue    # 全空行（模板底部带格式的幽灵行/文件尾部空行）：无键无字段，静默跳过
        # 单位/格式归一（与正式导入同规则，保证计划即所提交）
        if cfg.get("alcohol_col") and cfg["alcohol_col"] in d:
            norm = normalize_alcohol(str(d[cfg["alcohol_col"]]))
            if str(norm) != str(d[cfg["alcohol_col"]]):
                warnings.append(f"{cfg['alcohol_col']} 已按单位归一为 {norm}")
            d[cfg["alcohol_col"]] = norm
        normalize_locked_enums(tname, d)
        if tname in KEY_PREFIX_RE and d.get("MHBU #"):
            d["MHBU #"] = normalize_spec_key(str(d["MHBU #"]), kind="specimens")
        if tname == "mygalomorph" and d.get("KYUARA #"):
            d["KYUARA #"] = normalize_spec_key(str(d["KYUARA #"]), kind="mygalomorph")
        if (ri + 1) in formula_rows:   # formula_cells 为含表头的工作表绝对行号（0 基）
            w = "第 %d 行存在公式且无缓存值（按空值处理）" % srow
            warnings.append(w)
            warn_total += 1
        # 日期疑似
        for k, v in list(d.items()):
            if isinstance(v, str) and _looks_like_date(v) \
                    and k not in ("FullCollectionData", "Collecting data", "Habitat",
                                  "CollectionMethod", "Notes", "SpecimenHabitat",
                                  "TaxonomyComment", "Acknowledgement"):
                warnings.append(f"「{k}」疑似日期文本：{v}（请确认列映射或改为 YYYY-MM-DD）")
                warn_total += 1

        item = {"row": srow, "key": "", "action": "", "data": d,
                "base_rid": None, "base_ver": None,
                "errors": errors, "warnings": warnings, "changes": {}}

        # ---- 主键 ----
        kv = _canon_key(tname, d.get(key)) if key else ""
        kv_raw = str(d.get(key) or "").strip() if key else ""
        if key and not cfg.get("key_not_unique"):
            if not kv_raw:
                errors.append("主键为空（必填）")
            elif kv.upper() in seen_keys:
                errors.append(f"文件内重复主键：{kv_raw}（同键多次出现，仅首行生效，其余阻止）")
            seen_keys[kv.upper()] = ri
        item["key"] = kv_raw or kv
        if errors:
            item["action"] = "blocked"
            blocking += 1
            counts["error"] += 1
            plan_rows.append(item)
            continue

        # ---- 动作判定与字段级 before/after ----
        base = current_by_key.get(kv.upper()) if key else None
        if base is not None:
            item["base_rid"] = base["id"]
            item["base_ver"] = base["row_version"]
        clear_fields, add_fields = [], []
        for k2 in list(d.keys()):
            if d[k2] == "__CLEAR__":
                del d[k2]
                clear_fields.append(k2)
                d[k2] = None
        for k2, v in d.items():
            if v == "__CLEAR__":
                d[k2] = None
        if base is not None:
            diff = {}
            for k2, v in d.items():
                old_v = base["data"].get(k2)
                if not same(old_v, v):
                    diff[k2] = [old_v, v]
            item["changes"] = diff
            if diff:
                item["action"] = "update"
                counts["update"] += 1
            else:
                item["action"] = "noop"
                counts["noop"] += 1
            item["fields"] = {k2: v[1] for k2, v in diff.items()}
            item["before"] = {k2: v[0] for k2, v in diff.items()}
            if clear_fields:
                item["warnings"].append("明确清空字段：" + "、".join(clear_fields))
                warn_total += 1
        else:
            item["action"] = "add"
            counts["add"] += 1
            add_fields = list(d.keys())
            item["fields"] = dict(d)
            if clear_fields:
                item["warnings"].append("新增行忽略「明确清空」标记：" + "、".join(clear_fields))
        plan_rows.append(item)

    # ---- 文件内重复主键：仅首行生效，其余 blocking（先跑一遍标记） ----
    key_seen = {}
    for it in plan_rows:
        if it["action"] in ("blocked",):
            continue
        kv = str(it["key"] or "").upper()
        if not kv:
            continue
        if kv in key_seen:
            it["action"] = "blocked"
            it["errors"].append(f"文件内重复主键：{it['key']}（与第 {key_seen[kv]} 行重复，仅首行生效）")
            blocking += 1
            counts["error"] += 1
            if it in applicable_dummy(it):
                pass
        else:
            key_seen[kv] = it["row"]

    replace_impact = None
    if mode == "replace":
        vis = [r for r in load_rows(tname) if not r["deleted"]]
        att = 0
        for r in vis:
            att += get_db().execute("SELECT COUNT(*) c FROM attachments WHERE tname=? AND rid=?",
                                    (tname, r["id"])).fetchone()["c"]
        refs = _ref_count(tname, "*") if False else None
        replace_impact = {"recycle_rows": len(vis), "attachments_on_rows": att,
                          "note": "被替换行移入回收站（可还原）；其附件随行保留在回收站行上；"
                                  "若该表被其他表引用（如事件被标本引用），引用方将失配，请先核对依赖"}
        notices.append("替换模式：当前 %d 行可见数据将移入回收站（可还原）；行上附件随行保留" % len(vis))

    return {
        "tname": tname, "mode": mode, "source": source,
        "file_name": file_name, "file_digest": file_digest,
        "header": [str(h or "") for h in header],
        "mapping": mapping, "rules": _rules_versions(),
        "rows": plan_rows, "counts": counts, "blocking": blocking,
        "warn_total": warn_total, "notices": notices,
        "replace_impact": replace_impact,
    }


def applicable_dummy(it):
    return False


def _looks_like_date(s):
    import re as _re
    return bool(_re.fullmatch(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}/\d{1,2}/\d{4}", str(s).strip()))


def _store_import_plan(plan, user):
    conn = get_db()
    pid = "P" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    conn.execute(
        "INSERT INTO import_plans(plan_id,tname,username,mode,source,file_digest,file_name,"
        "mapping,plan,rules_versions,counts,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, plan["tname"], user["username"], plan["mode"], plan["source"],
         plan["file_digest"], plan.get("file_name") or "",
         json.dumps(plan["mapping"], ensure_ascii=False),
         json.dumps({"rows": plan["rows"], "replace_impact": plan["replace_impact"],
                     "blocking": plan["blocking"], "notices": plan["notices"],
                     "header": plan["header"]}, ensure_ascii=False),
         json.dumps(plan["rules"], ensure_ascii=False),
         json.dumps(plan["counts"], ensure_ascii=False), now()))
    conn.commit()
    return pid


def _load_import_plan(pid, user, tname=None, mode=None):
    conn = get_db()
    r = conn.execute("SELECT * FROM import_plans WHERE plan_id=?", (pid,)).fetchone()
    if not r or (tname and r["tname"] != tname) or (mode and r["mode"] != mode):
        abort(404, "导入计划不存在（请重新预览生成）")
    if r["username"] != user["username"] and not user["admin"]:
        abort(403, "只能使用本人创建的导入计划")
    created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
    if (datetime.now() - created).total_seconds() > IMPORT_PLAN_TTL_MIN * 60:
        abort(409, "导入计划已过期（超过 %d 分钟），请重新预览" % IMPORT_PLAN_TTL_MIN)
    plan = json.loads(r["plan"])
    plan.update({"plan_id": pid, "tname": r["tname"], "mode": r["mode"],
                 "source": r["source"], "file_digest": r["file_digest"],
                 "file_name": r["file_name"], "mapping": json.loads(r["mapping"]),
                 "rules": json.loads(r["rules_versions"]),
                 "counts": json.loads(r["counts"]), "created_at": r["created_at"]})
    return plan



# ---------------------------------------------------------------- 批次追溯与回滚
@app.get("/api/edits/batch/<batch_id>")
def api_batch_detail(batch_id):
    """某次批量事件（导入/批量操作/撤销）产生的全部变动明细。"""
    me = current_user()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, ts, user, tname, rid, rkey, action, changes FROM edits "
        "WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    if not rows:
        abort(404, "批次不存在")
    head_row = next((r for r in rows if r["action"] == "import"), rows[0])
    head = dict(head_row)
    if head.get("tname") and not me["admin"] and head["tname"] not in visible_tables(me):
        abort(403, "该批次属于您无权查看的数据表")
    try:
        hc = json.loads(head["changes"] or "{}")
    except (ValueError, TypeError):
        hc = {}
    items = []
    for r in rows[1:]:
        if r["action"] not in ("create", "update", "delete"):
            continue  # 跳过批次头/快照关联等非数据行
        try:
            ch = json.loads(r["changes"] or "{}")
        except (ValueError, TypeError):
            ch = {}
        items.append({"id": r["id"], "rid": r["rid"], "rkey": r["rkey"],
                      "action": r["action"], "changes": ch})
    vid = hc.get("_snapshot_vid")
    snap = conn.execute("SELECT id, name FROM versions WHERE id=?", (vid,)).fetchone() if vid else None
    return jsonify({"batch": batch_id, "head": {**head, "changes": hc},
                    "count": len(items), "items": items[:1000],
                    "rollback_version": {"id": snap["id"], "name": snap["name"]} if snap else None,
                    "truncated": len(items) > 1000})


@app.post("/api/edits/batch/<batch_id>/rollback")
def api_batch_rollback(batch_id):
    """整批回滚（增量逆操作）：基于该批次日志的 before/after 逐行回退，
    与撤销共享同一冲突判定（后续被改的字段/行 → 冲突保留，不整表覆盖）。
    历史批次无逐行日志时，请改用「版本管理 → 整表恢复」（独立功能，展示影响并留痕）。"""
    user = current_user()
    body = request.get_json(silent=True) or {}
    skip_conflicts = bool(body.get("skip_conflicts"))
    conn = get_db()
    head = conn.execute("SELECT user, tname FROM edits WHERE batch_id=? ORDER BY id LIMIT 1",
                        (batch_id,)).fetchone()
    if not head:
        abort(404, "批次不存在")
    tname = head["tname"]
    require_writer(user, tname)
    chk_table(tname)
    per_row = conn.execute(
        "SELECT COUNT(*) c FROM edits WHERE batch_id=? AND action IN ('create','update','delete')",
        (batch_id,)).fetchone()["c"]
    if not per_row:
        abort(400, "该批次没有逐行日志，不能增量回滚；请使用「版本管理 → 整表恢复」（会展示影响并留痕）")
    try:
        payload = _apply_inverse(user, batch_id, "undo", skip_conflicts=skip_conflicts)
    except WriteConflict as c:
        _tx_rollback(conn)
        return jsonify(c.payload), 409
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    if isinstance(payload, tuple):
        _tx_rollback(conn)
        return payload
    payload["rolled_back"] = batch_id
    invalidate(tname)
    return _finish_write(conn, payload, _idem_ctx(user, f"batch_rollback:{batch_id}", body))


@app.patch("/api/versions/<tname>/<int:vid>")
def api_version_patch(tname, vid):
    """版本元数据维护：类别/描述/保护标记（受保护仅管理员可设置与取消）。"""
    user = current_user()
    require_writer(user, tname)
    body = request.get_json(silent=True) or {}
    conn = get_db()
    row = conn.execute("SELECT id FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not row:
        abort(404, "版本不存在")
    sets, args = [], []
    for k in ("category", "description"):
        if k in body:
            sets.append(f"{k}=?")
            args.append(str(body[k] or ""))
    if "protected" in body:
        if not user["admin"]:
            abort(403, "保护标记仅管理员可设置")
        sets.append("protected=?")
        args.append(1 if body["protected"] else 0)
    if not sets:
        abort(400, "没有要修改的字段")
    conn.execute(f"UPDATE versions SET {','.join(sets)} WHERE id=?", (*args, vid))
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/versions/<tname>/cleanup-plan")
def api_version_cleanup_plan(tname):
    """留存政策计划（dry-run，不删除）：候选=自动快照超出保留数量且未受保护；
    展示拟清理清单与可回收容量。受保护/发布/手动版本永不进入候选。"""
    user = current_user()
    ensure_visible(user, tname)
    conn = get_db()
    keep = int(request.args.get("keep_auto", 20))
    rows = conn.execute(
        "SELECT id, name, category, protected, size_bytes, created_at FROM versions "
        "WHERE tname=? ORDER BY id DESC", (tname,)).fetchall()
    seen_auto = 0
    cands = []
    for r in rows:
        if int(r["protected"] or 0) or r["category"] in ("manual", "published"):
            continue
        seen_auto += 1
        if seen_auto > keep:
            cands.append({"id": r["id"], "name": r["name"],
                          "size_bytes": int(r["size_bytes"] or 0),
                          "created_at": r["created_at"]})
    return jsonify({"keep_auto": keep, "candidates": cands, "candidate_count": len(cands),
                    "reclaim_bytes": sum(c["size_bytes"] for c in cands),
                    "note": "计划仅供参考；执行需管理员显式调用 cleanup-apply，受保护/发布/手动版本永不清理"})


@app.post("/api/versions/<tname>/cleanup-apply")
def api_version_cleanup_apply(tname):
    """执行留存计划（管理员）：仅删除计划内候选（未保护自动快照）；单事务，留审计。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员")
    keep = int((request.get_json(silent=True) or {}).get("keep_auto", 20))
    conn = get_db()
    rows = conn.execute("SELECT id, category, protected, size_bytes FROM versions "
                        "WHERE tname=? ORDER BY id DESC", (tname,)).fetchall()
    seen_auto = 0
    ids, reclaimed = [], 0
    for r in rows:
        if int(r["protected"] or 0) or r["category"] in ("manual", "published"):
            continue
        seen_auto += 1
        if seen_auto > keep:
            ids.append(r["id"])
            reclaimed += int(r["size_bytes"] or 0)
    for vid in ids:
        conn.execute("DELETE FROM versions WHERE id=?", (vid,))
    log_edit(conn, user["username"], tname, None, None, "version",
             {"_summary": f"按留存政策清理自动快照 {len(ids)} 个，回收约 {reclaimed / 1e6:.1f}MB"},
             batch_id=_new_batch("V"))
    conn.commit()
    return jsonify({"ok": True, "deleted": len(ids), "reclaimed_bytes": reclaimed})


@app.post("/api/admin/vacuum")
def api_admin_vacuum():
    """数据库压缩整理（VACUUM）：仅计划维护流程使用（管理员，显式确认词；建议先备份）。"""
    user = current_user()
    if not user["admin"] or (request.get_json(silent=True) or {}).get("confirm") != "VACUUM":
        abort(403, "仅管理员且需 confirm=VACUUM（压缩整理只应在计划维护或副本替换时进行）")
    before = os.path.getsize(dbm.DB_PATH)
    get_db().execute("VACUUM")
    after = os.path.getsize(dbm.DB_PATH)
    return jsonify({"ok": True, "before_bytes": before, "after_bytes": after})


@app.delete("/api/versions/<tname>/<int:vid>")
def api_version_delete(tname, vid):

    user = current_user()
    conn = get_db()
    v = conn.execute("SELECT * FROM versions WHERE id=? AND tname=?", (vid, tname)).fetchone()
    if not v:
        abort(404, "版本不存在")
    if not (user["admin"] or v["created_by"] == user["username"]):
        abort(403, "只有创建者或管理员可以删除版本")
    if int(v["protected"] or 0) and not (user["admin"] and (request.args.get("force") == "1")):
        abort(409, "受保护版本（如已发布）不能删除；管理员可加 force=1 显式解除")
    conn.execute("DELETE FROM versions WHERE id=?", (vid,))
    log_edit(conn, user["username"], tname, None, None, "version",
             {"_summary": f"删除版本“{v['name']}”"})
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- Excel 导出
HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
HDR_FILL = PatternFill("solid", fgColor="4472C4")


def _xlsx_safe(v, colname=None, key=None):
    """Excel 单元格安全化：业务编号按文本保存（保护前导零）；
    可能被当作公式执行的文本（= + - @ 开头）加撇号前缀——原文去掉撇号即得，可追溯。"""
    if v is None:
        return None
    s = str(v)
    if s == "":
        return s
    looks_num = s.isdigit() or (len(s) > 1 and s[0] == "0" and s.replace("-", "").replace(" ", "").isdigit())
    is_key = key and colname == key
    if looks_num and (is_key or len(s) >= 6):
        return s  # openpyxl 写字符串本身即文本；配合列格式 '@' 防编辑回退
    if s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _sheet_title(label):
    """Excel 工作表名禁止 : \\ / ? * [ ]：v4.21 表名带类群前缀（如「跳蛛: 河北大学
    标本信息」），冒号统一转安全连字符（半角/全角都转——全角「：」虽合规范，但部分
    Office/WPS 版本打开含它的文件仍会触发「修复」并把表改名「已恢复_SheetN」，
    用户实测中招；其余非法字符兜底替换），并截断到 31 字符上限。"""
    t = (label or "").replace("：", "-").replace(": ", "-").replace(":", "-")
    t = re.sub(r"[\\/:*?\[\]]", "_", t)
    return t[:31]


def _write_sheet(wb, tname, title, rows_data, eidx, cols_sel=None, linked_mode="live"):
    """rows_data: [{id, deleted, data(自有列)}]；关联列实时解析。
    linked_mode: live=当前数据导出（关联列实时值）；exclude=历史版本导出
    （关联列一律不导出——避免用今天的采集地信息冒充历史时点值）。"""
    ws = wb.create_sheet(_sheet_title(title))
    cfg = TABLES[tname]
    cols = cfg["columns"]
    if linked_mode == "exclude":
        cols = [c for c in cols if not c.get("linked")]
    if cols_sel:
        # 严格按用户指定顺序输出（cols_sel 顺序即输出顺序）；请求过的列缺失即报错已在前置校验
        pick = []
        for want in cols_sel:
            hit = next((c for c in cols if c["name"] == want), None)
            if hit:
                pick.append(hit)
        if not pick:
            abort(400, "cols 与本表任何列都不匹配（含关联列）；请从数据字典选择列名")
        cols = pick
    ws.append([c.get("label") or c["name"] for c in cols])
    for c in ws[1]:
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = Alignment(vertical="center")
    widths = [min(60.0, max(9.0, len(c["name"]) * 1.9 + 2)) for c in cols]
    for r in rows_data:
        if r.get("deleted"):
            continue
        d, _ = display_row(tname, r, eidx) if linked_mode == "live" else (r.get("data") or {}, None)
        vals = []
        for i, c in enumerate(cols):
            v = d.get(c["name"])
            if isinstance(v, (list, dict)):  # 防御：非原始类型转为文本
                v = json.dumps(v, ensure_ascii=False)
            v = _xlsx_safe(v, c["name"], cfg.get("key"))
            vals.append(v)
            if v is not None:
                ln = max(len(s) for s in str(v).split("\n"))
                widths[i] = max(widths[i], min(60.0, ln * 1.9 + 2))
        ws.append(vals)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
        if cols[i - 1].get("name") == cfg.get("key"):
            ws.column_dimensions[get_column_letter(i)].number_format = "@"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def _version_deps(tname):
    """发布版本冻结的依赖引用：分类表与采集地表的内容摘要（可复现性凭据）。"""
    deps = {}
    for t in ("taxonomy", "events"):
        if t in TABLES:
            rows = load_rows(t)
            h = hashlib.sha256()
            for r in rows:
                h.update(json.dumps(r["data"], sort_keys=True, ensure_ascii=False, default=str).encode())
            deps[t] = {"digest": h.hexdigest()[:16], "count": len(rows)}
    return deps


def _snapshot_rows(tname, version):
    if version in (None, "current"):
        return load_rows(tname), None
    v = get_db().execute("SELECT * FROM versions WHERE id=?", (version,)).fetchone()
    if not v or v["tname"] != tname:
        abort(404, "版本不存在")
    _env, rows = _load_snapshot(v)
    return rows, v


def _export_response(tname, rows_data, vinfo, cols_sel=None, header_only=False, scope_meta=None):
    cfg = TABLES[tname]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    eidx = linked_eidx(tname)
    label = TABLES[tname]["label"]
    linked_mode = "live" if not vinfo else "exclude"
    _write_sheet(wb, tname, label, [] if header_only else rows_data, eidx, cols_sel=cols_sel,
                 linked_mode=linked_mode)
    if header_only:
        # 说明工作表：必填/单位/枚举/空值策略/日期格式（示例在独立工作表，不会被导入）
        ws2 = wb.create_sheet("说明（请先阅读）")
        tips = [
            ["项目", "说明"],
            ["数据位置", "请在「%s」工作表第 2 行起填写数据；本表与「示例」表不会被导入" % label],
            ["主键", "「%s」为必填主键，留空的行会被拒绝" % (cfg["key"])],
            ["空值策略", "空单元格 = 保持原值（更新时不变）；填 NULL（大写）= 明确清空该字段"],
            ["单位/格式", "保存浓度填数字（75/85/95/100，小数自动×100）；日期列填 YYYY-MM-DD；经纬度支持度分(秒)格式自动转换"],
            ["枚举列", "、".join("%s ∈ {%s}" % (c["name"], "/".join(str(o) for o in c["options"]))
                            for c in cfg["columns"] if c.get("enum_locked") and c.get("options")) or "无"],
            ["必填列", "、".join(cfg.get("required_fields") or []) or "无（主键除外）"],
            ["自动带出", "填属名后科/亚科/族等自动带出；填采集事件号后省份等采集字段自动带出"],
            ["示例", "见「示例」工作表；示例数据仅作格式参考，请勿复制进数据表"],
        ]
        for row in tips:
            ws2.append(row)
        for c in ws2[1]:
            c.font = HDR_FONT
        ws2.column_dimensions["A"].width = 16
        ws2.column_dimensions["B"].width = 100
        ws3 = wb.create_sheet("示例")
        example_cols = [c["name"] for c in cfg["columns"] if not c.get("linked")][:8]
        ws3.append(example_cols)
        ws3.append(["示例-勿导入-1"] + ["示例"] * (len(example_cols) - 1))
        ws3.append(["示例-勿导入-2"] + ["示例"] * (len(example_cols) - 1))
        for c in ws3[1]:
            c.font = HDR_FONT
    # ---- README / 数据字典（每次导出都带；元数据不含令牌、口令等敏感信息） ----
    rv = _rules_versions()
    me = current_user()
    n_rows = 0 if header_only else len([r for r in rows_data if not r.get("deleted")])
    scope = scope_meta or {}
    ws0 = wb.create_sheet("README", 0)
    read_me = [
        ["项目", "值"],
        ["导出人", me["username"]],
        ["服务器时点（导出时点）", now()],
        ["应用版本 / 结构版本 / 名录版本", f"{APP_VERSION} / {rv['schema']} / WSC {rv.get('wsc_sync') or '未同步'}"],
        ["表定义版本 TABLES_REV", TABLES_REV],
        ["导出范围", scope.get("scope_text", f"表「{label}」当前查询结果")],
        ["行数（数量口径）", f"{n_rows} 行（未删除记录；每行=一条标本/一条记录，数量列另行表示个体数；「—」或空=未记录，不当作 0）"],
        ["版本", f"{'历史版本「' + vinfo['name'] + '」（' + (dict(vinfo).get('created_at') or '') + '）' if vinfo else '当前数据'}"],
        ["缺失值口径", "单元格为空或「—」表示未记录，与 0 不同；0 是明确的零"],
        ["关联回退说明", ("历史版本导出：关联列（省份/经纬度等来自采集地信息）一律不包含——"
                    "历史快照不含关联表数据，用今天的关联值会冒充历史时点。需要关联信息请另行导出采集地表并注明版本。"
                    if vinfo else
                    "关联列（省份/经纬度等）为导出时点的实时解析值；之后采集地表修改不会回写到本文件。")],
        ["业务编号说明", "编号列按文本保存（保护前导零）；以 ' 开头的单元格表示原文以 = + - @ 开头（防公式执行），去掉撇号即原文"],
        ["依赖版本", f"采集地表（关联列来源，仅当前导出）：实时值；历史版本导出：不包含"],
        ["敏感性", "本文件不含访问令牌、账号口令或会话信息"],
    ]
    for row in read_me:
        ws0.append(row)
    for c in ws0[1]:
        c.font = HDR_FONT
    ws0.column_dimensions["A"].width = 30
    ws0.column_dimensions["B"].width = 110
    # 数据字典
    wsdict = wb.create_sheet("数据字典")
    wsdict.append(["列名", "显示名", "字段ID", "英文名", "含义/单位", "类型/选项", "是否关联列"])
    for c in TABLES[tname]["columns"]:
        doc = COLUMN_DOCS.get(c["name"], "")
        fcat = FC.by_table_key(tname, c["name"])
        fid = fcat["field_id"] if fcat else ""
        fen = fcat["label_en"] if fcat else ""
        typ = "/".join(str(o) for o in (c.get("options") or [])) if c.get("options") else (
            "数字" if c.get("numeric") else ("长文本" if c.get("wide") else "文本"))
        wsdict.append([c["name"], c.get("label") or c["name"], fid, fen, doc, typ,
                       "是（实时取自来源表）" if c.get("linked") else ""])
    for c in wsdict[1]:
        c.font = HDR_FONT
    for col, w in (("A", 30), ("B", 22), ("C", 70), ("D", 26), ("E", 24)):
        wsdict.column_dimensions[col].width = w
    wb.properties.creator = "Z.ai"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    if not header_only:
        try:
            _u = current_user()
            log_edit(get_db(), _u["username"], tname, None, None, "export",
                     {"_summary": f"表导出：{TD.display_label(tname)} {len(rows_data)} 行",
                      "_scope": {"rows": len(rows_data)}},
                     source="export")
            get_db().commit()
        except Exception:
            pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"版本{vinfo['name']}" if vinfo else "当前"
    disp = (label or "").replace(": ", "：").replace(":", "：")  # 文件名里冒号同样转全角，比替换成下划线可读
    fname = re.sub(r"[\\/:*?\"<>|]", "_", f"{disp}_{tag}_{stamp}.xlsx")
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/export/<tname>")
def api_export(tname):
    """统一范围解析：本页(scope=page+ids) / 勾选(ids) / 筛选全部(q+filters+preset) /
    历史版本(version)。规则：
      - ids 给出且非空 → 以显式行 ID 集合为准（可叠加 q/filters/preset 二次收敛）；
        ids 请求中的无效 ID 明确报错，不静默扩大。
      - 无 ids → q/filters/preset 全部生效（与列表查询同一实现口径）。
      - cols 严格按用户指定顺序输出；未知列/空集合明确报错，不静默扩为整表/全部列。
    """
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    version = request.args.get("version", "current")
    rows, vinfo = _snapshot_rows(tname, version)

    # ---- 列选择：严格顺序 + 未知列报错 ----
    raw_cols = request.args.get("cols") or ""
    cols_sel = None
    if raw_cols.strip():
        cols_sel = [c.strip() for c in raw_cols.split(",") if c.strip()]
        valid_cols = [c["name"] for c in TABLES[tname]["columns"]]
        unknown = [c for c in cols_sel if c not in valid_cols]
        if unknown:
            abort(400, f"未知导出列：{'、'.join(unknown)}（表 {TABLES[tname]['label']} 的有效列见数据字典）")
        if not cols_sel:
            abort(400, "cols 为空集合：请指定至少一列，或省略 cols 参数导出全部列")

    # ---- 行范围解析（仅 current） ----
    if version == "current":
        raw_ids = (request.args.get("ids") or "").strip()
        id_set = None
        invalid_ids = []
        if raw_ids:
            id_set = set()
            for x in raw_ids.split(","):
                x = x.strip()
                if x.isdigit():
                    id_set.add(int(x))
                elif x:
                    invalid_ids.append(x)
            if invalid_ids:
                abort(400, f"ids 含无效值：{'、'.join(invalid_ids[:5])}（需为行 ID 数字）")
            if not id_set:
                abort(400, "ids 为空集合：请至少提供一个行 ID，或省略 ids 参数")
            missing = id_set - {r["id"] for r in rows if not r.get("deleted")}
            if missing:
                abort(404, f"ids 中 {len(missing)} 个行 ID 不存在或已删除（如 {sorted(missing)[:3]}）；"
                           f"如需删除行请先从回收站还原")
        qs = (request.args.get("q") or "").strip().lower()
        preset = (request.args.get("preset") or "").strip()
        raw_filters = request.args.get("filters") or ""
        filters = None
        if raw_filters:
            try:
                filters = json.loads(raw_filters)
                if not isinstance(filters, dict) or not filters:
                    filters = None
            except ValueError:
                abort(400, "filters 解析失败（需为 JSON 对象）")
        if id_set or qs or preset or filters:
            eidx = linked_eidx(tname)
            disp = []
            for r in rows:
                if r.get("deleted"):
                    continue
                d, _ = display_row(tname, r, eidx)
                if id_set is not None and r["id"] not in id_set:
                    continue
                if preset == "loaned" and TABLES[tname].get("loan_view"):
                    if not ((d.get("LoanSpecimenNumber") or d.get("LoanSpecimenFrom")
                             or d.get("SentDate")) and not d.get("ReturnDate")):
                        continue
                if preset == "types" and TABLES[tname].get("type_field"):
                    tf = TABLES[tname]["type_field"]
                    if str(d.get(tf) or "").strip().lower() in ("", "no"):
                        continue
                if filters and r["id"] not in {x["id"] for x in _apply_col_filters(
                        [{"id": r["id"], "data": d}], filters)}:
                    continue
                if qs and not any(v is not None and qs in str(v).lower() for v in d.values()):
                    continue
                disp.append(r)
            rows = disp
    ensure_visible(user, tname)  # 发送前复核：构建期间权限被调整则拒绝
    scope_meta = _export_scope_meta(tname, user, rows, vinfo, cols_sel)
    if request.args.get("template") and version == "current":
        return _export_response(tname, rows, vinfo, cols_sel=cols_sel, header_only=True,
                                scope_meta=scope_meta)
    return _export_response(tname, rows, vinfo, cols_sel=cols_sel, scope_meta=scope_meta)


def _export_scope_meta(tname, user, rows, vinfo, cols_sel):
    """导出范围元数据（进 README；不含令牌/口令）。范围三模式由调用端明确给出：
    本页=当前页行 ID；勾选记录=ids；筛选全部=查询条件（q/filters/preset）。"""
    me_user = user["username"]
    mode = request.args.get("scope") or ""
    n = len([r for r in rows if not r.get("deleted")])
    scope_text = {
        "page": f"本页（导出时点选中页的 {n} 行）",
        "selected": f"勾选记录（{n} 行显式行 ID 集合）",
        "filtered": f"筛选全部（查询条件解析，{n} 行，指纹 {request.args.get('digest') or '未提供'}）",
        "version": f"历史版本「{vinfo['name'] if vinfo else ''}」全表（{n} 行）",
    }.get(mode) or (f"表「{TABLES[tname]['label']}」查询结果（{n} 行）")
    parts = [f"表={tname}"]
    g = user.get("group") or ""
    if g:
        parts.append(f"类群={g}")
    if request.args.get("q"):
        parts.append(f"搜索=「{request.args.get('q')}」")
    if request.args.get("filters"):
        try:
            parts.append(f"列筛选 {len(json.loads(request.args['filters']))} 项")
        except (ValueError, TypeError):
            pass
    if request.args.get("preset"):
        parts.append(f"预设={request.args.get('preset')}")
    if request.args.get("ids"):
        parts.append(f"显式记录集合={len([x for x in request.args.get('ids').split(',') if x.strip().isdigit()])} 行")
    if cols_sel:
        parts.append(f"列={len(cols_sel)}（可选列）")
    scope_text += "；" + "；".join(parts)
    return {"scope_text": scope_text}


@app.get("/api/export/all")
def api_export_all():
    me = current_user()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    vis_at_start = list(visible_tables(me))
    for t in vis_at_start:
        _write_sheet(wb, t, TD.display_label(t), load_rows(t), linked_eidx(t))
    ws0 = wb.create_sheet("README", 0)
    rv = _rules_versions()
    n_all = 0
    for t in vis_at_start:
        n_all += len([r for r in load_rows(t) if not r.get("deleted")])
    for row in [
        ["项目", "值"],
        ["导出人", me["username"]],
        ["服务器时点（导出时点）", now()],
        ["应用版本 / 结构版本 / 名录版本", f"{APP_VERSION} / {rv['schema']} / WSC {rv.get('wsc_sync') or '未同步'}"],
        ["表定义版本 TABLES_REV", TABLES_REV],
        ["导出范围", f"全库（您可见的全部表：{'、'.join(TD.display_label(t) for t in vis_at_start)}）"],
        ["行数（数量口径）", f"{n_all} 行（未删除记录；各表分表存放；「—」或空=未记录，不当作 0）"],
        ["缺失值口径", "单元格为空或「—」表示未记录，与 0 不同"],
        ["业务编号说明", "编号列按文本保存（保护前导零）；以 ' 开头 = 原文以 = + - @ 开头（防公式执行）"],
        ["敏感性", "本文件不含访问令牌、账号口令或会话信息"],
    ]:
        ws0.append(row)
    for c in ws0[1]:
        c.font = HDR_FONT
    ws0.column_dimensions["A"].width = 30
    ws0.column_dimensions["B"].width = 110
    wb.properties.creator = "Z.ai"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _exdb2 = get_db()
    log_edit(_exdb2, me["username"], "specimens", None, None, "export",
             {"_summary": f"全库导出：{len(vis_at_start)} 张可见表共 {n_all} 行（XLSX，每表一工作表+README）",
              "_scope": {"tables": len(vis_at_start), "rows": n_all, "fmt": "xlsx"}},
             source="export-all")
    _exdb2.commit()

    return send_file(buf, as_attachment=True, download_name=f"标本信息库_当前_{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- 字段注册表服务
@app.get("/api/fields/scan/<tname>")
def api_fields_scan(tname):
    """只读扫描（不写入）：按字段组返回异常清单、精度分布与解析建议。
    group=date（缺失/精度/月13/日越界/闰日）· coord（歧义/半球/范围，只提示疑点）·
    alcohol（非数值原文）· counts（负数/文本/未知计数）。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    group = request.args.get("group", "all")
    limit = min(500, int(request.args.get("limit", 200)))
    key = TABLES[tname]["key"]
    rows = [r for r in load_rows(tname) if not r["deleted"]]
    out = {"table": tname, "group": group, "registry_version": FR.VERSION, "issues": []}

    if group in ("date", "all"):
        prec = {}
        issues = []
        for r in rows:
            for gname, (fdd, fmm, fyy) in FR.DATE_GROUP.items():
                r0 = FR.parse_date_parts(r["data"].get(fdd), r["data"].get(fmm), r["data"].get(fyy))
                prec[r0["precision"]] = prec.get(r0["precision"], 0) + 1
                if r0["error"]:
                    issues.append({"rid": r["id"],
                                   "key": str(r["data"].get(key) or ""),
                                   "pair": gname, "error": r0["error"]})
        out["date_precision"] = prec
        out["date_issues"] = issues[:limit]

    if group in ("coord", "all"):
        amb = []
        coord_fields = [("Latitude1", "lat"), ("Longitude1", "lon"),
                        ("Latitude2", "lat"), ("Longitude2", "lon")]
        for r in rows:
            for f, axis in coord_fields:
                p0 = FR.parse_coordinate(r["data"].get(f), axis)
                if p0["confidence"] == "ambiguous":
                    amb.append({"rid": r["id"],
                                "key": str(r["data"].get(key) or ""),
                                "field": f, "raw": str(r["data"].get(f))[:60],
                                "error": p0["error"]})
        out["coord_ambiguous"] = amb[:limit]

    if group in ("alcohol", "all"):
        conc_fields = [c["name"] for c in TABLES[tname]["columns"]
                       if "浓度" in c["name"] or "Preserve" in c["name"]]
        texts = []
        for r in rows:
            for f in conc_fields:
                v = r["data"].get(f)
                if v is None:
                    continue
                p0 = FR.parse_concentration(v)
                if p0["unit_note"] == "含说明文字，原样保留":
                    texts.append({"rid": r["id"], "field": f, "raw": str(v)[:60]})
        out["alcohol_free_text"] = texts[:limit]

    if group in ("counts", "all"):
        cnt_fields = [c["name"] for c in TABLES[tname]["columns"]
                      if any(w in c["name"] for w in ("Adult", "Subadult", "Juvenile"))
                      or c["name"] in ("标本数量", "雄性数量", "雌性数量", "幼体数量")]
        bad, unknown_n = [], 0
        for r in rows:
            for f in cnt_fields:
                p0 = FR.parse_count(r["data"].get(f))
                if p0["error"]:
                    bad.append({"rid": r["id"], "field": f, "raw": str(r["data"].get(f))[:30],
                                "error": p0["error"]})
                elif p0["unknown"]:
                    unknown_n += 1
        out["count_issues"] = bad[:limit]
        out["count_unknown"] = unknown_n
        out["note_counts"] = "「未知」按缺失语义保留原文，不当 0；负数/文本为错误"
    return jsonify(out)


def key_of(tname):
    return TABLES[tname]["key"]


@app.post("/api/fields/apply/<tname>")
def api_fields_apply(tname):
    """人工确认后写入：items=[{rid, group:"date", dd,mm,yyyy} | {rid, group:"coord",
    field:"Latitude1", decimal}] —— 行版本条件更新，审计留痕（rules_ver 一并落库）。"""
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    body = request.get_json(force=True)
    items = body.get("items") or []
    if not items:
        abort(400, "没有要应用的项")
    conn = get_db()
    batch_id = _new_batch("F")
    applied, conflicts = [], []
    for it in items:
        rid = int(it.get("rid") or 0)
        bv = it.get("base_version")
        row = conn.execute("SELECT * FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
        if not row or row["deleted"]:
            conflicts.append({"rid": rid, "reason": "记录不存在或已删除"})
            continue
        data = json.loads(row["data"])
        upd, fields = {}, {}
        if it.get("group") == "date":
            p0 = FR.parse_date_parts(it.get("dd"), it.get("mm"), it.get("yyyy"))
            if p0["error"]:
                conflicts.append({"rid": rid, "reason": p0["error"]})
                continue
            for col, val in zip(FR.DATE_GROUP[it.get("pair", "start")],
                                (it.get("dd"), it.get("mm"), it.get("yyyy"))):
                if not same(data.get(col), val):
                    upd[col] = val
                    fields[col] = [data.get(col), val]
        elif it.get("group") == "coord":
            f = it.get("field")
            dec = it.get("decimal")
            if f not in {c["name"] for c in TABLES[tname]["columns"]}:
                conflicts.append({"rid": rid, "reason": f"未知字段 {f}"})
                continue
            if not same(data.get(f), dec):
                upd[f] = dec
                fields[f] = [data.get(f), dec]
        else:
            conflicts.append({"rid": rid, "reason": "未知 group"})
            continue
        if not upd:
            continue
        new = dict(data)
        new.update(upd)
        bv = it.get("base_version")
        if bv is not None and str(bv) != str(row["row_version"]):
            conflicts.append({"rid": rid, "reason": "计划过期：该行已变化"})
            continue
        cur = conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
            "WHERE id=? AND tname=? AND row_version=?",
            (json.dumps(new, ensure_ascii=False), now(), user["username"], rid, tname, row["row_version"]))
        if cur.rowcount != 1:
            conflicts.append({"rid": rid, "reason": "提交时被并发修改"})
            continue
        log_edit(conn, user["username"], tname, rid, new.get(TABLES[tname]["key"]) or None,
                 "update", {"_fields": fields,
                            "_rules_ver": FR.VERSION,
                            "_source": "field_registry.apply"}, batch_id=batch_id)
        applied.append({"rid": rid, "fields": list(upd.keys())})
    if conflicts:
        _tx_rollback(conn)
        return jsonify({"error": f"未应用：{len(conflicts)} 项存在问题", "executed": False,
                        "applied": applied, "conflicts": conflicts}), 409
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True, "applied": applied, "count": len(applied)})


@app.get("/api/rows/<tname>/<int:rid>/field-raws")
def api_field_raws(tname, rid):
    """行详情：解析前 raw 原文与规范值/解析规则版本。"""
    user = current_user()
    ensure_visible(user, tname)
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT field, raw, canonical, rules_ver, created_by, created_at FROM field_raw "
        "WHERE tname=? AND rid=? ORDER BY id DESC", (tname, rid))]
    return jsonify({"items": rows})


@app.get("/api/rows/<tname>/<int:rid>/determinations")
def api_row_determinations(tname, rid):
    """行详情：历次鉴定（名称原文/接受名/复核状态/限定符/鉴定人/日期/依据/
    人工确认/名录版本与收录状态/来源），按鉴定序号升序。
    收录状态与复核状态是两个维度：service_unavailable 不构成分类结论。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    conn = get_db()
    row = conn.execute("SELECT id, data FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not row:
        abort(404, "记录不存在")
    items = [dict(r) for r in conn.execute(
        "SELECT id, seq, name_raw, name_accepted, genus, family, det_status, det_detail, "
        "determiner, det_date, evidence, confirmed_by, checklist_ver, checklist_status, "
        "source, batch_id, created_by, created_at FROM determinations "
        "WHERE tname=? AND rid=? ORDER BY seq, id", (tname, rid))]
    return jsonify({"items": items,
                    "protected": _det_protected(conn, tname, rid, json.loads(row["data"]))})



# ---------------------------------------------------------------- Excel 导入
def _read_xlsx(fobj, tname=None):
    """读导入数据：选表规则见 _pick_import_sheet（模板工作簿 README 首位时自动跳到数据表）。"""
    wb = openpyxl.load_workbook(fobj, data_only=True, read_only=True)
    try:
        picked = _pick_import_sheet(wb, tname)
        if not picked:
            abort(400, "Excel 文件为空")
        _, hdr, rows = picked
        return hdr, rows
    finally:
        wb.close()


def _apply_merge(conn, tname, new_dicts, user, batch_id=None, det_map=None):
    """按主键合并：已存在（未删除）→ 更新差异字段；不存在 → 新增。
    名称行追加鉴定历史（det_map 为写事务前的预核对结果）。"""
    cfg = TABLES[tname]
    key = cfg["key"]
    key_not_unique = cfg.get("key_not_unique")
    existing_by_key = {}
    existing_full = set()
    for r in load_rows(tname):
        if r["deleted"]:
            continue
        if key and not key_not_unique and r["data"].get(key):
            existing_by_key.setdefault(r["data"][key], r)
        existing_full.add(tuple(sorted((k, clean_val(v)) for k, v in r["data"].items())))
    n_add = n_upd = n_skip = 0
    per_row = []
    seen_file_keys = set()
    file_dup_keys = set()
    for d in new_dicts:
        k0 = str(d.get(key) or "").strip() if (key and not key_not_unique) else ""
        if k0:
            if k0 in seen_file_keys:
                # 导入文件内同编号多次出现：阻断静默 last-wins，逐条显式留痕
                file_dup_keys.add(k0)
                per_row.append((None, k0, "ambiguous",
                                {"_ambiguous": "该编号在导入文件内出现多次，本行未执行"}))
                n_skip += 1
                continue
            seen_file_keys.add(k0)
        sp_fixed = 0
        tc = TD.taxon_cols(tname)
        if TABLES[tname].get("genus_lookup") and d.get(tc["final"]):
            sp = species_normalize(d[tc["final"]])
            if sp and sp["name"] != d[tc["final"]]:
                d[tc["final"]] = sp["name"]
                sp_fixed += 1
            if sp and sp.get("genus"):
                d["Genus"] = sp["genus"]
        det_pre = None
        if tname in DET_TABLES:
            _rn = (str(d.get("FinalSpeciesName") or "").strip()
                   or str(d.get("TemporarySpeciesName") or "").strip())
            if _rn:
                det_pre = (det_map or {}).get(_rn.lower())
        if TABLES[tname].get("genus_lookup") and d.get("Genus"):
            d.update({k: v for k, v in taxonomy_fill(d, tname).items()})  # 属名归一
        elif TABLES[tname].get("genus_lookup"):
            for k, v in taxonomy_fill_upward(d, tname).items():  # 初鉴未到属：向上补齐
                d.setdefault(k, v)
        if TABLES[tname].get("geo_normalize"):
            geo.normalize_geo(d)
            _geo_convert_fields(d)  # 地名标准化
        _normalize_row_alcohol(tname, d)  # 浓度归一（v4.34 补：合并导入此前漏归一，0.95 原样入库）
        k = d.get(key) if key and not key_not_unique else None
        target = existing_by_key.get(k) if k else None
        if key and key_not_unique:  # 分类信息：整行相同则跳过
            tup = tuple(sorted((kk, clean_val(vv)) for kk, vv in d.items()))
            if tup in existing_full:
                n_skip += 1
                continue
        if target is None:
            if tname in DET_TABLES and det_pre and d.get("DetStatus") in (None, ""):
                d["DetStatus"] = "暂定"  # 导入新增的鉴定一律「暂定」，已复核需人工确认
            cur2 = conn.execute(
                "INSERT INTO records(tname,data,updated_at,updated_by,deleted) VALUES(?,?,?,?,0)",
                (tname, json.dumps(d, ensure_ascii=False), now(), user),
            )
            if k:
                existing_by_key[k] = {"id": cur2.lastrowid, "data": d, "deleted": 0}
            n_add += 1
            per_row.append((cur2.lastrowid, k, "create", {"_data": d}))
            if det_pre:
                _det_after_write(conn, tname, cur2.lastrowid, {}, d, d, user=user,
                                 batch_id=batch_id, source="import", det_pre=det_pre)
        else:
            diff = {c: [target["data"].get(c), d.get(c)] for c in d
                    if not same(target["data"].get(c), d.get(c))}
            if diff:
                new = dict(target["data"])
                new.update({c: p[1] for c, p in diff.items()})
                conn.execute("UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=?, deleted=0 WHERE id=?",
                             (json.dumps(new, ensure_ascii=False), now(), user, target["id"]))
                n_upd += 1
                per_row.append((target["id"], k, "update", diff))
                if det_pre:
                    _det_after_write(conn, tname, target["id"], target["data"], new, diff,
                                     user=user, batch_id=batch_id, source="import", det_pre=det_pre)
    return n_add, n_upd, n_skip, per_row


def _snapshot_envelope(tname, rows):
    """快照 v2 数据契约：区分「字段不存在 / null / 空串」（JSON 原样保留），
    记录 schema 版本、表定义版本（TABLES_REV）与关联依赖（linked_source/linked_key），
    行携带恢复时的 row_version。软删除状态按 rows[].deleted 显式保存。
    注意：快照只含数据与软删状态，不含附件与版本历史（不是整库备份）。"""
    cfg = TABLES[tname]
    return {"snapshot_version": 2, "schema_version": dbm.SCHEMA_VERSION,
            "table_rev": TABLES_REV, "table": tname, "saved_at": now(),
            "deps": {"linked_source": cfg.get("linked_source"),
                     "linked_key": cfg.get("linked_key")},
            "rows": rows}


def _pack_snapshot(env):
    import base64
    import gzip as _gz
    raw = json.dumps(env, ensure_ascii=False).encode("utf-8")
    data = base64.b64encode(_gz.compress(raw, 6)).decode("ascii")
    return {"fmt": "v3", "enc": "gzip+base64",
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "data": data}


def _unpack_snapshot(stored):
    import base64
    import gzip as _gz
    j = json.loads(stored)
    if isinstance(j, dict) and j.get("fmt") == "v3":
        try:
            raw = _gz.decompress(base64.b64decode(j["data"]))
        except (OSError, ValueError, EOFError):
            raise ValueError("快照解压失败：内容已损坏（解码路径 fmt=v3 gzip+base64，校验未通过）")
        if hashlib.sha256(raw).hexdigest() != j.get("sha256"):
            raise ValueError("快照校验和不匹配：内容已损坏（解码路径 fmt=v3 gzip+base64）")
        return json.loads(raw)
    return j


def _load_snapshot(v):
    """读取快照并兼容旧格式：旧裸数组 → legacy 契约（snapshot_version=1，
    table_rev=legacy，无依赖信息）。返回 (envelope, rows)。"""
    try:
        raw = _unpack_snapshot(v["snapshot"])
    except ValueError as e:
        abort(400, str(e))
    if isinstance(raw, dict) and raw.get("snapshot_version") == 2:
        return raw, raw.get("rows") or []
    return ({"snapshot_version": 1, "schema_version": "legacy", "table_rev": "legacy",
             "table": v["tname"], "saved_at": v["created_at"], "deps": {}, "rows": raw}, raw)


def _pre_snapshot(conn, tname, user, label, batch_id=None, name=None):
    """批量写入/恢复前自动快照，保证可回退。返回快照版本 id。
    name 可显式指定（如恢复保护快照），默认「导入前快照 <时间>」。"""
    pre_rows = load_rows(tname)
    snap = _snapshot_envelope(tname, [{"id": r["id"], "deleted": r["deleted"],
                                       "data": r["data"], "row_version": r["row_version"]}
                                      for r in pre_rows])
    pre_total = sum(1 for r in pre_rows if not r["deleted"])
    cur = conn.execute(
        "INSERT INTO versions(tname,name,note,created_at,created_by,snapshot,total) VALUES(?,?,?,?,?,?,?)",
        (tname, name or f"导入前快照 {now()}", label, now(),
         user, json.dumps(snap, ensure_ascii=False), pre_total),
    )
    if batch_id:  # 快照与批次关联（批次详情/回滚据此定位）
        log_edit(conn, user, tname, None, None, "version",
                 {"_summary": f"自动快照（批次 {batch_id}）", "_batch": batch_id}, batch_id=batch_id)
    return cur.lastrowid


def _norm_header(h):
    """表头归一：全角→半角、去空白、小写。"""
    s = str(h or "")
    tbl = {ord(c): ord(k) for c, k in (("（", "("), ("）", ")"), ("：", ":"), ("，", ","), ("　", " "))}
    s = s.translate(tbl)
    return re.sub(r"\s+", "", s).lower()


def _map_columns(hdr, tname):
    hdr = list(hdr)
    # 字段语义目录兼容表头（表作用域）：唯一命中才自动映射，碰撞停止自动映射交人工
    for i, h in enumerate(hdr):
        if not h:
            continue
        res = FC.resolve_header(tname, str(h))
        if res and "key" in res:
            hdr[i] = res["key"]
        elif res and "collision" in res:
            hdr[i] = "__COLLISION__" + ",".join(res["collision"])
    return _map_columns_inner(hdr, tname)


def _map_columns_inner(hdr, tname):
    """Excel 表头 → 自有列匹配。精确优先，其次归一化匹配；返回 (idx_map, unknown, renames)。"""
    own = {c["name"]: c for c in TABLES[tname]["columns"] if not c.get("linked")}
    exact = {c["name"]: _norm_header(c["name"]) for c in own.values()}
    label_norms = {c["name"]: _norm_header(c["label"]) for c in own.values() if c.get("label")}
    aliases = {a: b for a, b in (TD.HEADER_ALIASES.get(tname) or {}).items()
               if b in own}   # 合作方文件的惯用表头变体（如伪蝎「活体照编号」）
    idx_map, unknown, renames = {}, [], []
    used = set()
    for i, h in enumerate(hdr):
        if not str(h).strip():
            continue
        if h in own:
            idx_map[i] = h
            used.add(h)
            continue
        nh = _norm_header(h)
        cand = next((name for name, nx in exact.items() if nx == nh and name not in used), None)
        if not cand:
            cand = next((name for name, nx in label_norms.items() if nx == nh and name not in used), None)
        if not cand:
            cand = aliases.get(str(h).strip())
            if cand and cand in used:
                cand = None
        if cand:
            idx_map[i] = cand
            used.add(cand)
            if cand != h:
                renames.append({"excel": str(h), "db": cand})
        else:
            unknown.append(str(h))
    return idx_map, unknown, renames


@app.post("/api/import/preview/<tname>")
def api_import_preview(tname):
    """预览 = 生成不可变导入计划：绑定文件摘要、映射、空值策略、规则版本、
    目标行基础版本；逐行返回 工作表行号/主键/动作/旧值新值/自动带出/错误/警告。"""
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    f = request.files.get("file")
    mode = request.form.get("mode", "merge")
    if mode not in ("merge", "replace"):
        abort(400, "mode 只能是 merge 或 replace")
    if f is None:
        abort(400, "未收到文件")
    data = f.read()
    digest = _file_digest(data)
    formula_cells = _read_xlsx_formula_cells(io.BytesIO(data), tname)
    hdr, raw_rows = _read_xlsx(io.BytesIO(data), tname)

    # 保存的映射模板：表头摘要一致则自动应用
    mapping_name = request.form.get("mapping_name") or ""
    conn = get_db()
    applied_saved = None
    hdr_digest = _file_digest("|".join(str(h or "") for h in hdr))
    saved = conn.execute(
        "SELECT template_name AS name, mapping FROM import_mappings WHERE tname=? AND header_digest=? "
        "ORDER BY id DESC LIMIT 1", (tname, hdr_digest)).fetchone()
    if saved:
        applied_saved = saved["name"]
        try:
            mp = json.loads(saved["mapping"])
            if isinstance(mp.get("idx_map"), dict):
                hdr = [None] * (max(int(k) for k in mp["idx_map"]) + 1)
                for k, v2 in mp["idx_map"].items():
                    hdr[int(k)] = v2
        except (ValueError, TypeError):
            pass

    plan = _build_import_plan(tname, mode, hdr, raw_rows, "excel",
                              f.filename or "upload.xlsx", digest, user,
                              formula_cells=formula_cells)
    if applied_saved:
        plan["mapping_source"] = applied_saved
        plan["notices"].insert(0, f"已自动应用保存的映射模板「{applied_saved}」")
    pid = _store_import_plan(plan, user)

    # 兼容旧响应键
    adds = plan["counts"]["add"]
    upds = plan["counts"]["update"]
    return jsonify({
        "plan_id": pid, "file_digest": digest, "mode": mode,
        "columns": sorted({c for r in plan["rows"] for c in (r.get("fields") or {})}),
        "rows": plan["counts"]["add"] + plan["counts"]["update"],
        "add": adds, "update": upds,
        "noop": plan["counts"]["noop"], "skip": plan["counts"]["ambiguous"] + plan["counts"]["error"],
        "sample_add": [r["key"] for r in plan["rows"] if r["action"] == "add"][:8],
        "sample_upd": [{"key": r["key"], "fields": list((r.get("fields") or {}).keys())}
                       for r in plan["rows"] if r["action"] == "update"][:8],
        "replace_will_delete": (mode == "replace"),
        "blocking": plan["blocking"], "warn_total": plan["warn_total"],
        "detail": [{"row": r["row"], "key": r["key"], "action": r["action"],
                    "fields": r.get("fields") or {}, "errors": r["errors"],
                    "warnings": r["warnings"]} for r in plan["rows"][:500]],
        "notices": plan["notices"], "replace_impact": plan["replace_impact"],
        "mapping": plan["mapping"], "mapping_source": plan.get("mapping_source"),
        # v3.41 统一计划器曾把这两键并入 mapping 漏发旧键：前端预览弹窗读 pv.unknown/
        # pv.renames，undefined.length 一抛异常按钮永远停在「解析中…」（服务端计划其实
        # 已落库成功）——补回顶层旧键，前端另有兜底
        "unknown": plan["mapping"].get("unknown") or [],
        "renames": plan["mapping"].get("renames") or [],
        "rules": plan["rules"], "errors_download": f"/api/import/plan/{pid}/errors.csv",
    })


@app.get("/api/import/plan/<pid>/errors.csv")
def api_import_plan_errors(pid):
    """错误/警告清单下载（CSV，Excel 友好）。"""
    user = current_user()
    plan = _load_import_plan(pid, user)
    import csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["工作表行号", "主键", "动作", "级别", "信息"])
    for r in plan["rows"]:
        for e in r["errors"]:
            w.writerow([r["row"], r["key"], r["action"], "错误", e])
        for warn in r["warnings"]:
            w.writerow([r["row"], r["key"], r["action"], "警告", warn])
    for n in plan.get("notices", []):
        w.writerow(["", "", "", "提示", n])
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=import-errors-{pid}.csv"})


# 映射模板：保存与列表
@app.post("/api/import/mappings/<tname>")
def api_import_mapping_save(tname):
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        abort(400, "映射模板名称必填")
    conn = get_db()
    hdr_digest = _file_digest("|".join(str(h or "") for h in (d.get("headers") or [])))
    conn.execute("INSERT INTO import_mappings(tname,template_name,header_digest,mapping,created_by,created_at) "
                 "VALUES(?,?,?,?,?,?)",
                 (tname, name, hdr_digest, json.dumps(d.get("mapping") or {}, ensure_ascii=False),
                  user["username"], now()))
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/import/mappings/<tname>")
def api_import_mapping_list(tname):
    user = current_user()
    chk_table(tname)
    conn = get_db()
    rows = conn.execute("SELECT id, template_name, header_digest, created_at FROM import_mappings "
                        "WHERE tname=? ORDER BY id DESC LIMIT 50", (tname,)).fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


@app.post("/api/import/<tname>")
def api_import(tname):
    """批量导入提交：优先执行预览生成的不可变计划（plan_id）；
    计划过期（超时）或目标行版本不符 → 409 要求重新预览；
    数据、计划关联与审计同事务；重复提交按幂等规则返回原结果；
    兼容旧调用：未携带 plan_id 时即时解析（同样落审计与保护快照）。"""
    user = current_user()
    require_writer(user, tname)
    cfg = chk_table(tname)
    mode = request.form.get("mode", "merge")
    if mode not in ("merge", "replace"):
        abort(400, "mode 只能是 merge 或 replace")
    plan_id = request.form.get("plan_id") or ""
    conn = get_db()
    ctx = _idem_ctx(user, "import:%s" % tname, {"plan_id": plan_id, "mode": mode},
                    force_req_id=("plan:" + plan_id) if plan_id else None)
    hit = _idem_lookup(conn, ctx)
    if hit:
        return hit
    f = request.files.get("file")
    snap_id = None

    if plan_id:
        plan = _load_import_plan(pid=plan_id, user=user, tname=tname, mode=mode)
        if plan.get("blocking"):
            return jsonify({"error": f"导入计划包含 {plan['blocking']} 行阻断性错误"
                                     "（空主键/文件内重复主键），已拒绝执行；请修正后重新预览",
                            "blocking": plan["blocking"],
                            "errors_download": "/api/import/plan/%s/errors.csv" % plan_id}), 409
        batch_id = _new_batch("B")
        stale = []
        for it in plan["rows"]:
            if it["action"] == "update" and it.get("base_rid"):
                cur = conn.execute("SELECT row_version FROM records WHERE id=? AND tname=?",
                                   (it["base_rid"], tname)).fetchone()
                if not cur or cur["row_version"] != it["base_ver"]:
                    stale.append({"rid": it["base_rid"], "key": it["key"],
                                  "reason": "目标行在预览后被修改（计划过期）"})
        replace_now = None
        if mode == "replace":
            replace_now = [r for r in load_rows(tname) if not r["deleted"]]
            ri_ = plan.get("replace_impact") or {}
            if replace_now and ri_.get("recycle_rows") not in (None, len(replace_now)):
                stale.append({"reason": "表内可见行数与预览时不一致（目标已变化）"})
        if stale:
            _tx_rollback(conn)
            return jsonify({"error": "导入计划已过期：%d 项目标与预览时不符，请重新预览" % len(stale),
                            "executed": False, "plan_id": plan_id, "stale": stale[:50]}), 409
        snap_id = _pre_snapshot(conn, tname, user["username"],
                                "导入（计划 %s）前自动创建" % plan_id, batch_id)
        det_map = _det_map_for(tname, [it["data"] for it in plan["rows"]])
        if mode == "replace":
            for r in replace_now or []:
                log_edit(conn, user["username"], tname, r["id"],
                         r["data"].get(cfg["key"]) if cfg.get("key") else None,
                         "delete", {"_replace_of": batch_id,
                                    "_compat": "history_import（替换导入前的原行完整内容）",
                                    "_data": r["data"]}, batch_id=batch_id)
            conn.execute("UPDATE records SET row_version=row_version+1, deleted=1, "
                         "updated_at=?, updated_by=? WHERE tname=? AND deleted=0",
                         (now(), user["username"], tname))
            n_add = 0
            for it in plan["rows"]:
                # 计划按清空前的表分类（键已存在的行被标 update/noop），但替换先清空——
                # 若只应用 add，这些行会被静默丢弃（v4.24 前真实数据丢失：每次替换少 229 行）。
                # 清空后须按文件值全量重建 add/update/noop 三类行；blocked/ambiguous 仍不应用。
                if it["action"] not in ("add", "update", "noop"):
                    continue
                d = dict(it["data"])
                if TABLES[tname].get("genus_lookup") and d.get("Genus"):
                    d.update(taxonomy_fill(d, tname))
                if TABLES[tname].get("geo_normalize"):
                    geo.normalize_geo(d)
                    _geo_convert_fields(d)
                _normalize_row_alcohol(tname, d)
                det_pre = (det_map or {}).get(
                    ((str(d.get("FinalSpeciesName") or "").strip()
                      or str(d.get("TemporarySpeciesName") or "").strip()).lower())) if tname in DET_TABLES else None
                if det_pre and d.get("DetStatus") in (None, ""):
                    d["DetStatus"] = "暂定"
                cur = conn.execute(
                    "INSERT INTO records(tname,data,updated_at,updated_by,deleted,row_version) "
                    "VALUES(?,?,?,?,0,1)",
                    (tname, json.dumps(d, ensure_ascii=False), now(), user["username"]))
                log_edit(conn, user["username"], tname, cur.lastrowid,
                         d.get(cfg["key"]) if cfg.get("key") else None,
                         "create", {"_data": d, "_replace_of": batch_id}, batch_id=batch_id)
                if det_pre:
                    _det_after_write(conn, tname, cur.lastrowid, {}, d, d,
                                     user=user["username"], batch_id=batch_id,
                                     source="import", det_pre=det_pre)
                n_add += 1
            n_upd, n_skip = 0, len([i for i in plan["rows"] if i["action"] == "blocked"])
        else:
            apply_rows = [dict(it["data"]) for it in plan["rows"] if it["action"] in ("add", "update")]
            n_add, n_upd, n_skip, per_row_log = _apply_merge(
                conn, tname, apply_rows, user["username"], batch_id=batch_id, det_map=det_map)
            for rid, k, act, ch in per_row_log:
                log_edit(conn, user["username"], tname, rid, k, act, ch, batch_id=batch_id)
        if n_add + n_upd + n_skip == 0:
            conn.commit()
            invalidate(tname)
            return jsonify({"ok": True, "added": 0, "updated": 0, "skipped": 0,
                            "plan_id": plan_id, "noop": True,
                            "file_digest": plan["file_digest"]})
        changes = {"_file": plan.get("file_name") or "",
                   "_mode": "合并（更新已有+新增）" if mode == "merge" else "替换（清空后导入）",
                   "_summary": ("按计划 %s 执行：新增 %d、更新 %d" % (plan_id, n_add, n_upd))
                               + ("、跳过 %d" % n_skip if n_skip else ""),
                   "_plan_id": plan_id, "_file_digest": plan["file_digest"],
                   "_added": n_add, "_updated": n_upd, "_skipped": n_skip, "_batch": batch_id}
        if plan["mapping"].get("unknown"):
            changes["_unknown_cols"] = plan["mapping"]["unknown"][:20]
        changes["_snapshot_vid"] = snap_id
        log_edit(conn, user["username"], tname, None, None, "import", changes, batch_id=batch_id)
        invalidate(tname)
        resp = {"ok": True, "added": n_add, "updated": n_upd, "skipped": n_skip,
                "snapshot_version_id": snap_id, "plan_id": plan_id,
                "file_digest": plan["file_digest"]}
        return _finish_write(conn, resp, ctx)

    # ---- 兼容路径：未携带 plan_id（脚本/旧客户端）——即时解析并落审计与保护快照 ----
    if f is None:
        abort(400, "未收到文件")
    hdr, raw_rows = _read_xlsx(io.BytesIO(f.read()), tname)
    idx_map, unknown, renames = _map_columns(hdr, tname)
    new_rows = []
    for r in raw_rows:
        d = {}
        for i, name in idx_map.items():
            v = clean_val(r[i]) if i < len(r) else None
            if v is not None:
                d[name] = v
        if d:
            new_rows.append(d)
    conn = get_db()
    batch_id = "B" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    digest = _file_digest("|".join(str(h or "") for h in hdr) + "\n" +
                          "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in raw_rows))
    snap_id = _pre_snapshot(conn, tname, user["username"],
                            "导入 %s（%s）前自动创建" % (f.filename, mode), batch_id)
    det_map = _det_map_for(tname, new_rows)
    if mode == "replace":
        # 逐行留痕（旧行移入回收站 + 新行入库），保证批次可增量撤销
        for r0 in load_rows(tname):
            if r0["deleted"]:
                continue
            log_edit(conn, user["username"], tname, r0["id"],
                     r0["data"].get(cfg["key"]) if cfg.get("key") else None,
                     "delete", {"_replace_of": batch_id,
                                "_compat": "history_import（替换导入前的原行完整内容）",
                                "_data": r0["data"]}, batch_id=batch_id)
        conn.execute("UPDATE records SET row_version=row_version+1, deleted=1, updated_at=?, updated_by=? "
                     "WHERE tname=? AND deleted=0", (now(), user["username"], tname))
        per_row_log = []
        for d in new_rows:
            if TABLES[tname].get("genus_lookup") and d.get("Genus"):
                d.update(taxonomy_fill(d, tname))
            if TABLES[tname].get("geo_normalize"):
                geo.normalize_geo(d)
                _geo_convert_fields(d)
            _normalize_row_alcohol(tname, d)
            det_pre = None
            if tname in DET_TABLES:
                _rn = (str(d.get("FinalSpeciesName") or "").strip()
                       or str(d.get("TemporarySpeciesName") or "").strip())
                det_pre = det_map.get(_rn.lower()) if _rn else None
                if det_pre and d.get("DetStatus") in (None, ""):
                    d["DetStatus"] = "暂定"
            cur = conn.execute(
                "INSERT INTO records(tname,data,updated_at,updated_by,deleted,row_version) "
                "VALUES(?,?,?,?,0,1)",
                (tname, json.dumps(d, ensure_ascii=False), now(), user["username"]))
            per_row_log.append((cur.lastrowid, d.get(cfg["key"]), "create", {"_data": d}))
            if det_pre:
                _det_after_write(conn, tname, cur.lastrowid, {}, d, d,
                                 user=user["username"], batch_id=batch_id,
                                 source="import", det_pre=det_pre)
        n_add, n_upd, n_skip = len(new_rows), 0, 0
    else:
        n_add, n_upd, n_skip, per_row_log = _apply_merge(
            conn, tname, new_rows, user["username"], batch_id=batch_id, det_map=det_map)
    if n_add + n_upd + n_skip == 0:
        # 无任何变化的导入：不产生修改事件（避免误导性审计噪音）
        conn.commit()
        return jsonify({"ok": True, "added": 0, "updated": 0, "skipped": 0,
                        "snapshot_version_id": snap_id, "noop": True})
    changes = {"_file": f.filename,
               "_mode": "合并（更新已有+新增）" if mode == "merge" else "替换（清空后导入）",
               "_summary": "新增 %d 行、更新 %d 行" % (n_add, n_upd)
                           + ("、跳过重复 %d 行" % n_skip if n_skip else ""),
               "_added": n_add, "_updated": n_upd, "_skipped": n_skip, "_batch": batch_id,
               "_file_digest": digest,
               "_compat": "history_import（外部文件按列映射导入，不强制当前必填/枚举约束）"}
    if unknown:
        changes["_unknown_cols"] = unknown[:20]
    changes["_snapshot_vid"] = snap_id
    log_edit(conn, user["username"], tname, None, None, "import", changes, batch_id=batch_id)
    for rid, k, act, ch in per_row_log:
        log_edit(conn, user["username"], tname, rid, k, act, ch, batch_id=batch_id)
    conn.commit()
    invalidate(tname)
    resp = {"ok": True, "added": n_add, "updated": n_upd, "skipped": n_skip,
            "snapshot_version_id": snap_id, "batch": batch_id}
    if unknown:
        resp["unknown_cols"] = unknown[:20]
    return jsonify(resp)


@app.post("/api/rows/<tname>/paste")
def api_paste(tname):
    """从 Excel 复制区域粘贴导入。两段式：
    {"text":..., "plan_only": true} → 生成导入计划（逐行动作/错误/警告，不写入）；
    {"text":..., "plan_id": ...}    → 执行该计划（数据与审计同事务）。
    未携带 plan_id/plan_only 时保持旧行为（直接合并）。"""
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    body = request.get_json(force=True)
    text = (body.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip() != ""]
    if not lines:
        abort(400, "没有可识别的内容")
    own = set(own_columns(tname))
    cells = [ln.split("	") for ln in lines]
    label_map = {c["label"]: c["name"] for c in TABLES[tname]["columns"] if c.get("label") and not c.get("linked")}

    def _own_of(h):
        s = h.strip()
        return s if s in own else label_map.get(s)
    header_hits = sum(1 for c in cells[0] if _own_of(c))
    if header_hits < max(1, int(len(cells[0]) * 0.6)):
        abort(400, "第一行必须是本表的表头行（请从表头开始复制区域）")
    hdr = [c.strip() for c in cells[0]]
    idx_map, unknown = {}, []
    for i, h in enumerate(hdr):
        name = _own_of(h)
        if name:
            idx_map[i] = name
        elif h:
            unknown.append(h)
    new_rows = []
    for r in cells[1:]:
        d = {}
        for i, name in idx_map.items():
            v = clean_val(r[i]) if i < len(r) else None
            if v is not None:
                d[name] = v
        if d:
            new_rows.append(d)
    for d in new_rows:
        _normalize_row_alcohol(tname, d)
    validate_field_values(tname, {k: v for d in new_rows for k, v in d.items()})
    conn = get_db()
    digest = _file_digest(text)
    plan_only = bool(body.get("plan_only"))

    if plan_only or body.get("plan_id"):
        plan = _build_import_plan(tname, "merge", hdr, new_rows, "paste",
                                  "（网页粘贴）", digest, user)
        if plan_only:
            pid = _store_import_plan(plan, user)
            return jsonify({"ok": True, "plan_id": pid,
                            "counts": plan["counts"], "blocking": plan["blocking"],
                            "detail": [{"row": r["row"], "key": r["key"], "action": r["action"],
                                        "fields": r.get("fields") or {},
                                        "errors": r["errors"], "warnings": r["warnings"]}
                                       for r in plan["rows"][:500]],
                            "notices": plan["notices"],
                            "errors_download": "/api/import/plan/%s/errors.csv" % pid})
        plan_id = body.get("plan_id")
        saved = conn.execute("SELECT * FROM import_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not saved or saved["tname"] != tname or saved["source"] != "paste":
            abort(404, "粘贴导入计划不存在（请重新生成预览）")
        plan = _load_import_plan(pid=plan_id, user=user, tname=tname, mode="merge")
        apply_rows = [dict(it["data"]) for it in plan["rows"] if it["action"] in ("add", "update")]
        batch_id = _new_batch("B")
        snap_id = _pre_snapshot(conn, tname, user["username"], "粘贴导入前自动创建", batch_id)
        det_map = _det_map_for(tname, apply_rows)
        n_add, n_upd, n_skip, per_row = _apply_merge(
            conn, tname, apply_rows, user["username"], batch_id=batch_id, det_map=det_map)
        changes = {"_file": "（网页粘贴）", "_plan_id": plan_id,
                   "_file_digest": plan["file_digest"],
                   "_mode": "合并（更新已有+新增）",
                   "_summary": "按计划 %s 粘贴 %d 行：新增 %d、更新 %d" % (plan_id, len(apply_rows), n_add, n_upd)
                               + ("、跳过 %d" % n_skip if n_skip else ""),
                   "_batch": batch_id}
        if unknown:
            changes["_unknown_cols"] = unknown[:20]
        changes["_snapshot_vid"] = snap_id
        log_edit(conn, user["username"], tname, None, None, "import", changes, batch_id=batch_id)
        for rid, k, act, ch in per_row:
            log_edit(conn, user["username"], tname, rid, k, act, ch, batch_id=batch_id)
        invalidate(tname)
        payload = {"ok": True, "plan_id": plan_id, "added": n_add, "updated": n_upd,
                   "skipped": n_skip, "batch": batch_id}
        return _finish_write(conn, payload, _idem_ctx(user, "paste:%s" % tname, {"digest": plan["file_digest"], "plan_id": plan_id}))

    batch_id = _new_batch("B")
    snap_id = _pre_snapshot(conn, tname, user["username"], "粘贴导入前自动创建", batch_id)
    det_map = _det_map_for(tname, new_rows)
    n_add, n_upd, n_skip, per_row = _apply_merge(
        conn, tname, new_rows, user["username"], batch_id=batch_id, det_map=det_map)
    changes = {"_file": "（网页粘贴）", "_mode": "合并（更新已有+新增）",
               "_summary": "粘贴 %d 行：新增 %d、更新 %d" % (len(new_rows), n_add, n_upd)
                           + ("、跳过 %d" % n_skip if n_skip else ""),
               "_batch": batch_id}
    if unknown:
        changes["_unknown_cols"] = unknown[:20]
    changes["_snapshot_vid"] = snap_id
    log_edit(conn, user["username"], tname, None, None, "import", changes, batch_id=batch_id)
    for rid, k, act, ch in per_row:
        log_edit(conn, user["username"], tname, rid, k, act, ch, batch_id=batch_id)
    conn.commit()
    invalidate(tname)
    return jsonify({"ok": True, "added": n_add, "updated": n_upd, "skipped": n_skip, "batch": batch_id})


@app.get("/api/taxonomy-lookup")
def api_taxonomy_lookup():
    """属名 -> 上级分类：先查分类信息表，未命中再查 WSC 属级名录。"""
    current_user()
    g = (request.args.get("genus") or "").strip()
    if not g:
        return jsonify({"found": False})
    res = taxonomy_fill({"Genus": g})
    in_local = any(not r["deleted"] and str(r["data"].get("GENUS") or "").strip().lower() == g.lower()
                   for r in load_rows("taxonomy"))
    if any(v for v in res.values()):
        return jsonify({"found": True, "source": "taxonomy" if in_local else "wsc", "record": res})
    rec = _WSC_LOW.get(g.lower())
    if rec:
        record = {"Order": "Araneae", "Family": rec["f"], "Genus": rec["g"]}
        if rec.get("sf"):
            record["Subfamily"] = rec["sf"]
        return jsonify({"found": True, "source": "wsc", "record": record})
    return jsonify({"found": False})


@app.get("/api/taxon-values")
def api_taxon_values():
    """分类信息表各阶元唯一值（联想数据源）+ 值→整条分类链记录（用于输入后带出）。"""
    current_user()
    vals = {f: [] for f, _ in TAXONOMY_CHAIN}
    lookup = {f: {} for f, _ in TAXONOMY_CHAIN}
    for r in load_rows("taxonomy"):
        if r["deleted"]:
            continue
        d = r["data"]
        for field, src in TAXONOMY_CHAIN:
            v = str(d.get(src) or "").strip()
            if not v:
                continue
            key = v.lower()
            exist = lookup[field].get(key)
            if not exist:
                lookup[field][key] = {f: d.get(s) for f, s in TAXONOMY_CHAIN}
                vals[field].append(v)
            else:
                for f2, s2 in TAXONOMY_CHAIN:
                    if not exist.get(f2) and d.get(s2):
                        exist[f2] = d.get(s2)
    return jsonify({"values": vals, "lookup": lookup})


@app.get("/api/taxonomy-lookup-by")
def api_taxonomy_lookup_by():
    """按某一分类字段值带出其余阶元（输入 Tribe/Clade 后向上/补齐空缺）。"""
    current_user()
    col = (request.args.get("col") or "").strip()
    val = (request.args.get("value") or "").strip()
    valid = {f for f, _ in TAXONOMY_CHAIN}
    if col not in valid or not val:
        return jsonify({"found": False})
    fill = taxonomy_fill_upward({col: val})
    if fill:
        return jsonify({"found": True, "fill": fill})
    return jsonify({"found": False})


@app.get("/api/genus-check")
def api_genus_check():
    """属名核对：是否在 WSC 属级名录或本地分类信息表中；未收录时给相近属建议。"""
    current_user()
    g = (request.args.get("genus") or "").strip()
    if not g:
        return jsonify({"known": True})
    in_wsc = g.lower() in _WSC_LOW
    in_tax = any(not r["deleted"] and str(r["data"].get("GENUS") or "").strip().lower() == g.lower()
                 for r in load_rows("taxonomy"))
    suggestion, sugg_family = "", ""
    if not in_wsc:
        close = difflib.get_close_matches(g.lower(), list(_WSC_LOW.keys()), n=1, cutoff=0.75)
        if close and close[0] != g.lower():
            suggestion = close[0].capitalize()
            sugg_family = _WSC_LOW[close[0]].get("f") or ""
    return jsonify({"known": bool(in_wsc or in_tax), "in_wsc": in_wsc, "in_taxonomy": in_tax,
                    "suggestion": suggestion, "suggestion_family": sugg_family})


@app.get("/api/genus-suggest")
def api_genus_suggest():
    """属名输入预测（基于 WSC 属级名录，前缀优先）。"""
    current_user()
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"items": []})
    starts, contains = [], []
    for low, rec in _WSC_LOW.items():
        if low.startswith(q):
            starts.append(rec)
        elif q in low:
            contains.append(rec)
    starts.sort(key=lambda r: r["g"].lower())
    contains.sort(key=lambda r: r["g"].lower())
    items = (starts + contains)[:25]
    return jsonify({"items": [{"genus": r["g"], "family": r["f"]} for r in items]})


@app.post("/api/geo/normalize")
def api_geo_normalize():
    """对采集地信息表现有数据做一次地名标准化整理（管理员）。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员可执行地名整理")
    conn = get_db()
    n_upd = 0
    field_cnt = {"Country": 0, "Province": 0, "City": 0}
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        d = dict(r["data"])
        changed = geo.normalize_geo(d)
        if changed:
            conn.execute("UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? WHERE id=?",
                         (json.dumps(d, ensure_ascii=False), now(), user["username"], r["id"]))
            for k in changed:
                field_cnt[k] += 1
            n_upd += 1
    log_edit(conn, user["username"], "events", None, None, "update",
             {"_summary": f"地名标准化整理：更新 {n_upd} 行"
                          f"（国家 {field_cnt['Country']}、省份 {field_cnt['Province']}、城市 {field_cnt['City']} 处）"})
    conn.commit()
    invalidate("events")
    return jsonify({"ok": True, "updated": n_upd, "fields": field_cnt})


def group_primary_table(group):
    """类群的主表：该类群下 primary=True 的表；管理员/无类群用全局主表。"""
    if group:
        for t, c in TABLES.items():
            if c.get("primary") and c.get("taxon_group", "spider") == group:
                return t
    return PRIMARY_TABLE


# 各类群主表的列名差异（伪蝎为中文列）
GROUP_COLS = {
    "pseudoscorpiones": {"family": "科", "genus": "属", "pres": "保存条件",
                         "sex": [("雄性数量", "male"), ("雌性数量", "female"), ("幼体数量", "juvenile")]},
}
_GROUP_DEFAULT_COLS = {"family": "Family", "genus": "Genus",
                       "pres": "PreserveCondition(75%, 85%, 95%, 100%)",
                       "sex": [("AdultMale", "male"), ("AdultFemale", "female"),
                               ("Subadult", "subadult"), ("Juvenile", "juvenile")]}


def group_cols(PT):
    return GROUP_COLS.get(PT, _GROUP_DEFAULT_COLS)


@app.get("/api/dashboard")
def api_dashboard():
    """首页仪表盘：一次返回全部关键统计 + 变更签名（前端轮询实时刷新）。"""
    me = current_user()
    perms = me.get("perms") or {}
    # 可见性一律以统一授权服务 visible_tables 为依据（逐表 rw/r/none），不按 taxon_group 推导
    vis = visible_tables(me)
    PT = next((t for t, c in vis.items() if c.get("primary")), None)
    if PT is None:
        # 无任何可见主表：返回空状态，绝不回退默认主表（防泄露）
        tables_stat = [{"t": t, "label": TD.display_label(t),
                        "total": conn_total(t)} for t, c in vis.items()]
        return jsonify({"primary": None, "empty": True, "total": 0,
                        "events_total": events_visible_total(me), "taxonomy_total": 0,
                        "genera": 0, "families": 0, "type_total": 0, "loaned": 0, "deleted": 0,
                        "versions": 0, "tables": tables_stat,
                        "health": {"dup_specimens": 0, "dup_events": 0, "missing_events": 0, "total": 0},
                        "top_families": [], "top_provinces": [], "provinces_all": {}, "types": [],
                        "preserve": [], "sex_stats": [], "pres_detail": [], "box_stats": [],
                        "proj_dist": [], "uce_stats": None,
                        "coverage": {"wsc_salt_gen": 0, "wsc_salt_spc": 0, "our_salt_gen": 0,
                                     "our_salt_gen_pct": 0, "our_spc_total": 0, "our_spc_final": 0,
                                     "our_spc_tmp": 0, "our_spc_wsc": 0, "our_spc_new": 0,
                                     "our_salt_spc_pct": 0, "uce_salt_spc_pct": 0},
                        "feed": _visible_feed(me), "years": []})
    visible = vis
    eidx = linked_eidx(PT)
    GC = group_cols(PT)
    spider_group = me["admin"] or perms.get("specimens") in ("r", "rw")
    fams, provs, types, years, preserve = Counter(), Counter(), Counter(), Counter(), Counter()
    genera = set()
    total = loaned = 0
    _od_rows = []
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        total += 1
        d = r["data"]
        if d.get(GC["genus"]):
            genera.add(d[GC["genus"]])
        if d.get(GC["family"]):
            fams[d[GC["family"]]] += 1
        t = d.get("TypeSpecimens") or d.get("IsTypeSpecimens")
        if t and str(t).strip().lower() != "no":
            key = str(t).strip()
            low = key.lower()
            if low == "holotyoe":  # 显示层归并常见笔误
                key, low = "Holotype", "holotype"
            if low not in types:
                types[low] = {"name": key, "value": 0}
            types[low]["value"] += 1
        p = d.get(GC["pres"])
        if p is not None and str(p).strip() != "":
            try:
                # 先归一再分桶（v4.34）：存量数据有早期 Excel 导入的小数写法（0.95/1/0.8），
                # 直接 float 分桶会把图表标签画成「0.95%」「1%」；归一后 0.95→95、1→100，
                # 带中文描述的原文（95%（死后泡））归一保留原文 → float 失败 → 进「其他」
                fv = float(normalize_alcohol(p))
                preserve[str(int(fv)) if fv == int(fv) else str(fv)] += 1
            except (TypeError, ValueError):
                preserve["其他"] += 1
        if (d.get("LoanSpecimenNumber") or d.get("LoanSpecimenFrom")
                        or d.get("LoanSpecimenTo") or d.get("SentDate")) and not d.get("ReturnDate"):
            loaned += 1
            _od_rows.append(r)
        ce = d.get(TABLES[PT].get("linked_key") or "CollectionEventNumber")
        src = eidx.get(ce) if ce else None
        if src:
            if src.get("Province"):
                provs[str(src["Province"])] += 1
            y = src.get("YYYY1")
            if y is not None and str(y)[:4].isdigit():
                years[int(str(y)[:4])] += 1
    conn = get_db()
    def box_usage_map_detail():
        all_boxes = conn.execute("SELECT * FROM boxes ORDER BY num_start").fetchall()
        usage = {b["id"]: 0 for b in all_boxes}
        for r in load_rows(PRIMARY_TABLE):
            if r["deleted"]:
                continue
            m = re.match(r"^MHBU-ARA-(\d{8})$", str(r["data"].get("MHBU #") or ""))
            if not m:
                continue
            num = int(m.group(1))
            for b in all_boxes:
                if b["num_start"] <= num <= b["num_end"]:
                    usage[b["id"]] += 1
                    break
        return usage, all_boxes

    def cnt(sql, *args):
        return conn.execute(sql, args).fetchone()[0]
    events_total = (cnt("SELECT COUNT(*) FROM records WHERE tname='events' AND deleted=0")
                    if "events" in vis else 0)
    taxonomy_total = (cnt("SELECT COUNT(*) FROM records WHERE tname='taxonomy' AND deleted=0")
                      if "taxonomy" in vis else 0)
    del_spec = cnt("SELECT COUNT(*) FROM records WHERE tname=? AND deleted=1", PT)
    ver_count = cnt("SELECT COUNT(*) FROM versions WHERE tname=?", PT)
    max_edit = cnt("SELECT COALESCE(MAX(id),0) FROM edits")
    last_edit = conn.execute(
        "SELECT ts, user, action, tname, rkey FROM edits WHERE action NOT IN ('version') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    feed_rows = conn.execute(
        "SELECT ts, user, action, tname, rkey, changes FROM edits ORDER BY id DESC LIMIT 60"
    ).fetchall()
    # 动态流只展示当前用户可见表的操作（管理员全量）
    if not me["admin"]:
        feed_rows = [e for e in feed_rows
                     if not e["tname"] or e["tname"] in vis][:9]
    feed = []
    for e in feed_rows:
        try:
            ch = json.loads(e["changes"] or "{}")
        except (ValueError, TypeError):
            ch = {}
        text = ch.get("_summary") or ""
        if e["action"] == "update" and not ch.get("_summary"):
            parts = [f"{k}: {str(v[0] if v[0] is not None else '空')} → {str(v[1] if v[1] is not None else '空')}"
                     for k, v in list(ch.items())[:3] if not k.startswith("_")][:2]
            text = "；".join(parts)
        feed.append({"ts": e["ts"], "user": e["user"], "action": e["action"],
                     "rkey": e["rkey"], "text": text[:120]})
    # 数据健康
    dup_spec = dup_evt = missing = 0
    seen = {}
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        k = r["data"].get(TABLES[PT]["key"])
        if k:
            seen[k] = seen.get(k, 0) + 1
    dup_spec = sum(1 for v in seen.values() if v > 1)
    seen = {}
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        k = r["data"].get("CollectionEventNumber")
        if k:
            seen[k] = seen.get(k, 0) + 1
    dup_evt = sum(1 for v in seen.values() if v > 1)
    ekeys = set(eidx)
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        ce = r["data"].get(TABLES[PT].get("linked_key", "CollectionEventNumber"))
        if ce and ce not in ekeys:
            missing += 1
    # 性别/虫态精细统计
    _sex = {k: 0 for _, k in GC["sex"]}
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        d = r["data"]
        for f, k in GC["sex"]:
            try:
                _sex[k] += int(float(d.get(f) or 0))
            except (TypeError, ValueError):
                pass
    sex_stats = [{"key": k, "value": _sex[k]} for _, k in GC["sex"]]
    # 保存浓度精细分布
    pres_detail = sorted(preserve.items(), key=lambda x: -x[1])[:8]
    # WSC 全球覆盖度：我们的跳蛛属/种 vs 全球
    wsc_gen_path = os.path.join(APP_DIR, "wsc_genera.json")
    wsc_spc_path = os.path.join(APP_DIR, "wsc_species.json")
    wsc_salt_gen = wsc_salt_spc = wsc_all_gen = wsc_all_spc = 0
    try:
        all_gen = json.load(open(wsc_gen_path, encoding="utf-8"))
        wsc_all_gen = len(all_gen)
        salt_gen_names = {x["g"].lower() for x in all_gen if x.get("f") == "Salticidae"}
        wsc_salt_gen = len(salt_gen_names)
    except (OSError, ValueError):
        salt_gen_names = set()
    try:
        all_spc = json.load(open(wsc_spc_path, encoding="utf-8"))
        wsc_all_spc = len({x["acc"] for x in all_spc})
        salt_spc_names = {x["acc"].lower() for x in all_spc if x.get("f") == "Salticidae" and not x.get("syn")}
        wsc_salt_spc = len(salt_spc_names)
    except (OSError, ValueError):
        salt_spc_names = set()
    # 我们的跳蛛标本覆盖（物种计数口径见下）
    our_salt_gen = set()
    our_fin = set()   # 最终鉴定种
    our_tmp = set()   # 暂定名种（仅统计最终鉴定名为空的行，避免同种重复计数）
    for r in load_rows(PRIMARY_TABLE):
        if r["deleted"]:
            continue
        d = r["data"]
        if d.get("Family") == "Salticidae" and d.get("Genus"):
            our_salt_gen.add(d["Genus"].lower())
        fin = _valid_species_name(d.get("FinalSpeciesName"))
        if fin:
            our_fin.add(fin.lower())
        elif not (d.get("FinalSpeciesName") or "").strip():
            tmp = _valid_species_name(d.get("TemporarySpeciesName"))
            if tmp:
                our_tmp.add(tmp.lower())
    # （WSC 依赖的计算统一在名录加载之后执行）
    uce_names_raw = set()
    # UCE 物种名原始收集（跳蛛过滤在 WSC 名单加载之后）
    for r in load_rows("uce"):
        if r["deleted"]:
            continue
        sn = _valid_species_name(r["data"].get("Species name"))
        if sn:
            uce_names_raw.add(sn.lower())
    # 覆盖度
    # WSC 名单已加载：统一做跳蛛过滤与覆盖度计算
    our_tmp = {n for n in our_tmp if n.split()[0].lower() in salt_gen_names}
    our_salt_spc = our_fin | our_tmp                    # 馆藏跳蛛物种总数
    our_salt_spc_wsc = our_salt_spc & salt_spc_names          # 其中 WSC 已收录
    our_salt_spc_new = our_salt_spc - salt_spc_names          # 其中新种/未收录
    uce_salt_spc = {n for n in uce_names_raw if n.split()[0].lower() in salt_gen_names}
    our_salt_gen_cov = round(len(our_salt_gen & salt_gen_names) / wsc_salt_gen * 100, 1) if wsc_salt_gen else 0
    our_salt_spc_cov = round(len(our_salt_spc_wsc) / wsc_salt_spc * 100, 1) if wsc_salt_spc else 0
    uce_salt_spc_cov = round(len(uce_salt_spc & salt_spc_names) / wsc_salt_spc * 100, 1) if wsc_salt_spc else 0
    # Box 占用
    box_stats = []
    try:
        usage, all_boxes = box_usage_map_detail()
        for b in all_boxes:
            box_total = b["num_end"] - b["num_start"] + 1
            used = usage.get(b["id"], 0)
            box_stats.append({"name": b["name"], "used": used, "total": box_total,
                              "start": b["num_start"], "end": b["num_end"]})
    except Exception:
        pass
    # Projects 分布（按类群主表；伪蝎表无 Projects 列则自然为空）
    proj = Counter()
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        v = str(r["data"].get("Projects") or "").strip()
        if v:
            proj[v] += 1
    # UCE 文库统计
    uce_total = conn.execute("SELECT COUNT(*) FROM records WHERE tname='uce' AND deleted=0").fetchone()[0]
    uce_fams = Counter()
    uce_sp = set()
    uce_yr = Counter()
    for r in load_rows("uce"):
        if r["deleted"]:
            continue
        d = r["data"]
        if d.get("Family"):
            uce_fams[d["Family"]] += 1
        sn = str(d.get("Species name") or "").strip()
        if sn:
            uce_sp.add(sn)
        y = str(d.get("Send date") or "")[:4]
        if y.isdigit():
            uce_yr[y] += 1
    uce_stats = {
        "total": uce_total,
        "species": len(uce_sp),
        "families": [{"name": k, "value": v} for k, v in uce_fams.most_common(8)],
        "years": [{"year": y, "value": v} for y, v in sorted(uce_yr.items())],
    }
    ty = sorted(types.values(), key=lambda x: -x["value"])[:8]
    yr_sorted = sorted(years.items())
    tables_stat = [{"t": t, "label": TD.display_label(t),
                    "total": conn.execute("SELECT COUNT(*) FROM records WHERE tname=? AND deleted=0", (t,)).fetchone()[0]}
                   for t, c in visible.items()]
    return jsonify({
        "total": total, "events_total": events_total, "taxonomy_total": taxonomy_total,
        "genera": len(genera), "families": len(fams),
    "type_total": sum(v["value"] for v in types.values()),
        "loaned": loaned, "deleted": del_spec, "versions": ver_count,
        "tables": tables_stat,
        "health": {"dup_specimens": dup_spec, "dup_events": dup_evt, "missing_events": missing,
                   "total": dup_spec + dup_evt + missing},
        "top_families": [{"name": k, "value": v} for k, v in fams.most_common(10)],
        "top_provinces": [{"name": k, "value": v} for k, v in provs.most_common(10)],
        "provinces_all": {k: v for k, v in provs.items()},
        "types": ty,
        "preserve": [{"name": k, "value": v} for k, v in
                     sorted(preserve.items(), key=lambda x: -x[1])[:6]],
        "sex_stats": sex_stats,
        "pres_detail": [{"name": k, "value": v} for k, v in pres_detail],
        "box_stats": box_stats if spider_group else [],
        "proj_dist": [{"name": k, "value": v} for k, v in proj.most_common(8)],
        "uce_stats": (uce_stats if ("uce" in vis and spider_group) else None),
        "coverage": {
            "wsc_salt_gen": wsc_salt_gen,
            "wsc_salt_spc": wsc_salt_spc,
            "our_salt_gen": len(our_salt_gen),
            "our_salt_gen_pct": our_salt_gen_cov,
            # 馆藏跳蛛物种：已鉴定 + 暂定名（去重）
            "our_spc_total": len(our_salt_spc),
            "our_spc_final": len(our_fin),
            "our_spc_tmp": len(our_tmp - our_fin),
            "our_spc_wsc": len(our_salt_spc_wsc),
            "our_spc_new": len(our_salt_spc_new),
            "our_spc_pct": our_salt_spc_cov,
            "uce_salt_spc": len(uce_salt_spc),
            "uce_salt_spc_wsc": len(uce_salt_spc & salt_spc_names),
            "uce_salt_spc_pct": uce_salt_spc_cov,
        } if spider_group else None,
        "years": [{"year": y, "value": v} for y, v in yr_sorted],
        "last_edit": dict(last_edit) if last_edit else None,
        "feed": feed,
        "sig": f"{max_edit}:{total}:{events_total}",
    })


# ---------------------------------------------------------------- WSC 同步与种名建议
WSC_API_KEY = os.environ.get("WSC_API_KEY", "e69a84b435a8cbdbb8e4c19fc2c9e4c561X1kQd9rr")
WSC_BASE = "https://wsc.nmbe.ch/api"
LAST_SYNC_FILE = os.path.join(APP_DIR, "wsc_last_sync.txt")


@app.get("/api/species-check")
def api_species_check():
    """核对暂定名是否为 WSC 收录名称（不含 cf./aff. 前缀与 sp./nov. 等开放命名）。"""
    current_user()
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"checked": False})
    import re as _re
    core = _re.sub(r"^(cf\.|aff\.)\s*", "", name, flags=_re.I).strip()
    low = core.lower()
    rec = _WSC_SPEC_LOW.get(low)
    if rec:
        accepted = rec["acc"] if rec["syn"] else rec["n"]
        return jsonify({"checked": True, "in_wsc": True, "accepted": accepted})
    online = gbif_match_species(core)
    if online:
        return jsonify({"checked": True, "in_wsc": True, "accepted": online[0]})
    return jsonify({"checked": True, "in_wsc": False})


@app.get("/api/species-suggest")
def api_species_suggest():
    """FinalSpeciesName 输入建议：离线种名录（GBIF 镜像）前缀优先。"""
    current_user()
    q = (request.args.get("q") or "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"items": []})
    starts, contains = [], []
    for rec in _WSC_SPEC_LIST:
        low = rec["n"].lower()
        if low.startswith(q):
            starts.append(rec)
        elif q in low:
            contains.append(rec)
    items = (starts + contains)[:25]
    return jsonify({"items": [{"name": x["acc"] if x["syn"] else x["n"],
                               "status": "异名→接受名" if x["syn"] else "",
                               "family": x["f"]} for x in items]})


PENDING_SYNC_FILE = os.path.join(APP_DIR, "wsc_pending_sync.json")


def _read_pending_sync():
    try:
        with open(PENDING_SYNC_FILE, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) and j.get("name_map") else None
    except (OSError, ValueError):
        return None


@app.post("/api/wsc/sync_species")
def api_wsc_sync_species():
    """管理员：按 WSC 官方 updates 流生成 FinalSpeciesName 校准差异（预览，不写库）。

    拉取上次同步以来的变更 LSID → 取详情 → 生成 {行, 现名, 接受名, 受保护状态} 待审阅清单；
    管理员逐条或成批接受（POST /api/wsc/sync_species/apply）后才写库并追加鉴定历史
    （source=calibration，可随批次撤销）。拉取成功后差异窗口存入 wsc_pending_sync.json；
    LAST_SYNC 仅在接受成功后推进——预览/接受失败保留旧名录版本及其时间。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员可执行 WSC 校准")
    import requests as _rq
    last = ""
    if os.path.exists(LAST_SYNC_FILE):
        try:
            last = open(LAST_SYNC_FILE, encoding="utf-8").read().strip()
        except OSError:
            last = ""
    if not last:
        last = "1750-01-01"  # 全量基线
    s = _rq.Session()
    s.headers["Accept"] = "application/json"
    lsids, url = [], f"{WSC_BASE}/updates?type=species&date={last}&apiKey={WSC_API_KEY}"
    while url:
        r = s.get(url, timeout=60)
        if r.status_code != 200:
            abort(502, f"WSC updates 接口返回 {r.status_code}")
        j = r.json()
        lsids.extend(j.get("updates", []))
        url = (j.get("_links") or {}).get("next")
    # 只解析与库中 FinalSpeciesName 相关的 LSID：取详情
    conn = get_db()
    finals = set()
    for r2 in load_rows("specimens"):
        if r2["deleted"]:
            continue
        v = str(r2["data"].get("FinalSpeciesName") or "").strip()
        if v:
            finals.add(v.lower())
    changed = 0
    name_map = {}
    for lsid in lsids[:2000]:  # 单次上限，避免超长阻塞
        rr = s.get(f"{WSC_BASE}/lsid/{lsid}", params={"apiKey": WSC_API_KEY}, timeout=30)
        if rr.status_code != 200:
            continue
        tax = rr.json().get("taxon", {})
        sp = tax.get("species") or ""
        status = tax.get("status") or ""
        changed += 1
        if sp and sp.lower() in finals and status and status.lower() != "valid":
            vt = (tax.get("validTaxon") or {}).get("_href")
            if vt:
                vr = s.get(vt, params={"apiKey": WSC_API_KEY}, timeout=30)
                if vr.status_code == 200:
                    vt_tax = vr.json().get("taxon", {})
                    valid_name = (vt_tax.get("genus") or "") + " " + (vt_tax.get("species") or "")
                    valid_name = valid_name.strip()
                    if valid_name:
                        name_map[sp.lower()] = {"to": valid_name, "lsid": lsid}
    # 生成待审阅差异清单（不改库）
    preview = []
    for r2 in load_rows("specimens"):
        if r2["deleted"]:
            continue
        v = str(r2["data"].get("FinalSpeciesName") or "").strip()
        mp = name_map.get(v.lower())
        if mp:
            preview.append({
                "rid": r2["id"], "key": r2["data"].get("MHBU #"),
                "from": v, "to": mp["to"],
                "protected": _det_protected(conn, "specimens", r2["id"], r2["data"]),
            })
    fetched_at = now()
    with open(PENDING_SYNC_FILE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": fetched_at, "window_from": last,
                   "changed_lsids": changed, "name_map": name_map}, f, ensure_ascii=False)
    return jsonify({
        "ok": True, "preview_only": True,
        "changed_lsids": changed, "pending": len(preview),
        "fetched_at": fetched_at,
        "preview": preview[:500],
        "hint": "差异未写库：请核对后调用 /api/wsc/sync_species/apply 逐条或成批接受",
    })


@app.post("/api/wsc/sync_species/apply")
def api_wsc_sync_apply():
    """管理员：接受 WSC 校准差异（逐条/成批）。

    updates 为空且 all_pending=true 时接受全部待审阅差异；updates 可只给部分行（逐条接受）。
    受保护行（人工锁定 DetLocked / 最近鉴定已确认或有依据）自动跳过并列出原因。
    每行：更名 + 属联动 + 追加鉴定历史（source=calibration、checklist_status=synonym），
    单事务提交、可随批次撤销；全部成功后推进 LAST_SYNC（失败保留旧版及其时间）。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员可执行 WSC 校准")
    body = request.get_json(silent=True) or {}
    conn0 = get_db()
    ctx = _idem_ctx(user, "wsc_sync_apply", body)
    hit = _idem_lookup(conn0, ctx)
    if hit:
        return hit
    pending = _read_pending_sync()
    req_updates = body.get("updates")
    if (not isinstance(req_updates, list) or not req_updates) and body.get("all_pending"):
        if not pending:
            abort(409, "没有可接受的待审阅差异（请先执行预览 /api/wsc/sync_species）")
        name_map = pending.get("name_map") or {}
        req_updates = []
        for r2 in load_rows("specimens"):
            if r2["deleted"]:
                continue
            v = str(r2["data"].get("FinalSpeciesName") or "").strip()
            mp = name_map.get(v.lower())
            if mp:
                req_updates.append({"rid": r2["id"], "from": v, "to": mp["to"]})
    if not isinstance(req_updates, list) or not req_updates:
        abort(400, "未提供 updates（[{rid, from, to}]）或 all_pending")
    conn = conn0
    batch_id = _new_batch("W")
    applied, skipped = [], []
    ck_ver = (pending or {}).get("fetched_at") or _rules_versions().get("wsc_sync") or None
    try:
        for u in req_updates:
            try:
                rid = int(u.get("rid"))
            except (TypeError, ValueError):
                skipped.append({"rid": u.get("rid"), "reason": "rid 无效"})
                continue
            frm = str(u.get("from") or "").strip()
            to = str(u.get("to") or "").strip()
            if not frm or not to or frm.lower() == to.lower():
                skipped.append({"rid": rid, "reason": "from/to 缺失或相同"})
                continue
            row = conn.execute("SELECT * FROM records WHERE id=? AND tname='specimens'", (rid,)).fetchone()
            if not row or row["deleted"]:
                skipped.append({"rid": rid, "reason": "记录不存在或已删除"})
                continue
            old = json.loads(row["data"])
            cur_v = str(old.get("FinalSpeciesName") or "").strip()
            if cur_v.lower() != frm.lower():
                skipped.append({"rid": rid, "reason": f"现名「{cur_v or '（空）'}」与差异基准「{frm}」不符"
                                                      "（可能已被其他修改处理）"})
                continue
            prot = _det_protected(conn, "specimens", rid, old)
            if prot:
                skipped.append({"rid": rid, "key": old.get("MHBU #"), "reason": f"受保护：{prot}"})
                continue
            latest = _latest_determination(conn, "specimens", rid)
            det = {"name_raw": frm, "name_accepted": to,
                   "genus": to.split()[0] if to.split() else None, "family": None,
                   "det_detail": "WSC 校准：原名称已非接受名（异名/无效名），更换为当前接受名",
                   "determiner": _nz(old.get("DetBy")) or (_nz(latest["determiner"]) if latest else None),
                   "det_date": _nz(old.get("DetDate")) or (_nz(latest["det_date"]) if latest else None),
                   "evidence": None, "confirmed_by": None,
                   "checklist_ver": ck_ver, "checklist_status": "synonym"}
            det_status = (latest["det_status"] if latest and latest["det_status"] in _DET_STATUS_VALUES
                          else (old.get("DetStatus") if old.get("DetStatus") in _DET_STATUS_VALUES
                                else "暂定"))
            new = dict(old)
            new["FinalSpeciesName"] = to
            new["Genus"] = to.split()[0] if to.split() else new.get("Genus")
            for k, v in taxonomy_fill(new, "specimens").items():
                if not same(new.get(k), v):
                    new[k] = v
            cur = conn.execute(
                "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
                "WHERE id=? AND row_version=?",
                (json.dumps(new, ensure_ascii=False), now(), user["username"], rid, row["row_version"]))
            if cur.rowcount != 1:
                raise WriteConflict({"error": f"行 {rid} 在接受时被并发修改", "rid": rid})
            det_rec = record_determination(conn, "specimens", rid, det, source="calibration",
                                           user=user["username"], batch_id=batch_id,
                                           det_status=det_status)
            log_edit(conn, user["username"], "specimens", rid, new.get("MHBU #") or None,
                     "update",
                     {"_auto": "WSC 校准（人工接受）",
                      "FinalSpeciesName": [frm, to],
                      "Genus": [old.get("Genus"), new.get("Genus")],
                      "_det": det_rec, "_checklist_ver": ck_ver},
                     batch_id=batch_id, actor_type="human",
                     reason="管理员接受 WSC updates 流校准：原名称已非接受名")
            applied.append({"rid": rid, "from": frm, "to": to})
        if not applied:
            _tx_rollback(conn)
            return jsonify({"ok": False, "applied": 0, "skipped": skipped,
                            "error": "没有可接受的行（全部缺失/不符或受保护）"}), 409
        log_edit(conn, user["username"], "specimens", None, None, "update",
                 {"_summary": f"WSC 校准接受：更新 {len(applied)} 行 FinalSpeciesName"
                              f"（跳过 {len(skipped)} 行）", "_batch": batch_id},
                 batch_id=batch_id)
        payload = {"ok": True, "applied": len(applied), "applied_rows": applied,
                   "skipped": skipped, "batch": batch_id, "checklist_ver": ck_ver}
    except WriteConflict as c:
        _tx_rollback(conn)
        return jsonify({"ok": False, "error": c.payload.get("error"),
                        "conflict": c.payload}), 409
    except sqlite3.OperationalError as e:
        _tx_rollback(conn)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn)
        raise
    invalidate("specimens")
    resp = _finish_write(conn, payload, ctx)
    if resp.status_code == 200:
        # 接受成功才推进同步窗口：失败保留旧名录版本及其时间
        try:
            open(LAST_SYNC_FILE, "w").write(now()[:10])
        except OSError:
            pass
    return resp


# ---------------------------------------------------------------- 级联下拉 / 体检 / 统计
@app.get("/api/cascade/<tname>")
def api_cascade(tname):
    """分类级联树 Order→Family→Subfamily→Genus：来源 = 分类信息表 ∪ 本表实际取值。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    tree = {}

    def add(o, f, sf, g):
        o, f, sf = o or "", f or "", sf or ""
        fams = tree.setdefault(o, {}).setdefault(f, {}).setdefault(sf, set())
        if g:
            fams.add(g)

    if TABLES[tname].get("genus_lookup"):
        for r in load_rows(tname):
            if r["deleted"]:
                continue
            d = r["data"]
            add(d.get("Order"), d.get("Family"), d.get("Subfamily"), d.get("Genus"))
    src = TABLES[tname].get("cascade_from")
    if src:
        for r in load_rows(src):
            if r["deleted"]:
                continue
            d = r["data"]
            add(d.get("Order"), d.get("Family"), d.get("SUBFAMILY"), d.get("GENUS"))
    return jsonify({"orders": sorted(tree), "tree": {
        o: {f: {sf: sorted(gs) for sf, gs in fams.items()} for f, fams in fams.items()}
        for o, fams in tree.items()}})



# ---------------------------------------------------------------- 材料清单 / 事件联想 / 借出超期
@app.get("/api/material-examined")
def api_material_examined():
    """按分类学论文 Material examined 惯例聚合标本，返回可直接粘贴的文本。"""
    me = current_user()
    ensure_visible(me, "specimens")
    eidx = linked_eidx("specimens")
    raw = request.args.get("filters") or ""
    rows = [r for r in load_rows("specimens") if not r["deleted"]]
    if raw:
        try:
            filters = json.loads(raw)
        except ValueError:
            filters = None
        if isinstance(filters, dict) and filters:
            disp = []
            for r in rows:
                d, _ = display_row("specimens", r, eidx)
                disp.append({"id": r["id"], "data": d})
            keep = {x["id"] for x in _apply_col_filters(disp, filters)}
            rows = [r for r in rows if r["id"] in keep]
    rids_raw = request.args.get("rids") or ""
    if rids_raw:
        keep2 = {int(x) for x in rids_raw.split(",") if x.strip().isdigit()}
        if keep2:
            rows = [r for r in rows if r["id"] in keep2]

    def spname(d):
        n = (d.get("FinalSpeciesName") or "").strip()
        if not n:
            n = (d.get("TemporarySpeciesName") or "").strip()
        return n or "Unidentified"

    def src_of(d):
        ce = d.get("CollectionEventNumber")
        return eidx.get(ce) if ce else None

    def geo_bits(d):
        s = src_of(d)
        if not s:
            return []
        bits = []
        for f in ("Country", "Province", "City", "County", "Locality"):
            v = s.get(f)
            if v:
                bits.append(str(v).strip())
        return bits

    def date_bits(d):
        s = src_of(d)
        if not s:
            return []
        y, m, dd = s.get("YYYY1"), s.get("MM1"), s.get("DD1")
        if not any((y, m, dd)):
            y, m, dd = s.get("YYYY2"), s.get("MM2"), s.get("DD2")
        parts = []
        try:
            if y:
                parts.append(str(int(float(y))))
            if m:
                parts.append("%02d" % int(float(m)))
            if dd:
                parts.append("%02d" % int(float(dd)))
        except (TypeError, ValueError):
            return []
        return [".".join(parts)] if parts else []

    def collector_bits(d):
        s = src_of(d)
        if s and s.get("Collector"):
            return [str(s["Collector"]).strip()]
        return []

    def counts(d):
        c = []
        for f, lbl in (("AdultMale", "♂"), ("AdultFemale", "♀"),
                       ("Subadult", "subad."), ("Juvenile", "juv.")):
            v = d.get(f)
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                c.append(("%d%s" % (v, lbl)) if v == int(v) else ("%s%s" % (v, lbl)))
        return ", ".join(c)

    groups = {}
    for r in rows:
        d = r["data"]
        name = spname(d)
        g = groups.setdefault(name, {"rows": [], "genus": (d.get("Genus") or "").strip()})
        g["rows"].append(r)

    def sortkey(name):
        g = groups[name]
        return (g["genus"] or "zzz", name.lower())

    lines = []
    for name in sorted(groups, key=sortkey):
        g = groups[name]
        lines.append(name)
        for r in sorted(g["rows"], key=lambda x: str(x["data"].get("MHBU #") or "")):
            d = r["data"]
            seg = []
            c = counts(d)
            if c:
                seg.append("(" + c + ")")
            seg += geo_bits(d)
            db = date_bits(d)
            cb = collector_bits(d)
            if cb:
                seg.append(", ".join(cb) + ((", " + db[0]) if db else ""))
            elif db:
                seg.append(db[0])
            if seg:
                lines.append("  " + ", ".join(x for x in seg if x))
                kv = (d.get("MHBU #") or "").strip()
                if kv:
                    lines[-1] += " (" + kv + ")"
    return jsonify({"count": len(rows), "species": len(groups), "text": "\n".join(lines)})


@app.get("/api/events-suggest")
def api_events_suggest():
    """采集事件号联想：前缀匹配，返回编号 + 地点日期摘要。"""
    me = current_user()
    ensure_visible(me, "events")
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"items": []})
    out = []
    for r in load_rows("events"):
        if r["deleted"]:
            continue
        k = str(r["data"].get("CollectionEventNumber") or "")
        if not k or not k.lower().startswith(q):
            continue
        d = r["data"]
        bits = [str(d.get(f)) for f in ("Province", "City", "Locality", "Collector", "YYYY1") if d.get(f)]
        out.append({"key": k, "summary": " · ".join(bits[:4])[:60]})
        if len(out) >= 15:
            break
    return jsonify({"items": out})


LOAN_DATE_PAT = re.compile(r"(\d{4})\s*[.\-/年]\s*(\d{1,2})?")


def _loan_overdue_items(rows):
    """借出文本中提取年月，超过 12 个月未归还的行。"""
    import datetime as _dt
    today = _dt.date.today()
    out = []
    for r in rows:
        if r["deleted"]:
            continue
        d = r["data"]
        if not ((d.get("LoanSpecimenNumber") or d.get("LoanSpecimenFrom")
                 or d.get("LoanSpecimenTo") or d.get("SentDate")) and not d.get("ReturnDate")):
            continue
        text = " ".join(str(d.get(k) or "") for k in ("LoanSpecimenFrom", "LoanSpecimenTo", "SentDate"))
        m = LOAN_DATE_PAT.search(text)
        if not m:
            continue
        try:
            y = int(m.group(1))
            mo = int(m.group(2) or 6)
        except ValueError:
            continue
        if not (1990 <= y <= today.year and 1 <= mo <= 12):
            continue
        borrowed = _dt.date(y, mo, 1)
        if (today - borrowed).days > 365:
            out.append({"id": r["id"], "key": d.get("MHBU #"),
                        "text": str(d.get("LoanSpecimenTo") or d.get("LoanSpecimenFrom"))[:40],
                        "since": borrowed.strftime("%Y-%m")})
    return out


# ---------------------------------------------------------------- 数据审查与一键纠错
COUNT_FIELDS = ("AdultMale", "AdultFemale", "Subadult", "Juvenile")
PRES_COL = "PreserveCondition(75%, 85%, 95%, 100%)"


def _cap_species(n):
    w = n.strip().split()
    if not w:
        return n
    w[0] = w[0][0].upper() + w[0][1:]
    return " ".join(w)


def _scan_fixable():
    """扫描四类可修复问题：属首小写 / 属≠种名属首 / 数量格式 / 浓度夹带文字。"""
    name_case, genus_mm, count_bad, pres_bad = [], [], [], []
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        d = r["data"]
        rid = r["id"]
        key = d.get("MHBU #")
        fs = str(d.get("FinalSpeciesName") or "").strip()
        gen = str(d.get("Genus") or "").strip()
        if fs and fs[0].islower():
            name_case.append({"id": rid, "key": key, "value": fs,
                              "suggest": _cap_species(fs)})
        elif fs and gen and not fs.lower().startswith(gen.lower()):
            genus_mm.append({"id": rid, "key": key, "value": gen,
                             "suggest": _cap_species(fs).split()[0]})
        bad = {f: d.get(f) for f in COUNT_FIELDS
               if d.get(f) is not None and str(d.get(f)).strip()
               and (not str(d.get(f)).strip().replace(".", "").isdigit()
                    or float(d[f]) < 0 or float(d[f]) != int(float(d[f])))}
        if bad:
            count_bad.append({"id": rid, "key": key, "fields": bad,
                              "suggest": {f: int(float(v)) for f, v in bad.items()
                                          if str(v).strip().replace(".", "").isdigit()}})
        pv = d.get(PRES_COL)
        if pv is not None:
            m = re.match(r"^(\d+)\s*%?\s*（(.+?)）\s*$", str(pv))
            if m:
                pres_bad.append({"id": rid, "key": key, "value": str(pv),
                                 "suggest": int(m.group(1)), "note": m.group(2)})
    pos_fill = []
    for r in load_rows("specimens"):
        if r["deleted"]:
            continue
        d = r["data"]
        if str(d.get("Position") or "").strip():
            continue
        pos = _compute_position(d.get("Box # (Start#-End#)"), d.get("MHBU #"))
        if pos:
            pos_fill.append({"id": r["id"], "key": d.get("MHBU #"), "suggest": pos})
    return {"name_case": name_case, "genus_mismatch": genus_mm,
            "count_anomaly": count_bad, "pres_anomaly": pres_bad,
            "pos_fill": pos_fill}


@app.get("/api/fix/scan")
def api_fix_scan():
    current_user()
    ensure_visible(current_user(), "specimens")
    s = _scan_fixable()
    return jsonify({"counts": {k: len(v) for k, v in s.items()}, "items": s})


@app.post("/api/fix/apply")
def api_fix_apply():
    """一键修复（兼容入口）：{fix: name_case|genus_sync|count_cast|pres_extract|pos_fill, rids?}。
    委托统一规则引擎（预览语义沿用旧版：直接应用；单批次、留痕、可撤销）。"""
    user = current_user()
    require_writer(user, "specimens")
    ensure_visible(user, "specimens")
    d = request.get_json(silent=True) or {}
    fix = d.get("fix")
    rule_id = LEGACY_FIX_RULE.get(fix)
    if not rule_id:
        abort(400, "未知修复类型：" + str(fix))
    dedups = None
    if d.get("rids"):
        # 旧行号过滤 → 工单 dedup 集合
        conn0 = get_db()
        ph = ",".join("?" * len(d["rids"]))
        rows = conn0.execute(
            f"SELECT dedup FROM issues WHERE rule_id=? AND rid IN ({ph})",
            [rule_id] + [int(x) for x in d["rids"]]).fetchall()
        dedups = [r["dedup"] for r in rows]
        if not dedups:
            return jsonify({"ok": True, "fixed": 0, "batch": None, "noop": True})
    payload = _cr_fix(user, rule_id, dedups, dry_run=False)
    conn = get_db()
    return _finish_write(conn, payload, _idem_ctx(user, f"legacy_fix:{fix}",
                                                  {"fix": fix, "rids": sorted(d["rids"] or [])}))


@app.get("/api/checkup")
def api_checkup():
    me = current_user()
    vis = visible_tables(me)
    out = {"dups": {}}
    for t, cfg in TABLES.items():
        if t not in vis:
            continue  # 不可见的表完全不进入体检（防泄漏）
        key = cfg["key"]
        if not key or cfg.get("key_not_unique"):
            continue
        seen = {}
        for r in load_rows(t):
            if r["deleted"]:
                continue
            k = r["data"].get(key)
            if k:
                seen.setdefault(k, []).append(r["id"])
        dups = sorted(({"key": k, "ids": ids} for k, ids in seen.items() if len(ids) > 1),
                      key=lambda x: -len(x["ids"]))
        out["dups"][t] = {"key": key, "count": len(dups), "items": dups[:200]}
    if "specimens" in vis:
        ekeys = set(linked_eidx("specimens"))
        miss = []
        for r in load_rows("specimens"):
            if r["deleted"]:
                continue
            ce = r["data"].get("CollectionEventNumber")
            if ce and ce not in ekeys:
                miss.append({"id": r["id"], "key": r["data"].get("MHBU #"), "ce": ce})
        out["missing_events"] = {"count": len(miss), "items": miss[:500]}
    # 疑似填错的国家（不在已知国家列表中，需人工核对）——采集地信息可见时
    if "events" in vis:
        geo_bad = []
        for r in load_rows("events"):
            if r["deleted"]:
                continue
            c = r["data"].get("Country")
            if c and str(c).strip().lower() not in geo.KNOWN_COUNTRIES:
                geo_bad.append({"id": r["id"], "ce": r["data"].get("CollectionEventNumber"),
                                "value": str(c)[:40]})
        out["geo_anomaly"] = {"count": len(geo_bad), "items": geo_bad[:80]}
    # 鉴定待复查：cf. 标记 / 暂定名与最终名属名不一致 / 暂定名未被 WSC 收录
    PT = PRIMARY_TABLE
    pt_key = TABLES[PT]["key"]
    _rq_on = PT in vis
    review = []
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        d = r["data"]
        tmp = str(d.get("TemporarySpeciesName") or "").strip()
        fin = str(d.get("FinalSpeciesName") or "").strip()
        reasons = []
        if tmp.lower().startswith("cf"):
            reasons.append("cf. 鉴定不确定")
        tg, fg = _genus_of(tmp), _genus_of(fin)
        if tmp and fin and tg and fg and tg.lower() != fg.lower():
            reasons.append("暂定名属(" + tg + ")≠最终名属(" + fg + ")")
        if reasons:
            review.append({"id": r["id"], "key": d.get(pt_key),
                           "tmp": tmp[:50], "fin": fin[:50] or "（空）", "reasons": reasons})
        if len(review) >= 400:
            break
    if _rq_on:
        out["review_queue"] = {"count": len(review), "items": review[:200]}
    # 属名未收录：Genus 在 WSC 属级名录与本地分类信息表中都查不到（拼写存疑或未发表属）
    tax_genera = {str(r["data"].get("GENUS") or "").strip().lower()
                  for r in load_rows("taxonomy") if not r["deleted"]}
    g_out = []
    _close_cache = {}
    for t, kcol in ((PT, pt_key), ("mygalomorph", "KYUARA #")):
        if t not in vis:
            continue
        for r in load_rows(t):
            if r["deleted"]:
                continue
            d = r["data"]
            g = str(d.get("Genus") or "").strip()
            if not g or g.lower() in tax_genera or g.lower() in _WSC_LOW:
                continue
            if g.lower() not in _close_cache:
                hit = difflib.get_close_matches(g.lower(), list(_WSC_LOW.keys()), n=1, cutoff=0.75)
                _close_cache[g.lower()] = hit[0] if hit else ""
            sug = ""
            if _close_cache[g.lower()] and _close_cache[g.lower()] != g.lower():
                rec = _WSC_LOW[_close_cache[g.lower()]]
                sug = _close_cache[g.lower()].capitalize() + ("（" + rec["f"] + "）" if rec.get("f") else "")
            g_out.append({"id": r["id"], "tname": t, "key": d.get(kcol),
                          "genus": g[:40], "suggestion": sug[:60]})
            if len(g_out) >= 300:
                break
        if len(g_out) >= 300:
            break
    if _rq_on or "mygalomorph" in vis:
        out["genus_unknown"] = {"count": len(g_out), "items": g_out[:150]}
    # WSC 异名过期：FinalSpeciesName 在 WSC 中已是异名
    syn_out = []
    for r in load_rows(PT):
        if r["deleted"]:
            continue
        n = str(r["data"].get("FinalSpeciesName") or "").strip()
        if not n:
            continue
        s = _WSC_SPEC_LOW.get(n.lower())
        if s and s.get("syn") and s.get("acc") and s["acc"].lower() != n.lower():
            syn_out.append({"id": r["id"], "key": r["data"].get(pt_key),
                            "name": n, "accepted": s["acc"]})
        if len(syn_out) >= 300:
            break
    if _rq_on:
        out["wsc_synonyms"] = {"count": len(syn_out), "items": syn_out[:150]}
    # 经纬度合理性 / 日期完整性（采集地信息）
    bad_geo, bad_date = [], []
    for r in (load_rows("events") if "events" in vis else []):
        if r["deleted"]:
            continue
        d = r["data"]
        for lat_f, lon_f in (("Latitude1", "Longitude1"), ("Latitude2", "Longitude2")):
            lat, lon = d.get(lat_f), d.get(lon_f)
            if lat is None and lon is None:
                continue
            try:
                la, lo = float(lat), float(lon)
            except (TypeError, ValueError):
                bad_geo.append({"id": r["id"], "ce": d.get("CollectionEventNumber"),
                                "field": lat_f, "value": (str(lat) + "," + str(lon))[:36],
                                "why": "非数字"})
                continue
            if not (-90 <= la <= 90) or not (-180 <= lo <= 180):
                bad_geo.append({"id": r["id"], "ce": d.get("CollectionEventNumber"),
                                "field": lat_f, "value": (str(la) + "," + str(lo))[:36],
                                "why": "超出范围"})
        for yf, mf, df, tag in (("YYYY1", "MM1", "DD1", "起"), ("YYYY2", "MM2", "DD2", "止")):
            y, m, dd = d.get(yf), d.get(mf), d.get(df)
            if y and not (m and dd):
                bad_date.append({"id": r["id"], "ce": d.get("CollectionEventNumber"),
                                 "field": yf, "value": (str(y) + "-" + str(m or "??") + "-" + str(dd or "??"))[:24],
                                 "why": tag + "日期缺月或日"})
    out["geo_bounds"] = {"count": len(bad_geo), "items": bad_geo[:100]}
    # 可自动纠错问题
    if "specimens" in vis:
        try:
            out["fixable"] = _scan_fixable()
        except Exception:
            pass
    return jsonify(out)


# ================================================================ 统一规则引擎（checkup / fix / workbench）
# 规则定义统一在 checkup_rules.py（rule_id/版本/严重度/理由/适用表/修复方式）；
# 本节只实现检测器与工单生命周期。不产生综合质量得分：按维度分别展示
# 计数、分母（受检行数）与例外数（暂缓+例外工单）。

CR_ISSUE_STATUSES = ("open", "processing", "resolved", "deferred", "exception")
CR_STATUS_LABEL = {"open": "未处理", "processing": "处理中", "resolved": "已解决",
                   "deferred": "暂缓", "exception": "例外"}


def _cr_dedup(rule_id, f):
    if f.get("dedup_key") is not None:
        return f"{f['tname']}|{rule_id}|key:{f['dedup_key']}"
    return f"{f['tname']}|{rule_id}|rid:{f.get('rid')}"


def _cr_digest(vals):
    return hashlib.sha1(json.dumps(vals, ensure_ascii=False, sort_keys=True, default=str)
                        .encode("utf-8")).hexdigest()[:16]


def _cr_rows(conn, tname):
    return conn.execute("SELECT id, data FROM records WHERE tname=? AND deleted=0",
                        (tname,)).fetchall()


def _cr_detectors(vis):
    """检测器注册表：rule_id → fn() → {"items": [finding], "denominator": 受检行数}。
    finding：{tname, rid, rkey, fields:[相关字段], vals:{字段:扫描时值},
              detail:{before/suggest/原因…}, dedup_key?(键级问题)}。
    只检出调用方可见的表（计数与条目按权限隔离，防泄漏）。"""
    conn = get_db()

    def rows(t):
        return _cr_rows(conn, t)

    def _jd(b):
        return json.loads(b)

    def _keyed_visible(v):
        return {t: c for t, c in v.items() if c.get("key") and not c.get("key_not_unique")}

    def _den(t):
        return conn.execute("SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=0",
                            (t,)).fetchone()["c"]

    dets = {}

    # ---- DUP_EVENT：采集事件号重复（键级） ----
    def dup_event():
        if "events" not in vis:
            return {"items": [], "denominator": 0}
        seen = {}
        for r in rows("events"):
            k = _jd(r["data"]).get("CollectionEventNumber")
            if k:
                seen.setdefault(str(k), []).append(r["id"])
        items = [{"tname": "events", "rid": None, "rkey": k, "dedup_key": k,
                  "fields": ["CollectionEventNumber"], "vals": {"CollectionEventNumber": k},
                  "detail": {"ids": ids}}
                 for k, ids in sorted(seen.items(), key=lambda x: -len(x[1])) if len(ids) > 1]
        return {"items": items, "denominator": _den("events")}
    dets["DUP_EVENT"] = dup_event

    # ---- DUP_KEY：各表主键重复（键级） ----
    def dup_key():
        out = []
        for t, cfg in _keyed_visible(vis).items():
            seen = {}
            for r in rows(t):
                k = _jd(r["data"]).get(cfg["key"])
                if k:
                    seen.setdefault(str(k), []).append(r["id"])
            for k, ids in sorted(seen.items(), key=lambda x: -len(x[1])):
                if len(ids) > 1:
                    out.append({"tname": t, "rid": None, "rkey": k, "dedup_key": f"{t}:{k}",
                                "fields": [cfg["key"]], "vals": {cfg["key"]: k},
                                "detail": {"ids": ids, "key_col": cfg["key"]}})
        return {"items": out, "denominator": sum(_den(t) for t in _keyed_visible(vis))}
    dets["DUP_KEY"] = dup_key

    # ---- REF_MISSING：非空失配 ----
    def ref_missing():
        ekeys = set(linked_eidx("specimens"))
        items = []
        for t, lk in (("specimens", "CollectionEventNumber"), ("mygalomorph", "CollectionEventNumber"),
                      ("dali", "CollectionEventNumber"), ("swu", "CollectionEventNumber"),
                      ("pseudoscorpiones", "采集编号")):
            if t not in vis:
                continue
            for r in rows(t):
                d = _jd(r["data"])
                ce = str(d.get(lk) or "").strip()
                if ce and ce not in ekeys:
                    items.append({"tname": t, "rid": r["id"], "rkey": d.get(TABLES[t]["key"]),
                                  "fields": [lk], "vals": {lk: ce},
                                  "detail": {"ce": ce}})
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu", "pseudoscorpiones") if t in vis)}
    dets["REF_MISSING"] = ref_missing

    # ---- EMPTY_KEY：空业务键 ----
    def empty_key():
        items = []
        for t, cfg in _keyed_visible(vis).items():
            for r in rows(t):
                d = _jd(r["data"])
                if not str(d.get(cfg["key"]) or "").strip():
                    items.append({"tname": t, "rid": r["id"], "rkey": None,
                                  "fields": [cfg["key"]], "vals": {cfg["key"]: None},
                                  "detail": {}})
        return {"items": items, "denominator": sum(_den(t) for t in _keyed_visible(vis))}
    dets["EMPTY_KEY"] = empty_key

    # ---- DEAD_DATE：危险日期（分量越界/起止倒置，field_registry 口径） ----
    def dead_date():
        if "events" not in vis:
            return {"items": [], "denominator": 0}
        items = []
        n = 0
        for r in rows("events"):
            d = _jd(r["data"])
            n += 1
            for tag, (fdd, fmm, fyy) in (("起", FR.DATE_GROUP["start"]), ("止", FR.DATE_GROUP["end"])):
                p = FR.parse_date_parts(d.get(fdd), d.get(fmm), d.get(fyy))
                if p["error"]:
                    items.append({"tname": "events", "rid": r["id"],
                                  "rkey": d.get("CollectionEventNumber"),
                                  "fields": [fdd, fmm, fyy], "vals": {fdd: d.get(fdd), fmm: d.get(fmm), fyy: d.get(fyy)},
                                  "detail": {"why": p["error"], "which": tag}})
            y1, m1, dd1 = d.get("YYYY1"), d.get("MM1"), d.get("DD1")
            y2, m2, dd2 = d.get("YYYY2"), d.get("MM2"), d.get("DD2")
            if all(str(x or "") for x in (y1, m1, dd1, y2, m2, dd2)):
                try:
                    a = date(int(y1), int(m1), int(dd1))
                    b = date(int(y2), int(m2), int(dd2))
                    if a > b:
                        items.append({"tname": "events", "rid": r["id"],
                                      "rkey": d.get("CollectionEventNumber"),
                                      "fields": ["DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2"],
                                      "vals": {"start": f"{y1}-{m1}-{dd1}", "end": f"{y2}-{m2}-{dd2}"},
                                      "detail": {"why": "开始日期晚于结束日期", "which": "起止"}})
                except ValueError:
                    pass  # 分量越界已由上面 DEAD_DATE 分量检查覆盖
        return {"items": items, "denominator": n}
    dets["DEAD_DATE"] = dead_date

    # ---- EDIT_CONFLICT：近期 force 覆盖留痕（阻断级待复核） ----
    def edit_conflict():
        since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        items = []
        n = 0
        for e in conn.execute(
                "SELECT tname, rid, rkey, changes, ts, user, row_version_before, row_version_after "
                "FROM edits WHERE ts>=? ORDER BY id DESC LIMIT 5000", (since,)):
            if not e["tname"] or e["tname"] not in vis:
                continue
            try:
                ch = json.loads(e["changes"] or "{}")
            except (ValueError, TypeError):
                continue
            if "_override" not in ch:
                continue
            n += 1
            dup = f"{e['tname']}|EDIT_CONFLICT|rid:{e['rid']}|{e['ts'][:10]}"
            items.append({"tname": e["tname"], "rid": e["rid"], "rkey": e["rkey"],
                          "fields": [], "vals": {"ts": e["ts"]}, "dedup_key": f"rid:{e['rid']}|{e['ts'][:10]}",
                          "detail": {"by": e["user"], "at": e["ts"],
                                     "overridden_version": (ch.get("_override") or {}).get("overridden_version"),
                                     "reason": (ch.get("_override") or {}).get("reason", "")}})
        return {"items": items, "denominator": n}
    dets["EDIT_CONFLICT"] = edit_conflict

    # ---- 可修复四类 + 位置（复用 _scan_fixable 的口径） ----
    def fixable_rule(key):
        def fn():
            if "specimens" not in vis:
                return {"items": [], "denominator": 0}
            s = _scan_fixable()
            items = []
            for it in s.get(key, []):
                vals = it.get("fields") or {"FinalSpeciesName": it.get("value")}
                items.append({"tname": "specimens", "rid": it["id"], "rkey": it.get("key"),
                              "fields": list(vals.keys()), "vals": vals,
                              "detail": {"suggest": it.get("suggest"), "note": it.get("note", "")}})
            return {"items": items, "denominator": _den("specimens")}
        return fn
    dets["FINAL_CASE"] = fixable_rule("name_case")
    dets["GENUS_MISMATCH"] = fixable_rule("genus_mismatch")
    dets["COUNT_FMT"] = fixable_rule("count_anomaly")
    dets["PRES_TEXT"] = fixable_rule("pres_anomaly")
    dets["POS_MISSING"] = fixable_rule("pos_fill")

    # ---- WSC_NOT_FOUND：最终名未收录本地名录 ----
    def wsc_not_found():
        items = []
        for t in ("specimens", "mygalomorph", "dali", "swu"):
            if t not in vis:
                continue
            for r in rows(t):
                d = _jd(r["data"])
                n = str(d.get("FinalSpeciesName") or "").strip()
                if n and n.lower() not in _WSC_SPEC_LOW:
                    items.append({"tname": t, "rid": r["id"], "rkey": d.get(TABLES[t]["key"]),
                                  "fields": ["FinalSpeciesName"], "vals": {"FinalSpeciesName": n},
                                  "detail": {"hint": "本地名录未收录（异名过期/拼写/名录滞后/暂定名误填）"}})
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu") if t in vis)}
    dets["WSC_NOT_FOUND"] = wsc_not_found

    # ---- GENUS_UNKNOWN：属名未收录（含相近属建议） ----
    def genus_unknown():
        tax_genera = {str(_jd(r["data"]).get("GENUS") or "").strip().lower() for r in rows("taxonomy")}
        items = []
        close_cache = {}
        for t in ("specimens", "mygalomorph", "dali", "swu"):
            if t not in vis:
                continue
            for r in rows(t):
                d = _jd(r["data"])
                g = str(d.get("Genus") or "").strip()
                if not g or g.lower() in tax_genera or g.lower() in _WSC_LOW:
                    continue
                if g.lower() not in close_cache:
                    hit = difflib.get_close_matches(g.lower(), list(_WSC_LOW.keys()), n=1, cutoff=0.75)
                    close_cache[g.lower()] = hit[0] if hit else ""
                sug = close_cache[g.lower()]
                items.append({"tname": t, "rid": r["id"], "rkey": d.get(TABLES[t]["key"]),
                              "fields": ["Genus"], "vals": {"Genus": g},
                              "detail": {"suggestion": sug if sug and sug != g.lower() else ""}})
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu") if t in vis)}
    dets["GENUS_UNKNOWN"] = genus_unknown

    # ---- CF_REVIEW / DET_PENDING / DET_DISPUTED ----
    def spec_where(where):
        items = []
        for t in ("specimens", "mygalomorph", "dali", "swu"):
            if t not in vis:
                continue
            kcol = TABLES[t]["key"]
            for r in rows(t):
                d = _jd(r["data"])
                tmp = str(d.get("TemporarySpeciesName") or "").strip()
                fin = str(d.get("FinalSpeciesName") or "").strip()
                hit = where(d, tmp, fin)
                if hit:
                    hit_d = hit if isinstance(hit, dict) else {}
                    items.append({"tname": t, "rid": r["id"], "rkey": d.get(kcol),
                                  "fields": hit_d.get("fields", ["FinalSpeciesName", "TemporarySpeciesName"]),
                                  "vals": {"TemporarySpeciesName": tmp, "FinalSpeciesName": fin},
                                  "detail": hit_d.get("detail", {})})
        return items

    def cf_review():
        def has_q(tmp, fin):
            for n in (tmp, fin):
                nl = " " + n.lower() + " "
                if " cf." in nl or " aff." in nl or nl.startswith(" cf.") or nl.startswith(" aff."):
                    return True
            return False
        items = spec_where(lambda d, tmp, fin: has_q(tmp, fin))
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu") if t in vis)}
    dets["CF_REVIEW"] = cf_review

    def det_pending():
        items = spec_where(lambda d, tmp, fin: bool(tmp) and not fin)
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu") if t in vis)}
    dets["DET_PENDING"] = det_pending

    def det_disputed():
        items = spec_where(lambda d, tmp, fin: str(d.get("DetStatus") or "").strip() == "有争议")
        return {"items": items, "denominator": sum(_den(t) for t in
                ("specimens", "mygalomorph", "dali", "swu") if t in vis)}
    dets["DET_DISPUTED"] = det_disputed

    # ---- MISSING_EVENT：缺采集事件号（含伪蝎的采集编号） ----
    def missing_event():
        items = []
        den = 0
        for t, lk in (("specimens", "CollectionEventNumber"), ("mygalomorph", "CollectionEventNumber"),
                      ("dali", "CollectionEventNumber"), ("swu", "CollectionEventNumber"),
                      ("pseudoscorpiones", "采集编号")):
            if t not in vis:
                continue
            den += _den(t)
            for r in rows(t):
                d = _jd(r["data"])
                if not str(d.get(lk) or "").strip():
                    items.append({"tname": t, "rid": r["id"], "rkey": d.get(TABLES[t]["key"]),
                                  "fields": [lk], "vals": {lk: None}, "detail": {}})
        return {"items": items, "denominator": den}
    dets["MISSING_EVENT"] = missing_event

    # ---- MISSING_COORD / DATE_INCOMPLETE / GEO_RANGE / AMBIG_COORD / COUNTRY_UNKNOWN ----
    def events_scan():
        miss_c, bad_date, geo_bad, ambig, cty_bad = [], [], [], [], []
        n = 0
        if "events" in vis:
            for r in rows("events"):
                d = _jd(r["data"])
                n += 1
                lat, lon = d.get("Latitude1"), d.get("Longitude1")
                if not str(lat or "").strip() and not str(lon or "").strip():
                    miss_c.append({"tname": "events", "rid": r["id"],
                                   "rkey": d.get("CollectionEventNumber"),
                                   "fields": ["Latitude1", "Longitude1"],
                                   "vals": {"Latitude1": None, "Longitude1": None}, "detail": {}})
                for yf, mf, df, tag in (("YYYY1", "MM1", "DD1", "起"), ("YYYY2", "MM2", "DD2", "止")):
                    y, m, dd = d.get(yf), d.get(mf), d.get(df)
                    if str(y or "").strip() and (not str(m or "").strip() or not str(dd or "").strip()):
                        bad_date.append({"tname": "events", "rid": r["id"],
                                         "rkey": d.get("CollectionEventNumber"),
                                         "fields": [yf, mf, df], "vals": {yf: y, mf: m, df: dd},
                                         "detail": {"why": tag + "日期缺月或日"}})
                for latf, lonf in (("Latitude1", "Longitude1"), ("Latitude2", "Longitude2")):
                    la, lo = d.get(latf), d.get(lonf)
                    if la is None and lo is None:
                        continue
                    amb = FR.parse_coordinate(str(la or ""), axis="lat")
                    if amb.get("confidence") == "ambiguous":
                        ambig.append({"tname": "events", "rid": r["id"],
                                      "rkey": d.get("CollectionEventNumber"),
                                      "fields": [latf], "vals": {latf: str(la)},
                                      "detail": {"why": amb.get("error") or "歧义坐标"}})
                        continue
                    try:
                        flA, flB = float(la), float(lo)
                    except (TypeError, ValueError):
                        geo_bad.append({"tname": "events", "rid": r["id"],
                                        "rkey": d.get("CollectionEventNumber"),
                                        "fields": [latf, lonf], "vals": {latf: str(la), lonf: str(lo)},
                                        "detail": {"why": "非数字"}})
                        continue
                    if not (-90 <= flA <= 90) or not (-180 <= flB <= 180):
                        geo_bad.append({"tname": "events", "rid": r["id"],
                                        "rkey": d.get("CollectionEventNumber"),
                                        "fields": [latf, lonf], "vals": {latf: str(la), lonf: str(lo)},
                                        "detail": {"why": "超出范围"}})
                c = str(d.get("Country") or "").strip().lower()
                if c and c not in geo.KNOWN_COUNTRIES:
                    cty_bad.append({"tname": "events", "rid": r["id"],
                                    "rkey": d.get("CollectionEventNumber"),
                                    "fields": ["Country"], "vals": {"Country": d.get("Country")},
                                    "detail": {}})
        return ({"items": miss_c, "denominator": n},
                {"items": bad_date, "denominator": n},
                {"items": geo_bad, "denominator": n},
                {"items": ambig, "denominator": n},
                {"items": cty_bad, "denominator": n})

    dets["MISSING_COORD"] = lambda: events_scan()[0]
    dets["DATE_INCOMPLETE"] = lambda: events_scan()[1]
    dets["GEO_RANGE"] = lambda: events_scan()[2]
    dets["AMBIG_COORD"] = lambda: events_scan()[3]
    dets["COUNTRY_UNKNOWN"] = lambda: events_scan()[4]

    # ---- DNA_ORPHAN ----
    def dna_orphan():
        if "dna_extract" not in vis or "uce" not in vis:
            return {"items": [], "denominator": 0}
        known = set()
        for r in rows("dna_extract"):
            v = (_jd(r["data"]).get("DNAVoucherID") or "").strip().upper()
            if v:
                known.add(v)
        items = []
        n = 0
        for r in rows("uce"):
            d = _jd(r["data"])
            sc = (d.get("Sample Code") or "").strip()
            if not sc:
                continue
            n += 1
            if sc.upper() not in known:
                items.append({"tname": "uce", "rid": r["id"], "rkey": d.get("Sample Key"),
                              "fields": ["Sample Code"], "vals": {"Sample Code": sc},
                              "detail": {"species": d.get("Species name") or ""}})
        return {"items": items, "denominator": n}
    dets["DNA_ORPHAN"] = dna_orphan

    # ---- HIST_ENUM：历史枚举值（允许的历史例外，只计数） ----
    def hist_enum():
        items = []
        for t, cfg in ((t, c) for t, c in vis.items() if t in TABLES):
            colmeta = {c["name"]: c for c in cfg.get("columns", [])}
            for r in rows(t):
                d = _jd(r["data"])
                for f, col in colmeta.items():
                    if not col.get("enum_locked"):
                        continue
                    v = d.get(f)
                    if v in (None, ""):
                        continue
                    opts = [str(o) for o in (col.get("options") or [])]
                    if str(v) not in opts:
                        items.append({"tname": t, "rid": r["id"], "rkey": d.get(cfg["key"]),
                                      "fields": [f], "vals": {f: v},
                                      "detail": {"allowed": "历史导入值按兼容政策保留"}})
        return {"items": items, "denominator": sum(_den(t) for t in vis if t in TABLES)}
    dets["HIST_ENUM"] = hist_enum

    return dets


def _cr_run(vis):
    """按可见表运行全部适用规则。返回 {rule_id: {"items", "denominator"}}（只含注册表内的规则）。"""
    results = {}
    for rule_id, fn in _cr_detectors(vis).items():
        rule = CR.get(rule_id)
        if not rule:
            continue
        try:
            res = fn()
        except Exception as e:
            results[rule_id] = {"items": [], "denominator": 0, "error": str(e)[:120]}
            continue
        if isinstance(res, dict) and "items" in res:
            res["denominator"] = res.get("denominator") or 0
            results[rule_id] = res
    return results


def _cr_issue_stats(conn):
    """按规则的状态计数（工单维度）。"""
    out = {}
    for r in conn.execute("SELECT rule_id, status, COUNT(*) n FROM issues GROUP BY rule_id, status"):
        out.setdefault(r["rule_id"], {})[r["status"]] = r["n"]
    return out


def _cr_summary_payload(me, vis, results=None, stats=None):
    results = results if results is not None else _cr_run(vis)
    stats = stats if stats is not None else _cr_issue_stats(get_db())
    cats = {c["key"]: {"key": c["key"], "label": c["label"], "rules": []} for c in CR.CATEGORIES}
    for rule in CR.RULES:
        res = results.get(rule["rule_id"], {"items": [], "denominator": 0})
        st = stats.get(rule["rule_id"], {})
        cats[rule["category"]]["rules"].append({
            "rule_id": rule["rule_id"], "ver": rule["ver"], "title": rule["title"],
            "rationale": rule["rationale"], "tables": rule["tables"], "fix": rule["fix"],
            "fix_note": rule["fix_note"], "priority": rule["priority"], "queue": rule["queue"],
            "in_scope": rule["rule_id"] in results,
            "count": len(res.get("items", [])),
            "denominator": res.get("denominator", 0),
            "error": res.get("error"),
            "issues": {k: st.get(k, 0) for k in CR_ISSUE_STATUSES},
            "exception_n": st.get("deferred", 0) + st.get("exception", 0),
            "items": res.get("items", [])[:50],
        })
    for c in cats.values():
        c["rules"].sort(key=lambda r: (r["priority"], r["rule_id"]))
    order = {rid: i for i, rid in enumerate(CR.PRIORITY)}
    return {
        "registry_version": CR.REGISTRY_VERSION,
        "schema_note": "不提供综合质量得分：每个维度分别给出 计数/分母/例外数，解释权在研究者",
        "categories": [cats[c["key"]] for c in CR.CATEGORIES],
        "priority_order": sorted([r for c in cats.values() for r in c["rules"]],
                                 key=lambda r: (0, order[r["rule_id"]]) if r["rule_id"] in order
                                 else (1, r["priority"])),
        "tables_visible": list(vis.keys()),
    }


@app.get("/api/checkup/summary")
def api_checkup_summary():
    """规则引擎汇总（实时检测，不改工单）：分类/计数/分母/例外数 + 优先级排序。"""
    me = current_user()
    vis = visible_tables(me)
    return jsonify(_cr_summary_payload(me, vis))


@app.post("/api/checkup/scan")
def api_checkup_scan():
    """体检扫描：检测结果落工单（去重=稳定标识+规则；已解决复发→重开；
    暂缓/例外→只记检出时间不重开；未再检出的未处理项按规则口径自动解决）。
    扫描本身不修改研究数据，任何登录用户可执行；计数只覆盖其可见表。"""
    me = current_user()
    vis = visible_tables(me)
    conn = get_db()
    results = _cr_run(vis)
    ts = now()
    stats = {"created": 0, "reopened": 0, "autoclosed": 0, "updated": 0, "kept": 0}
    for rule_id, res in results.items():
        rule = CR.get(rule_id)
        if not rule:
            continue
        seen = set()
        for f in res.get("items", []):
            dedup = _cr_dedup(rule_id, f)
            seen.add(dedup)
            digest = _cr_digest(f.get("vals") or {})
            row = conn.execute("SELECT * FROM issues WHERE dedup=?", (dedup,)).fetchone()
            detail = json.dumps(f.get("detail") or {}, ensure_ascii=False)
            fields = json.dumps(f.get("fields") or [], ensure_ascii=False)
            if row is None:
                conn.execute(
                    "INSERT INTO issues(rule_id,rule_ver,tname,rid,rkey,dedup,fields,vals_digest,"
                    "status,detail,last_seen_at,created_by,created_at,updated_at) VALUES("
                    ":rule_id,:rule_ver,:tname,:rid,:rkey,:dedup,:fields,:digest,"
                    "'open',:detail,:ts,:by,:ts,:ts)",
                    {"rule_id": rule_id, "rule_ver": rule["ver"], "tname": f["tname"],
                     "rid": f.get("rid"), "rkey": f.get("rkey"), "dedup": dedup,
                     "fields": fields, "digest": digest, "detail": detail,
                     "ts": ts, "by": me["username"]})
                stats["created"] += 1
                continue
            sets, args = ["last_seen_at=?", "rule_ver=?", "detail=?", "vals_digest=?",
                          "rkey=COALESCE(?, rkey)", "rid=COALESCE(?, rid)", "updated_at=?"], \
                         [ts, rule["ver"], detail, digest, f.get("rkey"), f.get("rid"), ts]
            if row["status"] == "resolved":
                sets += ["status='open'", "reopen_count=reopen_count+1",
                         "handled_by=NULL", "handled_at=NULL",
                         "note=COALESCE(NULLIF(note,''), NULL)"]
                stats["reopened"] += 1
            elif row["status"] in ("deferred", "exception"):
                stats["kept"] += 1  # 有理由的例外/暂缓：不重开、不重复生成
            else:
                stats["updated"] += 1
            conn.execute("UPDATE issues SET " + ",".join(sets) + " WHERE id=?", (*args, row["id"]))
        # 未再检出：open/processing → 自动解决（仅限本次实际扫描过的表，
        # 不可见表的工单不受影响——受限用户的扫描不得关闭他人范围的工单）
        if rule.get("auto_resolve", "auto") == "auto":
            scanned = CR.rule_tables(rule, vis)
            if scanned:
                ph = ",".join("?" * len(scanned))
                for row in conn.execute(
                        f"SELECT id, dedup, note FROM issues WHERE rule_id=? "
                        f"AND status IN ('open','processing') AND tname IN ({ph})",
                        [rule_id] + scanned):
                    if row["dedup"] in seen:
                        continue
                    conn.execute(
                        "UPDATE issues SET status='resolved', handled_by='scan', handled_at=?, "
                        "note=CASE WHEN IFNULL(note,'')='' THEN '复核扫描未再检出，自动关闭' ELSE note END, "
                        "updated_at=? WHERE id=?", (ts, ts, row["id"]))
                    stats["autoclosed"] += 1
    log_edit(conn, me["username"], "specimens", None, None, "scan",
             {"_summary": f"数据体检扫描：新增 {stats['created']}、重开 {stats['reopened']}、"
                          f"自动关闭 {stats['autoclosed']}、更新 {stats['updated']}、例外保留 {stats['kept']}",
              "_registry": CR.REGISTRY_VERSION})
    conn.commit()
    return jsonify({"ok": True, "stats": stats,
                    "summary": _cr_summary_payload(me, vis, results,
                                                   _cr_issue_stats(conn))})


@app.get("/api/checkup/issues")
def api_checkup_issues():
    """工单列表：rule_id / status / tname / page 过滤。计数与列表按可见表隔离。"""
    me = current_user()
    vis = visible_tables(me)
    conn = get_db()
    conds, args = [], []
    rule_id = (request.args.get("rule_id") or "").strip()
    status = (request.args.get("status") or "").strip()
    tname = (request.args.get("tname") or "").strip()
    if rule_id:
        conds.append("rule_id=?")
        args.append(rule_id)
    if status:
        conds.append("status=?")
        args.append(status)
    if tname:
        if tname not in vis:
            return jsonify({"items": [], "total": 0})
        conds.append("tname=?")
        args.append(tname)
    else:
        conds.append("tname IN (%s)" % ",".join("?" * max(1, len(vis))))
        args.extend(list(vis.keys()) or ["__none__"])
    page = max(1, int(request.args.get("page", 1)))
    per = min(200, max(10, int(request.args.get("per", 50))))
    where = " AND ".join(conds) if conds else "1=1"
    total = conn.execute("SELECT COUNT(*) c FROM issues WHERE " + where, args).fetchone()["c"]
    rows = conn.execute("SELECT * FROM issues WHERE " + where +
                        " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'processing' THEN 1 "
                        "WHEN 'deferred' THEN 2 WHEN 'exception' THEN 3 ELSE 4 END, rule_id, id "
                        "LIMIT ? OFFSET ?", (*args, per, (page - 1) * per)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        d["fields"] = json.loads(d["fields"] or "[]")
        d["status_label"] = CR_STATUS_LABEL.get(d["status"], d["status"])
        rule = CR.get(d["rule_id"])
        d["rule_title"] = rule["title"] if rule else d["rule_id"]
        d["category_label"] = rule["category_label"] if rule else ""
        items.append(d)
    return jsonify({"items": items, "total": total, "page": page, "per": per,
                    "status_labels": CR_STATUS_LABEL})


@app.post("/api/checkup/issues/<int:iid>")
def api_checkup_issue_update(iid):
    """工单处理：action ∈ assign/status。状态转 resolved/deferred/exception 必须写理由备注；
    exception/deferred → open 视为重开处理。分派与状态流转需要该表写权限（权限控制覆盖
    计数、分派与修复：计数按可见表隔离，分派/修复按写权限）。"""
    me = current_user()
    conn = get_db()
    row = conn.execute("SELECT * FROM issues WHERE id=?", (iid,)).fetchone()
    if not row:
        abort(404, "工单不存在")
    if row["tname"] not in visible_tables(me):
        abort(403, "该表对您不可见")
    require_writer(me, row["tname"])
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    ts = now()
    if action == "assign":
        who = str(body.get("to") or "").strip() or me["username"]
        conn.execute("UPDATE issues SET assigned_to=?, updated_at=? WHERE id=?", (who, ts, iid))
        conn.commit()
        return jsonify({"ok": True, "assigned_to": who})
    if action == "status":
        st = body.get("status")
        if st not in CR_ISSUE_STATUSES:
            abort(400, "status 需为 " + "/".join(CR_ISSUE_STATUSES))
        note = str(body.get("note") or "").strip()
        if st in ("resolved", "deferred", "exception") and not note and not (row["note"] or "").strip():
            abort(400, "转为「%s」必须写明理由备注（有理由的例外与暂缓才不反复生成工单）"
                       % CR_STATUS_LABEL[st])
        sets = ["status=?", "updated_at=?", "handled_by=?", "handled_at=?"]
        args = [st, ts, me["username"], ts]
        if note:
            sets += ["note=?"]
            args.append(note)
        if body.get("reviewer"):
            sets += ["reviewer=?", "reviewed_at=?"]
            args += [str(body["reviewer"]), ts]
        conn.execute("UPDATE issues SET " + ",".join(sets) + " WHERE id=?", (*args, iid))
        conn.commit()
        return jsonify({"ok": True, "status": st})
    abort(400, "action 需为 assign / status")


# ---------------- 一键修复：预览 before/after → 统一写入（单批次可撤销） ----------------
def _fix_genus_sync(d):
    fs = str(d.get("FinalSpeciesName") or "").strip()
    gen = str(d.get("Genus") or "").strip()
    if fs and " " in fs and gen and not fs.lower().startswith(gen.lower()):
        return {"Genus": _cap_species(fs).split()[0]}
    return None


def _fix_name_case(d):
    fs = str(d.get("FinalSpeciesName") or "").strip()
    if fs and fs[0].islower():
        return {"FinalSpeciesName": _cap_species(fs)}
    return None


def _fix_count_cast(d):
    out = {}
    for f in COUNT_FIELDS:
        v = d.get(f)
        if v is None or not str(v).strip():
            continue
        s = str(v).strip()
        if s.replace(".", "", 1).isdigit():
            fv = float(s)
            out[f] = int(fv) if fv == int(fv) else fv
    return out or None


def _fix_pres_extract(d):
    m = re.match(r"^(\d+)\s*%?\s*（(.+?)）\s*$", str(d.get(PRES_COL) or ""))
    if not m:
        return None
    out = {PRES_COL: int(m.group(1))}
    note = f"保存浓度备注：{m.group(2)}"
    out["Notes"] = (str(d.get("Notes") or "").strip() + "；" + note).strip("；")
    return out


def _fix_pos_fill(d):
    if str(d.get("Position") or "").strip():
        return None
    pos = _compute_position(d.get("Box # (Start#-End#)"), d.get("MHBU #"))
    return {"Position": pos} if pos else None


CR_FIX_HANDLERS = {
    "genus_sync": _fix_genus_sync, "name_case": _fix_name_case,
    "count_cast": _fix_count_cast, "pres_extract": _fix_pres_extract,
    "pos_fill": _fix_pos_fill,
}
LEGACY_FIX_RULE = {"name_case": "FINAL_CASE", "genus_sync": "GENUS_MISMATCH",
                   "count_cast": "COUNT_FMT", "pres_extract": "PRES_TEXT",
                   "pos_fill": "POS_MISSING"}


def _cr_fix_targets(rule_id, dedups=None):
    conn = get_db()
    conds = ["rule_id=?", "status IN ('open','processing')"]
    args = [rule_id]
    if dedups:
        conds.append("dedup IN (%s)" % ",".join("?" * len(dedups)))
        args += list(dedups)
    return conn.execute("SELECT * FROM issues WHERE " + " AND ".join(conds) +
                        " ORDER BY id LIMIT 2000", args).fetchall()


def _cr_fix(user, rule_id, dedups=None, dry_run=True):
    """统一修复入口：预览 before/after（dry_run）→ 应用（单批次、审计、可撤销、
    修复行同步解决工单）。歧义规则（fix=never）默认绝不自动修复。"""
    rule = CR.get(rule_id)
    if not rule:
        abort(404, f"规则 {rule_id} 不存在")
    mode, handler = CR.fix_handler(rule)
    if mode == "never":
        abort(400, f"规则 {rule_id}（{rule['title']}）默认不自动修复：{rule['fix_note'] or '需人工判断'}")
    if mode != "auto":
        abort(400, f"规则 {rule_id} 无自动修复方式，请人工处理")
    fn = CR_FIX_HANDLERS[handler]
    conn = get_db()
    preview, applied = [], 0
    for row in _cr_fix_targets(rule_id, dedups):
        r0 = conn.execute("SELECT data FROM records WHERE id=? AND tname=? AND deleted=0",
                          (row["rid"], row["tname"])).fetchone()
        if not r0:
            continue
        d = json.loads(r0["data"])
        changes = fn(d) or {}
        changes = {k: v for k, v in changes.items() if not same(d.get(k), v)}
        if not changes:
            preview.append({"dedup": row["dedup"], "tname": row["tname"], "rid": row["rid"],
                            "key": row["rkey"], "changes": {}, "note": "无需改动（可能已修复）"})
            continue
        preview.append({"dedup": row["dedup"], "tname": row["tname"], "rid": row["rid"],
                        "key": row["rkey"],
                        "changes": {k: [d.get(k), v] for k, v in changes.items()}})
    if dry_run:
        return {"ok": True, "dry_run": True, "rule_id": rule_id, "title": rule["title"],
                "rationale": rule["rationale"], "fix": rule["fix"],
                "impact": len([p for p in preview if p["changes"]]), "preview": preview}
    # 应用：受影响表逐一验证写权限；单批次写入 + 审计 + 工单解决
    for t in {p["tname"] for p in preview if p["changes"]}:
        require_writer(user, t)
    batch_id = _new_batch("F")
    ts = now()
    applied_dedups = []
    for p in preview:
        if not p["changes"]:
            continue
        r0 = conn.execute("SELECT data, row_version FROM records WHERE id=? AND tname=?",
                          (p["rid"], p["tname"])).fetchone()
        if not r0:
            continue
        d = json.loads(r0["data"])
        new = dict(d)
        new.update({k: v[1] for k, v in p["changes"].items()})
        cur = conn.execute(
            "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
            "WHERE id=? AND tname=? AND row_version=?",
            (json.dumps(new, ensure_ascii=False), ts, user["username"], p["rid"], p["tname"],
             r0["row_version"]))
        if cur.rowcount != 1:
            continue  # 并发修改：该行跳过（预览过期），不影响其余行
        cfg = TABLES[p["tname"]]
        log_edit(conn, user["username"], p["tname"], p["rid"],
                 new.get(cfg["key"]) if cfg.get("key") else None, "update",
                 {"_auto": f"一键修复（{rule['title']}）", **p["changes"]}, batch_id=batch_id)
        if p["tname"] in DET_TABLES:
            _det_after_write(conn, p["tname"], p["rid"], d, new, list(p["changes"].keys()),
                             user=user["username"], batch_id=batch_id, source="manual")
        applied += 1
        applied_dedups.append(p["dedup"])
    if applied_dedups:
        qmarks = ",".join("?" * len(applied_dedups))
        conn.execute(
            f"UPDATE issues SET status='resolved', handled_by=?, handled_at=?, "
            f"note=CASE WHEN IFNULL(note,'')='' THEN '一键修复自动解决' ELSE note END, "
            f"reviewer=?, reviewed_at=?, updated_at=? WHERE dedup IN ({qmarks})",
            [user["username"], ts, user["username"], ts, ts] + applied_dedups)
    log_edit(conn, user["username"], "specimens", None, None, "update",
             {"_summary": f"数据纠错·{rule['title']}：修复 {applied} 行", "_batch": batch_id,
              "_rule": rule_id}, batch_id=batch_id)
    return {"ok": True, "fixed": applied, "batch": batch_id, "rule_id": rule_id}


@app.post("/api/checkup/fix")
def api_checkup_fix():
    """一键修复：{rule_id, dedups?, dry_run(默认 true)}。
    先预览 before/after、规则与影响；确认后 dry_run=false 应用（统一写入、可撤销）。"""
    user = current_user()
    body = request.get_json(silent=True) or {}
    rule_id = body.get("rule_id")
    if not rule_id:
        abort(400, "缺少 rule_id")
    conn0 = get_db()
    ctx = _idem_ctx(user, f"checkup_fix:{rule_id}", body)
    hit = _idem_lookup(conn0, ctx)
    if hit:
        return hit
    me = user
    try:
        payload = _cr_fix(user, rule_id, body.get("dedups"),
                          dry_run=bool(body.get("dry_run", True)))
    except sqlite3.OperationalError as e:
        _tx_rollback(conn0)
        return _tx_fail(e)
    except Exception:
        _tx_rollback(conn0)
        raise
    if payload.get("dry_run"):
        conn0.rollback()
        return jsonify(payload)
    for t in visible_tables(user):
        if t in TABLES:
            invalidate(t)
    return _finish_write(conn0, payload, ctx)


# ---------------- 长任务（名录更新/导入/SRA 批量的任务化入口，v3.54） ----------------
# ---------------- 标本流转事件（v3.55：最小范围=跳蛛盒位与借出台账） ----------------
MOV_KINDS = ("accession", "move", "tissue_sample", "loan", "return", "transfer", "loss")


def _mov_row(tname, rid):
    r = get_db().execute("SELECT * FROM records WHERE id=? AND tname=? AND deleted=0",
                         (rid, tname)).fetchone()
    if not r:
        abort(404, "记录不存在")
    return r


def _hole_taken(tname, box, pos, exclude_rid=None):
    return get_db().execute(
        "SELECT id FROM records WHERE tname=? AND deleted=0 "
        "AND json_extract(data,'$.\"Box # (Start#-End#)\"')=? "
        "AND json_extract(data,'$.\"Position\"')=? AND id<>?",
        (tname, box, pos, exclude_rid or -1)).fetchone()


def _scoped_disp(tname, q, filters, preset, sort, direction, show_deleted):
    """与 api_rows 同一过滤/排序语义的抽屉导航辅助（preset/列筛选/q/两段排序）。
    供上一条/下一条在全量排序中定位相邻记录——不用当前页行号冒充全量位置。"""
    cfg = TABLES[tname]
    type_field = cfg.get("type_field")
    eidx = linked_eidx(tname)
    disp = []
    for r in load_rows(tname):
        if r["deleted"] != show_deleted:
            continue
        d, miss = display_row(tname, r, eidx)
        if preset == "loaned" and TABLES[tname].get("loan_view") and not show_deleted:
            loaned = (d.get("LoanSpecimenNumber") or d.get("LoanSpecimenFrom")
                      or d.get("SentDate")) and not d.get("ReturnDate")
            if not loaned:
                continue
        if preset == "types" and type_field:
            tv = str(d.get(type_field) or "").strip().lower()
            if not tv or tv == "no":
                continue
        disp.append({"id": r["id"], "data": d})
    disp = _apply_col_filters(disp, filters or {})
    if q:
        disp = [r for r in disp if any(
            v is not None and q in str(v).lower() for v in r["data"].values())]
    if not sort and cfg.get("key"):
        key_col = next((c for c in cfg["columns"] if c["name"] == cfg["key"]), None)
        if key_col and not key_col.get("numeric"):
            def nat_key(r):
                v = str(r["data"].get(cfg["key"]) or "")
                return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", v)]
            disp.sort(key=nat_key)
    if sort:
        colmeta = {c["name"]: c for c in cfg["columns"]}
        col = colmeta.get(sort)
        if col and not col.get("numeric"):
            def nat_key2(r):
                v = str(r["data"].get(sort) or "")
                return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", v)]
            disp.sort(key=nat_key2, reverse=(direction == "desc"))
        elif sort in colmeta:
            numeric = colmeta[sort].get("numeric")

            def sk(r):
                v = r["data"].get(sort)
                if numeric or isinstance(v, (int, float)):
                    try:
                        return (0, float(v))
                    except (TypeError, ValueError):
                        return (1, 0)
                return (1, str(v) if v is not None else "")
            disp.sort(key=sk, reverse=(direction == "desc"))
    return disp


@app.get("/api/rows/<tname>/<int:rid>")
def api_row_get(tname, rid):
    """单行读取（详情深链）：无权限与不存在同响应 404（不泄露存在性）；
    已删除返回 deleted 标记（回收站还原入口可用）。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    r = get_db().execute(
        "SELECT id, data, deleted, row_version, updated_at, updated_by FROM records "
        "WHERE id=? AND tname=?", (rid, tname)).fetchone()
    if not r:
        abort(404, "记录不存在，或您没有查看该表的权限")
    return jsonify({"row": {"id": r["id"], "tname": tname, "data": json.loads(r["data"]),
                            "deleted": bool(r["deleted"]), "row_version": r["row_version"],
                            "updated_at": r["updated_at"], "updated_by": r["updated_by"],
                            "key": TABLES[tname].get("key")}})


@app.get("/api/rows/<tname>/<int:rid>/neighbors")
def api_row_neighbors(tname, rid):
    """详情抽屉上一条/下一条：与列表同一查询表达与排序，跨页按全量排序定位。"""
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    q = (request.args.get("q") or "").strip().lower()
    raw = request.args.get("filters") or ""
    filters = None
    if raw:
        try:
            filters = json.loads(raw)
            if not isinstance(filters, dict):
                filters = None
        except ValueError:
            filters = None
    preset = (request.args.get("preset") or "").strip()
    sort = request.args.get("sort") or ""
    direction = request.args.get("dir", "asc")
    show_deleted = request.args.get("deleted") == "1"
    disp = _scoped_disp(tname, q, filters, preset, sort, direction, show_deleted)
    cfg = TABLES[tname]
    total = len(disp)
    pos = next((i for i, r in enumerate(disp) if r["id"] == rid), None)

    def brief(r):
        key = (cfg.get("key") and r["data"].get(cfg["key"])) or ("#" + str(r["id"]))
        return {"rid": r["id"], "key": str(key)}

    if pos is None:
        first = brief(disp[0]) if disp else None
        return jsonify({"in_scope": False, "total": total, "suggestion": first,
                        "note": "该记录已不在当前筛选范围内" if total else "当前筛选没有命中行"})
    return jsonify({"in_scope": True, "index": pos + 1, "total": total,
                    "prev": brief(disp[pos - 1]) if pos > 0 else None,
                    "next": brief(disp[pos + 1]) if pos + 1 < total else None})


def _activity_visible_tables(user):
    v = visible_tables(user)
    return set(v.keys()) if isinstance(v, dict) else set(v)


@app.get("/api/activity/recent")
def api_activity_recent():
    """最近操作统一入口（v3.76）：复用既有事实存储（edits 批次审计/任务注册表/
    稳定引用迁移记录），不另建事实源。按当前权限过滤：普通用户只见自己发起、
    且触及可见表的条目；管理员见全部。"""
    user = current_user()
    v = visible_tables(user)
    vis = set(v.keys()) if isinstance(v, dict) else set(v)
    conn = get_db()
    own = "" if user["admin"] else " AND user=? "
    params = [] if user["admin"] else [user["username"]]
    vparams = list(vis)
    place = "(" + ",".join("?" for _ in vis) + ")"
    batches = [dict(r) for r in conn.execute(
        f"""SELECT batch_id, MIN(ts) AS started, MAX(ts) AS ended, COUNT(*) AS rows_n,
                   MAX(user) AS user, GROUP_CONCAT(DISTINCT tname) AS tnames,
                   GROUP_CONCAT(DISTINCT action) AS actions
            FROM edits WHERE batch_id IS NOT NULL AND tname IN {place}{own}
            GROUP BY batch_id ORDER BY MIN(id) DESC LIMIT 40""",
        vparams + params)]
    imports = [dict(r) for r in conn.execute(
        f"""SELECT batch_id, MIN(ts) AS started, MAX(ts) AS ended, COUNT(*) AS rows_n,
                   MAX(user) AS user, MAX(tname) AS tname, MAX(source) AS source
            FROM edits WHERE action='import' AND tname IN {place}{own}
            GROUP BY batch_id ORDER BY MIN(id) DESC LIMIT 20""",
        vparams + params)]
    catalogs = [dict(r) for r in conn.execute(
        f"""SELECT id, ts, user, action, changes FROM edits
            WHERE action='catalog_update'{own} ORDER BY id DESC LIMIT 10""",
        list(reversed(params)))]
    for c in catalogs:
        try:
            c["summary"] = json.loads(c.pop("changes") or "{}").get("_summary", "名录更新")
        except ValueError:
            c["summary"] = "名录更新"
    refs = [dict(r) for r in conn.execute(
        f"""SELECT batch_id, MIN(ts) AS started, MAX(user) AS user, MAX(tname) AS tname
            FROM edits WHERE (changes LIKE '%稳定引用%' OR changes LIKE '%别名绑定%'
                              OR changes LIKE '%引用迁移%') AND tname IN {place}{own}
            GROUP BY batch_id ORDER BY MIN(id) DESC LIMIT 10""",
        vparams + params)]
    exports = [dict(r) for r in conn.execute(
        f"""SELECT id, ts, user, tname, changes FROM edits
            WHERE action='export'{own} ORDER BY id DESC LIMIT 20""",
        list(reversed(params)))]
    for e in exports:
        try:
            e["summary"] = json.loads(e.pop("changes") or "{}").get("_summary", "")
        except ValueError:
            e["summary"] = ""
    return jsonify({"batches": batches, "imports": imports, "catalogs": catalogs,
                    "refs": refs, "exports": exports,
                    "note": "复用既有审计/任务/包记录；按权限过滤——普通用户仅见本人发起且触及可见表的条目"})


@app.get("/api/activity/batch/<bid>")
def api_activity_batch(bid):
    """批次明细（可点击定位/复制/导出清单的数据源）：按可见表过滤行级内容。"""
    user = current_user()
    v = visible_tables(user)
    vis = set(v.keys()) if isinstance(v, dict) else set(v)
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, ts, user, tname, rid, rkey, action, changes FROM edits "
        "WHERE batch_id=? ORDER BY id", (bid,))]
    if not rows:
        abort(404, "批次不存在")
    rows = [r for r in rows if r["tname"] in vis]
    if not user["admin"]:
        rows = [r for r in rows if r["user"] == user["username"]]
    if not rows:
        abort(404, "批次不存在，或您没有查看该批次的权限")
    for r in rows:
        try:
            ch = json.loads(r.pop("changes") or "{}")
            r["summary"] = ch.get("_summary", "")
            detail = ch.get("detail") if isinstance(ch.get("detail"), dict) else ch
            r["fields"] = {k: v for k, v in list(detail.items())[:6] if not k.startswith("_")}
        except ValueError:
            r["summary"] = ""
    return jsonify({"batch_id": bid, "rows": rows, "rows_n": len(rows),
                    "undo_endpoint": "/api/undo/" + bid + "/preview"})

@app.post("/api/resolve/entities")

def api_resolve_entities():
    """可复用实体选择器的解析端点（v3.72）：
    输入编号/名称列表 + 允许表范围 → 逐条分类：matched（唯一精确/唯一前缀，自动）/
    multi（多候选：同编号跨表、同表重复、前缀多条、名称检索——必须人工确认，绝不默认取第一条）/
    not_found / duplicate_input（同输入重复出现）/ out_of_scope（存在于可见但范围外的表，
    仅提示中文表名，不含任何记录内容）。用户不可见的表完全不参与解析——查无结果与无权限不可区分（不泄露存在性）。
    匹配携带稳定记录标识（tname+rid）供各业务端点提交；提交时各端点仍核对身份/权限/行版本。"""
    user = current_user()
    body = request.get_json(silent=True) or {}
    inputs = [str(x).strip() for x in (body.get("inputs") or []) if str(x).strip()]
    if not inputs:
        abort(400, "inputs 必填（编号/名称列表，支持批量）")
    if len(inputs) > 500:
        abort(400, "一次最多解析 500 条输入")
    vis = visible_tables(user)
    asked = [str(t).strip() for t in (body.get("tables") or [])]
    scope = [t for t in asked if t in vis and TABLES.get(t, {}).get("key")]
    if not scope:
        return jsonify({"results": [{"input": i, "status": "not_found", "matches": []} for i in inputs],
                        "note": "所选范围不含您有权限的表"})
    NAME_FIELDS = ("FinalSpeciesName", "TemporarySpeciesName", "Genus")
    keymap, meta = {}, {}
    for t in scope:
        kf = TABLES[t]["key"]
        km = {}
        for r in load_rows(t):
            if r["deleted"]:
                continue
            d = r["data"]
            k = str(d.get(kf) or "").strip()
            if not k:
                continue
            sci = next((str(d.get(f)).strip() for f in NAME_FIELDS
                        if d.get(f) and str(d.get(f)).strip()), "")
            vou = next((str(d.get(f)).strip() for f in ("DNAVoucherID", "OtherVoucherCode")
                        if d.get(f) and str(d.get(f)).strip()), "")
            km.setdefault(k, []).append(r["id"])
            meta[(t, r["id"])] = {"key": k, "sci": sci, "voucher": vou}
        keymap[t] = km
    # 可见但范围外的表：只记录编号存在性（不含 rid/内容），用于「不在范围」提示
    outsets = {}
    for t in vis:
        if t in scope or not TABLES.get(t, {}).get("key"):
            continue
        kf = TABLES[t]["key"]
        ks = set()
        for r in load_rows(t):
            if not r["deleted"]:
                k = str(r["data"].get(kf) or "").strip()
                if k:
                    ks.add(k)
        if ks:
            outsets[t] = ks

    def match_row(t, rid):
        m = meta[(t, rid)]
        return {"tname": t, "label": TD.display_label(t), "rid": rid, **m}

    PREFIX_CAP = 9
    results, seen = [], {}
    for raw in inputs:
        q = raw.strip()
        if q in seen:
            results.append({"input": raw, "status": "duplicate_input", "matches": [],
                            "first_at": seen[q]})
            continue
        seen[q] = len(results)
        exact = [(t, rid) for t in scope for rid in keymap[t].get(q, [])]
        if len(exact) == 1:
            results.append({"input": raw, "status": "matched", "via": "exact",
                            "matches": [match_row(*exact[0])]})
            continue
        if len(exact) > 1:
            results.append({"input": raw, "status": "multi",
                            "reason": "同编号命中多个实体（跨表或同表重复），请人工确认",
                            "matches": [match_row(t, rid) for t, rid in exact]})
            continue
        ql = q.lower()
        prefix = [(t, rid) for t in scope for k, rids in keymap[t].items()
                  if k.lower().startswith(ql) for rid in rids]
        if len(prefix) == 1:
            results.append({"input": raw, "status": "matched", "via": "prefix",
                            "matches": [match_row(*prefix[0])]})
            continue
        if len(prefix) > 1:
            truncated = len(prefix) > PREFIX_CAP
            results.append({"input": raw, "status": "multi",
                            "reason": f"前缀命中 {len(prefix)} 条，请人工确认" + ("（已截断显示前 9 条）" if truncated else ""),
                            "matches": [match_row(t, rid) for t, rid in prefix[:PREFIX_CAP]],
                            "candidate_count": len(prefix), "truncated": truncated})
            continue
        named = [(t, rid) for t in scope for k, rids in keymap[t].items() for rid in rids
                 if meta[(t, rid)]["sci"] and ql in meta[(t, rid)]["sci"].lower()]
        if named:
            truncated = len(named) > PREFIX_CAP
            results.append({"input": raw, "status": "multi",
                            "reason": f"按名称命中 {len(named)} 条，请人工确认" + ("（已截断显示前 9 条）" if truncated else ""),
                            "matches": [match_row(t, rid) for t, rid in named[:PREFIX_CAP]],
                            "candidate_count": len(named), "truncated": truncated})
            continue
        where = [TD.display_label(t) for t, ks in outsets.items() if q in ks]
        if where:
            results.append({"input": raw, "status": "out_of_scope", "where": where,
                            "matches": [],
                            "note": "该编号存在于下列表，但不在本选择器的表范围内（仅提示位置，不含内容）"})
            continue
        results.append({"input": raw, "status": "not_found", "matches": []})
    return jsonify({"results": results,
                    "note": "matched=唯一命中（自动）；multi=多候选必须人工确认；"
                            "无权限的表不参与解析（与不存在不可区分，不泄露存在性）；提交时业务端点仍核对身份/权限/版本"})


@app.get("/api/movements")
def api_movements_global():
    """近期流转（跨行台账视图）：按可见表过滤，时间倒序；供「流转与借出」页使用。"""
    user = current_user()
    vis = visible_tables(user)
    tnames = [t for t in ("specimens", "dali", "swu") if t in vis]
    if not tnames:
        return jsonify({"items": []})
    limit = request.args.get("limit") or 100
    try:
        limit = max(1, min(int(limit), 500))
    except ValueError:
        limit = 100
    conds = ["tname IN (%s)" % ",".join("?" * len(tnames))]
    params = list(tnames)
    kinds = [k.strip() for k in (request.args.get("kinds") or "").split(",") if k.strip() in MOV_KINDS]
    if kinds:
        conds.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params += kinds
    conn = get_db()
    items = []
    for r in conn.execute(
            "SELECT * FROM movements WHERE " + " AND ".join(conds) + " ORDER BY id DESC LIMIT ?",
            (*params, limit)):
        d = dict(r)
        row = conn.execute("SELECT data FROM records WHERE id=? AND tname=?",
                           (d["rid"], d["tname"])).fetchone()
        d["key"] = (json.loads(row["data"]).get(TABLES[d["tname"]].get("key"))
                    if row and TABLES[d["tname"]].get("key") else "(已删除)")
        items.append(d)
    return jsonify({"items": items})


@app.get("/api/rows/<tname>/<int:rid>/movements")
def api_movements_list(tname, rid):
    user = current_user()
    ensure_visible(user, tname)
    conn = get_db()
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM movements WHERE tname=? AND rid=? ORDER BY id DESC", (tname, rid))]
    out = {"items": items, "loan_state": None}
    if tname in ("specimens", "dali", "swu"):
        loans = [i for i in items if i["kind"] == "loan"]
        rets = [i for i in items if i["kind"] == "return"]
        if loans:
            known = all(str(i["qty"] or "").strip().isdigit() for i in items)
            if known:
                sent = sum(int(i["qty"]) for i in loans)
                back = sum(int(i["qty"]) for i in rets)
                out["loan_state"] = {"sent": sent, "returned": back, "still_out": sent - back,
                                     "unknown": False}
            else:
                out["loan_state"] = {"sent": None, "returned": None, "still_out": None,
                                     "unknown": True,
                                     "note": "数量未知（借出/归还存在未记数量的流转，保持未知）"}
    return jsonify(out)


@app.post("/api/movements")
def api_movements_create():
    """创建流转事件。move：事务内检查目标盒孔唯一性（并发同孔 409），确认后按行版本
    条件更新兼容字段；loan：记录外借单位/人/期限/数量（未知保持未知），重复借出阻止；
    借出未归还时移盒需显式 force。"""
    user = current_user()
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    tname = body.get("tname")
    if kind not in MOV_KINDS:
        abort(400, "kind 需为 " + "/".join(MOV_KINDS))
    if tname not in TABLES:
        abort(400, "未知表")
    require_writer(user, tname)
    try:
        rid = int(body.get("rid"))
    except (TypeError, ValueError):
        abort(400, "rid 无效")
    conn0 = get_db()
    ctx = _idem_ctx(user, f"movement:{kind}", body)
    hit = _idem_lookup(conn0, ctx)
    if hit:
        return hit
    qty = body.get("qty")
    qty = (str(qty).strip() or None) if qty is not None else None
    if qty is not None and not qty.isdigit():
        abort(400, "数量需为非负整数；未知请留空")
    from_pos = str(body.get("from_pos") or "").strip() or None
    counterparty = str(body.get("counterparty") or "").strip() or None
    due_date = str(body.get("due_date") or "").strip() or None
    note = str(body.get("note") or "").strip() or None
    ref = str(body.get("ref") or "").strip() or None
    batch_id = _new_batch("M")
    try:
        row = _mov_row(tname, rid)
        cfg = TABLES[tname]
        open_loan = conn0.execute(
            "SELECT COUNT(*) c FROM movements WHERE tname=? AND rid=? AND kind='loan'",
            (tname, rid)).fetchone()["c"]
        rets = conn0.execute(
            "SELECT COUNT(*) c FROM movements WHERE tname=? AND rid=? AND kind='return'",
            (tname, rid)).fetchone()["c"]
        if kind == "move" and open_loan > rets and not body.get("force"):
            abort(409, "该标本存在未归还的借出事件；请先归还，或确认实物在库时附 force 与原因")
        if kind == "loan" and open_loan > rets:
            abort(409, "该标本已在借出中（未归还），不能重复借出")
        to_pos_final = None
        if kind == "move":
            box = str(body.get("to_box") or "").strip()
            pos = str(body.get("to_hole") or "").strip()
            if not box or not pos:
                abort(400, "移盒需提供 to_box 与 to_hole")
            if _hole_taken(tname, box, pos, exclude_rid=rid):
                abort(409, f"目标盒孔已被占用：{box} / {pos}（请选择其他空孔）")
            to_pos_final = f"{box}|{pos}"
        conn0.execute(
            "INSERT INTO movements(kind,tname,rid,qty,from_pos,to_pos,counterparty,due_date,"
            "note,ref,op_user,created_at,undo_of,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, tname, rid, qty, from_pos, to_pos_final, counterparty, due_date,
             note, ref, user["username"], now(), body.get("undo_of") or None, batch_id))
        if kind == "move" and to_pos_final:
            box, pos = to_pos_final.split("|", 1)
            d = json.loads(row["data"])
            changes = {}
            if not same(d.get("Box # (Start#-End#)"), box):
                changes["Box # (Start#-End#)"] = box
            if not same(d.get("Position"), pos):
                changes["Position"] = pos
            if changes:
                new = dict(d)
                new.update(changes)
                cur = conn0.execute(
                    "UPDATE records SET row_version=row_version+1, data=?, updated_at=?, updated_by=? "
                    "WHERE id=? AND row_version=?",
                    (json.dumps(new, ensure_ascii=False), now(), user["username"], rid, row["row_version"]))
                if cur.rowcount != 1:
                    raise WriteConflict({"error": "行在移盒确认时被并发修改（盒位占用以服务器为准）"})
                r2 = conn0.execute("SELECT data FROM records WHERE id=?", (rid,)).fetchone()
                new2 = json.loads(r2["data"])
                if _hole_taken(tname, new2.get("Box # (Start#-End#)"), new2.get("Position"), exclude_rid=rid):
                    _tx_rollback(conn0)
                    abort(409, "目标盒孔在提交瞬间被并发占用（两人同时取同一空孔），本次未写入；请刷新重选")
                log_edit(conn0, user["username"], tname, rid,
                         new2.get(cfg["key"]) if cfg.get("key") else None,
                         "update", {"_auto": "移盒事件同步",
                                    **{k: [d.get(k), v] for k, v in changes.items()}},
                         batch_id=batch_id)
        log_edit(conn0, user["username"], tname, rid, None, "update",
                 {"_summary": f"流转事件 {kind}" + (f" → {counterparty}" if counterparty else ""),
                  "_kind": kind, "_qty": qty, "_to": to_pos_final, "_counterparty": counterparty},
                 batch_id=batch_id)
        payload = {"ok": True, "kind": kind, "rid": rid, "qty": qty, "batch": batch_id}
    except WriteConflict as cw:
        _tx_rollback(conn0)
        return jsonify({"ok": False, "conflict": cw.payload}), 409
    except sqlite3.IntegrityError as e:
        _tx_rollback(conn0)
        if "idx_records_hole" in str(e) or "UNIQUE" in str(e):
            abort(409, "目标盒孔被并发占用（唯一索引拦截），本次未写入；请刷新后重选")
        abort(400, "写入失败：" + str(e)[:120])
    except Exception:
        _tx_rollback(conn0)
        raise
    invalidate(tname)
    return _finish_write(conn0, payload, ctx)


@app.post("/api/movements/<int:mid>/undo")
def api_movements_undo(mid):
    """撤销流转事件：有后续依赖流转时按依赖阻止（生成纠正事件而非回写历史）。"""
    user = current_user()
    conn = get_db()
    m = conn.execute("SELECT * FROM movements WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404, "流转事件不存在")
    require_writer(user, m["tname"])
    dep = conn.execute(
        "SELECT COUNT(*) c FROM movements WHERE tname=? AND rid=? AND id>? AND kind IN "
        "('move','loan','return','transfer','loss')",
        (m["tname"], m["rid"], mid)).fetchone()["c"]
    if dep:
        abort(409, f"该事件后有 {dep} 条依赖流转（已归还/已移位等），撤销会与现状冲突；"
                   f"请生成纠正事件而不是回写历史")
    conn.execute("DELETE FROM movements WHERE id=?", (mid,))
    log_edit(conn, user["username"], m["tname"], m["rid"], None, "update",
             {"_summary": f"撤销流转事件 #{mid}（{m['kind']}）"})
    conn.commit()
    invalidate(m["tname"])
    return jsonify({"ok": True})


@app.get("/api/loans")
def api_loans_ledger():
    """借出台账：外借单位/人、期限、数量、仍在外数量（未知保持未知）。"""
    user = current_user()
    vis = visible_tables(user)
    conn = get_db()
    out = []
    for t in ("specimens", "dali", "swu"):
        if t not in vis:
            continue
        cfg = TABLES[t]
        for r in conn.execute("SELECT id, data FROM records WHERE tname=? AND deleted=0", (t,)):
            d = json.loads(r["data"])
            items = [dict(x) for x in conn.execute(
                "SELECT * FROM movements WHERE tname=? AND rid=? AND kind IN ('loan','return') "
                "ORDER BY id", (t, r["id"]))]
            if not any(i["kind"] == "loan" for i in items):
                continue
            rets = sum(1 for i in items if i["kind"] == "return")
            known = all(str(i["qty"] or "").strip().isdigit() for i in items)
            still = None
            if known:
                sent = sum(int(i["qty"]) for i in items if i["kind"] == "loan")
                back = sum(int(i["qty"]) for i in items if i["kind"] == "return")
                still = sent - back
            last = items[-1]
            out.append({"tname": t, "rid": r["id"], "key": d.get(cfg["key"]),
                        "counterparty": last["counterparty"], "due_date": last["due_date"],
                        "qty": last["qty"], "returns": rets,
                        "still_out": still, "qty_unknown": not known,
                        "last_event": last["created_at"]})
    return jsonify({"loans": out,
                    "note": "still_out=null 表示数量未知（保持未知，不当 0）；归还允许部分数量"})



BIB.register(app)
LAB.register(app)
SS.register(app)
PRJ.register(app)
COLLAB.register(app)
import composite as CT
CT.register(app)
ET.register(app)


@app.get("/api/tasks")
def api_tasks_list():
    current_user()
    return jsonify({"tasks": [t for t in TASKS.snapshot_list()
                              if t["kind"] in ("catalog_update",) or True][:50]})


@app.get("/api/tasks/<tid>")
def api_tasks_get(tid):
    current_user()
    t = TASKS.get(tid)
    if not t:
        abort(404, "任务不存在")
    return jsonify(t)


@app.post("/api/tasks/<tid>/cancel")
def api_tasks_cancel(tid):
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员可取消任务")
    t = TASKS.request_cancel(tid)
    if not t:
        abort(404, "任务不存在")
    return jsonify({"ok": True, "status": t["status"], "cancel_requested": t["cancel_requested"]})


@app.post("/api/catalogs/refresh")
def api_catalogs_refresh_task():
    """名录更新任务化：阶段=拉取 WSC→拉取 WAC→热重载；幂等（进行中返回原任务）；管理员。"""
    user = current_user()
    if not user["admin"]:
        abort(403, "仅管理员")
    t = TASKS.create("catalog_update", ["拉取 WSC 名录", "拉取 WAC 名录", "热重载缓存"],
                     lambda tk: _run_catalog_task(tk), idem_key="catalog_update")
    return jsonify({"ok": True, "task": t})


def _run_catalog_task(tk):
    def run_catalog_update2(trigger="task"):
        if not _catalog_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有一次名录更新在进行中"}
        try:
            import update_wsc
            import update_wac
            update_wsc.main(with_species=True)
            TASKS.advance(tk, "WSC 完成")
            update_wac.main()
            TASKS.advance(tk, "WAC 完成")
            reload_catalogs()
            return {"ok": True}
        finally:
            _catalog_lock.release()
    r = run_catalog_update2()
    tk["result"] = r
    if not r.get("ok"):
        raise RuntimeError(r.get("error"))


# ---------------- 共享来源（events）修改影响预览（v3.57） ----------------
_REF_TABLES = (("specimens", "CollectionEventNumber"), ("mygalomorph", "CollectionEventNumber"),
               ("dali", "CollectionEventNumber"), ("swu", "CollectionEventNumber"),
               ("pseudoscorpiones", "采集编号"))
_IMPACT_FIELDS = ("Province", "City", "County", "Locality", "Latitude1", "Longitude1",
                  "Elevation1", "Habitat", "Collector", "DD1", "MM1", "YYYY1")


def _norm_loose(s):
    import re as _re
    _punct = chr(32) + chr(12288) + chr(65288) + chr(65289) + "()-—_.，。;:,\"’”"
    return _re.sub("[" + _re.escape(_punct) + "]+", "", str(s or "")).lower()


def _classify_change(old_v, new_v):
    """区分：纠正拼写（宽松归一后相同）/ 展示格式（官方地名归一后相同）/ 修改源事实。"""
    a, b = str(old_v or ""), str(new_v or "")
    if a.strip() == b.strip():
        return "none"
    if _norm_loose(a) == _norm_loose(b):
        return "spelling"
    try:
        if geo.norm_province(a) == geo.norm_province(b) or geo.norm_country(a) == geo.norm_country(b):
            return "format"
    except Exception:
        pass
    return "fact"


def _event_impact(tname, rid, proposed=None, user=None):
    """共享来源影响预览：按稳定引用（当前明确关联的事件号）计算受影响记录。
    权限外的表只提示「存在受限依赖」，不泄露标识与数量。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id=? AND tname='events' AND deleted=0",
                       (rid,)).fetchone()
    if not row:
        abort(404, "采集事件不存在")
    cur = json.loads(row["data"])
    evnum = str(cur.get("CollectionEventNumber") or "").strip()
    changes = {}
    ev_cols = {c["name"] for c in TABLES["events"]["columns"]}
    if proposed:
        for k, v in proposed.items():
            if k in ev_cols and not same(cur.get(k), v):
                changes[k] = [cur.get(k), v]
    affected, restricted = [], []
    total = 0
    vis = visible_tables(user) if user else {}
    for t, key in _REF_TABLES:
        n = conn.execute(
            "SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=0 "
            "AND json_extract(data,'$.\"' || ? || '\"')=?",
            (t, key, evnum)).fetchone()["c"]
        if n == 0:
            continue
        total += n
        if t in vis:
            sample = []
            for r in conn.execute(
                    "SELECT id, data FROM records WHERE tname=? AND deleted=0 "
                    "AND json_extract(data,'$.\"' || ? || '\"')=? LIMIT 5", (t, key, evnum)):
                d = json.loads(r["data"])
                sample.append({"rid": r["id"], "key": d.get(TABLES[t].get("key"))})
            affected.append({"table": t, "label": TD.display_label(t), "count": n,
                             "sample": sample,
                             "linked_fields": [f for f in _IMPACT_FIELDS]})
        else:
            restricted.append(t)
    field_kinds = {k: _classify_change(p[0], p[1]) for k, p in changes.items()}
    return {"rid": rid, "event": evnum, "affected_total": total,
            "affected": affected, "restricted_tables": restricted,
            "has_restricted": bool(restricted),
            "changes": changes, "field_kinds": field_kinds,
            "change_kind": ("fact" if "fact" in field_kinds.values()
                            else ("spelling" if "spelling" in field_kinds.values()
                                  else ("format" if "format" in field_kinds.values() else "none"))),
            "source_row_version": row["row_version"],
            "note": "影响=通过当前明确关联（事件号）实时展示的记录；审计只记录源操作与本影响范围，"
                    "不为动态展示变化伪造逐条编辑"}


@app.post("/api/events/<int:rid>/impact-preview")
def api_event_impact_preview(rid):
    """事件修改影响预览（dry-run）：传 proposed 变更（字段→新值）；不写库。"""
    user = current_user()
    ensure_visible(user, "events")
    body = request.get_json(silent=True) or {}
    imp = _event_impact("events", rid, proposed=body.get("changes") or {}, user=user)
    imp["high_impact"] = imp["change_kind"] == "fact" and bool(imp["affected_total"])
    return jsonify(imp)


@app.get("/api/events/<int:rid>/impact-preview")
def api_event_impact_current(rid):
    user = current_user()
    ensure_visible(user, "events")
    return jsonify(_event_impact("events", rid, user=user))


@app.get("/api/field-catalog")
def api_field_catalog():
    """字段语义目录（稳定 field_id + 各表内部键 + 双语 + 定义 + 兼容表头 + 使用情况）。"""
    user = current_user()
    vis = visible_tables(user)
    tname = request.args.get("tname") or None
    items = []
    for d in FC.FIELD_DEFS:
        tables = {t: k for t, k in d["tables"].items() if t in vis}
        if tname and tname not in d["tables"]:
            continue
        items.append({**{k: v for k, v in d.items() if k != "tables"}, "tables": tables})
    return jsonify({"fields": items, "usage": FC.usage(tname),
                    "note": "field_id 稳定：改显示名不改数据身份、不使已存视图失效；"
                            "兼容表头按表作用域识别，碰撞停止自动映射"})


@app.get("/api/metrics")
def api_metrics():
    """核心计量（分母与排除条件随值返回，不做单一综合得分）：
    标本记录数 / 已知个体数（有 WSC 收录最终名的记录）/ 数量未知记录数 / 提取数 / 文库数 / 不重复凭证数。
    全部按调用者可见表计（权限隔离）；类群账号只见本类群。"""
    user = current_user()
    vis = visible_tables(user)
    conn = get_db()
    def n(t):
        return conn.execute("SELECT COUNT(*) c FROM records WHERE tname=? AND deleted=0", (t,)).fetchone()["c"] if t in vis else 0
    rec = n("specimens")
    known = unknown_q = 0
    if "specimens" in vis:
        for r in conn.execute("SELECT data FROM records WHERE tname='specimens' AND deleted=0"):
            try:
                d = json.loads(r["data"])
            except (ValueError, TypeError):
                continue
            fin = str(d.get("FinalSpeciesName") or "").strip()
            if fin:
                known += 1
            qs = [str(d.get(f) or "").strip() for f in ("AdultMale", "AdultFemale", "Subadult", "Juvenile")]
            if not any(qs):
                unknown_q += 1
    # 不重复凭证数：提取台账 ∪ UCE.Sample Code（大小写不敏感）
    vset = set()
    if "dna_extract" in vis:
        for r in conn.execute("SELECT data FROM records WHERE tname='dna_extract' AND deleted=0"):
            v = (json.loads(r["data"]).get("DNAVoucherID") or "").strip()
            if v:
                vset.add(v.lower())
    if "uce" in vis:
        for r in conn.execute("SELECT data FROM records WHERE tname='uce' AND deleted=0"):
            v = (json.loads(r["data"]).get("Sample Code") or "").strip()
            if v:
                vset.add(v.lower())
    return jsonify({
        "metrics": [
            {"key": "specimen_records", "label": "标本记录数", "value": rec,
             "denominator": "specimens 表未删除行", "exclusions": "不含回收站"},
            {"key": "known_individuals", "label": "已知个体数", "value": known,
             "denominator": f"标本记录 {rec} 条", "exclusions": "仅计 FinalSpeciesName 非空的记录；cf./sp. 等限定名按已填名计入（鉴定历史可查限定符）"},
            {"key": "unknown_quantity", "label": "数量未知的记录数", "value": unknown_q,
             "denominator": f"标本记录 {rec} 条", "exclusions": "四类数量列（雄/雌/亚成/幼）全部为空的记录"},
            {"key": "extracts", "label": "提取数", "value": n("dna_extract"),
             "denominator": "DNA 提取台账未删除行", "exclusions": "同凭证重复提取按行数计（一条=一次提取）"},
            {"key": "libraries", "label": "文库数", "value": n("uce"),
             "denominator": "UCE 文库未删除行", "exclusions": "同凭证重复建库按行数计（Sample Key 唯一）"},
            {"key": "distinct_vouchers", "label": "不重复凭证数", "value": len(vset),
             "denominator": "提取台账 DNAVoucherID ∪ UCE Sample Code（大小写不敏感去重）",
             "exclusions": "空值不计"},
        ],
        "generated_at": now(), "tables_visible": list(vis.keys()),
    })


# /api/stats 图表端点已并入 stats_scope.register（v4.11：与 /api/stats/enhanced 共用
# 筛选参数 table/group/q/filters 与同一聚合管道，避免双份实现口径漂移）。


def conn_total(tname):
    from flask import has_app_context
    try:
        return get_db().execute(
            "SELECT COUNT(*) FROM records WHERE tname=? AND deleted=0", (tname,)).fetchone()[0]
    except Exception:
        return 0


def events_visible_total(me):
    if "events" in visible_tables(me):
        return conn_total("events")
    return 0


def _visible_feed(me, limit=9):
    """按可见表过滤的全局动态（无可见主表空状态也在用）。"""
    vis = visible_tables(me)
    rows = get_db().execute(
        "SELECT ts, user, action, tname, rkey, changes FROM edits ORDER BY id DESC LIMIT 60"
    ).fetchall()
    if me["admin"]:
        rows = rows[:limit]
    else:
        rows = [e for e in rows if not e["tname"] or e["tname"] in vis][:limit]
    out = []
    for e in rows:
        try:
            ch = json.loads(e["changes"] or "{}")
        except (ValueError, TypeError):
            ch = {}
        text = ch.get("_summary") or ""
        if e["action"] == "update" and not ch.get("_summary"):
            parts = [f"{k}: {str(v[0] if v[0] is not None else '空')} → {str(v[1] if v[1] is not None else '空')}"
                     for k, v in list(ch.items())[:3] if not k.startswith("_")][:2]
            text = "；".join(parts)
        out.append({"ts": e["ts"], "user": e["user"], "action": e["action"],
                    "rkey": e["rkey"], "text": text[:120]})
    return out


# ---------------------------------------------------------------- 附件
UPLOAD_DIR = os.path.join(APP_DIR, config.UPLOADS_DIR)


@app.get("/api/rows/<tname>/<int:rid>/files")
def api_files_list(tname, rid):
    user = current_user()
    ensure_visible(user, tname)
    chk_table(tname)
    show_del = request.args.get("deleted") == "1"
    rows = get_db().execute(
        "SELECT id, fname, size, uploaded_by, uploaded_at, sha256, mime, note, deleted, deleted_at "
        "FROM attachments WHERE tname=? AND rid=? AND (deleted=0 OR ?) ORDER BY id DESC",
        (tname, rid, 1 if show_del else 0)).fetchall()
    # deleted 历史行可能存成 TEXT '0'/'1'——出口统一归一为 int，前端布尔判断不再踩类型坑
    out_files = []
    for r in rows:
        d0 = dict(r)
        d0["deleted"] = int(d0["deleted"] or 0)
        out_files.append(d0)
    return jsonify({"files": out_files})


@app.post("/api/rows/<tname>/<int:rid>/files")
def api_files_upload(tname, rid):
    user = current_user()
    require_writer(user, tname)
    chk_table(tname)
    backup.wait_window()  # 附件一致性窗口：备份期间暂停附件写入
    if not get_db().execute("SELECT 1 FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone():
        abort(404, "记录不存在")
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "未收到文件")
    # 规范命名：{业务主键}_{原文件名}.{后缀}（原名已带主键则不重复加；同名自动加序号）
    row = get_db().execute("SELECT data FROM records WHERE id=? AND tname=?", (rid, tname)).fetchone()
    key0 = TABLES[tname].get("key")
    keyval = ""
    if key0:
        try:
            keyval = str((json.loads(row["data"]) or {}).get(key0) or "").strip()
        except ValueError:
            keyval = ""
    if not keyval:
        keyval = f"{tname}-r{rid}"
    stem, ext = os.path.splitext(f.filename)
    stem = re.sub(r'[^\w\-一-鿿.（）()]+', "_", stem).strip("._") or "file"
    stem = stem[:60].strip("_")
    ext = (ext or ".bin").lower()
    base = stem if stem.startswith(keyval) else f"{keyval}_{stem}"
    conn = get_db()
    fname, stored, n = base + ext, base + ext, 1
    d = os.path.join(UPLOAD_DIR, tname, str(rid))
    os.makedirs(d, exist_ok=True)
    while (conn.execute("SELECT 1 FROM attachments WHERE tname=? AND rid=? AND fname=?",
                        (tname, rid, fname)).fetchone()
           or os.path.exists(os.path.join(d, stored))):
        n += 1
        fname = f"{base}_{n}{ext}"
        stored = fname
    # 暂存 → 校验（摘要）→ 发布（原子改名进正式目录，与附件行同批落库）。
    # 发布失败/事务失败自动清理暂存与半发布文件，可重试不留半份。
    import hashlib
    staging = os.path.join(d, ".staging-" + secrets.token_hex(6))
    final = os.path.join(d, stored)
    try:
        f.save(staging)
        h = hashlib.sha256()
        with open(staging, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        size = os.path.getsize(staging)
        mime = f.mimetype or "application/octet-stream"
        note = (request.form.get("note") or "").strip()
        conn.execute(
            "INSERT INTO attachments(tname,rid,fname,stored,size,uploaded_by,uploaded_at,"
            "sha256,mime,note,deleted,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,'')",
            (tname, rid, fname, stored, size, user["username"], now(), sha, mime, note))
        os.replace(staging, final)   # 原子发布
        log_edit(conn, user["username"], tname, rid, None, "file",
                 {"_summary": f"上传附件 {fname}", "_sha256": sha})
        conn.commit()
        return jsonify({"ok": True, "sha256": sha})
    except Exception:
        conn.rollback()
        for p in (staging, final):
            try:
                if os.path.exists(p) and os.path.getsize(p) >= 0 and p.endswith(tuple(staging.split("-")[-1:])) is not False and p != final or p == staging:
                    os.remove(p)
            except OSError:
                pass
        # 半发布残留清理（事务失败但已 replace）：仅当无附件行指向它时删除
        if not conn.execute("SELECT 1 FROM attachments WHERE tname=? AND rid=? AND stored=?",
                            (tname, rid, stored)).fetchone():
            try:
                if os.path.exists(final):
                    os.remove(final)
            except OSError:
                pass
        raise


@app.get("/api/files/<int:fid>/download")
def api_file_download(fid):
    user = current_user()
    r = get_db().execute("SELECT * FROM attachments WHERE id=?", (fid,)).fetchone()
    if not r:
        abort(404, "附件不存在")
    ensure_visible(user, r["tname"])   # 按真实归属表鉴权（跨表共享不放大权限）
    if str(r["deleted"]) in ("1", "True"):
        abort(404, "附件已移入回收站（可在附件列表还原）")
    path = os.path.join(UPLOAD_DIR, r["tname"], str(r["rid"]), r["stored"])
    if not os.path.exists(path):
        abort(404, "附件文件丢失（可用「附件缺失诊断」核查）")
    inline = request.args.get("inline") == "1"
    return send_file(path, as_attachment=not inline, download_name=r["fname"])


@app.delete("/api/files/<int:fid>")
def api_file_delete(fid):
    user = current_user()
    backup.wait_window()  # 附件一致性窗口：备份期间暂停附件删除
    r = get_db().execute("SELECT * FROM attachments WHERE id=?", (fid,)).fetchone()
    if not r:
        abort(404, "附件不存在")
    require_writer(user, r["tname"])
    if r["uploaded_by"] != user["username"] and not user["admin"]:
        abort(403, "只有上传者或管理员可删除附件")
    conn = get_db()
    conn.execute("UPDATE attachments SET deleted=1, deleted_at=? WHERE id=?", (now(), fid))
    log_edit(conn, user["username"], r["tname"], r["rid"], None, "file",
             {"_summary": f"附件移入回收站 {r['fname']}（文件保留，可还原）"})
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/files/<int:fid>/restore")
def api_file_restore(fid):
    """附件还原（软删除 → 恢复可见；文件未删除）。"""
    user = current_user()
    r = get_db().execute("SELECT * FROM attachments WHERE id=?", (fid,)).fetchone()
    if not r:
        abort(404, "附件不存在")
    require_writer(user, r["tname"])
    conn = get_db()
    conn.execute("UPDATE attachments SET deleted=0, deleted_at='' WHERE id=?", (fid,))
    log_edit(conn, user["username"], r["tname"], r["rid"], None, "file",
             {"_summary": f"还原附件 {r['fname']}"})
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 版本对比
@app.get("/api/versions/<tname>/compare")
def api_version_compare(tname):
    current_user()
    chk_table(tname)
    ref_a = request.args.get("a", "current")
    ref_b = request.args.get("b", "current")
    ra, va = _snapshot_rows(tname, ref_a)
    rb, vb = _snapshot_rows(tname, ref_b)
    key = TABLES[tname]["key"]

    def index(rows):
        out = {}
        for r in rows:
            if r.get("deleted"):
                continue
            k = r["data"].get(key)
            if k:
                out.setdefault(k, r["data"])
        return out

    ia, ib = index(ra), index(rb)
    added = [k for k in ib if k not in ia]
    removed = [k for k in ia if k not in ib]
    changed = []
    for k in ia:
        if k not in ib:
            continue
        da, db_ = ia[k], ib[k]
        for f in set(da) | set(db_):
            if not same(da.get(f), db_.get(f)):
                changed.append({"key": k, "field": f, "a": da.get(f), "b": db_.get(f)})
    return jsonify({
        "a_name": va["name"] if va else "当前数据", "b_name": vb["name"] if vb else "当前数据",
        "added_total": len(added), "removed_total": len(removed), "changed_total": len(changed),
        "added": added[:300], "removed": removed[:300], "changed": changed[:2000],
    })


# ---------------------------------------------------------------- 静态页面
@app.after_request
def no_cache_static(resp):
    if request.path == "/" or request.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.after_request
def _no_heuristic_cache(resp):
    """静态资源禁用浏览器启发式缓存：没有 Cache-Control 时浏览器会按
    Last-Modified 自行缓存数小时，部署间隙的坏版本会一直命中缓存。"""
    p = (resp.headers.get("Content-Type") or "")
    if "/static/" in (request.path or "") or request.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    # 静态资源带版本参数（取 js/css 修改时间的较大者），任一前端文件更新后浏览器立即拉新版
    ver = int(max(os.path.getmtime(os.path.join(APP_DIR, "static", "app.js")),
                  os.path.getmtime(os.path.join(APP_DIR, "static", "style.css"))) * 1000)
    with open(os.path.join(APP_DIR, "static", "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/app.js", f"/static/app.js?v={ver}")
    html = html.replace("/static/style.css", f"/static/style.css?v={ver}")
    html = html.replace("<head>", f"<head><script>window.__APP_VERSION__={json.dumps(APP_VERSION)}</script>", 1)
    resp = app.make_response(html)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(os.path.join(APP_DIR, "static"), p)


# ---------------------------------------------------------------- 启动
def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()
    dbm.init_db()
    if not os.environ.get("SPM_DB_FILE"):  # 测试隔离库不做启动备份（backup.py 只认生产库文件名）
        try:
            m = backup.create_package(kind="startup", app_version=APP_VERSION, silent=True)
            if m:
                print(f"  启动时已自动备份完整恢复包 -> {m['name']}")
        except Exception as e:  # 备份失败不阻塞启动
            print(f"  启动自动备份失败（不影响运行）：{e}")
    _start_autoclean()
    _start_catalog_updater()
    _start_recycle_maintenance()
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{config.PORT}")).start()
    print(f"{'=' * 56}\n  {config.SYSTEM_NAME} 已启动")
    print(f"  本机访问:   http://127.0.0.1:{config.PORT}")
    for ip in lan_ips():
        print(f"  局域网访问: http://{ip}:{config.PORT}  <- 把这个地址发给协作者")
    print(f"  数据文件:   {dbm.DB_PATH}\n  按 Ctrl+C 停止\n{'=' * 56}")
    # 优先用生产级 WSGI 服务器 waitress（pip install waitress），未安装则回退开发服务器
    try:
        from waitress import serve
        print("  WSGI:       waitress（生产模式）")
        serve(app, host=config.HOST, port=config.PORT, threads=8)
    except ImportError:
        app.run(host=config.HOST, port=config.PORT, threaded=True)


if __name__ == "__main__":
    main()
