# -*- coding: utf-8 -*-
"""数据表注册表 —— 新增其他目（蝎目、伪蝎目、蜱螨目……）的表只需改这个文件。

快速扩表（三步）：
  1. 复制文件末尾的 scorpiones 示例段，改 label（页签名）和 enabled=True；
  2. 重启服务（python server.py）—— 新页签自动出现，编辑/筛选/版本/导入导出/
     附件/回收站/变更历史全部自动生效；
  3. 用页面上的「导入 Excel」灌入该目数据，或参照 seed.py 扩展初始导入。

每张表支持的配置项：
  label          页签/按钮显示名
  key            业务主键列名（用于导入合并、by-key 接口、体检查重）
  columns        列定义（_cols 生成；linked 列实时从 linked_source 表带出）
  enabled        False = 下线该表（数据保留，页签消失）
  primary        True = 首页仪表盘图表的主表（只能有一张）
  linked_source  本表的关联列从哪张表取值（如 specimens 的地名来自 events）
  genus_lookup   True = 启用属名 WSC 预测 + 分类链自动带出（蜘蛛目适用；
                 其他目请换用对应目名录后再开启）
  geo_normalize  True = 国家/省份/城市自动标准化为官方地名（地名来源表启用）
  cascade_from   分类级联下拉的参照表（ specimens 用 taxonomy）
  genus_col / temp_species_col / final_species_col
                 本表的属名/暂定名/最终名列名。标本表用 TemporarySpeciesName、
                 FinalSpeciesName，而 DNA 提取与 UCE 台账存的是 Temporary Name、
                 Species name、Final Name——联想与名录核对一律经 taxon_cols() 解析，
                 不假设各表同名（缺省即标本表列名）。
  groups         行详情抽屉的字段分组排版（不填则单组平铺）
  key_not_unique 主键允许重复（如分类层级表）
"""

# 标本信息 —— 自有列（前段，20260928 版：A..AA）
SPECIMEN_OWN_1 = [
    "MHBU #",
    "Box # (Start#-End#)",
    "Position",
    "DNAVoucherID",
    "Notes",
    "Order",
    "Infraorder",
    "Family",
    "Subfamily",
    "Clade1",
    "Clade2",
    "Clade3",
    "Tribe",
    "Subtribe",
    "Genus",
    "TemporarySpeciesName",
    "FinalSpeciesName",
    "TaxonomyComment",
    "DetBy",
    "DetDate",
    "DetStatus",
    "DetLocked",
    "PreserveCondition(75%, 85%, 95%, 100%)",
    "TypeSpecimens",
    "AdultMale",
    "AdultFemale",
    "Subadult",
    "Juvenile",
    "SpecimenCondition (good/ok/bad)",
    "LivingPhotoNumber",
    "CollectionEventNumber",
]

# 标本信息 —— 关联列（原表 AH..AZ，VLOOKUP 自采集地信息）：列名 -> 采集地表列名
SPECIMEN_LINKED = {
    "OtherVoucherCode": "OtherVoucherCode",
    "FullCollectionData": "FullCollectionData",
    "Country": "Country",
    "Province": "Province",
    "City": "City",
    "County": "County",
    "Locality": "Locality",
    "Latitude1": "Latitude1",
    "Latitude2": "Latitude2",
    "Longitude1": "Longitude1",
    "Longitude2": "Longitude2",
    "Elevation1": "Elevation1",
    "Elevation2": "Elevation2",
    "Collectors": "Collector",  # 注意：标本表叫 Collectors，采集地表叫 Collector
    "DD1": "DD1",
    "MM1": "MM1",
    "YYYY1": "YYYY1",
    "DD2": "DD2",
    "MM2": "MM2",
    "YYYY2": "YYYY2",
    "Habitat": "Habitat",
    "CollectionMethod": "CollectionMethod",
}

# 标本信息 —— 自有列（后段，20260928 版）
SPECIMEN_OWN_2 = [
    "SpecimenHabitat",
    "Acknowledgement",
    "Projects",
    "FinalHBUDepositLocality",
    "LoanSpecimenFrom",
    "LoanSpecimenTo",
]

# 长文本列（加宽显示、导出时加宽列）
WIDE_COLS = {
    "FullCollectionData",
    "TaxonomyComment",
    "Notes",
    "Projects",
    "Locality",
    "Habitat",
    "CollectionMethod",
    "Wrong Number in Publication #",
    "TemporarySpeciesName",
    "IdentifiedSpeciesName",
    "FinalSpeciesName",
    "Author, Date",
    "Collector",
    "Collectors",
    "SpecimenHabitat",
    "Acknowledgement",
    "Species name",
    "Publication",
    "Accession number",
    "Collecting data",
    "Post-Illumiprocessor/bbduk total bases (bp) [AE*150 or 3rd column from \"count.bbduk.statistics\"]",
}

# 数字列（排序按数值比较；录入严格校验必须为数字）
NUMERIC_HINT = {
    "AdultMale", "AdultFemale", "Subadult", "Juvenile",
    "DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2",
    "Elevation1", "Elevation2",
    # UCE 测序统计（值均为纯数字文本）
    "Raw reads number (# from _1.fq.gz *2)",
    "Post-Illumiprocessor/bbduk reads number (phyluce_assembly_get_fastq_lengths)",
    "SPAdes assembled contigs number",
    "Number of UCEs", "Total bp", "Mean length", "95 CI length",
    "Min length", "Max length", "Median length", "Number of contigs >1kb",
}

# 默认值 + 锁定枚举：这些字段不可自由键入，只能从选项中选择
DEFAULT_VALUES = {
    "TypeSpecimens": "no",
    "SpecimenCondition (good/ok/bad)": "good",
    "PreserveCondition(75%, 85%, 95%, 100%)": "95",
    "DetBy": "{{me}}",   # 鉴定人默认=当前账号（v4.03）；前端渲染时展开，仍可手动更改
}
ENUM_LOCKED = {"TypeSpecimens", "SpecimenCondition (good/ok/bad)", "PreserveCondition(75%, 85%, 95%, 100%)"}

# 建议值下拉（datalist：给出常用选项，仍允许保留/输入历史自定义值）
ENUM_OPTIONS = {
    "PreserveCondition(75%, 85%, 95%, 100%)": [75, 80, 85, 95, 100],
    "SpecimenCondition (good/ok/bad)": ["good", "ok", "bad"],
    "TypeSpecimens": ["no", "Holotype", "Paratype"],
    "IsTypeLocality": ["yes", "no"],
    "Sex": ["male", "female", "juvenile"],
    "remove <200bp contigs": ["是", "否"],
    "DetStatus": ["待鉴定", "暂定", "已复核", "有争议"],
    "DetLocked": ["no", "yes"],
}

# 分类级联列（Order -> Family -> Subfamily -> Genus）
CASCADE_COLS = ["Order", "Family", "Subfamily", "Genus"]

EVENTS_COLS = [
    "采集批次备注",  # 原表 A 列（原为合并单元格的批次标签，导入时已向下填充）
    "CollectionEventNumber",
    "OtherVoucherCode",
    "FullCollectionData",
    "Country",
    "Province",
    "City",
    "County",
    "Locality",
    "Latitude1",
    "Latitude2",
    "Longitude1",
    "Longitude2",
    "Elevation1",
    "Elevation2",
    "Collector",
    "DD1",
    "MM1",
    "YYYY1",
    "DD2",
    "MM2",
    "YYYY2",
    "Habitat",
    "CollectionMethod",
]

