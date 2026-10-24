# -*- coding: utf-8 -*-
"""SQLite 连接与建表。所有数据（含账号）都在一个 data.db 文件里，备份即拷走。"""
import os
import sqlite3

import config

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, config.DB_FILE)

# ======================= 数据库结构版本 =======================
# 设计更新时：SCHEMA_VERSION +1，SCHEMA_CHANGELOG 顶部加条目，
# 并在 init_db() 的迁移区补上对应的 ALTER/数据迁移代码。
SCHEMA_VERSION = "3.18"
SCHEMA_CHANGELOG = [
    {"ver": "3.18", "date": "2026-10-04",
     "note": "自定义组合表（配置驱动，v4.07）：composite_tables（稳定 ct_id/名称说明/所有者/主数据源/"
             "行粒度关联与字段配置 JSON/status=draft|published|disabled/revision 配置修订/"
             "permissions 每表访问授权 JSON{用户:r|rw}）+ composite_revisions 修订历史（每次配置变更"
             "存完整定义快照与修改者，恢复旧配置只恢复表定义）；users.can_composite 构建器功能权限"
             "（管理员默认、逐用户授权，镜像 can_boxes 交互）；组合表数据权限="
             "组合表授权 × 源表三态权限 × 记录可见性的交集，服务器逐请求实时计算（收回即时生效）；"
             "行身份由实际源实体（表+行id）组成，编辑经源记录 row_version/校验/幂等/审计通道；"
             "配置修改/停用/删除定义不触碰源数据"},
    {"ver": "3.17", "date": "2026-10-04",
     "note": "协作回传冲突审阅：edit_packages 增 pending_review 列（409 冲突回传时持久化待处理尝试=updates/conflicts/决策状态），"
             "支持决策端点逐项重检（行版本/字段范围/删除态）后部分合并——已应用与待处理分开记账，包保持 active 保留后续处理"},
    {"ver": "3.16", "date": "2026-10-03",
     "note": "自定义/期刊模板导出：export_templates 表（scope personal|team/team 修改生成新修订 revision/"
             "favorite/项目默认/template_kind=phylo|evolution|material|custom/期刊元数据 journal 等/"
             "规则来源 official|lab|personal/列定义 JSON[稳定字段身份+输出名+复合函数白名单+格式]）+ "
             "template_revisions（团队模板修订历史）；渲染走统一范围解析与行身份选择"
             "（record=标本记录/specimen、one_to_many=展开|aggregate|representative 禁止静默取第一条）；"
             "格式化只影响导出不改库；干净投稿文件+独立伴随说明文件（README 不强制插入数据表）"},
    {"ver": "3.15", "date": "2026-10-03",
     "note": "协作编辑包：edit_packages（pkg_id/创建者/表/列白名单/状态 active|merged|revoked|expired）+ "
             "edit_package_rows（稳定记录标识/导出时基础值/基础 row_version）；权威基线存服务器，"
             "Excel 只带包 ID+数据（不含令牌/身份/版本，隐藏列不被信任）；回传三方比较"
             "（基础值 vs 服务器当前值 vs 回传值），同字段双方修改 409 审阅；"
             "幂等；审计记 包创建者/实际提交者/协作者署名未认证"},
    {"ver": "3.14", "date": "2026-10-03",
     "note": "项目工作集：projects（名称/kind=fixed|dynamic/purpose/owner/filters[动态条件]/stage）+ "
             "project_members（稳定记录标识引用主库，不复制事实；加入/移除原因/时间/操作者/项目内备注/"
             "任务状态/展示顺序/排除原因 与主库字段分开）；移除成员不删主库记录；"
             "动态工作集可固化成员版本；冻结导出复用版本机制"},
    {"ver": "3.13", "date": "2026-10-03",
     "note": "文献引用与材料引证：bibliographic_reference（稳定标识/DOI 可空/题名/作者/年份/页码图版——"
             "无 DOI 老文献保留完整书目信息不虚构标识）；material_citation（文献→标本引证：原文材料引用、"
             "当前接受名并列、证据附件、冻结时点）；模式标本身份增加 依据/确认人/确认时间 与状态来源"
             "（文献确认/标签记载/待核实/历史导入——术语按实验室规范配置，程序不自动裁定命名学地位）；"
             "材料清单生成前缺项预检（数量/日期/地点/保藏单位/引用依据）支持科学上合理的缺失说明；"
             "双向入口（从文献看材料/从标本看被引文献）"},
    {"ver": "3.12", "date": "2026-10-03",
     "note": "实验批次/板/孔最小关系模型（依据实测：UDI i5/i7 各 96 行、长度 8bp、无非法碱基/无重复 UDI#）："
             "experiment_batch（批次/送测清单版本/已发送标记）/ plate（板，规格配置化 96/384）/ "
             "plate_well（孔位占用：板+行+列唯一索引，对象类型=样本/对照/空，异常状态；行列统一坐标）/ "
             "pool + pool_member（有效混池内 UDI 组合冲突与长度/碱基校验，阈值来自实测与实验室配置不自造标准）/ "
             "barcode_pair（i5/i7 保存原序列、方向、来源；导出转换绑定仪器/软件/试剂配置，不覆盖源序列）/ "
             "qc_measurement（定量多次测量：值/单位/仪器方法/采用理由，单位不明不自动换算）"},
    {"ver": "3.11", "date": "2026-10-03",
     "note": "标本流转事件（最小范围=跳蛛盒位与借出台账）：movements 表记录 入库/移盒/组织取样/借出/归还/转赠/损耗"
             "（对象、数量[可未知]、起止位置、对方、期限、原因、凭据、操作人）；当前盒位与借出状态从已确认事件一致更新"
             "并保留兼容字段（Box #/Position 仍为权威存储）；历史存量不伪造移动；盒位占用事务内唯一性检查（并发同孔 409）；"
             "借出支持部分归还与仍在外数量（未知数量保持未知）；事件撤销有依赖时阻止"},
    {"ver": "3.10", "date": "2026-10-03",
     "note": "版本元数据与压缩快照（v3 格式 gzip+base64+SHA-256，实测 35×；增量经实测无收益否决）；"
             "versions 增 category/description/protected/source_op_id/结构/规则版本/content_sha256/size_bytes/format/deps；"
             "受保护版本不可普通删除；留存政策计划/执行；VACUUM 仅计划维护"},
    {"ver": "3.9", "date": "2026-10-03",
     "note": "附件生命周期：attachments 新增 sha256 摘要/mime 媒体类型/note 拍摄或来源说明/"
             "deleted·deleted_at 软删除状态；上传改为 暂存→校验→发布（staging 文件校验摘要后原子发布，"
             "事务失败自动清理并可重试）；删除先软删除保留可恢复文件，永久清理遵守留存期规则；"
             "下载与预览按真实归属表鉴权；跨表共享附件标注实际归属；新增附件缺失诊断"},
    {"ver": "3.8", "date": "2026-10-03",
     "note": "数据体检/纠错/工作台统一规则注册表（checkup_rules）与问题工单表 issues：规则带 "
             "rule_id/定义版本/分类（阻断错误·事实待复核·缺失资料·格式待解析·允许的历史例外）/理由/"
             "适用表/修复方式；问题以 稳定记录标识+规则 去重（UNIQUE dedup），状态机 "
             "open/processing/resolved/deferred/exception（暂缓与例外须有理由），记录负责人/处理备注/"
             "时间/复核人/重开计数/末次检出；复核扫描：已解决复发自动重开、暂缓与例外只更新检出时间"
             "不重复生成工单、未再检出的未处理项按规则口径自动解决；不产生综合质量得分——按维度分别"
             "展示计数、分母与例外数"},
    {"ver": "3.7", "date": "2026-10-03",
     "note": "鉴定历史/证据模型：determinations 表按行累积历次鉴定（名称原文/规范接受名/属科级/"
             "cf.·aff.·sp. 限定符/鉴定人/日期/依据/人工确认 confirmed_by/名录版本 checklist_ver/"
             "名录收录状态 checklist_status[accepted·synonym·not_found·service_unavailable]/来源 source）；"
             "标本表新增 DetStatus（待鉴定·暂定·已复核·有争议，两维度之一：复核状态）与 "
             "DetLocked（人工锁定，后台名录校准跳过，有证据或已确认的鉴定亦不被覆盖）；"
             "存量行一次性回填（source=backfill，状态一律暂定、confirmed_by 为空——不推定其经过专家复核）；"
             "名录服务不可用与「未收录」分开报告，不再把服务故障当成分类错误"},
    {"ver": "3.6", "date": "2026-10-03",
     "note": "字段注册表（field_registry v1.0）与 raw 原文留存：field_raw 表记录解析前原文/规范值/解析规则版本；"
             "日期三分量精度（完整日/年月/仅年/未知，月13/日越界拒绝、闰日合法，严禁仅年补1月1日）；"
             "坐标歧义（如 30.03.965′N）入人工队列不自动转换；数量非负整数或「未知」不当 0；"
             "/api/fields/scan 只读扫描与 /api/fields/apply 人工确认写入"},
    {"ver": "3.5", "date": "2026-10-03",
     "note": "导入计划契约：import_plans（不可变预览计划：文件摘要/映射/空值策略/规则版本/"
             "目标行基础版本/逐行动作与原因，60 分钟有效期）与 import_mappings（按来源模板保存表头映射，"
             "表头摘要一致自动应用）；模板工作簿含说明与示例独立工作表"},
    {"ver": "3.4", "date": "2026-10-03",
     "note": "稳定引用：number_aliases 别名表（历史编号/合并重定向 → 稳定行 id，批次可撤销）"
             "与 ref_mismatch 失配处置表（匹配现有/待完善/历史未知，不猜填地点）；"
             "软删除保留编号占用、彻底清理后释放；导入重复合并键显式标注不再静默"},
    {"ver": "3.3", "date": "2026-10-03",
     "note": "edits 升级为统一审计事件模型：新增 op_id/parent_op_id/req_id/source/actor_type/user_perms/"
             "row_version_before/row_version_after/rules_versions/reason/evidence 列与 prev_hash/hash 摘要链；"
             "触发器强制审计事件不可修改/删除；存量行 evidence=legacy 不回填新元数据"},
    {"ver": "3.2", "date": "2026-10-02",
     "note": "新增 idempotency_keys 幂等键表（作用域=用户+请求ID，绑定操作类型与请求摘要）：同键同请求返回原结果、同键异请求拒绝；幂等结果与数据/日志同事务提交，覆盖提交成功但响应丢失的场景；键保留 24 小时"},
    {"ver": "3.1", "date": "2026-10-02",
     "note": "records 新增 row_version 递增修订号（服务器维护）：存量行迁移为初始版本 1，此后修改/删除/还原/导入/撤销/恢复每次 +1，供乐观锁（base_version 条件更新）与字段级三方合并使用"},
    {"ver": "3.0", "date": "2026-09-30",
     "note": "新增 schema_info 版本追踪表（本机制本身）；users.perms 三态权限列（✏可编辑/👁只读/✕不可见），存量账号按旧权限清单自动换算"},
    {"ver": "2.2", "date": "2026-09-29",
     "note": "edits 增加 batch_id 列（批次追溯：导入/批量操作/撤销整批留痕）；invite_codes 支持 taxon_group"},
    {"ver": "2.1", "date": "2026-09-29",
     "note": "boxes 标本盒表（号段登记与占用统计）"},
    {"ver": "2.0", "date": "2026-09-29",
     "note": "users.prefs 用户偏好（列显示/列宽/我的视图）；taxon_group 类群隔离；can_boxes 盒管理权限"},
    {"ver": "1.1", "date": "2026-09-28",
     "note": "users.role 与 tables 表级权限清单（JSON）；旧角色一次性换算"},
    {"ver": "1.0", "date": "2026-09-28",
     "note": "初始结构：users / sessions / records / edits / versions / attachments / invite_codes / api_tokens"},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tname TEXT NOT NULL,
    data TEXT NOT NULL,            -- JSON: {列名: 值}（仅自有列）
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_t ON records(tname, deleted);
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    tname TEXT NOT NULL,
    rid INTEGER,                   -- 对应 records.id（导入/恢复等汇总条目可为 NULL）
    rkey TEXT,                     -- 编辑当时的业务主键值（如 MHBU-ARA-xxxx）
    action TEXT NOT NULL,          -- create/update/delete/import/restore/version
    changes TEXT                   -- JSON，内容随 action 而定
);
CREATE INDEX IF NOT EXISTS idx_edits_t ON edits(tname);
CREATE INDEX IF NOT EXISTS idx_edits_rid ON edits(tname, rid);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tname TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    snapshot TEXT NOT NULL,        -- JSON: [{id, deleted, data}, ...]
    total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_versions_t ON versions(tname);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tname TEXT NOT NULL,
    rid INTEGER NOT NULL,
    fname TEXT NOT NULL,           -- 原始文件名
    stored TEXT NOT NULL,          -- 存储文件名（uploads/<tname>/<rid>/<stored>）
    size INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_row ON attachments(tname, rid);
