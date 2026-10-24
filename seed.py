# -*- coding: utf-8 -*-
"""把原 Excel（MasterSpecimenInfo*.xlsx）导入到 data.db。

用法：
    python seed.py [Excel文件路径]

不指定路径时，自动在本目录及其上级目录里找 MasterSpecimenInfo*.xlsx。
注意：会清空并重建 data.db 中的数据表（不影响账号表）。
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime

import openpyxl

import config
import db
import geo
from server import normalize_alcohol as server_norm_alcohol
from tables_def import PSEUDO_COLS, TABLES as _TABLES_DEF
from tables_def import EVENTS_COLS, SPECIMEN_LINKED, SPECIMEN_OWN_1, SPECIMEN_OWN_2, \
    TAXONOMY_COLS, UCE_COLS, UDI_COLS, UDI7_COLS

EXCEL_ERRORS = {"#REF!", "#N/A", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!"}
NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean(v):
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
        return int(v)  # 1.0 -> 1，避免表格里出现 2020.0
    return v


def row_nonempty(d):
    return any(v is not None and str(v).strip() != "" for v in d.values())


def find_excel(pattern):
    """匹配目录下（含上级）符合模式的 Excel；多个命中时取最新修改的那个。"""
    import re as _re
    root = os.path.dirname(os.path.abspath(__file__))
    up = os.path.dirname(root)
    best = None
    for d in (root, up):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if _re.match(pattern, name):
                p = os.path.join(d, name)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
    return best


def load_uce(wb):
    """读取 UCE 主表：生成合成主键 Sample Key = 文库号-样本号；丢弃 #N/A 残留行。"""
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [clean(h) for h in rows[0]]
    i_sc, i_lib = hdr.index("Sample Code"), hdr.index("UCE library Code")
    out = []
    for r in rows[1:]:
        d = {}
        for i, h in enumerate(hdr):
            if h is None:
                continue
            v = clean(r[i]) if i < len(r) else None
            if v is not None:
                d[h] = v
        sc = str(d.get("Sample Code") or "").strip()
        lib = str(d.get("UCE library Code") or "").strip()
        if not sc:  # 无样本号的行均为 #N/A 公式残留
            continue
        d = {k: v for k, v in d.items() if k in UCE_COLS}
        d["Sample Key"] = f"{lib}-{sc}" if lib else sc
        out.append(d)
    return out


def load_pseudo(wb):
    """伪蝎标本表：主键"标本位置编号"；保存条件小数归一为百分比；无采集编号照常入库。"""
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {}
        for i, h in enumerate(hdr):
            if h not in PSEUDO_COLS:
                continue
            v = clean(r[i]) if i < len(r) else None
            if v is not None:
                d[h] = v
        if not d.get("标本位置编号"):
            continue
        if d.get("保存条件") is not None:
            d["保存条件"] = server_norm_alcohol(d["保存条件"])
        out.append(d)
    return out