TAXONOMY_COLS = [
    "Order", "Infraorder", "Family", "SUBFAMILY",
    "CLADE1", "CLADE2", "CLADE3", "TRIBE", "SUBTRIBE", "GENUS",
]


# 列显示标签：存储键不变，界面/导出用标签展示（统一表格语言）
COLUMN_LABELS = {
    "MHBU #": "Specimen #",
    "PreserveCondition(75%, 85%, 95%, 100%)": "PreserveCondition",
    "Sample Code": "DNAVoucherID",
    "Species name": "Temporary Name",
}

# 导入表头别名（v4.32）：合作方文件里的惯用列名变体 → 本表存储列名。
# 精确/归一化/显示名匹配都未命中时使用，命中后按「renames」在导入预览中明示。
HEADER_ALIASES = {
    "pseudoscorpiones": {"活体照编号": "LivingPhotoNumber"},
}


def _cols(names, linked_map=None):
    out = []
    for n in names:
        c = {"name": n}
        if n in COLUMN_LABELS:
            c["label"] = COLUMN_LABELS[n]
        if linked_map and n in linked_map:
            c["linked"] = linked_map[n]
        if n in WIDE_COLS:
            c["wide"] = True
        if n in NUMERIC_HINT:
            c["numeric"] = True
        if n in ENUM_OPTIONS:
            c["options"] = ENUM_OPTIONS[n]
        if n in DEFAULT_VALUES:
            c["default"] = DEFAULT_VALUES[n]
        if n in ENUM_LOCKED:
            c["enum_locked"] = True
        if n in CASCADE_COLS:
            c["cascade"] = True
        out.append(c)
    return out


def _specimen_columns():
    return (_cols(SPECIMEN_OWN_1)
            + _cols(list(SPECIMEN_LINKED.keys()), SPECIMEN_LINKED)
            + _cols(SPECIMEN_OWN_2))


# 行详情抽屉的字段分组（specimens / 其他同构标本表通用）
SPECIMEN_GROUPS = [
    {"title": "编号与保存", "cols": ["MHBU #", "Box # (Start#-End#)", "Position", "DNAVoucherID",
                                      "PreserveCondition(75%, 85%, 95%, 100%)",
                                      "SpecimenCondition (good/ok/bad)", "FinalHBUDepositLocality"]},
    {"title": "分类信息（按属名自动匹配）", "cols": ["Order", "Infraorder", "Family", "Subfamily",
        "Clade1", "Clade2", "Clade3", "Tribe", "Subtribe", "Genus",
        "TemporarySpeciesName", "FinalSpeciesName", "TaxonomyComment", "DetBy",
        "DetDate", "DetStatus", "DetLocked"]},
    {"title": "模式类型与数量", "cols": ["TypeSpecimens", "AdultMale", "AdultFemale",
                                          "Subadult", "Juvenile"]},
    {"title": "采集信息（按采集事件号自动带出）", "cols": ["CollectionEventNumber",
        "OtherVoucherCode", "FullCollectionData", "Country", "Province", "City", "County",
        "Locality", "Latitude1", "Latitude2", "Longitude1", "Longitude2",
        "Elevation1", "Elevation2", "Collectors", "DD1", "MM1", "YYYY1", "DD2", "MM2",
        "YYYY2", "Habitat", "CollectionMethod"]},
    {"title": "备注与其他", "cols": ["Notes", "LivingPhotoNumber", "SpecimenHabitat",
                                      "Acknowledgement", "Projects",
                                      "LoanSpecimenFrom", "LoanSpecimenTo"]},
]


def _table(label, key, columns, nav="main", **opts):
    cfg = {"label": label, "key": key, "columns": columns, "enabled": True, "nav": nav}
    cfg.update(opts)
    return cfg


# ======================= 伪蝎目（拟暴伪蝎科） =======================
# 来源：普通标本-Pseudotyrannochthoniidae-拟暴伪蝎科-*.xlsx
# 保存条件原为小数（0.75/0.95/1），导入/编辑时归一为百分比；无采集编号的行照常入库。
PSEUDO_COLS = [
    "标本位置编号", "标本盒位置", "目", "科", "属", "种", "保存条件",
    "标本数量", "雄性数量", "雌性数量", "幼体数量",
    "采集编号", "OtherVoucherCode", "FullCollectionData", "Country", "Province",
    "City", "County", "Locality", "Latitude1", "Latitude2", "Longitude1",
    "Longitude2", "Elevation1", "Elevation2", "Collectors", "DD1", "MM1",
    "YYYY1", "DD2", "MM2", "YYYY2", "Habitat", "CollectionMethod",
    "LivingPhotoNumber", "备注",
]

PSEUDO_NUMERIC = {
    "保存条件", "标本数量", "雄性数量", "雌性数量", "幼体数量",
    "DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2", "Elevation1", "Elevation2",
}
PSEUDO_WIDE = {"FullCollectionData", "Locality", "Habitat", "CollectionMethod",
               "Collectors", "种", "备注"}

def _pseudo_cols():
    out = []
    own = {"标本位置编号", "标本盒位置", "目", "科", "属", "种", "保存条件",
           "标本数量", "雄性数量", "雌性数量", "幼体数量", "LivingPhotoNumber", "备注"}
    for n in PSEUDO_COLS:
        c = {"name": n}
        if n not in own:  # 其余列与采集地信息表列名一致，直接关联带出
            c["linked"] = n
        if n in PSEUDO_NUMERIC:
            c["numeric"] = True
        if n in PSEUDO_WIDE:
            c["wide"] = True
        if n == "保存条件":
            c["options"] = ["75", "80", "85", "95", "100"]  # 存储数字，展示带 %
            c["enum_locked"] = True
            c["default"] = "95"
        out.append(c)
    return out

PSEUDO_GROUPS = [
    {"title": "编号与保存", "cols": ["标本位置编号", "标本盒位置", "保存条件", "标本数量"]},
    {"title": "分类", "cols": ["目", "科", "属", "种"]},
    {"title": "数量", "cols": ["雄性数量", "雌性数量", "幼体数量"]},
    {"title": "采集信息（按采集编号自动带出）", "cols": PSEUDO_COLS[10:33]},
    {"title": "其他", "cols": ["LivingPhotoNumber", "备注"]},
]

# ======================= 原蛛下目（Mygalomorphae）组织/标本库 =======================
# 来源：MygalomorphSpecimensMHBU-KYU.xlsx
# 行主键为 KYUARA #（组织样本编号，全表唯一）；MHBU # 大多未分配（原 Excel 该列
# 混有人名/批次标签，导入时仅保留真正的 MHBU-ARA- 编号，其余清洗为空）。
# 采集信息 sheet 与跳蛛的采集地信息表同构，合并进共享 events 表。
MYGA_OWN = ["KYUARA #", "MHBU #", "TemporaryNumber", "TissueBodyPart",
            "KYU-TissueBox#number", "MYG-BOX#number",
            "DNA extracted [yes/no]", "DNAdataObtained", "DNAVoucherID", "MorphVoucherID",
            "LivingPhotoNumber", "SpecimenHabitat"]
MYGA_OWN_2 = ["PreserveCondition(75%, 85%, 95%, 100%)", "IsTypeSpecimens", "IsTypeLocality",
              "AdultMale", "AdultFemale", "Subadult", "Juvenile",
              "SpecimenCondition (good/ok/bad)"]
