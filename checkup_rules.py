# -*- coding: utf-8 -*-
"""数据体检/纠错/工作台统一规则注册表（v3.45）。

checkup（数据体检）、fix/scan（一键修复）、workbench（工作台队列）共用同一份
规则定义：每条规则带 rule_id、定义版本 ver、严重度 severity、分类 category、
理由 rationale、适用表 tables、修复方式 fix、展示优先级 priority。

问题分类（category，两维度之外的处理口径，互斥）：
  blocking 阻断错误       —— 影响引用/唯一性/数据可信，须处理
  fact     事实待复核     —— 数据可能有误，需人工核对（不自动改）
  missing  缺失资料       —— 资料未录全（坐标/日期/事件/鉴定），进「资料完善队列」
  format   格式待解析     —— 值本身可机械解析/转换（可一键修复或明确不自动修）
  history  允许的历史例外 —— 历史数据按政策允许保留，只计数不计错

修复方式（fix）：
  auto:<handler>  可自动修复，走统一写入服务（预览 before/after → 单批次应用 → 可撤销）
  never           默认不自动修复（歧义数据必须人工判断，如歧义坐标）
  manual          无自动修复，人工处理

不含任何「综合质量得分」——得分无法给出科学解释；本注册表只分维度给出
计数、分母（该规则检查的行数）与例外数（暂缓/例外工单数），解释权在人。
"""

# ---------------------------------------------------------------- 分类元数据
CATEGORIES = [
    {"key": "blocking", "label": "阻断错误"},
    {"key": "fact", "label": "事实待复核"},
    {"key": "missing", "label": "缺失资料"},
    {"key": "format", "label": "格式待解析"},
    {"key": "history", "label": "允许的历史例外"},
]
CATEGORY_LABEL = {c["key"]: c["label"] for c in CATEGORIES}

# 展示优先级：阻断错误内按此顺序置顶（重复事件号 → 非空失配 → 空业务键 → 危险日期 → 编辑冲突）
PRIORITY = ["DUP_EVENT", "REF_MISSING", "EMPTY_KEY", "DEAD_DATE", "EDIT_CONFLICT"]

REGISTRY_VERSION = "2026-10-03.1"


def R(rule_id, ver, title, category, rationale, tables, fix, priority=100,
      queue=None, fix_note=None, auto_resolve="auto"):
    return {
        "rule_id": rule_id, "ver": ver, "title": title,
        "severity": category, "category": category, "category_label": CATEGORY_LABEL.get(category, category),
        "rationale": rationale, "tables": tables, "fix": fix,
        "fix_note": fix_note or "", "priority": priority, "queue": queue,
        # 扫描复核口径：auto=未再检出即自动解决；manual=需人工确认解决（默认 auto）
        "auto_resolve": auto_resolve,
    }


