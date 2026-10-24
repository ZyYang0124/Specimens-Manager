# -*- coding: utf-8 -*-
"""样本链关系引擎（v3.50）：采集事件 → 标本 → DNA 提取 → UCE 文库 → 测序 accession。

关系字典（基于 2026-10-03 生产库只读核查，详见 关系字典_样本链_2026-10-03.md）：
  DNAVoucherID      dna_extract 主键（1954 全唯一）：实验室 DNA 凭证号
                    （JXZ=自产 1806、KYU/DDK/TRW 等合作方前缀）；标本表同名列指向其提取凭证
  Specimen #        dna_extract 列：标本号（MHBU-ARA-…）；外部合作样本为说明文字
                    （倪义编号/MaddisonUBC/WGS sample 等）——不是编号，不做编号解析
  Sample Code       uce 列：建库样本的 DNA 凭证号（界面显示名 DNAVoucherID）
  Sample Key        uce 主键：UCE library Code-Sample Code 合成（2642 全唯一）
  UCE library Code  建库批次号（135 个批次；同凭证可多次建库 → 一对多）
  Accession number  测序提交号（SRR/DRR/ERR）；"-" 为占位、属名等为误填（格式待解析）

匹配原则：只有明确的值相等（含已确认的编号别名）才建立实体引用；
未匹配一律分类呈现（绝不自动选择非唯一匹配）：
  traced           记录可追溯（提取表有该凭证）
  alias_candidate  大小写/编号归一后唯一命中 → 仅作为候选供人工确认（确认后写入
                   number_aliases：原文/规则/确认人留痕，此后按别名匹配）
  pending          JXZ 系未收录（本实验室体系内，待补历史记录）
  external         凭证前缀从未在提取台账出现（合作方/外部来源样本）
  unknown          空值/占位符/无法归类
"""
import json
import re

import db as dbm

# 提取台账中出现过的凭证前缀 = 本实验室/合作体系（动态学习，不写死猜测）
_PLACEHOLDERS = {"-", "--", "—", "无", "unknown", "n/a", ""}
_ACC_RE = re.compile(r"^(SRR|DRR|ERR)[0-9]+$", re.I)


def _rows(conn, tname):
    out = []
    for r in conn.execute("SELECT id, data, deleted FROM records WHERE tname=?", (tname,)):
        if r["deleted"]:
            continue
        try:
            out.append({"id": r["id"], "data": json.loads(r["data"])})
        except (ValueError, TypeError):
            continue
    return out


def _aliases(conn, tname):
    """已确认的编号别名（原文 → 规范值）：确认人/规则留痕在 number_aliases。"""
    out = {}
    for r in conn.execute("SELECT alias, canonical, reason, created_by FROM number_aliases WHERE tname=?", (tname,)):
        if r["alias"] and r["canonical"]:
            out[r["alias"].strip().lower()] = {"canonical": r["canonical"].strip(),
                                               "reason": r["reason"] or "", "by": r["created_by"] or ""}
    return out


def _norm_num(s):
    """编号归一：去前导零的数字尾（JXZ0123 → jxz123），大小写不敏感。"""
    k = s.strip().lower()
    return re.sub(r"0*(\d+)$", r"\1", k)


def classify_voucher(conn, vid, extracts, aliases):
    """未匹配凭证分类（不改变数据，只产出候选）。"""
    v = (vid or "").strip()
    if not v or v.lower() in _PLACEHOLDERS:
        return "unknown", []
    if v in extracts:                             # 精确值相等 → 记录可追溯
        return "traced", []
    if v.lower() in aliases:                      # 已确认别名 → 直接按别名匹配（在 chain 中体现）
        return "traced", []
    low = {x.lower(): x for x in extracts}
    # 候选：大小写差异（唯一命中才成候选；绝不自动选择）
    cands = []
    if v.lower() in low:
        cands.append({"type": "case", "original": v, "canonical": low[v.lower()],
                      "basis": "大小写差异（唯一命中）"})
    # 候选：数字归一后唯一
    nk = _norm_num(v)
    hit = low.get(nk)
    if hit and hit.lower() != v.lower():
        cands.append({"type": "number", "original": v, "canonical": hit,
                      "basis": "编号归一（前导零差异，唯一命中）"})
    if cands:
        return "alias_candidate", cands
    # 本实验室体系（JXZ 系）→ 待补历史记录
    if v.upper().startswith("JXZ"):
        return "pending", []
    # 提取台账从未出现过的前缀 → 外部来源
    pref = re.match(r"^[A-Za-z]+", v)
    if pref and (pref.group(0).lower() not in {re.match(r"^[A-Za-z]+", x).group(0).lower()
                                               for x in extracts if re.match(r"^[A-Za-z]+", x)}):
        return "external", []
    return "unknown", []