MYGA_OWN_3 = ["Order", "Infraorder", "Family", "Subfamily", "Clade1", "Clade2", "Clade3",
              "Tribe", "Subtribe", "Genus", "TemporarySpeciesName", "IdentifiedSpeciesName",
              "FinalSpeciesName", "Author, Date", "TaxonomyComment", "DetBy", "DetDate", "DetNotes",
              "DetStatus", "DetLocked"]

def _myga_cols():
    own = set(MYGA_OWN + MYGA_OWN_2 + MYGA_OWN_3)
    out = []
    for n in MYGA_OWN + MYGA_OWN_3[:6]:
        pass
    # 顺序：标识 → 组织/DNA → 分类 → 模式与保存 → 采集编号+关联列 → 借出等
    seq = (["KYUARA #", "MHBU #", "TemporaryNumber", "TissueBodyPart",
            "KYU-TissueBox#number", "MYG-BOX#number",
            "DNA extracted [yes/no]", "DNAdataObtained", "DNAVoucherID", "MorphVoucherID"]
           + ["Order", "Infraorder", "Family", "Subfamily", "Genus",
              "TemporarySpeciesName", "IdentifiedSpeciesName", "FinalSpeciesName",
              "DetBy", "DetDate", "TaxonomyComment"]
           + ["PreserveCondition(75%, 85%, 95%, 100%)", "IsTypeSpecimens",
              "AdultMale", "AdultFemale", "Subadult", "Juvenile",
              "SpecimenCondition (good/ok/bad)"]
           + ["CollectionEventNumber", "OtherVoucherCode", "FullCollectionData",
              "Country", "Province", "City", "County", "Locality",
              "Latitude1", "Latitude2", "Longitude1", "Longitude2",
              "Elevation1", "Elevation2", "Collectors",
              "DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2",
              "Habitat", "CollectionMethod"]
           + ["LivingPhotoNumber", "SpecimenHabitat"])
    for n in seq:
        c = {"name": n}
        if n not in own:
            c["linked"] = n
        if n in ("PreserveCondition(75%, 85%, 95%, 100%)", "AdultMale", "AdultFemale",
                 "Subadult", "Juvenile", "DD1", "MM1", "YYYY1", "DD2", "MM2", "YYYY2",
                 "Elevation1", "Elevation2"):
            c["numeric"] = True
        if n in ("FullCollectionData", "Locality", "Habitat", "CollectionMethod",
                 "Collectors", "TemporarySpeciesName", "IdentifiedSpeciesName",
                 "FinalSpeciesName", "TaxonomyComment", "TissueBodyPart"):
            c["wide"] = True
        if n == "PreserveCondition(75%, 85%, 95%, 100%)":
            c["options"] = ["75", "80", "85", "95", "100"]  # 存储数字，展示带 %
            c["enum_locked"] = True
            c["default"] = "95"
        out.append(c)
    return out

MYGA_GROUPS = [
    {"title": "样本标识与组织", "cols": ["KYUARA #", "MHBU #", "TemporaryNumber",
                                          "TissueBodyPart", "KYU-TissueBox#number", "MYG-BOX#number",
                                          "DNA extracted [yes/no]", "DNAdataObtained", "DNAVoucherID"]},
    {"title": "分类与鉴定", "cols": ["Order", "Infraorder", "Family", "Subfamily", "Genus",
                                      "TemporarySpeciesName", "IdentifiedSpeciesName", "FinalSpeciesName",
                                      "DetBy", "DetDate", "DetStatus", "DetLocked"]},
    {"title": "保存状态", "cols": ["PreserveCondition(75%, 85%, 95%, 100%)",
                                    "SpecimenCondition (good/ok/bad)", "AdultMale", "AdultFemale",
                                    "Subadult", "Juvenile"]},
    {"title": "采集信息（按采集事件号自动带出）", "cols": ["CollectionEventNumber",
        "OtherVoucherCode", "FullCollectionData", "Country", "Province", "City", "County",
        "Locality", "Latitude1", "Longitude1", "Elevation1", "Collectors",
        "YYYY1", "Habitat", "CollectionMethod"]},
]

# ======================= 分子实验（UCE 文库） =======================
# 来源：UCE.library.master.*.xlsx 的 Sheet1（42 个有效列）。
# 主键为合成列 Sample Key = "UCE library Code-Sample Code"（同一样本可能多次建库，
# 单用 Sample Code 不唯一；seed 导入时自动生成）。
UCE_COLS = [
    "Sample Key",
    "UCE library Code",
    "Qubit (ng/ul)",
    "Send Volume (ul)",
    "测序公司",
    "Sequencing stradegy",
    "Required data",
    "完成人",
    "Send date",
    "Sample Code",
    "Order",
    "Family",
    "Subfamily",
    "Tribe",
    "Genus",
    "Species name",
    "Final Name",
    "Accession number",
    "Publication",
    "Sex",
    "Tissue for DNA extraction",
    "CollectionEventNumber",
    "Collecting data",
    "Library preparation DNA input amount (ng)",
    "UCE probe set",
    "UCE capture library input amount (ng)",
    "UDI barcode number",
    "I5 barcode number",
    "I5 barcode sequence (正向)",
    "I7 barcode number",
    "I7 barcode sequence (正向)",
    "Receive result date",
    "Raw reads number (# from _1.fq.gz *2)",
    "Post-Illumiprocessor/bbduk reads number (phyluce_assembly_get_fastq_lengths)",
    "Post-Illumiprocessor/bbduk total bases (bp) [AE*150 or 3rd column from \"count.bbduk.statistics\"]",
    "SPAdes assembled contigs number",
    "Number of UCEs",
    "Total bp",
    "Mean length",
    "95 CI length",
    "Min length",
    "Max length",
    "Median length",
    "Number of contigs >1kb",
]

UCE_GROUPS = [
    {"title": "建库与送测", "cols": UCE_COLS[0:9]},
    {"title": "样本与分类", "cols": UCE_COLS[9:23]},
    {"title": "文库制备与条码", "cols": UCE_COLS[23:31]},
    {"title": "测序与组装结果", "cols": UCE_COLS[31:44]},
]

# ======================= DNA 提取台账（跳蛛系统） =======================
# 来源：JXZDNAVouchers-2026.10.2.xlsx Sheet1（1954 行，DNAVoucherID 全表唯一）。
# Excel 列名映射：MHBU_Number→Specimen #、CollectingNumber→CollectionEventNumber、
# UBC 临时号并入 Notes 前缀。
DNA_EXTRACT_COLS = [
    "Specimen #",
    "DNAVoucherID",
    "Project",
    "Notes",
    "Order",
    "Infraorder",
    "Family",
    "Subfamily",
    "Clade1",
    "Clade2",
    "Clade3",
    "Tribe",
    "Subtribe",
    "Genus",
    "Temporary Name",
    "Final Name",
    "Sex",
    "Tissue for DNA extraction",
    "PreserveCondition",
    "DNAExtractionTime",
    "DNA extraction Qubit (ng/ul)",
    "DNA library UDI#",
    "DNA library Volume(μL)",
    "DNA library Qubit浓度(ng/μL)",
    "Who did  library prep.",
    "CollectionEventNumber",
    "CollectingData",
    "Country",
    "HabitatCollectionMethod",
    "Acknowledgement",
    "Loan info",
]