RULES = [
    # ---------------- 阻断错误（置顶） ----------------
    R("DUP_EVENT", 1, "采集事件号重复", "blocking",
      "同一采集事件号对应多行采集记录，标本引用与关联带出会有歧义；需合并或改正编号",
      ["events"], "manual", priority=1),
    R("DUP_KEY", 1, "业务主键重复（如重复标本号）", "blocking",
      "同表内主键出现在多行，导入合并、稳定引用与编号定位都会歧义；需核对实物后改正或合并",
      "all-keyed", "manual", priority=6),
    R("REF_MISSING", 1, "标本引用了不存在的事件号（非空失配）", "blocking",
      "标本的采集事件号在采集地信息表中不存在，省份/日期等关联列全部为空；须补建事件或改正引用",
      ["specimens", "mygalomorph", "dali", "swu", "pseudoscorpiones"], "manual", priority=2),
    R("EMPTY_KEY", 1, "业务主键为空", "blocking",
      "主键为空的行无法被引用、合并与定位（导入会跳过、稳定引用无法建立）",
      "all-keyed", "manual", priority=3),
    R("DEAD_DATE", 1, "危险日期（分量越界/起止倒置）", "blocking",
      "月>12、日>31 或开始晚于结束的日期在科学上不可能成立；新版录入已被拦截，存量行需人工改正",
      ["events"], "manual", priority=4),
    R("EDIT_CONFLICT", 1, "编辑冲突覆盖留痕（14 天内）", "blocking",
      "该行最近一次修改是「确认覆盖他人修改」（force），被覆盖的内容需复核是否误删",
      "all", "manual", priority=5),

    # ---------------- 事实待复核 ----------------
    R("GENUS_MISMATCH", 1, "属名与最终名属首不一致", "fact",
      "Genus 与 FinalSpeciesName 的属不相同，两者必有一处录入错误；确认后可按种名改属",
      ["specimens", "mygalomorph", "dali", "swu"], "auto:genus_sync"),
    R("WSC_NOT_FOUND", 1, "最终鉴定名未收录于本地 WSC 名录", "fact",
      "FinalSpeciesName 不在本地 WSC 种名录（53,586 种）中：可能是异名过期、拼写笔误、名录滞后或暂定名误填；逐条核对，确为新种应移入 TemporarySpeciesName 并留鉴定记录",
      ["specimens", "mygalomorph", "dali", "swu"], "manual"),
    R("GENUS_UNKNOWN", 1, "属名未收录（WSC 与分类表均查无）", "fact",
      "属名在 WSC 属级名录与本地分类信息表中都找不到——多为拼写笔误或未发表属；不自动改动",
      ["specimens", "mygalomorph", "dali", "swu"], "manual"),
    R("CF_REVIEW", 1, "cf./aff. 待复查", "fact",
      "暂定名带 cf./aff. 限定符，鉴定未完全确定；论文发表前需复核或请专家确认（可在历次鉴定中登记确认）",
      ["specimens", "mygalomorph", "dali", "swu"], "manual", queue="cf_review"),
    R("DET_DISPUTED", 1, "鉴定状态为「有争议」", "fact",
      "该行 DetStatus=有争议：存在未决议的分类学分歧，需专家仲裁并记录结论",
      ["specimens", "mygalomorph", "dali", "swu"], "manual"),
    R("COUNTRY_UNKNOWN", 1, "国家取值不在官方地名列表", "fact",
      "「国家」取值不在标准国家列表中（可能误填采集人/备注），需人工核对",
      ["events"], "manual"),
    R("GEO_RANGE", 1, "经纬度异常（非数字/超范围）", "fact",
      "经纬度非数字或超出物理范围（纬度 ±90 / 经度 ±180），常见于度分秒混入小数度；歧义坐标另列不混入本规则",
      ["events"], "manual"),
    R("DNA_ORPHAN", 1, "DNA 凭证断链", "fact",
      "UCE 已建库样本的 DNAVoucherID 在 DNA 提取台账无对应记录，溯源链断裂；需补台账或改正凭证号",
      ["uce"], "manual", queue="dna_gap"),

    # ---------------- 缺失资料（资料完善队列） ----------------
    R("DET_PENDING", 1, "待鉴定（有暂定名无最终名）", "missing",
      "已有初步鉴定但尚未确定最终名；属资料完善队列，不阻断任何操作",
      ["specimens", "mygalomorph", "dali", "swu"], "manual", queue="identify"),
    R("MISSING_EVENT", 1, "标本缺采集事件号", "missing",
      "未关联采集事件，产地/日期等采集要素缺失；补录事件号或补建历史事件（不猜填）",
      ["specimens", "mygalomorph", "dali", "swu", "pseudoscorpiones"], "manual", queue="no_event"),
    R("MISSING_COORD", 1, "采集事件缺坐标", "missing",
      "采集事件无经纬度；历史标本可声明例外保留，新采集事件应尽量补 GPS",
      ["events"], "manual"),
    R("DATE_INCOMPLETE", 1, "采集日期不完整", "missing",
      "有年份但缺月/日；历史标签确实只到年时可在工单声明例外（不反复提醒）",
      ["events"], "manual"),

    # ---------------- 格式待解析 ----------------
    R("FINAL_CASE", 1, "最终名属首小写", "format",
      "学名属首应大写，属拼写规范问题；可自动修复（不改含义）",
      ["specimens", "mygalomorph", "dali", "swu"], "auto:name_case"),
    R("COUNT_FMT", 1, "性别数量格式异常", "format",
      "数量列含非负整数以外的写法（如 1w、1/1）；能安全转整数的可自动修复，其余人工",
      ["specimens", "mygalomorph", "dali", "swu"], "auto:count_cast"),
    R("PRES_TEXT", 1, "保存浓度夹带文字", "format",
      "浓度列如「95%（死后泡）」：提取数字、备注移入 Notes，可自动修复",
      ["specimens", "mygalomorph", "dali", "swu"], "auto:pres_extract"),
    R("POS_MISSING", 1, "盒内位置坐标缺失（可推算）", "format",
      "按编号与盒号段可机械推算 Position；可自动补算",
      ["specimens"], "auto:pos_fill"),
    R("AMBIG_COORD", 1, "歧义坐标（无法判定度分）", "format",
      "如 30.03.965′N 多段数字无法判定度分秒——解析有歧义，默认绝不自动转换，须查原始标签人工录入",
      ["events"], "never", fix_note="歧义数据默认不修复：自动转换可能制造假坐标"),

    # ---------------- 允许的历史例外 ----------------
    R("HIST_ENUM", 1, "历史枚举值不在当前选项内", "history",
      "历史导入数据的锁定枚举列取值超出当前选项（如旧「Topotype」写法）；按历史数据兼容政策原样保留，只计数不计错",
      "all", "manual"),
]


def get(rule_id):
    for r in RULES:
        if r["rule_id"] == rule_id:
            return r
    return None


def rule_tables(rule, tables):
    """规则的适用表（解析 all/all-keyed 与清单），限定在传入的可见表内。"""
    t = rule["tables"]
    if t == "all":
        return list(tables)
    if t == "all-keyed":
        return [x for x in tables if tables[x].get("key") and not tables[x].get("key_not_unique")]
    return [x for x in t if x in tables]


def fix_handler(rule):
    """返回 (mode, handler_name)。mode: auto/never/manual。"""
    f = rule["fix"]
    if f.startswith("auto:"):
        return "auto", f.split(":", 1)[1]
    return f, None