def chain_for_voucher(conn, vid, visible, alias_map=None):
    """按凭证号构建样本链。只返回调用者可见表的节点；连接全部基于值匹配。"""
    extracts_t = "dna_extract" in visible
    uce_t = "uce" in visible
    spec_t = "specimens" in visible
    v = (vid or "").strip()
    extracts_all = _rows(conn, "dna_extract") if extracts_t else []
    # 引用建立用【精确值相等】；大小写/编号差异只作为候选（classify_voucher），绝不自动匹配
    # 已确认别名参与匹配：原文 → 规范值（别名留原文/规则/确认人）
    aliases = alias_map if alias_map is not None else _aliases(conn, "uce")
    effective = v
    if v.lower() in aliases:
        effective = aliases[v.lower()]["canonical"]
    extracts = [d for d in extracts_all if str(d["data"].get("DNAVoucherID") or "").strip() == effective]
    if effective != v:
        extracts += [d for d in extracts_all if str(d["data"].get("DNAVoucherID") or "").strip() == v]
    classification, candidates = classify_voucher(
        conn, v, [str(d["data"].get("DNAVoucherID") or "").strip() for d in extracts_all], aliases)
    libs = []
    accessions = []
    if uce_t:
        for d in _rows(conn, "uce"):
            if str(d["data"].get("Sample Code") or "").strip().lower() == v.lower():
                acc = str(d["data"].get("Accession number") or "").strip()
                libs.append({"rid": d["id"], "sample_key": d["data"].get("Sample Key"),
                             "library_code": d["data"].get("UCE library Code"),
                             "accession": acc or None,
                             "acc_kind": ("srr" if _ACC_RE.match(acc) else ("placeholder" if acc in _PLACEHOLDERS or acc == "-" else ("invalid" if acc else None)))})
                if _ACC_RE.match(acc):
                    accessions.append({"accession": acc.upper(), "sample_key": d["data"].get("Sample Key"),
                                       "species": d["data"].get("Species name") or "",
                                       "basis": "UCE 行 Sample Code=凭证 " + v})
    # 一对多实验过程：同一标本号下的其他提取凭证（同标本多次提取各持独立凭证）
    related = []
    if extracts:
        my_spec = {str(d["data"].get("Specimen #") or "").strip() for d in extracts
                   if str(d["data"].get("Specimen #") or "").startswith("MHBU")}
        related = [{"rid": d["id"], "voucher": d["data"].get("DNAVoucherID"),
                    "specimen": d["data"].get("Specimen #"),
                    "time": d["data"].get("DNAExtractionTime")}
                   for d in extracts_all
                   if str(d["data"].get("DNAVoucherID") or "").strip() != v
                   and str(d["data"].get("Specimen #") or "").strip() in my_spec]
    specimens = []
    if spec_t:
        for d in _rows(conn, "specimens"):
            if str(d["data"].get("DNAVoucherID") or "").strip().lower() == v.lower():
                specimens.append({"rid": d["id"], "key": d["data"].get("MHBU #"),
                                  "genus": d["data"].get("Genus"),
                                  "species": d["data"].get("FinalSpeciesName") or d["data"].get("TemporarySpeciesName")})
    matched = bool(extracts)
    if not matched and (libs or specimens):
        matched = True  # 文库/标本侧有同凭证记录也算可追溯（链的任一环有实体即可）
    return {
        "voucher": v, "matched": matched, "classification": classification,
        "candidates": candidates,
        "extracts": [{"rid": d["id"], "specimen": d["data"].get("Specimen #"),
                      "time": d["data"].get("DNAExtractionTime"),
                      "qubit": d["data"].get("DNA extraction Qubit (ng/ul)")} for d in extracts],
        "libraries": libs, "specimens": specimens, "accessions": accessions,
        "related_extracts": related,
        "note": "记录可追溯" if matched else "暂未匹配（" + classification + "）",
    }


def unmatched_stats(conn, visible):
    """UCE 未匹配分类计数（工作台/体检用）。只统计调用者可见的 uce/dna_extract。"""
    if "uce" not in visible:
        return {"total": 0, "by": {}}
    extracts_all = [str(d["data"].get("DNAVoucherID") or "").strip()
                    for d in _rows(conn, "dna_extract")] if "dna_extract" in visible else []
    aliases = _aliases(conn, "uce")
    by = {}
    total = 0
    seen = set()
    for d in _rows(conn, "uce"):
        v = str(d["data"].get("Sample Code") or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        cls, _ = classify_voucher(conn, v, extracts_all, aliases)
        if cls != "traced":
            total += 1
            by[cls] = by.get(cls, 0) + 1
    return {"total": total, "by": by}
