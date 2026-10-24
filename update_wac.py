# -*- coding: utf-8 -*-
"""下载 GBIF Backbone 的 Pseudoscorpiones（伪蝎目）属/种名录到 wac_genera.json / wac_species.json。
文件格式与 WSC 名录一致：
  wac_genera.json:  [{"g": 属名, "f": 科, "syn": 是否异名, "acc": 接受属名(异名时)}]
  wac_species.json: [{"n": 种名, "acc": 接受名(异名时), "g": 属, "f": 科, "syn": 是否异名}]
运行：python update_wac.py
"""
import json
import os
import time
import urllib.parse
import urllib.request

BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
BASE = "https://api.gbif.org/v1/species/search"
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def get(url, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "specimen-manager/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def paged(params):
    offset = 0
    total = None
    while True:
        q = dict(params, offset=offset, limit=1000, datasetKey=BACKBONE)
        d = get(BASE + "?" + urllib.parse.urlencode(q))
        total = d.get("count", 0)
        res = d.get("results", [])
        if not res:
            break
        yield from res
        offset += len(res)
        if offset >= total:
            break
        time.sleep(0.3)


def main():
    order_key = 1161  # GBIF backbone: Pseudoscorpiones (ORDER)
    genera, species = [], []

    print("下载属级名录…")
    for r in paged({"highertaxon_key": order_key, "rank": "GENUS"}):
        name = r.get("canonicalName") or ""
        if not name:
            continue
        syn = r.get("taxonomicStatus", "ACCEPTED").upper() != "ACCEPTED"
        genera.append({
            "g": name,
            "f": r.get("family") or "",
            "syn": syn,
            "acc": (r.get("accepted") or "").replace(name, "").strip() or (r.get("accepted") or ""),
        })

    print("下载种级名录…")
    for rank in ("SPECIES", "SUBSPECIES"):
        for r in paged({"highertaxon_key": order_key, "rank": rank}):
            name = r.get("canonicalName") or ""
            if not name:
                continue
            syn = r.get("taxonomicStatus", "ACCEPTED").upper() != "ACCEPTED"
            acc = r.get("accepted") or ""
            species.append({
                "n": name,
                "acc": acc if syn and acc and acc.lower() != name.lower() else None,
                "g": r.get("genus") or "",
                "f": r.get("family") or "",
                "syn": syn,
            })

    # 属的去重（同属名可能在多个科历史组合出现）：保留同名首条
    seen_g, g_out = set(), []
    for g in genera:
        k = g["g"].lower()
        if k in seen_g:
            continue
        seen_g.add(k)
        g_out.append(g)
    # 种按名称+接受名去重
    seen_s, s_out = set(), []
    for s in species:
        k = (s["n"].lower(), (s["acc"] or "").lower())
        if k in seen_s:
            continue
        seen_s.add(k)
        s_out.append(s)

    with open(os.path.join(APP_DIR, "wac_genera.json"), "w", encoding="utf-8") as f:
        json.dump(g_out, f, ensure_ascii=False)
    with open(os.path.join(APP_DIR, "wac_species.json"), "w", encoding="utf-8") as f:
        json.dump(s_out, f, ensure_ascii=False)
    print(f"完成：属 {len(g_out)} / 种 {len(s_out)} → wac_genera.json / wac_species.json")


if __name__ == "__main__":
    main()