DNA_EXTRACT_GROUPS = [
    {"title": "提取标识", "cols": ["Specimen #", "DNAVoucherID", "Project",
                                    "DNAExtractionTime", "Who did  library prep."]},
    {"title": "分类信息", "cols": ["Order", "Infraorder", "Family", "Subfamily",
                                    "Clade1", "Clade2", "Clade3", "Tribe", "Subtribe",
                                    "Genus", "Temporary Name", "Final Name"]},
    {"title": "样本与组织", "cols": ["Sex", "Tissue for DNA extraction",
                                      "PreserveCondition", "Notes"]},
    {"title": "DNA 定量与建库", "cols": ["DNA extraction Qubit (ng/ul)", "DNA library UDI#",
                                          "DNA library Volume(μL)", "DNA library Qubit浓度(ng/μL)"]},
    {"title": "采集与借出", "cols": ["CollectionEventNumber", "CollectingData", "Country",
                                      "HabitatCollectionMethod", "Acknowledgement", "Loan info"]},
]

# 伪蝎系统 DNA 提取台账（v4.23，用户以跳蛛表导出的表头模板提出：结构同跳蛛 DNA 提取表，
# 「Specimen #」改为「凭证标本位置」（伪蝎标本位置编号），另加 凭证标本盒位置/DNA保存位置/
# Sanger Data/Geniomic Data 四列。存储名与用户模板表头逐一相同，导入按名直映射零改名；
# 采集相关列同跳蛛表为快照文本列（events 表无同名列，不做实时关联）。
PSEUDO_DNA_EXTRACT_COLS = [
    "凭证标本盒位置",
    "凭证标本位置",
    "DNA保存位置",
    "DNAVoucherID",
    "Project",
    "Notes",
    "Order",
    "Infraorder",
    "Family",
    "Subfamily",
    "Clade1",
    "Clade2",
    "Clade3",
    "Tribe",
    "Subtribe",
    "Genus",
    "Temporary Name",
    "Final Name",
    "Sex",
    "Tissue for DNA extraction",
    "PreserveCondition",
    "DNAExtractionTime",
    "DNA extraction Qubit (ng/ul)",
    "DNA library UDI#",
    "DNA library Volume(μL)",
    "DNA library Qubit浓度(ng/μL)",
    "Who did  library prep.",
    "CollectionEventNumber",
    "CollectingData",
    "Country",
    "HabitatCollectionMethod",
    "Acknowledgement",
    "Loan info",
    "Sanger Data",
    "Geniomic Data",
]

PSEUDO_DNA_EXTRACT_GROUPS = [
    {"title": "凭证与提取标识", "cols": ["凭证标本盒位置", "凭证标本位置", "DNA保存位置",
                                          "DNAVoucherID", "Project", "DNAExtractionTime",
                                          "Who did  library prep."]},
    {"title": "分类信息", "cols": ["Order", "Infraorder", "Family", "Subfamily",
                                    "Clade1", "Clade2", "Clade3", "Tribe", "Subtribe",
                                    "Genus", "Temporary Name", "Final Name"]},
    {"title": "样本与组织", "cols": ["Sex", "Tissue for DNA extraction",
                                      "PreserveCondition", "Notes"]},
    {"title": "DNA 定量与建库", "cols": ["DNA extraction Qubit (ng/ul)", "DNA library UDI#",
                                          "DNA library Volume(μL)", "DNA library Qubit浓度(ng/μL)"]},
    {"title": "测序数据", "cols": ["Sanger Data", "Geniomic Data"]},
    {"title": "采集与借出", "cols": ["CollectionEventNumber", "CollectingData", "Country",
                                      "HabitatCollectionMethod", "Acknowledgement", "Loan info"]},
]

UDI_COLS = ["UDI#", "UDI-i5-seq."]
UDI7_COLS = ["UDI#", "UDI-i7-seq."]

# ======================= 跳蛛借入标本（大理 / 西南大学） =======================
# 两份台账同为 64 列 Specimen_info 格式（VLOOKUP 自 Collecting_info 的值已存为
# 单元格缓存），列集取并集：dali 有 Type、swu 有 SWU_note，其余 63 列一致。
# 采集信息 22 列与采集地信息表同名同构，走 linked 实时带出（原蛛表同款做法）。
LOAN_OWN_HEAD = [
    "Tempt #", "Box # (Start#-End#)", "FinalHBUDepositLocality",
    "DNAVoucherID", "MorphVoucherID",
    "Order", "Infraorder", "Family", "Subfamily", "Clade1", "Clade2", "Clade3",
    "Tribe", "Subtribe", "Genus",
    "TemporarySpeciesName", "IdentifiedSpeciesName", "FinalSpeciesName",
    "Author, Date", "TaxonomyComment", "DetBy", "DetDate", "DetNotes",
    "DetStatus", "DetLocked",
    "IsTypeLocality", "PreserveCondition(75%, 85%, 95%, 100%)", "IsTypeSpecimens",
    "Identified_by",
    "AdultMale", "AdultFemale", "Subadult", "Juvenile",
    "Type", "SpecimenCondition (good/ok/bad)", "CollectionEventNumber",
]
LOAN_OWN_TAIL = [
    "LivingPhotoNumber", "LoanSpecimenFrom", "LoanSpecimenTo", "LoanSpecimenNumber",
    "SentDate", "ReturnDate", "DrawingPhotos", "Acknowledgement", "SWU_note",
]


def _loan_cols():
    out = (_cols(LOAN_OWN_HEAD)
           + _cols(list(SPECIMEN_LINKED.keys()), SPECIMEN_LINKED)
           + _cols(LOAN_OWN_TAIL))
    for c in out:
        if c["name"] == "PreserveCondition(75%, 85%, 95%, 100%)":
            c["default"] = "95"
    return out


LOAN_GROUPS = [
    {"title": "编号与保存", "cols": ["Tempt #", "Box # (Start#-End#)", "FinalHBUDepositLocality",
                                      "DNAVoucherID", "MorphVoucherID",
                                      "PreserveCondition(75%, 85%, 95%, 100%)",
                                      "SpecimenCondition (good/ok/bad)"]},
    {"title": "分类与鉴定", "cols": ["Order", "Infraorder", "Family", "Subfamily", "Clade1",
                                      "Clade2", "Clade3", "Tribe", "Subtribe", "Genus",
                                      "TemporarySpeciesName", "IdentifiedSpeciesName",
                                      "FinalSpeciesName", "Author, Date", "TaxonomyComment",
                                      "DetBy", "DetDate", "DetNotes", "DetStatus", "DetLocked",
                                      "IsTypeLocality",
                                      "IsTypeSpecimens", "Identified_by", "Type"]},
    {"title": "数量", "cols": ["AdultMale", "AdultFemale", "Subadult", "Juvenile"]},
    {"title": "采集信息（按采集事件号自动带出）", "cols": ["CollectionEventNumber"]
        + list(SPECIMEN_LINKED.keys())},
    {"title": "借入与其他", "cols": ["LivingPhotoNumber", "LoanSpecimenFrom", "LoanSpecimenTo",
                                      "LoanSpecimenNumber", "SentDate", "ReturnDate",
                                      "DrawingPhotos", "Acknowledgement", "SWU_note"]},
]