CREATE TABLE IF NOT EXISTS boxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,     -- 如 Box1(20000 -20035)
    num_start INTEGER NOT NULL,
    num_end INTEGER NOT NULL,
    note TEXT,
    created_by TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    role TEXT NOT NULL,              -- admin / editor / specimen_editor / viewer
    note TEXT,
    max_uses INTEGER NOT NULL DEFAULT 0,   -- 0 = 不限次数
    used INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
"""


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=8000")
    return db


def now_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db_path(path):
    """对任意路径的 SQLite 库执行结构迁移（恢复演练/旧结构恢复用）。
    通过临时切换 DB_PATH 实现，不触碰生产库。"""
    global DB_PATH
    old = DB_PATH
    DB_PATH = path
    try:
        init_db()
    finally:
        DB_PATH = old


# 拥有鉴定历史模型的表（与 server.DET_TABLES 保持一致）
DET_TABLES = ("specimens", "mygalomorph", "dali", "swu")


def _wsc_species_low():
    """回填用：本地名录收录表（小写名 -> 原条目）。文件缺失/损坏返回 None（= 服务不可用）。"""
    import json
    d = os.environ.get("SPM_WSC_DIR") or APP_DIR
    try:
        with open(os.path.join(d, "wsc_species.json"), encoding="utf-8") as f:
            data = json.load(f)
        return {x["n"].lower(): x for x in data}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _backfill_determinations(conn):
    """存量行一次性回填鉴定历史（幂等，schema_info 标记 + 逐行 NOT EXISTS 双保险）。

    口径（不推定专家复核）：状态一律「暂定」、confirmed_by 留空、evidence 留空；
    只忠实转录已有 FinalSpeciesName/TemporarySpeciesName 与 DetBy/DetDate；
    名录收录状态按当时本地名录核对，名录文件不可用记 service_unavailable（不当作未收录）。"""
    import json
    conn.execute("CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT)")
    done = conn.execute("SELECT value FROM schema_info WHERE key='det_backfill'").fetchone()
    if done:
        return
    species = _wsc_species_low()
    ver = ""
    try:
        ver = open(os.path.join(APP_DIR, "wsc_last_sync.txt"), encoding="utf-8").read().strip()
    except OSError:
        pass
    rows = conn.execute(
        "SELECT id, tname, data FROM records WHERE deleted=0 "
        "AND tname IN (%s)" % ",".join("?" * len(DET_TABLES)), DET_TABLES).fetchall()
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        raw = str(d.get("FinalSpeciesName") or "").strip() or str(d.get("TemporarySpeciesName") or "").strip()
        if not raw:
            continue
        low = raw.lower()
        rec = species.get(low) if species is not None else None
        if rec:
            acc, fam, cks = rec["acc"], rec.get("f"), "accepted"
        elif species is None:
            acc, fam, cks = None, None, "service_unavailable"
        else:
            acc, fam, cks = None, None, "not_found"
        genus = (acc or raw).split()[0] if (acc or raw) else None
        has = conn.execute("SELECT 1 FROM determinations WHERE tname=? AND rid=? LIMIT 1",
                           (r["tname"], r["id"])).fetchone()
        if has:
            continue
        conn.execute(
            "INSERT INTO determinations(tname,rid,seq,name_raw,name_accepted,genus,family,"
            "det_status,det_detail,determiner,det_date,evidence,confirmed_by,"
            "checklist_ver,checklist_status,source,batch_id,created_by,created_at) VALUES("
            ":tname,:rid,1,:raw,:acc,:genus,:fam,'暂定',NULL,:detby,:detdate,"
            "NULL,NULL,:ver,:cks,'backfill',:bkey,'migration',:ts)",
            {"tname": r["tname"], "rid": r["id"], "raw": raw, "acc": acc, "genus": genus,
             "fam": fam, "detby": str(d.get("DetBy") or "").strip() or None,
             "detdate": str(d.get("DetDate") or "").strip() or None,
             "ver": ver or None, "cks": cks, "bkey": "backfill:" + r["tname"] + ":" + str(r["id"]),
             "ts": now_str()})
    conn.execute("INSERT OR REPLACE INTO schema_info(key, value) VALUES('det_backfill', ?)",
                 (SCHEMA_VERSION,))


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # 旧库迁移：users 增加 role 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'editor'")
        # 权限细化：users/invite_codes 增加可编辑表清单（JSON 数组，"*"=全部，空=只读）
        if "tables" not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN tables TEXT NOT NULL DEFAULT \'["*"]\'')
            # 旧角色一次性换算成表清单
            conn.execute("UPDATE users SET tables='[]' WHERE role='viewer'")
            conn.execute("UPDATE users SET tables='[\"specimens\"]' WHERE role='specimen_editor'")
        if "taxon_group" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN taxon_group TEXT NOT NULL DEFAULT ''")
        if "can_boxes" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN can_boxes INTEGER NOT NULL DEFAULT 0")
        if "can_composite" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN can_composite INTEGER NOT NULL DEFAULT 0")
        if "prefs" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN prefs TEXT NOT NULL DEFAULT '{}'")
        if "perms" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN perms TEXT NOT NULL DEFAULT '{}'")
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(edits)")}
        if ecols and "batch_id" not in ecols:
            conn.execute("ALTER TABLE edits ADD COLUMN batch_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edits_batch ON edits(batch_id)")
        # ---- v3.1：records.row_version 递增修订号（乐观锁 + 三方合并） ----
        # 存量行统一迁移为初始版本 1；此后所有变更路径 row_version = row_version + 1
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
        if rcols and "row_version" not in rcols:
            conn.execute("ALTER TABLE records ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE records SET row_version = 1 WHERE row_version IS NULL OR row_version < 1")
            conn.commit()
        # ---- v3.2：幂等键（用户+请求ID 作用域；绑定操作类型与请求摘要） ----
        conn.execute("""CREATE TABLE IF NOT EXISTS idempotency_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            req_id TEXT NOT NULL,
            op_type TEXT NOT NULL,
            req_digest TEXT NOT NULL,
            status INTEGER NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(username, req_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency_keys(created_at)")
        conn.execute("DELETE FROM idempotency_keys WHERE created_at < datetime('now','localtime','-1 day')")
        # ---- v3.3：edits 统一审计事件模型 ----
        ecols2 = {r[1] for r in conn.execute("PRAGMA table_info(edits)")}
        if ecols2:
            for col, dft in (("op_id", "''"), ("parent_op_id", "''"), ("req_id", "''"),
                             ("source", "''"), ("actor_type", "'human'"),
                             ("user_perms", "''"), ("row_version_before", "''"),
                             ("row_version_after", "''"), ("rules_versions", "''"),
                             ("reason", "''"), ("prev_hash", "''"), ("hash", "''"),
                             ("evidence", "'legacy'")):
                if col not in ecols2:
                    conn.execute(f"ALTER TABLE edits ADD COLUMN {col} TEXT NOT NULL DEFAULT {dft}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edits_op ON edits(op_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edits_hash ON edits(hash)")
        # ---- v3.4：稳定引用（别名 + 失配处置） ----
        conn.execute("""CREATE TABLE IF NOT EXISTS number_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tname TEXT NOT NULL,
            alias TEXT NOT NULL,
            rid INTEGER NOT NULL,
            canonical TEXT,
            reason TEXT,
            batch_id TEXT,
            created_by TEXT,
            created_at TEXT,
            UNIQUE(tname, alias)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ref_mismatch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tname TEXT NOT NULL,
            rid INTEGER NOT NULL,
            event_number TEXT,
            status TEXT NOT NULL,
            note TEXT,
            resolved_rid INTEGER,
            created_by TEXT,
            created_at TEXT
        )""")
        # ---- v3.7：鉴定历史/证据模型（determinations 表 + 存量行一次性回填） ----
        conn.execute("""CREATE TABLE IF NOT EXISTS determinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tname TEXT NOT NULL,
            rid INTEGER NOT NULL,
            seq INTEGER NOT NULL,          -- 同一行内的鉴定序号（从 1 递增，只增不改）
            name_raw TEXT NOT NULL,        -- 名称原文（鉴定/录入时的写法，含 cf. 等限定符）
            name_accepted TEXT,            -- 名录接受名（未收录/服务不可用时为空）
            genus TEXT,
            family TEXT,
            det_status TEXT,               -- 待鉴定/暂定/已复核/有争议（复核维度）
            det_detail TEXT,               -- cf./aff./sp. 等限定符说明
            determiner TEXT,
            det_date TEXT,
            evidence TEXT,                 -- 依据（文献/附件/备注；JSON 或文本）
            confirmed_by TEXT,             -- 人工确认人（空 = 未经专家复核）
            checklist_ver TEXT,            -- 核对时所用名录版本（如 WSC 同步日期）
            checklist_status TEXT,         -- accepted/synonym/not_found/service_unavailable
            source TEXT,                   -- manual/create/import/calibration/backfill
            batch_id TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_det_row ON determinations(tname, rid, seq)")
        _backfill_determinations(conn)
        # ---- v3.8：问题工单（体检/纠错/工作台统一规则引擎的持久层） ----
        conn.execute("""CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            rule_ver INTEGER NOT NULL,
            tname TEXT NOT NULL,
            rid INTEGER,                   -- 稳定行标识（键级问题如主键重复可为 NULL）
            rkey TEXT,                     -- 扫描当时的业务键快照
            dedup TEXT NOT NULL,           -- 去重指纹：tname|rule_id|rid 或 tname|rule_id|key:<值>
            fields TEXT,                   -- 相关字段清单（JSON 数组）
            vals_digest TEXT,              -- 相关字段值摘要（复发/修复判断）
            status TEXT NOT NULL DEFAULT 'open',  -- open/processing/resolved/deferred/exception
            detail TEXT,                   -- 发现详情 JSON（before/suggest/原因…）
            assigned_to TEXT,
            note TEXT,                     -- 处理备注（例外/暂缓必须写明理由）
            handled_by TEXT,
            handled_at TEXT,
            reviewer TEXT,
            reviewed_at TEXT,
            reopen_count INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(dedup)
        )""")
        # ---- v3.9：附件生命周期（摘要/媒体类型/说明/软删除） ----
        acols = {r[1] for r in conn.execute("PRAGMA table_info(attachments)")}
        if acols:
            for col, dft in (("sha256", "''"), ("mime", "''"), ("note", "''"),
                             ("deleted", "0"), ("deleted_at", "''")):
                if col not in acols:
                    conn.execute(f"ALTER TABLE attachments ADD COLUMN {col} TEXT NOT NULL DEFAULT {dft}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_att_deleted ON attachments(tname, rid, deleted)")
        # ---- v3.16：导出模板 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS export_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'personal',
            owner TEXT,
            base_template_id INTEGER,
            revision INTEGER NOT NULL DEFAULT 1,
            template_kind TEXT NOT NULL DEFAULT 'custom',
            tname TEXT NOT NULL DEFAULT 'specimens',
            row_identity TEXT NOT NULL DEFAULT 'record',
            one_to_many TEXT NOT NULL DEFAULT 'expand',
            columns_def TEXT NOT NULL,
            group_by TEXT, sort_by TEXT,
            sheet_name TEXT, title TEXT, footer TEXT,
            fmt TEXT NOT NULL DEFAULT 'xlsx',
            journal TEXT, article_type TEXT, author_guide_url TEXT,
            rules_checked_at TEXT, rules_source TEXT DEFAULT 'personal',
            project_default_for INTEGER,
            favorite INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS template_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            columns_def TEXT NOT NULL, changed_by TEXT, note TEXT,
            created_at TEXT NOT NULL)""")
        # ---- v3.15：协作编辑包 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS edit_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pkg_id TEXT NOT NULL UNIQUE,
            created_by TEXT NOT NULL,
            tname TEXT NOT NULL,
            allowed_cols TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )""")
        try:
            conn.execute("ALTER TABLE edit_packages ADD COLUMN pending_review TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.execute("""CREATE TABLE IF NOT EXISTS edit_package_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id INTEGER NOT NULL,
            tname TEXT NOT NULL,
            rid INTEGER NOT NULL,
            base_values TEXT NOT NULL,
            base_row_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(package_id, tname, rid)
        )""")
        # ---- v3.14：项目工作集 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fixed',
            purpose TEXT, owner TEXT, filters TEXT, stage TEXT,
            created_by TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS project_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            tname TEXT NOT NULL, rid INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            join_reason TEXT, added_by TEXT, added_at TEXT,
            remove_reason TEXT, removed_by TEXT, removed_at TEXT,
            project_note TEXT, task_status TEXT, sort_order INTEGER,
            UNIQUE(project_id, tname, rid)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projm ON project_members(project_id, status)")
        pmcols = {r[1] for r in conn.execute("PRAGMA table_info(project_members)")}
        if pmcols and "exclude_reason" not in pmcols:
            conn.execute("ALTER TABLE project_members ADD COLUMN exclude_reason TEXT NOT NULL DEFAULT ''")
        # ---- v3.13：文献引用与材料引证 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS bibliographic_reference (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stable_id TEXT UNIQUE,             -- 稳定标识（如 DOI/ holep；可空）
            doi TEXT,                          -- DOI（可空；无 DOI 老文献不虚构）
            title TEXT NOT NULL,
            authors TEXT, year TEXT, pages TEXT, plates TEXT,
            journal TEXT,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS material_citation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_id INTEGER NOT NULL,
            tname TEXT NOT NULL DEFAULT 'specimens',
            rid INTEGER NOT NULL,
            orig_text TEXT,                    -- 原文材料引用（论文中原文）
            orig_number TEXT,                  -- 原文材料编号（含历史编号）
            evidence_att INTEGER,              -- 证据附件（截图/PDF 页）
            frozen_spec_version INTEGER,       -- 引用时标本版本（冻结）
            frozen_event_digest TEXT,          -- 引用时采集信息摘要
            frozen_image_att INTEGER,          -- 引用时图像附件
            created_by TEXT, created_at TEXT NOT NULL,
            UNIQUE(ref_id, tname, rid)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_matcit ON material_citation(ref_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_matcit_row ON material_citation(tname, rid)")
        # 模式标本身份扩展：依据/确认人/确认时间/状态来源
        for t in ("specimens", "mygalomorph", "dali", "swu"):
            try:
                conn.execute(f"ALTER TABLE records ADD COLUMN type_basis TEXT NOT NULL DEFAULT ''")
                conn.execute(f"ALTER TABLE records ADD COLUMN type_confirmed_by TEXT NOT NULL DEFAULT ''")
                conn.execute(f"ALTER TABLE records ADD COLUMN type_confirmed_at TEXT NOT NULL DEFAULT ''")
                conn.execute(f"ALTER TABLE records ADD COLUMN type_status_source TEXT NOT NULL DEFAULT ''")
                break
            except Exception:
                pass
        # ---- v3.12：实验批次/板/孔/混池/条码 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS experiment_batch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL,
            sent_at TEXT, sent_version INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS plate (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            spec TEXT NOT NULL DEFAULT '96',   -- 96 / 384（config.PLATE_SPECS）
            batch_id INTEGER,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS plate_well (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_id INTEGER NOT NULL,
            row TEXT NOT NULL, col INTEGER NOT NULL,   -- 统一行列坐标（A1 起）
            kind TEXT NOT NULL DEFAULT 'sample',       -- sample/control/blank
            obj_table TEXT, obj_rid INTEGER,           -- 对象（如 uce 行）
            label TEXT, status TEXT,                   -- 异常状态（如 failed/qc_hold）
            note TEXT,
            created_by TEXT, created_at TEXT NOT NULL,
            UNIQUE(plate_id, row, col)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pool_member (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_id INTEGER NOT NULL, udi5 TEXT, udi7 TEXT,
            plate_id INTEGER, well_id INTEGER,
            created_by TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS barcode_pair (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udi5 TEXT, udi7 TEXT,
            i5_seq_raw TEXT NOT NULL, i7_seq_raw TEXT NOT NULL,
            i5_dir TEXT DEFAULT 'read1', i7_dir DEFAULT 'read2',
            source TEXT,                              -- 来源（台账/试剂表行）
            created_by TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS qc_measurement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            obj_table TEXT NOT NULL, obj_rid INTEGER NOT NULL,
            value TEXT NOT NULL, unit TEXT, method TEXT, instrument TEXT,
            adopted INTEGER NOT NULL DEFAULT 0, adopt_reason TEXT,
            created_by TEXT, created_at TEXT NOT NULL)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_well_pos ON plate_well(plate_id, row, col)")
        # ---- v3.11：标本流转事件 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            tname TEXT NOT NULL,
            rid INTEGER NOT NULL,
            qty TEXT,
            from_pos TEXT, to_pos TEXT,
            counterparty TEXT,
            due_date TEXT,
            note TEXT, ref TEXT,
            op_user TEXT NOT NULL,
            created_at TEXT NOT NULL,
            undo_of INTEGER,
            batch_id TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_row ON movements(tname, rid, kind)")
        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_records_hole ON records'
                         "(json_extract(data,'$.\"Box # (Start#-End#)\"'),"
                         " json_extract(data,'$.\"Position\"')) "
                         "WHERE tname='specimens' AND deleted=0 "
                         "AND json_extract(data,'$.\"Position\"') IS NOT NULL "
                         "AND json_extract(data,'$.\"Position\"') != ''")
        except sqlite3.OperationalError:
            pass  # 存量重复占位：保持可启动，应用层事务检查兜底
        # ---- v3.10：版本元数据与压缩格式 ----
        vcols = {r[1] for r in conn.execute("PRAGMA table_info(versions)")}
        if vcols:
            for col, dft in (("category", "'auto'"), ("description", "''"), ("protected", "0"),
                             ("source_op_id", "''"), ("schema_ver", "''"), ("tables_rev", "''"),
                             ("rules_ver", "''"), ("content_sha256", "''"), ("size_bytes", "0"),
                             ("format", "'legacy'"), ("deps", "'{}'")):
                if col not in vcols:
                    conn.execute(f"ALTER TABLE versions ADD COLUMN {col} TEXT NOT NULL DEFAULT {dft}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_rule ON issues(rule_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_row ON issues(tname, rid)")
        # ---- v3.6：字段解析 raw 原文留存 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS field_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tname TEXT NOT NULL,
            rid INTEGER NOT NULL,
            field TEXT NOT NULL,
            raw TEXT NOT NULL,
            canonical TEXT,
            rules_ver TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_field_raw ON field_raw(tname, rid)")
                # ---- v3.5：导入计划与映射模板 ----
        conn.execute("""CREATE TABLE IF NOT EXISTS import_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL UNIQUE,
            tname TEXT NOT NULL,
            username TEXT NOT NULL,
            mode TEXT NOT NULL,
            source TEXT NOT NULL,
            file_digest TEXT NOT NULL,
            file_name TEXT,
            mapping TEXT NOT NULL,
            plan TEXT NOT NULL,
            rules_versions TEXT,
            counts TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS import_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tname TEXT NOT NULL,
            template_name TEXT NOT NULL,
            header_digest TEXT NOT NULL,
            mapping TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT
        )""")
        trig = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='edits_no_update'")
        if not trig.fetchone():
            conn.execute("CREATE TRIGGER edits_no_update BEFORE UPDATE ON edits "
                         "BEGIN SELECT RAISE(ABORT, '审计事件不可修改'); END;")
            conn.execute("CREATE TRIGGER edits_no_delete BEFORE DELETE ON edits "
                         "BEGIN SELECT RAISE(ABORT, '审计事件不可删除'); END;")
        # ---- v3.2b：主键表达式唯一索引（并发编号竞态的存储层兜底） ----
        # 应用层查重（check_key_unique）存在检查-提交窗口；唯一索引保证同一表内
        # 相同主键值至多一行。存量数据已有重复的表跳过索引（保持可启动），由数据体检处理。
        try:
            import tables_def as _td
        except Exception:
            _td = None
        if _td:
            # 旧口径索引（未排除软删行）先删除重建：软删行不占用编号（复用策略），还原冲突由应用层 409
            for r0 in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND name LIKE 'idx_records_key_%'"):
                if r0["sql"] and "deleted=0" not in r0["sql"]:
                    conn.execute("DROP INDEX " + r0["name"])
            for t, c in _td.TABLES.items():
                key = c.get("key")
                if not key or c.get("key_not_unique"):
                    continue
                idx = "idx_records_key_" + t
                have = {r[1] for r in conn.execute("PRAGMA index_list(records)")}
                if idx in have:
                    continue
                try:
                    conn.execute(
                        "CREATE UNIQUE INDEX " + idx + " ON records"
                        "(json_extract(data, '$.\"" + key.replace('"', "") + "\"')) "
                        "WHERE tname='" + t.replace("'", "") + "' AND deleted=0 "
                        "AND json_extract(data, '$.\"" + key.replace('"', "") + "\"') IS NOT NULL")
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    pass  # 存量重复/表达式不支持：保持可启动，应用层查重继续兜底
        conn.commit()
        icols = {r[1] for r in conn.execute("PRAGMA table_info(invite_codes)")}
        if icols and "tables" not in icols:
            conn.execute('ALTER TABLE invite_codes ADD COLUMN tables TEXT NOT NULL DEFAULT \'["*"]\'')
            conn.execute("UPDATE invite_codes SET tables='[]' WHERE role='viewer'")
            conn.execute("UPDATE invite_codes SET tables='[\"specimens\"]' WHERE role='specimen_editor'")
        if icols and "taxon_group" not in icols:
            conn.execute("ALTER TABLE invite_codes ADD COLUMN taxon_group TEXT NOT NULL DEFAULT ''")
        # 数据库结构版本追踪
        conn.execute("CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO schema_info(key, value) VALUES('schema_version', ?)",
                     (SCHEMA_VERSION,))
        conn.execute("INSERT OR REPLACE INTO schema_info(key, value) VALUES('schema_updated_at', ?)",
                     (now_str(),))
        # ---- v3.18：自定义组合表（定义 + 修订历史） ----
        conn.execute("""CREATE TABLE IF NOT EXISTS composite_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ct_id TEXT NOT NULL UNIQUE,          -- 稳定标识 ct_xxxxxxxx（改名列/配置不影响）
            name TEXT NOT NULL,
            description TEXT,
            nav_group TEXT NOT NULL DEFAULT '组合表',
            owner TEXT NOT NULL,                 -- 创建者（默认可管理自己的定义）
            base_table TEXT NOT NULL,            -- 主数据源（行粒度所在表）
            config TEXT NOT NULL,                -- JSON: {joins, fields, derived, default_filters, default_sort, options}
            status TEXT NOT NULL DEFAULT 'draft',   -- draft / published / disabled
            revision INTEGER NOT NULL DEFAULT 1,    -- 配置修订（并发修改保护）
            permissions TEXT NOT NULL DEFAULT '{}', -- 每表访问授权 {username: r|rw}（管理员维护）
            published_at TEXT,
            updated_by TEXT, updated_at TEXT,
            created_by TEXT, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS composite_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ct_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            config TEXT NOT NULL,                -- 完整定义快照（恢复旧配置只恢复表定义）
            changed_by TEXT, note TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ctrev ON composite_revisions(ct_id, revision)")
        # ---- 未来结构更新的迁移代码加在这里（按 SCHEMA_VERSION 递增）----
        conn.commit()
    finally:
        conn.close()
