# -*- coding: utf-8 -*-
"""字段语义目录（v3.56）：稳定 field_id → 各表内部键 / 双语名 / 定义 / 兼容表头。

原则：
- 按**业务含义**定义，不按相似字符串自动合并（Sample Code 与 Specimen # 同为"编号样"
  但语义不同，分别建档并在 notes 写明具体意义）。
- field_id 稳定不变：改显示名不影响数据身份、已存视图（存的是内部键）与历史。
- compatible_headers 为**表作用域**的旧表头别名，导入映射时仅在指定表内识别；
  同一别名在同一表内命中多个字段（碰撞）时停止自动映射，交人工选择。
- 列设置/详情/录入/导入映射/导出数据字典/历史展示共用本目录（COLUMN_DOCS 由本目录生成）。
"""

FIELD_DEFS = [
    {
        "field_id": "specimen_number",
        "label_zh": "标本编号", "label_en": "Specimen number",
        "definition": "博物馆正式标本编号，一条记录对应一份（组）标本实物；编号含年份与流水，前导零有意义",
        "examples": ["MHBU-ARA-00020001", "KYUARA#0497（原蛛表）", "YZZ-0001（大理借入）"],
        "dtype": "文本（按文本保存保护前导零）", "unit": None,
        "null_semantics": "主键列不允许为空；借入台账 Tempt # 同义",
        "attr": "raw", "source": "采集/入库时分配", "editable": "rw（表三态权限内可编辑）",
        "tables": {"specimens": "MHBU #", "dna_extract": "Specimen #",
                   "mygalomorph": "MHBU #", "dali": "Tempt #", "swu": "Tempt #"},
        "compatible_headers": {"dna_extract": ["MHBU_Number", "MHBU#", "标本号"],
                               "specimens": ["Specimen#", "标本编号"]},
        "notes": "dna_extract.Specimen # 的具体意义：提取时对应的标本编号；合作方样本为来源说明文字"
                 "（倪义编号/MaddisonUBC/WGS sample 等），不是编号、不做编号解析（见关系字典）",
    },
    {
        "field_id": "event_number",
        "label_zh": "采集事件号", "label_en": "Collection event number",
        "definition": "一次采集活动的稳定引用；标本经它关联产地/日期/采集人等采集要素",
        "examples": ["YNDH-19-06", "M-0007（系统生成的手填事件）", "XZGBJD-18-100"],
        "dtype": "文本", "unit": None,
        "null_semantics": "可为空（标本未关联采集事件，产地日期缺失）；空≠未知事件号",
        "attr": "raw（标本表引用，事件表为主）", "source": "采集记录 / 录入时生成",
        "editable": "rw", "tables": {"events": "CollectionEventNumber", "specimens": "CollectionEventNumber",
                                     "mygalomorph": "CollectionEventNumber", "dali": "CollectionEventNumber",
                                     "swu": "CollectionEventNumber", "pseudoscorpiones": "采集编号",
                                     "uce": "CollectionEventNumber"},
        "compatible_headers": {"pseudoscorpiones": ["采集事件号"]},
        "notes": "改号经 number_aliases 保持旧号可解析（稳定引用）",
    },
    {
        "field_id": "dna_voucher",
        "label_zh": "DNA 凭证号", "label_en": "DNA voucher ID",
        "definition": "实验室 DNA 提取凭证：DNA 提取台账主键；UCE 建库样本经它指向提取记录",
        "examples": ["JXZ0443", "KYU001", "DDK012"],
        "dtype": "文本", "unit": None,
        "null_semantics": "标本/UCE 行可为空（未提取）；空=暂无提取，不当作未匹配错误",
        "attr": "raw", "source": "提取登记时分配（JXZ=自产、KYU/DDK/TRW=合作方）",
        "editable": "rw", "tables": {"dna_extract": "DNAVoucherID", "specimens": "DNAVoucherID",
                                     "uce": "Sample Code", "dali": "DNAVoucherID", "swu": "DNAVoucherID"},
        "compatible_headers": {"uce": ["DNAVoucherID", "DNA凭证"]},
        "notes": "uce.Sample Code 的具体意义：该建库样本所用的 DNA 提取凭证号（界面显示名 DNAVoucherID）；"
                 "同一凭证可多次建库（一对多）；大小写/前缀差异只作候选别名，不自动合并",
    },
    {
        "field_id": "library_code",
        "label_zh": "文库编号", "label_en": "Library code",
        "definition": "UCE 建库批次号：同批次可含多个样本（批次↔样本一对多）",
        "examples": ["UCE001", "UCE002A"],
        "dtype": "文本", "unit": None,
        "null_semantics": "UCE 行主键的一部分（Sample Key=文库号-凭证号），不允许为空",
        "attr": "raw", "source": "建库实验登记", "editable": "rw",
        "tables": {"uce": "UCE library Code"},
        "compatible_headers": {},
        "notes": "Sample Key = UCE library Code-Sample Code 合成（内部主键，界面不直接编辑）",
    },
    {
        "field_id": "temp_species",
        "label_zh": "暂定名", "label_en": "Temporary / provisional name",
        "definition": "鉴定人的原始写法，含 cf./sp. nov. 等限定符；不随名录自动更名",
        "examples": ["Brettus cf. cingulatus", "Liphistius nabang sp. nov."],
        "dtype": "文本（学名语义：属种斜体、限定词正体）", "unit": None,
        "null_semantics": "可为空（未鉴定）；空=待鉴定，不是无种",
        "attr": "raw（证据在历次鉴定中）", "source": "鉴定人录入",
        "editable": "rw", "tables": {"specimens": "TemporarySpeciesName", "mygalomorph": "TemporarySpeciesName",
                                     "dali": "TemporarySpeciesName", "swu": "TemporarySpeciesName",
                                     "uce": "Species name", "dna_extract": "Temporary Name"},
        "compatible_headers": {"uce": ["Temporary Name"]},
        "notes": "未收录名称允许有理由保留（新种候选/历史名），历次鉴定记录核对状态",
    },
    {
        "field_id": "final_species",
        "label_zh": "最终名", "label_en": "Final (accepted) name",
        "definition": "受控最终鉴定名：只接受名录收录名，异名自动转接受名并联动属级分类",
        "examples": ["Abracadabrella elegans", "Brettus cingulatus"],
        "dtype": "文本（受控；名录核对状态在历次鉴定）", "unit": None,
        "null_semantics": "可为空（尚未定到种）；空不是错误",
        "attr": "raw + 推导（保存时名录核对/属联动产生连带字段）", "source": "鉴定确认 + WSC 名录",
        "editable": "rw（受控：名录服务不可用时明确 503，不当作未收录）",
        "tables": {"specimens": "FinalSpeciesName", "mygalomorph": "FinalSpeciesName",
                   "dali": "FinalSpeciesName", "swu": "FinalSpeciesName",
                   "uce": "Final Name", "dna_extract": "Final Name"},
        "compatible_headers": {},
        "notes": "名录收录状态与复核状态两个维度分离；历次鉴定保存原文/接受名/确认人",
    },
    {
        "field_id": "collectors",
        "label_zh": "采集人", "label_en": "Collectors",
        "definition": "采集活动执行人（多人分号分隔）；标本表为关联实时值，事件表为权威",
        "examples": ["杨自忠；张俊霞"],
        "dtype": "文本", "unit": None,
        "null_semantics": "可为空（采集人未记录）",
        "attr": "derived（标本表）/ raw（事件表）", "source": "采集标签 / 事件表带出",
        "editable": "rw（事件表）；标本表为关联只读展示", "tables": {"events": "Collector", "specimens": "Collectors"},
        "compatible_headers": {},
        "notes": "标本表 Collectors 关联自采集事件（来源图标标注）；请到采集地信息修改",
    },
    {
        "field_id": "preservation",
        "label_zh": "保存条件（浓度）", "label_en": "Preservation (ethanol %)",
        "definition": "标本保存液乙醇浓度百分比；小数写法自动 ×100 归一",
        "examples": ["95", "0.95→95", "100"],
        "dtype": "数字（75/85/95/100，锁定枚举）", "unit": "%",
        "null_semantics": "可为空（历史记录未记）；空≠100",
        "attr": "raw", "source": "入库登记",
        "editable": "rw（锁定枚举：非法值回落默认 95 并留痕）",
        "tables": {"specimens": "PreserveCondition(75%, 85%, 95%, 100%)",
                   "mygalomorph": "PreserveCondition(75%, 85%, 95%, 100%)",
                   "dali": "PreserveCondition(75%, 85%, 95%, 100%)",
                   "swu": "PreserveCondition(75%, 85%, 95%, 100%)",
                   "pseudoscorpiones": "保存条件", "dna_extract": "PreserveCondition"},
        "compatible_headers": {"dna_extract": ["保存浓度"]},
        "notes": "夹带文字（如「95%（死后泡）」）经一键纠错提取数字、备注移入 Notes",
    },
]