ALL_TABLES = {
    "specimens": _table(
        "跳蛛: 河北大学标本信息", "MHBU #", _specimen_columns(),
        primary=True, sort=1,
        alcohol_col="PreserveCondition(75%, 85%, 95%, 100%)",
        type_field="TypeSpecimens",
        taxon_group="spider",
        taxon_group_label="跳蛛系统",
        add_first=["MHBU #", "Genus", "CollectionEventNumber", "TemporarySpeciesName",
                   "FinalSpeciesName", "PreserveCondition(75%, 85%, 95%, 100%)",
                   "TypeSpecimens", "AdultMale", "AdultFemale", "Subadult", "Juvenile",
                   "LivingPhotoNumber"],
        linked_key="CollectionEventNumber",
        linked_source="events",
        genus_lookup=True,
        cascade_from="taxonomy",
        loan_view=True,
        groups=SPECIMEN_GROUPS,
    ),
    "events": _table(
        "采集地信息", "CollectionEventNumber", _cols(EVENTS_COLS),
        sort=2, nav="more",  # 低频更新，收进“更多”菜单
        taxon_group="shared",  # 共享表：所有类群可见
        geo_normalize=True,
    ),
    "taxonomy": _table(
        "跳蛛分类信息", "GENUS", _cols(TAXONOMY_COLS),
        sort=3,
        nav="more",  # 低频更新，收进“更多”菜单
        taxon_group="spider",  # 内容为跳蛛分类（WSC 骨架），仅跳蛛系统可见
        key_not_unique=True,
    ),
    # ---------- 分子实验：UCE 文库（源自 UCE.library.master.*.xlsx） ----------
    "uce": _table(
        "UCE文库", "Sample Key", _cols(UCE_COLS),
        add_first=["Sample Key", "UCE library Code", "Sample Code", "Genus", "Species name", "Final Name"],
        sort=4,
        taxon_group="spider",
        groups=UCE_GROUPS,
        genus_lookup=True,          # 跳蛛文库：本表只 5 个分类列，带出时按自有列过滤
        cascade_from="taxonomy",
        temp_species_col="Species name", final_species_col="Final Name",
    ),
    "pseudoscorpiones": _table(
        "伪蝎等: 河北大学标本信息", "标本位置编号", _pseudo_cols(),
        sort=5,
        taxon_group="pseudo",
        taxon_group_label="伪蝎系统",
        linked_key="采集编号",
        genus_lookup=True,  # GBIF 通用属名联想（非 WSC）
        add_first=["标本位置编号", "保存条件", "标本数量", "目", "科", "属", "种",
                    "采集编号", "FullCollectionData"],
        required_fields=["标本位置编号", "保存条件", "标本数量"],
        require_one_of=["标本数量", "雄性数量", "雌性数量", "幼体数量"],
        key_prefix="Pset-",
        primary=True,
        wac_lookup=True,          # WAC 伪蝎名录联想（属/种/科联动）
        linked_source="events",   # 与跳蛛共用同一张采集地信息表
        alcohol_col="保存条件",
        groups=PSEUDO_GROUPS,
    ),
    "dna_extract": _table(
        "跳蛛: DNA 提取信息", "DNAVoucherID", _cols(DNA_EXTRACT_COLS),
        add_first=["DNAVoucherID", "Specimen #", "Project", "Sex",
                    "Tissue for DNA extraction", "DNAExtractionTime",
                    "DNA extraction Qubit (ng/ul)"],
        sort=4.5,
        taxon_group="spider",
        alcohol_col="PreserveCondition",
        groups=DNA_EXTRACT_GROUPS,
        required_fields=["DNAVoucherID"],
        genus_lookup=True,          # 跳蛛 DNA 台账：属名 WSC 联想 + 上级分类带出
        cascade_from="taxonomy",
        temp_species_col="Temporary Name", final_species_col="Final Name",
    ),
    "pseudo_dna_extract": _table(
        "伪蝎等: DNA 提取信息", "DNAVoucherID", _cols(PSEUDO_DNA_EXTRACT_COLS),
        add_first=["DNAVoucherID", "凭证标本位置", "凭证标本盒位置", "DNA保存位置",
                    "Project", "Sex", "Tissue for DNA extraction",
                    "DNAExtractionTime", "DNA extraction Qubit (ng/ul)"],
        sort=5.5,
        taxon_group="pseudo",
        taxon_group_label="伪蝎系统",
        alcohol_col="PreserveCondition",
        groups=PSEUDO_DNA_EXTRACT_GROUPS,
        required_fields=["DNAVoucherID"],
    ),
    "mygalomorph": _table(
        "原蛛: 河北大学标本信息", "KYUARA #", _myga_cols(),
        sort=6,
        type_field="IsTypeSpecimens",
        taxon_group="mygalomorph",
        taxon_group_label="原蛛系统",
        linked_key="CollectionEventNumber",
        add_first=["KYUARA #", "MHBU #", "Genus", "CollectionEventNumber",
                    "TemporarySpeciesName", "FinalSpeciesName",
                    "PreserveCondition(75%, 85%, 95%, 100%)",
                    "TissueBodyPart", "SpecimenCondition (good/ok/bad)"],
        required_fields=["KYUARA #"],
        genus_lookup=True,  # 原蛛也是蜘蛛，WSC 覆盖
        primary=True,
        linked_source="events",
        alcohol_col="PreserveCondition(75%, 85%, 95%, 100%)",
        groups=MYGA_GROUPS,
    ),
    "udi_i5": _table(
        "UDI i5 条码", "UDI#", _cols(UDI_COLS),
        sort=7, nav="more",
        taxon_group="spider",
    ),
    "udi_i7": _table(
        "UDI i7 条码", "UDI#", _cols(UDI7_COLS),
        sort=8, nav="more",
        taxon_group="spider",
    ),
    # ---------- 跳蛛借入标本：大理（杨自忠）/ 西南大学（张志升、倪义） ----------
    # 来源：YunnanDaliSpecimenInfo20260828_Junxia.xlsx（大理，YZZ- 系列，含 Type 列）、
    #       SouthwestUniversity.SalticidaeSpecimen.Info.20240913.倪义.xlsx（西南大学，
    #       ZZS- 系列，含 SWU_note 列；其 Collecting_info 的批次标题在独立的
    #       CollectionEvent 列）。两表共用列集（并集，缺项为空列）。
    # 采集信息列与共享采集地表同构（linked），标本引用到的采集事件已并入 events；
    # Higher_rank sheet 仅为来源参考，未并入共享分类表（WSC 骨架另行维护）。
    "dali": _table(
        "跳蛛: 大理大学标本信息", "Tempt #", _loan_cols(),
        sort=9,
        type_field="IsTypeSpecimens",
        taxon_group="spider",
        taxon_group_label="跳蛛系统",
        add_first=["Tempt #", "Genus", "CollectionEventNumber", "TemporarySpeciesName",
                    "FinalSpeciesName", "AdultMale", "AdultFemale", "Subadult", "Juvenile"],
        required_fields=["Tempt #"],
        genus_lookup=True,       # 跳蛛，WSC 覆盖
        cascade_from="taxonomy",
        linked_key="CollectionEventNumber",
        linked_source="events",
        alcohol_col="PreserveCondition(75%, 85%, 95%, 100%)",
        groups=LOAN_GROUPS,
    ),
    "swu": _table(
        "跳蛛: 西南大学标本信息", "Tempt #", _loan_cols(),
        sort=10,
        type_field="IsTypeSpecimens",
        taxon_group="spider",
        taxon_group_label="跳蛛系统",
        add_first=["Tempt #", "Genus", "CollectionEventNumber", "TemporarySpeciesName",
                    "FinalSpeciesName", "AdultMale", "AdultFemale", "Subadult", "Juvenile"],
        required_fields=["Tempt #"],
        genus_lookup=True,       # 跳蛛，WSC 覆盖
        cascade_from="taxonomy",
        linked_key="CollectionEventNumber",
        linked_source="events",
        alcohol_col="PreserveCondition(75%, 85%, 95%, 100%)",
        groups=LOAN_GROUPS,
    ),
    # ---------- 扩表示例：蝎目标本表（复制此段即可新增其他目） ----------
    # 开启方法：把 enabled=False 改为 True，重启服务即可。
    # 列结构与标本信息一致；如需不同的列，传入自定义列清单即可。
    "scorpiones": _table(
        "蝎目标本", "MHBU #", _specimen_columns(),
        enabled=False,            # 改为 True 即上线
        linked_source="events",   # 采集地名同样从采集地信息表带出
        genus_lookup=False,       # WSC 名录仅覆盖蜘蛛目；蝎目接入对应名录后再开启
        groups=SPECIMEN_GROUPS,
    ),
}