def load_myga(wb):
    """原蛛组织库：主键 KYUARA #；MHBU # 列只保留真正的 MHBU-ARA- 编号；
    保存条件小数归一为百分比。附带的“采集信息”sheet 合并进共享 events 表。"""
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h).strip() if h is not None else "" for h in rows[0]]
    n = max(i for i, h in enumerate(hdr) if h) + 1
    allowed = {c["name"] for c in _TABLES_DEF["mygalomorph"]["columns"]}
    import re as _re
    out = []
    for r in rows[1:]:
        d = {}
        for i in range(min(n, len(r))):
            h = hdr[i]
            if h not in allowed:
                continue
            v = clean(r[i])
            if v is not None:
                d[h] = v
        ky = str(d.get("KYUARA #") or "").strip()
        if not ky:
            continue
        # MHBU # 清洗：只保留 MHBU-ARA- 格式
        mh = str(d.get("MHBU #") or "").strip()
        if mh and not _re.match(r"^MHBU-ARA-\d{8}$", mh):
            d["MHBU #"] = None
        if d.get("PreserveCondition(75%, 85%, 95%, 100%)") is not None:
            d["PreserveCondition(75%, 85%, 95%, 100%)"] = server_norm_alcohol(
                d["PreserveCondition(75%, 85%, 95%, 100%)"])
        out.append(d)
    # 采集信息 sheet → events 结构（列名与 EVENTS_COLS 基本一致，首列为批次备注）
    ws2 = wb["采集信息"]
    rows2 = list(ws2.iter_rows(values_only=True))
    h2 = [clean(h) for h in rows2[0]]
    ev = []
    for ri, r in enumerate(rows2[1:], start=2):
        d2 = {}
        for i, h in enumerate(h2):
            if h is None or i >= len(EVENTS_COLS):
                continue
            v = clean(r[i])
            if v is not None:
                d2[h] = v
        geo.normalize_geo(d2)
        if row_nonempty(d2) and d2.get("CollectionEventNumber"):
            ev.append(d2)
    return out, ev