def get(fid):
    for d in FIELD_DEFS:
        if d["field_id"] == fid:
            return d
    return None


def by_table_key(tname, key):
    """内部键 → 字段定义（表作用域；同键不同义天然分开，因为逐表登记）。"""
    for d in FIELD_DEFS:
        if d["tables"].get(tname) == key:
            return d
    return None


def resolve_header(tname, header):
    """旧表头 → 内部键（表作用域别名）。碰撞（一个别名命中同表多个字段）返回 None 并给出碰撞清单。"""
    hits = []
    for d in FIELD_DEFS:
        aliases = d["compatible_headers"].get(tname) or []
        if header.strip().lower() in [a.strip().lower() for a in aliases]:
            hits.append(d)
    if len(hits) == 1:
        return {"field_id": hits[0]["field_id"], "key": hits[0]["tables"][tname],
                "definition": hits[0]["definition"]}
    if len(hits) > 1:
        return {"collision": [h["field_id"] for h in hits]}
    return None


def usage(tname=None):
    """字段使用情况：出现在哪些表、哪些体检规则（规则引用按内部键文本匹配）。"""
    import json as _json
    import checkup_rules as CR
    out = []
    for d in FIELD_DEFS:
        u = {"field_id": d["field_id"], "label_zh": d["label_zh"], "label_en": d["label_en"],
             "tables": sorted(d["tables"].keys()), "rules": []}
        for r in CR.RULES:
            text = _json.dumps(r["tables"], ensure_ascii=False)
            for tname2, key in d["tables"].items():
                if key in str(r.get("tables")) or key in str(r["rationale"]):
                    u["rules"].append(r["rule_id"])
                    break
        if tname and tname in d["tables"]:
            out.append(u)
        elif not tname:
            out.append(u)
    return out