# 供服务端使用的"启用中"表；新增表只要 enabled=True 就会出现在页签与全部接口里
# 表定义版本：列结构 / 枚举 / 必填等定义变更时递增（快照契约记录恢复时的表定义版本）
TABLES_REV = "2026-10-07.1"

# ======================= 任务视图（v3.47） =======================
# 每张表可定义一组预设任务视图：列集合与顺序、固定列（横向滚动时保持可见的身份列）、
# 默认排序、解释文字。权限沿用所在表的三态权限（视图只是列的呈现方式，不新增数据权限）。
# freeze 为列名清单（按左→右固定）；columns 未列出的列默认隐藏（「列显示」可找回）；
# sort 为默认排序字段；restore 可一键恢复整表默认。
VIEWS = [
    # —— 跳蛛鉴定 ——
    {"id": "spider_det", "table": "specimens", "label": "跳蛛鉴定", "perm": "r",
     "desc": "鉴定主线：编号定身份，属/种/鉴定人/复核状态一眼可查；编号与属固定在左侧",
     "columns": ["MHBU #", "Genus", "TemporarySpeciesName", "FinalSpeciesName", "DetBy",
                 "DetStatus", "DetDate", "TypeSpecimens", "TaxonomyComment", "CollectionEventNumber"],
     "freeze": ["MHBU #", "Genus"], "sort": "MHBU #", "dir": "asc"},
    # —— 采集信息 ——
    {"id": "collect", "table": "specimens", "label": "采集信息", "perm": "r",
     "desc": "核对采集要素：事件号、产地、经纬度、采集人与日期（关联列来自采集地信息表）",
     "columns": ["MHBU #", "CollectionEventNumber", "Country", "Province", "City", "Locality",
                 "Latitude1", "Longitude1", "Elevation1", "Collectors", "DD1", "MM1", "YYYY1", "Habitat"],
     "freeze": ["MHBU #"], "sort": "MHBU #", "dir": "asc"},
    # —— 保存 / 盒位 ——
    {"id": "storage", "table": "specimens", "label": "保存与盒位", "perm": "r",
     "desc": "入库管理：盒号、盒内位置、保存浓度与标本完好度，取管/盘点用",
     "columns": ["MHBU #", "Box # (Start#-End#)", "Position", "PreserveCondition(75%, 85%, 95%, 100%)",
                 "SpecimenCondition (good/ok/bad)", "FinalHBUDepositLocality", "DNAVoucherID",
                 "AdultMale", "AdultFemale", "Subadult", "Juvenile"],
     "freeze": ["MHBU #", "Position"], "sort": "Box # (Start#-End#)", "dir": "asc"},
    # —— DNA 提取 ——
    {"id": "dna_ext", "table": "dna_extract", "label": "DNA 提取", "perm": "r",
     "desc": "提取台账：凭证编号、标本对应、组织部位、提取时间与浓度",
     "columns": ["DNAVoucherID", "Specimen #", "Genus", "Final Name", "Tissue for DNA extraction",
                 "DNAExtractionTime", "DNA extraction Qubit (ng/ul)", "Project", "Sex"],
     "freeze": ["DNAVoucherID"], "sort": "DNAVoucherID", "dir": "asc"},
    # —— UCE 文库 ——
    {"id": "uce_lib", "table": "uce", "label": "UCE 文库", "perm": "r",
     "desc": "建库与送测：样本键、建库批次、浓度送样、测序结果与 UCE 产出",
     "columns": ["Sample Key", "UCE library Code", "Sample Code", "Genus", "Species name",
                 "Qubit (ng/ul)", "Send date", "测序公司", "Number of UCEs", "Total bp",
                 "Accession number"],
     "freeze": ["Sample Key"], "sort": "Sample Key", "dir": "asc"},
    # —— 借入管理（大理/西南大学共用列结构） ——
    {"id": "loan_mgr", "table": "dali", "label": "借入管理", "perm": "r",
     "desc": "借入台账：来源、凭证、借出/归还日期与数量（大理、西南大学两表同构）",
     "columns": ["Tempt #", "Genus", "FinalSpeciesName", "LoanSpecimenFrom", "LoanSpecimenNumber",
                 "SentDate", "ReturnDate", "DNAVoucherID", "Type", "CollectionEventNumber"],
     "freeze": ["Tempt #"], "sort": "Tempt #", "dir": "asc"},
    {"id": "loan_mgr_swu", "table": "swu", "label": "借入管理", "perm": "r",
     "desc": "借入台账（西南大学）：来源、凭证、借出/归还日期与数量",
     "columns": ["Tempt #", "Genus", "FinalSpeciesName", "LoanSpecimenFrom", "LoanSpecimenNumber",
                 "SentDate", "ReturnDate", "DNAVoucherID", "Type", "CollectionEventNumber"],
     "freeze": ["Tempt #"], "sort": "Tempt #", "dir": "asc"},
    # —— 伪蝎鉴定（铺陈跳蛛鉴定视图经验，v4.20） ——
    {"id": "pseudo_det", "table": "pseudoscorpiones", "label": "伪蝎鉴定", "perm": "r",
     "desc": "鉴定主线：位置编号定身份，科/属/种与保存状态一眼可查；位置编号固定在左侧",
     "columns": ["标本位置编号", "科", "属", "种", "保存条件", "标本数量", "LivingPhotoNumber", "采集编号"],
     "freeze": ["标本位置编号"], "sort": "标本位置编号", "dir": "asc"},
    # —— 伪蝎采集信息 ——
    {"id": "pseudo_collect", "table": "pseudoscorpiones", "label": "采集信息", "perm": "r",
     "desc": "核对采集要素：采集编号、产地、经纬度、采集人与日期（关联列来自采集地信息表）",
     "columns": ["标本位置编号", "采集编号", "Country", "Province", "City", "Locality",
                 "Latitude1", "Longitude1", "Collectors", "DD1", "MM1", "YYYY1", "Habitat"],
     "freeze": ["标本位置编号"], "sort": "标本位置编号", "dir": "asc"},
    # —— 伪蝎 DNA 提取 ——
    {"id": "pseudo_dna_ext", "table": "pseudo_dna_extract", "label": "伪蝎 DNA 提取", "perm": "r",
     "desc": "伪蝎提取台账：凭证编号、对应标本位置与盒位、保存位置、组织、提取时间与浓度定量、测序数据",
     "columns": ["DNAVoucherID", "凭证标本位置", "凭证标本盒位置", "DNA保存位置", "Genus",
                 "Final Name", "Tissue for DNA extraction", "PreserveCondition",
                 "DNAExtractionTime", "DNA extraction Qubit (ng/ul)", "Sanger Data",
                 "Geniomic Data", "Project", "Sex"],
     "freeze": ["DNAVoucherID"], "sort": "DNAVoucherID", "dir": "asc"},
    # —— 原蛛鉴定 ——
    {"id": "myga_det", "table": "mygalomorph", "label": "原蛛鉴定", "perm": "r",
     "desc": "鉴定主线：组织编号定身份，属/临时名/已鉴定名/终名与鉴定人、鉴定日期一眼可查；编号与属固定在左侧",
     "columns": ["KYUARA #", "MHBU #", "Genus", "TemporarySpeciesName", "IdentifiedSpeciesName",
                 "FinalSpeciesName", "DetBy", "DetDate", "TaxonomyComment", "CollectionEventNumber"],
     "freeze": ["KYUARA #", "Genus"], "sort": "KYUARA #", "dir": "asc"},
    # —— 原蛛保存与组织 ——
    {"id": "myga_storage", "table": "mygalomorph", "label": "保存与组织", "perm": "r",
     "desc": "入库管理：组织部位、组织盒/标本盒、保存浓度与 DNA 提取状态，取管/盘点用",
     "columns": ["KYUARA #", "MHBU #", "TissueBodyPart", "KYU-TissueBox#number", "MYG-BOX#number",
                 "PreserveCondition(75%, 85%, 95%, 100%)", "DNA extracted [yes/no]", "DNAdataObtained"],
     "freeze": ["KYUARA #"], "sort": "KYUARA #", "dir": "asc"},
]


