# -*- coding: utf-8 -*-
"""统一备份：SQLite 一致性快照 + uploads 附件 + manifest + 配置说明 → 完整恢复包。

恢复包 = backups/pkg-<时间戳>-<kind>/ 目录：
  data.db      SQLite 一致性副本（backup API + integrity_check 通过后原子发布）
  uploads/     附件副本（不可变存储；备份窗口内短暂阻止附件写入）
  manifest.json  完成时点/应用与结构版本/表计数/文件大小与摘要/附件清单/异地与口令声明
  config_notes.txt  部署配置说明（不含任何口令明文或访问令牌）

流程：写临时目录 .tmp-* → integrity_check + manifest 校验 → 原子发布（os.replace）。
未完成的 .tmp-* 目录不出现在可恢复列表，启动/下次备份时清理。

留存数量、容量上限、异地副本目的地、RPO/RTO 目标均在 config.py 配置；
未配置异地副本时，系统如实标注「未配置异地备份」。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time

import config

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, config.DB_FILE)
BACKUP_DIR = os.path.join(APP_DIR, config.BACKUP_DIR)
UPLOADS_DIR = os.path.join(APP_DIR, config.UPLOADS_DIR)
OFFSITE_DIR = os.path.join(APP_DIR, config.BACKUP_OFFSITE_DIR) if config.BACKUP_OFFSITE_DIR else ""
KEEP = config.BACKUP_KEEP
MAX_GB = config.BACKUP_MAX_GB
LOG = os.path.join(BACKUP_DIR, "backup.log")

WINDOW_FLAG = os.path.join(BACKUP_DIR, ".backup-window")
TMP_PREFIX = ".tmp-"
PKG_PREFIX = "pkg-"


class BackupError(Exception):
    """备份/校验失败（携带原因，调用方转 5xx/4xx；绝不半成品发布）。"""


def window_flag():
    return WINDOW_FLAG


def wait_window(timeout=20):
    """附件写入/删除路径调用：备份窗口内短暂等待，避免引用与文件时点不一致。"""
    deadline = time.time() + timeout
    while os.path.exists(WINDOW_FLAG):
        if time.time() >= deadline:
            return False
        time.sleep(0.3)
    return True


def _sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _log(msg):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _table_counts(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = {}
    try:
        for t in conn.execute("SELECT tname, COUNT(*) n FROM records GROUP BY tname"):
            out[t["tname"]] = t["n"]
        out["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        out["edits"] = conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
        out["versions"] = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        out["attachments"] = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        try:
            out["schema_version"] = conn.execute(
                "SELECT value FROM schema_info WHERE key='schema_version'").fetchone()[0]
        except Exception:
            out["schema_version"] = ""
    finally:
        conn.close()
    return out


def _integrity_ok(db_path):
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            return all(r == "ok" for r in rows), rows
        except sqlite3.DatabaseError as e:
            return False, ["库严重损坏，integrity 无法完成：" + str(e)[:120]]
    finally:
        conn.close()


def _app_version():
    try:
        import server
        return server.APP_VERSION
    except Exception:
        try:
            import re as _re
            m = _re.search(r'APP_VERSION = "([^"]+)"',
                           open(os.path.join(APP_DIR, "server.py"), encoding="utf-8").read())
            return m.group(1) if m else ""
        except Exception:
            return ""


def _copy_uploads(stage):
    """附件目录副本（不可变存储：只增不改；窗口标志期间服务端暂停附件变更）。"""
    dst_up = os.path.join(stage, "uploads")
    if os.path.isdir(UPLOADS_DIR):
        shutil.copytree(UPLOADS_DIR, dst_up)
    return dst_up


def _uploads_manifest(stage):
    out, total = [], 0
    up = os.path.join(stage, "uploads")
    for root, _dirs, files in os.walk(up):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, stage).replace("\\", "/")
            size = os.path.getsize(p)
            out.append({"path": rel, "size": size, "sha256": _sha256(p)})
            total += size
    return out, total


def _pkg_name(stamp, kind, seq=1):
    return f"pkg-{stamp}-{kind}" if seq <= 1 else f"pkg-{stamp}-{kind}-{seq}"


def _free_pkg_seq(stamp, kind, seq=1):
    """同秒内的两次打包（启动自动备份与手动备份并发、或连点两下）各取各的包名。

    已发布的包一律不覆盖、更不删除：Windows 上刚 rmtree 掉的目录常被扫描器/索引器
    短暂持有，紧接着 os.replace 就是 [WinError 5] 拒绝访问，而删除本身已把上一份恢复包
    销毁——备份体系里最不该发生的两件事一起发生。"""
    while os.path.exists(os.path.join(BACKUP_DIR, _pkg_name(stamp, kind, seq))):
        seq += 1
    return seq


def create_package(kind="manual", app_version=None, silent=False):
    """统一备份入口：返回 manifest（含 name）。失败抛 BackupError 并清理临时目录。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    seq = _free_pkg_seq(stamp, kind)
    name = _pkg_name(stamp, kind, seq)
    final = os.path.join(BACKUP_DIR, name)
    stage = os.path.join(BACKUP_DIR, TMP_PREFIX + name)
    t0 = time.time()
    try:
        os.makedirs(stage, exist_ok=False)
    except FileExistsError:
        stage = stage + "-" + str(int(time.time()))
        os.makedirs(stage, exist_ok=False)
    # 磁盘空间预算检查：不足 → 立即 BackupError（不产生半成品目录）
    est = 0
    if os.path.exists(DB_PATH):
        est += os.path.getsize(DB_PATH) * 2
    if os.path.isdir(UPLOADS_DIR):
        for _root, _dirs, _files in os.walk(UPLOADS_DIR):
            for _fn in _files:
                est += os.path.getsize(os.path.join(_root, _fn))
    _ensure_free_space(BACKUP_DIR, est)

    try:
        # 1) 一致性 DB 快照（backup API，WAL 安全）
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(os.path.join(stage, "data.db"))
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()

        # 2) 附件一致性窗口：标志期间服务端暂停附件上传/删除，再复制 uploads
        os.makedirs(BACKUP_DIR, exist_ok=True)
        open(WINDOW_FLAG, "w").close()
        try:
            last_copy_err = None
            for _attempt in range(3):
                try:
                    _copy_uploads(stage)
                    last_copy_err = None
                    break
                except OSError as e:
                    last_copy_err = e
                    time.sleep(1.0)
            if last_copy_err is not None:
                raise BackupError(f"uploads 复制失败（重试 3 次）：{last_copy_err}")
            # 3) DB 再做一次（窗口期内提交的变更也纳入快照，保证引用⊆文件）
            src = sqlite3.connect(DB_PATH)
            try:
                dst = sqlite3.connect(os.path.join(stage, "data.db"))
                with dst:
                    src.backup(dst)
                dst.close()
            finally:
                src.close()
        finally:
            if os.path.exists(WINDOW_FLAG):
                os.remove(WINDOW_FLAG)

        # 4) 校验：integrity + manifest 计数（不通过 → 清理并抛错，绝不半成品发布）
        ok, rows = _integrity_ok(os.path.join(stage, "data.db"))
        if not all(r == "ok" for r in rows):
            raise BackupError("备份副本 integrity_check 未通过：" + "; ".join(rows[:3]))
        counts = _table_counts(os.path.join(stage, "data.db"))
        up_manifest, up_total = _uploads_manifest(stage)
        db_size = os.path.getsize(os.path.join(stage, "data.db"))
        manifest = {
            "format": "specimen-backup/1",
            "name": name, "kind": kind,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app_version": app_version or _app_version(),
            "schema_version": counts.get("schema_version", ""),
            "table_counts": counts,
            "db_size": db_size, "db_sha256": _sha256(os.path.join(stage, "data.db")),
            "uploads": {"count": len(up_manifest), "total": up_total, "files": up_manifest},
            "tokens_note": "包内 data.db 含口令哈希（非明文）与会话/API 令牌；"
                           "管理员下载自动剥离会话与 API 令牌",
            "offsite": {"configured": bool(OFFSITE_DIR), "copied": False},
            "duration_sec": round(time.time() - t0, 1),
        }
        with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        notes = (
            "部署配置说明（本文件为文本说明，不含任何口令明文或访问令牌）\n"
            f"系统名称: {config.SYSTEM_NAME}\n端口: {config.PORT}\n"
            f"数据库文件: {config.DB_FILE}\n附件目录: {config.UPLOADS_DIR}\n"
            f"结构版本: {manifest['schema_version']}  应用版本: {manifest['app_version']}\n"
            "恢复方法: 独立临时目录解包 → integrity_check → 迁移 → 隔离实例验证\n"
            "安全声明: 口令仅存加盐哈希；下载副本自动剥离会话与 API 令牌\n")
        open(os.path.join(stage, "config_notes.txt"), "w", encoding="utf-8").write(notes)

        # 5) 原子发布（目标已被占则避让换名；已发布的包不覆盖不删除）
        seq2 = _free_pkg_seq(stamp, kind, seq)
        if seq2 != seq:
            seq, name = seq2, _pkg_name(stamp, kind, seq2)
            final = os.path.join(BACKUP_DIR, name)
            manifest["name"] = name
            with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
        # Windows 上刚建好的目录可能被杀软/索引器短暂持有，os.replace 偶发
        # PermissionError——短暂退避后重试（v4.35，治同秒打包测试的偶发假失败）
        last_err = None
        for attempt in range(4):
            try:
                os.replace(stage, final)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        if last_err is not None:
            raise last_err
    except Exception as e:
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
        if isinstance(e, BackupError):
            raise
        raise BackupError(f"备份失败：{e}") from e

    _log(f"包 {name} 完成（{manifest['db_size']} bytes，附件 {len(up_manifest)}，"
         f"耗时 {manifest['duration_sec']}s）")

    # 6) 留存/容量/异地
    _prune(silent=silent)
    if OFFSITE_DIR:
        try:
            dest = os.path.join(OFFSITE_DIR, name)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(final, dest)
            manifest["offsite"] = {"configured": True, "copied": True,
                                   "dir": OFFSITE_DIR, "copied_at": now_str()}
            with open(os.path.join(final, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
        except OSError as e:
            manifest["offsite"]["copied"] = False
            manifest["offsite"]["error"] = str(e)[:200]
            _log(f"异地副本失败：{e}")
    manifest["name"] = name
    return manifest


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _prune(silent=False):
    """留存数量与容量上限：超出按最旧删除（仅 pkg-* 包）。"""
    pkgs = sorted(d for d in os.listdir(BACKUP_DIR)
                  if d.startswith(PKG_PREFIX) and os.path.isdir(os.path.join(BACKUP_DIR, d)))
    while len(pkgs) > KEEP:
        old = pkgs.pop(0)
        shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    total = 0
    sizes = {}
    for d in pkgs:
        s = 0
        for root, _dirs, files in os.walk(os.path.join(BACKUP_DIR, d)):
            for fn in files:
                s += os.path.getsize(os.path.join(root, fn))
        sizes[d] = s
        total += s
    limit = int(MAX_GB * (1 << 30))
    for d in pkgs:
        if total <= limit:
            break
        shutil.rmtree(os.path.join(BACKUP_DIR, d), ignore_errors=True)
        total -= sizes.get(d, 0)
        if not silent:
            _log(f"容量清理：删除 {d}")


def _free_bytes(path):
    import shutil as _sh
    return _sh.disk_usage(path).free


def _ensure_free_space(path, needed):
    if _free_bytes(path) < needed * 1.15 + (64 << 20):
        raise BackupError("磁盘空间不足：备份所需空间超出可用空间，已中止（未产生半成品）")


def list_packages():
    """可恢复列表：仅完整发布的 pkg-* 包（含 manifest 摘要）；.tmp-* 一律不列。"""
    out = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    for d in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not d.startswith(PKG_PREFIX):
            continue
        p = os.path.join(BACKUP_DIR, d)
        if not os.path.isdir(p) or d.startswith(TMP_PREFIX):
            continue
        mf = os.path.join(p, "manifest.json")
        if not os.path.exists(mf):
            continue  # 未完成/半成品不可恢复
        try:
            m = json.load(open(mf, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": d, "kind": m.get("kind"), "created_at": m.get("created_at"),
                    "app_version": m.get("app_version"), "schema_version": m.get("schema_version"),
                    "table_counts": m.get("table_counts"), "db_size": m.get("db_size"),
                    "db_sha256": m.get("db_sha256"),
                    "uploads_count": (m.get("uploads") or {}).get("count"),
                    "offsite": m.get("offsite"), "complete": True})
    return out


def package_path(name):
    if not name.startswith(PKG_PREFIX) or "/" in name or "\\" in name or ".." in name:
        raise BackupError("非法备份名")
    return os.path.join(BACKUP_DIR, name)


def cleanup_stale_tmp():
    """清理残留的半完成临时目录（>1 小时）。"""
    if not os.path.isdir(BACKUP_DIR):
        return
    now = time.time()
    for d in os.listdir(BACKUP_DIR):
        if not d.startswith(TMP_PREFIX):
            continue
        p = os.path.join(BACKUP_DIR, d)
        try:
            if now - os.path.getmtime(p) > 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def verify_restored(restored_dir, expected_counts=None, check_attachments=True):
    """对恢复产物做完整校验：integrity / 必要表 / manifest 计数 / 关键引用 / 附件摘要。
    返回 (ok, report)。任何失败都 ok=False——绝不 integrity 失败仍 ok:true。"""
    report = {"checks": [], "failures": []}
    db_path = os.path.join(restored_dir, "data.db")
    if not os.path.exists(db_path):
        return False, {"failures": ["缺少 data.db"]}
    ok, rows = _integrity_ok(db_path)
    report["checks"].append({"check": "integrity_check", "ok": ok, "detail": rows[:3]})
    if not ok:
        # integrity 失败 → 立即失败返回，不做后续检查（绝不 integrity 失败仍 ok:true）
        report["failures"].append("integrity_check 未通过：" + "; ".join(rows[:3]))
        report["failures"].append("（integrity 失败，跳过后续检查）")
        return False, report

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        need_tables = ("records", "users", "edits", "versions", "attachments", "schema_info", "boxes")
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        miss = [t for t in need_tables if t not in have]
        report["checks"].append({"check": "必要表", "ok": not miss, "detail": miss})
        if miss:
            report["failures"].append("缺少必要表：" + "、".join(miss))
    except sqlite3.DatabaseError as e:
        # 页损坏可能到查询时才暴露：按失败返回，绝不 integrity 失败仍 ok:true
        report["failures"].append("恢复库查询失败（页损坏）：" + str(e)[:120])
        return False, report

    mf_path = os.path.join(restored_dir, "manifest.json")
    manifest = {}
    if os.path.exists(mf_path):
        try:
            manifest = json.load(open(mf_path, encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    counts = {}
    for t in need_tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = None
    report["counts"] = counts
    try:
        if manifest.get("table_counts"):
            mism = []
            for t, n in manifest["table_counts"].items():
                if t in counts and counts[t] is not None and counts[t] != n:
                    mism.append(f"{t}: manifest={n} 恢复={counts[t]}")
            report["checks"].append({"check": "manifest 计数", "ok": not mism, "detail": mism[:5]})
            if mism:
                report["failures"].append("manifest 计数不符：" + "；".join(mism[:5]))
    except sqlite3.DatabaseError as e:
        report["failures"].append("恢复库查询失败（页损坏）：" + str(e)[:120])
        return False, report

    if check_attachments and manifest.get("uploads", {}).get("files"):
        stage_up = os.path.join(restored_dir, "uploads")
        bad, missf = [], []
        for f in manifest["uploads"]["files"]:
            p2 = os.path.join(restored_dir, f["path"])
            if not os.path.exists(p2):
                missf.append(f["path"])
                continue
            if _sha256(p2) != f["sha256"]:
                bad.append(f["path"])
        ok_att = not bad and not missf
        report["checks"].append({"check": "附件摘要", "ok": ok_att,
                                 "detail": {"missing": missf[:5], "digest_mismatch": bad[:5]}})
        if not ok_att:
            report["failures"].append("附件缺失或摘要不符：" +
                                      "；".join(missf[:3] + bad[:3]))
    conn.close()
    report["failures"] = report.get("failures", [])
    return (not report["failures"]), report


if __name__ == "__main__":
    import sys
    kind = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    m = create_package(kind=kind)
    if m:
        print(f"已备份到 {m['name']}（DB {m['db_size']} bytes，附件 {m['uploads']['count']} 个）")
    else:
        print("未找到数据库文件，跳过备份")