def load_udi(wb, sheet, cols):
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [clean(h) for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {}
        for i, h in enumerate(hdr):
            v = clean(r[i]) if i < len(r) else None
            if v is not None and h in cols:
                d[h] = v
        if d:
            out.append(d)
    return out


def build_merged_map(ws):
    """合并单元格区域 -> {(row, col): 左上角值}，用于向下/向右填充。"""
    m = {}
    for rng in ws.merged_cells.ranges:
        tl = ws.cell(rng.min_row, rng.min_col).value
        if tl is None:
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    m[(r, c)] = tl
    return m


def main():
    db.init_db()
    path = find_excel(r"MasterSpecimenInfo.*\.xlsx$") if len(sys.argv) <= 1 else sys.argv[1]
    print(f"读取 {path} （大文件约需 1-2 分钟）...")
    wb = openpyxl.load_workbook(path, data_only=True)  # 取公式的缓存计算值
    conn = db.connect()
    cur = conn.cursor()

    # ---------- 标本信息 ----------
    ws = wb["标本信息"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [clean(h) for h in rows[0]]
    n_col = max(i for i, h in enumerate(hdr) if h) + 1
    hdr = hdr[:n_col]
    spec_own = [c for c in SPECIMEN_OWN_1 if c in hdr] + [c for c in SPECIMEN_OWN_2 if c in hdr]
    # 20260928 版表头校验：前 27 列应与 SPECIMEN_OWN_1 一致
    assert hdr[:len(SPECIMEN_OWN_1)] == SPECIMEN_OWN_1, (
        "标本信息表头与预期不符：" + str(hdr[:len(SPECIMEN_OWN_1)]))
    specimens = []
    for r in rows[1:]:
        d = {hdr[i]: clean(r[i]) for i in range(n_col) if i < len(r)}
        d = {k: v for k, v in d.items() if k in spec_own}
        if d.get("PreserveCondition(75%, 85%, 95%, 100%)") is not None:
            d["PreserveCondition(75%, 85%, 95%, 100%)"] = server_norm_alcohol(
                d["PreserveCondition(75%, 85%, 95%, 100%)"])
        if row_nonempty(d):
            specimens.append(d)

    # ---------- 采集地信息（合并单元格向下填充） ----------
    ws = wb["采集地信息"]
    mmap = build_merged_map(ws)
    rows = list(ws.iter_rows(values_only=True))
    hdr2 = [clean(h) for h in rows[0]]
    events = []
    for ri, r in enumerate(rows[1:], start=2):
        vals = []
        for ci in range(len(EVENTS_COLS)):
            v = r[ci] if ci < len(r) else None
            if v is None and (ri, ci + 1) in mmap:  # openpyxl 列从 1 计
                v = mmap[(ri, ci + 1)]
            vals.append(clean(v))
        d = dict(zip(EVENTS_COLS, vals))
        geo.normalize_geo(d)  # 地名标准化（国家/省份/自治州）
        if row_nonempty(d):
            events.append(d)

    # ---------- 分类信息 ----------
    ws = wb["分类信息"]
    rows = list(ws.iter_rows(values_only=True))
    taxonomy = []
    for r in rows[1:]:
        d = {TAXONOMY_COLS[i]: clean(r[i]) for i in range(min(len(TAXONOMY_COLS), len(r)))}
        if row_nonempty(d):
            taxonomy.append(d)

    # ---------- 分子实验：UCE 文库（可选，文件存在即导入） ----------
    myga_path = find_excel(r"MygalomorphSpecimensMHBU-KYU\.xlsx$")
    myga, myga_ev = [], []
    myga_file = ""
    if myga_path:
        print(f"读取原蛛组织库 {myga_path} ...")
        m_wb = openpyxl.load_workbook(myga_path, data_only=True)
        myga, myga_ev = load_myga(m_wb)
        myga_file = os.path.basename(myga_path)
        m_wb.close()
        # 采集信息 sheet 里的事件号若不在共享 events 中，补充导入（跳蛛 events 导入之后）
        exist = {e.get("CollectionEventNumber") for e in events}
        added_ev = [e for e in myga_ev if e["CollectionEventNumber"] not in exist]
        events.extend(added_ev)
        print(f"  合并原蛛采集事件 {len(added_ev)} 条进共享采集地信息表")
    else:
        print("未找到 MygalomorphSpecimensMHBU-KYU.xlsx，跳过")
    pseudo_path = find_excel(r"普通标本-Pseudotyrannochthoniidae-.*\.xlsx$")
    pseudo = []
    pseudo_file = ""
    if pseudo_path:
        print(f"读取伪蝎标本表 {pseudo_path} ...")
        p_wb = openpyxl.load_workbook(pseudo_path, data_only=True)
        pseudo = load_pseudo(p_wb)
        pseudo_file = os.path.basename(pseudo_path)
        p_wb.close()
    else:
        print("未找到伪蝎标本 Excel，跳过")
    uce_path = find_excel(r"UCE\.library\.master.*\.xlsx$")
    uce, udi5, udi7 = [], [], []
    uce_file = ""
    if uce_path:
        print(f"读取分子实验表 {uce_path} ...")
        uce_wb = openpyxl.load_workbook(uce_path, data_only=True)
        uce = load_uce(uce_wb)
        udi5 = load_udi(uce_wb, "UDI_i5_seq", UDI_COLS)
        udi7 = load_udi(uce_wb, "UDI_i7_seq", UDI7_COLS)
        uce_file = os.path.basename(uce_path)
        uce_wb.close()
    else:
        print("未找到 UCE.library.master*.xlsx，跳过分子实验表")
    wb.close()

    # ---------- 写库 ----------
    print("清空旧数据...")
    cur.execute("DELETE FROM records")
    cur.execute("DELETE FROM edits")
    cur.execute("DELETE FROM versions")
    cur.execute("DELETE FROM attachments")

    def insert(tname, dicts):
        n = 0
        for d in dicts:
            cur.execute(
                "INSERT INTO records(tname,data,updated_at,updated_by,deleted) VALUES(?,?,?,? ,0)",
                (tname, json.dumps(d, ensure_ascii=False), NOW, "system"),
            )
            n += 1
            if n % 2000 == 0:
                print(f"  {tname}: {n} 行...")
        return n

    print("写入标本信息...")
    n1 = insert("specimens", specimens)
    print("写入采集地信息...")
    n2 = insert("events", events)
    print("写入分类信息...")
    n3 = insert("taxonomy", taxonomy)
    print("写入 UCE 文库...")
    n4 = insert("uce", uce)
    print("写入伪蝎标本...")
    n_p = insert("pseudoscorpiones", pseudo)
    print("写入原蛛组织库...")
    n_m = insert("mygalomorph", myga)
    print("写入 UDI 条码...")
    n5 = insert("udi_i5", udi5)
    n6 = insert("udi_i7", udi7)

    # 标本盒登记：从 Box 列扫描 BoxN(start-end) 格式
    import re as _re
    box_pat = _re.compile(r"^Box\s*(\d+)\s*[（(]\s*(\d+)\s*[-–]\s*(\d+)\s*[)）]\s*$")
    seen_boxes = {}
    for d in specimens:
        v = str(d.get("Box # (Start#-End#)") or "").strip()
        m = box_pat.match(v)
        if m and v not in seen_boxes:
            seen_boxes[v] = (int(m.group(2)), int(m.group(3)))
    cur.execute("DELETE FROM boxes")
    for name, (start, end) in sorted(seen_boxes.items(), key=lambda kv: kv[1][0]):
        cur.execute("INSERT INTO boxes(name,num_start,num_end,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (name, start, end, "存量导入", "system", NOW))
    print(f"  登记标本盒 {len(seen_boxes)} 个")

    # 校验报告
    key1 = [d.get("MHBU #") for d in specimens]
    dup1 = {k: c for k, c in Counter(key1).items() if c > 1 and k}
    ce_set = {d.get("CollectionEventNumber") for d in events if d.get("CollectionEventNumber")}
    miss_ce = [d.get("CollectionEventNumber") for d in specimens
               if d.get("CollectionEventNumber") and d["CollectionEventNumber"] not in ce_set]
    dup2 = {k: c for k, c in Counter(d.get("CollectionEventNumber") for d in events
                                     if d.get("CollectionEventNumber")).items() if c > 1}

    for tname, dicts, label, src_file in (
            ("specimens", specimens, "标本信息", path),
            ("events", events, "采集地信息", path),
            ("taxonomy", taxonomy, "分类信息", path),
            ("uce", uce, "UCE文库", uce_file),
            ("udi_i5", udi5, "UDI i5 条码", uce_file),
            ("udi_i7", udi7, "UDI i7 条码", uce_file),
            ("pseudoscorpiones", pseudo, "伪蝎标本", pseudo_file),
            ("mygalomorph", myga, "原蛛组织库", myga_file)):
        if not dicts:
            continue
        cur.execute(
            "INSERT INTO edits(ts,user,tname,rid,rkey,action,changes,batch_id) VALUES(?,?,?,NULL,NULL,?,?,?)",
            (NOW, "system", tname, "import",
             json.dumps({"_summary": f"初始导入 {len(dicts)} 行", "_file": os.path.basename(src_file)},
                        ensure_ascii=False), "B-INIT-" + tname),
        )
        snap = [{"id": r[0], "deleted": 0, "data": json.loads(r[1])} for r in cur.execute(
            "SELECT id, data FROM records WHERE tname=?", (tname,))]
        cur.execute(
            "INSERT INTO versions(tname,name,note,created_at,created_by,snapshot,total) "
            "VALUES(?,?,?,?,?,?,?)",
            (tname, "v1 初始导入", f"从 {os.path.basename(path)} 初始导入", NOW, "system",
             json.dumps(snap, ensure_ascii=False), len(snap)),
        )

    conn.commit()
    conn.close()
    print(f"""
导入完成：
  标本信息   {n1} 行（重复 MHBU# {len(dup1)} 个：{list(dup1)[:5]}）
  采集地信息 {n2} 行（重复 CollectionEventNumber {len(dup2)} 个：{list(dup2)[:5]}）
  分类信息   {n3} 行
  有 {len(miss_ce)} 行标本引用的采集事件号在采集地信息中不存在（关联列将显示为空）
每张表已建立版本快照 v1。现在可以运行 python server.py 启动服务。
""")


if __name__ == "__main__":
    main()