def table_views(tname):
    """某张表的预设任务视图（列名已对照当前列定义过滤，避免脏数据）。"""
    cfg = ALL_TABLES.get(tname) or {}
    valid = {c["name"] for c in cfg.get("columns", [])}
    out = []
    for v in VIEWS:
        if v["table"] != tname:
            continue
        v2 = dict(v)
        v2["columns"] = [c for c in v["columns"] if c in valid]
        v2["freeze"] = [c for c in v.get("freeze", []) if c in valid]
        out.append(v2)
    return out

TABLES = {k: v for k, v in ALL_TABLES.items() if v.get("enabled", True)}

# 不变式：linked_key 是本行存储的外键值（属于自有列），不是带出列。
# 个别类群早期把键列也打了 linked 标记，导致写入被「请到来源表修改」拦下（编辑门控只在前端放行不够），
# 这里统一剥掉键列的 linked 标记，使全部类群与跳蛛口径一致。
for _cfg in TABLES.values():
    _lk = _cfg.get("linked_key")
    if _lk:
        for _c in _cfg["columns"]:
            if _c["name"] == _lk and _c.get("linked"):
                _c.pop("linked", None)


# v4.21 表名带类群前缀（「跳蛛: 河北大学标本信息」等，用户要求：侧边栏同名表分不清，
# 且后续其它类群要加 FDNA 提取等表），当前六表名已唯一。本函数保留为同名守卫：未来
# 若再出现同名表，跨表清单（导航、导出全库工作表名、变更历史、全局搜索、统计按表
# 分布）自动按类群追加后缀区分；判重一律看全部启用表，避免因账号可见集合不同而让
# 同一张表在各处显示成不同名字。
def display_label(tname):
    cfg = TABLES.get(tname)
    if not cfg:
        return tname
    lab = cfg["label"]
    if sum(1 for c in TABLES.values() if c["label"] == lab) <= 1:
        return lab
    grp = (cfg.get("taxon_group_label") or "").replace("系统", "") or tname
    return f"{lab}（{grp}）"


def own_columns(tname):
    return [c["name"] for c in TABLES[tname]["columns"] if not c.get("linked")]


# ======================= 鉴定联动（v4.31） =======================
# UCE 文库的物种鉴定以 DNA 提取台账为权威来源，两表经
# UCE「Sample Code」（界面显示名 DNAVoucherID）= 提取台账「DNAVoucherID」关联。
#   - 写入联动：任一表修改 属名/暂定名/最终名，改了哪些字段就把新值推给关联行的
#     对应字段（只推非空值；目标行走正常更新通道——级联带出/审计/行版本/撤销全部生效）；
#   - 差异标记：非权威侧行的联动字段与权威行不一致（含本侧为空）时，读接口逐行
#     标注 dna_diff（前端徽标呈现，一键「以DNA提取表为准同步」可消除）；
#   - authority 指权威侧：「以DNA提取表为准」——差异基准与一键同步方向都在提取台账。
# fields 三元组：(逻辑名, DNA提取表列, UCE文库列)。两表科学名列不同名
# （Temporary Name vs Species name），联动/标记/报错文案一律经此解析。
IDENT_LINK = {
    "a": "dna_extract", "a_key": "DNAVoucherID",
    "b": "uce", "b_key": "Sample Code",
    "fields": [
        ("genus", "Genus", "Genus"),
        ("temp", "Temporary Name", "Species name"),
        ("final", "Final Name", "Final Name"),
    ],
    "authority": "a",
}


def ident_link(tname):
    """本表参与的鉴定联动配置：返回 {peer, self_key, peer_key, cols, authority,
    peer_label}；未参与联动返回 None。cols 为 {本表列名: 对方表列名}。"""
    lnk = IDENT_LINK
    if not lnk:
        return None
    if tname == lnk["a"]:
        peer, self_key, peer_key = lnk["b"], lnk["a_key"], lnk["b_key"]
        cols = {a: b for _n, a, b in lnk["fields"]}
        authority = lnk["authority"] == "a"
    elif tname == lnk["b"]:
        peer, self_key, peer_key = lnk["a"], lnk["b_key"], lnk["a_key"]
        cols = {b: a for _n, a, b in lnk["fields"]}
        authority = lnk["authority"] == "b"
    else:
        return None
    return {"peer": peer, "self_key": self_key, "peer_key": peer_key, "cols": cols,
            "authority": authority, "peer_label": display_label(peer)}


# 各表的科学名列名：标本表存 TemporarySpeciesName/FinalSpeciesName，而 DNA 提取台账与
# UCE 文库存 Temporary Name/Species name/Final Name。联想、名录核对与分类链带出统一经
# 此解析（写层与前端都从 /api/meta 拿同一份），不再假设所有表用标本表的列名。
_TAXON_DEFAULT_COLS = {"genus": "Genus", "temp": "TemporarySpeciesName", "final": "FinalSpeciesName"}


def taxon_cols(tname):
    cfg = TABLES.get(tname) or {}
    out = dict(_TAXON_DEFAULT_COLS)
    for key, opt in (("genus", "genus_col"), ("temp", "temp_species_col"), ("final", "final_species_col")):
        if cfg.get(opt):
            out[key] = cfg[opt]
    return out


def linked_columns(tname):
    return {c["name"]: c["linked"] for c in TABLES[tname]["columns"] if c.get("linked")}


# ======================= 数据字典：字段含义悬停提示 =======================
# 覆盖各表常用字段；未列出的字段悬停仅显示字段名本身。
COLUMN_DOCS = {
    # —— 编号 / 存储 ——
    "MHBU #": "河北大学博物馆标本号，主编号。格式 MHBU-ARA-+8 位数字，录入数字部分自动补前缀",
    "KYUARA #": "原蛛组织样本编号（KYU 系列），原蛛表主键，全表唯一",
    "DNAVoucherID": "DNA 凭证编号（JXZ 系列）。UCE 文库与 DNA 提取台账凭此编号互相关联（附件跨表共享）",
    "MorphVoucherID": "形态学凭证编号",
    "OtherVoucherCode": "其他凭证编号（采集地信息）",
    "Specimen #": "河北大学博物馆标本号（内部存储名 MHBU #），格式 MHBU-ARA-+8 位数字",
    "PreserveCondition": "保存液浓度（%）（内部存储名 PreserveCondition(75%, 85%, 95%, 100%)）",
    "Box # (Start#-End#)": "标本盒位置。按管理员登记的编号区间自动匹配，也可手动指定",
    "Final Name": "最终鉴定名（建议填 WSC 正式名）",
    "Position": "标本在盒内的位置坐标 [行,列]：按编号在盒内顺序自动推算（容量为平方数按 √容量 列数，否则按每行 10 列），盒或编号变更时自动重算，可手工修正",
    "标本盒位置": "伪蝎标本存放的盒子",
    "标本位置编号": "伪蝎标本在盒内的位置编号（如 A1、B10）",
    "KYU-TissueBox#number": "原蛛组织存放的 tissue box 编号",
    "MYG-BOX#number": "原蛛标本存放的盒子编号",
    "FinalHBUDepositLocality": "标本最终入库位置",
    "TemporaryNumber": "原蛛临时编号",
    # —— 分类 ——
    "Order": "目（蜘蛛目 Araneae / 伪蝎目 Pseudoscorpiones）",
    "Infraorder": "下目（如跳蛛salticid归属的网蛛亚目等）",
    "Family": "科。属名确定后自动带出",
    "Subfamily": "亚科。属名确定后自动带出",
    "Clade1": "进化支 1（分类信息表维护）",
    "Clade2": "进化支 2（分类信息表维护）",
    "Clade3": "进化支 3（分类信息表维护）",
    "Tribe": "族。属名确定后自动带出",
    "Subtribe": "亚族。属名确定后自动带出",
    "Genus": "属名。输入时按 WSC 联想；确定后自动带出科/亚科/族等上级阶元",
    "TemporarySpeciesName": "初步鉴定名（保留输入者原始写法，不随 WSC 变动）。输入时按 WSC 联想校验，可加 cf. 标记不确定鉴定",
    "IdentifiedSpeciesName": "已鉴定种名（原蛛/早期记录）",
    "FinalSpeciesName": "最终鉴定名（受控字段）：只接受 WSC 收录的名称，异名自动转为接受名，更新后属等阶元联动",
    "Author, Date": "命名人与命名年份",
    "TaxonomyComment": "分类学备注（鉴定变更缘由等）",
    "DetBy": "鉴定人",
    "DetDate": "鉴定日期",
    "DetNotes": "鉴定备注",
    "DetStatus": "鉴定复核状态（待鉴定/暂定/已复核/有争议）：与「名录是否收录」相互独立；"
                 "改为已复核时自动在历次鉴定中登记确认人",
    "DetLocked": "人工锁定（yes/no）：锁定后名录自动校准不再覆盖该行的鉴定结果"
                 "（历次鉴定中有证据或已确认的记录同样受保护）；手工修改不受影响",
    "IsTypeSpecimens": "是否为模式标本",
    "IsTypeLocality": "是否来自模式产地",
    "TypeSpecimens": "模式标本类型（Holotype/Paratype/Topotype 等；锁定枚举，选 no 表示非模式）",
    "目": "目。伪蝎系统当前为伪蝎目 Pseudoscorpiones（含其它目时按鉴定填写）",
    "科": "伪蝎的科（中文）",
    "属": "伪蝎的属（中文或拉丁属名）",
    "种": "伪蝎的种名",
    # —— 保存 / 状态 ——
    "PreserveCondition(75%, 85%, 95%, 100%)": "保存液浓度（%）。输入 0-1 小数自动转为百分比",
    "保存条件": "伪蝎保存液浓度（%）",
    "SpecimenCondition (good/ok/bad)": "标本完好程度：good / ok / bad（锁定枚举）",
    "TissueBodyPart": "用于提取 DNA 的组织部位（如附肢、步足）",
    "DNA extracted [yes/no]": "是否已提取 DNA",
    "DNAdataObtained": "是否已获得 DNA 数据",
    # —— 数量 / 性别 ——
    "AdultMale": "成体雄性数量",
    "AdultFemale": "成体雌性数量",
    "Subadult": "亚成体数量",
    "Juvenile": "幼体数量",
    "标本数量": "标本总数量",
    "雄性数量": "雄性标本数量",
    "雌性数量": "雌性标本数量",
    "幼体数量": "幼体标本数量",
    "LivingPhotoNumber": "活体照片编号",
    # —— 采集 ——
    "CollectionEventNumber": "采集事件编号，关联采集地信息表（一行为空时系统自动生成 M-xxxx 内部编号）",
    "采集编号": "伪蝎的采集事件编号",
    "Collector": "采集人（采集地信息表）",
    "Collectors": "采集人（标本表关联列，实时取自采集地信息）",
    "FullCollectionData": "完整采集标签原文（关联列）",
    "Country": "国家（关联列，自动标准化为官方地名）",
    "Province": "省/自治区（关联列，自动标准化）",
    "City": "市（关联列，自动标准化）",
    "County": "县（关联列）",
    "Locality": "具体地点（关联列）",
    "Latitude1": "纬度开始值（关联列）",
    "Latitude2": "纬度结束值（关联列）",
    "Longitude1": "经度开始值（关联列）",
    "Longitude2": "经度结束值（关联列）",
    "Elevation1": "海拔开始值（米，关联列）",
    "Elevation2": "海拔结束值（米，关联列）",
    "DD1": "采集日期-日（开始，关联列）",
    "MM1": "采集日期-月（开始，关联列）",
    "YYYY1": "采集日期-年（开始，关联列）",
    "DD2": "采集日期-日（结束，关联列）",
    "MM2": "采集日期-月（结束，关联列）",
    "YYYY2": "采集日期-年（结束，关联列）",
    "Habitat": "生境描述（关联列）",
    "CollectionMethod": "采集方法（如扫网、灯诱，关联列）",
    "SpecimenHabitat": "标本生境备注（自有列）",
    # —— 借出 / 项目 ——
    "LoanSpecimenFrom": "借出对象（借给谁/从谁处借入）",
    "LoanSpecimenTo": "归还日期（留空 = 尚未归还）",
    "LoanSpecimenFrom ": "借出起始日期",
    "LoanSpecimenNumber": "本次借入/借出的标本数量",
    "SentDate": "寄出日期",
    "ReturnDate": "应归还日期",
    "DrawingPhotos": "手绘图/图版编号",
    "Tempt #": "借入跳蛛标本临时编号：大理 YZZ- 系列、西南大学 ZZS 系列（两表主键）",
    "SWU_note": "西南大学台账备注列",
    "Identified_by": "鉴定人（借入台账原始记录）",
    "IsTypeLocality": "是否来自模式产地",
    "Projects": "所属项目（多个用分号分隔）",
    "Acknowledgement": "致谢/经费来源",
    "Notes": "备注",
    "备注": "备注",
    # —— UCE 文库 ——
    "Sample Code": "DNA 凭证编号（界面显示为 DNAVoucherID），与 DNA 提取台账 DNAVoucherID 对应，附件跨表共享",
    "UCE library Code": "建库批次编号（同一标本可多次建库，与 Sample Code 合成主键 Sample Key）",
    "Species name": "暂定物种名（界面显示为 Temporary Name）",
    "Publication": "相关文献",
    "Accession number": "序列提交号（SRA/GenBank）",
}
