# -*- coding: utf-8 -*-
"""从 GBIF 分类骨架拉取全世界蜘蛛属级与种级名录（数据源即 WSC），存为
wsc_genera.json（属，用于属名建议）与 wsc_species.json（种，用于
FinalSpeciesName 校验：含 异名→接受名 映射与科属信息）。

用法：python update_wsc.py [--species]
  不带参数：只刷新属名录（快）
  --species：同时刷新种级名录（首次约 10-20 分钟；WSC 每年更新，建议每年跑一次）
"""
import json
import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter, Retry

BASE = "https://api.gbif.org/v1"
OUT_GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsc_genera.json")
OUT_SPC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsc_species.json")
ARANEAE_KEY = 1496

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "specimen-manager-wsc-sync/1.0"
RETRIES = Retry(total=6, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"])
SESSION.mount("https://", HTTPAdapter(max_retries=RETRIES))


def get(url, **params):
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=40)
            if r.status_code == 200:
                return r.json()
            raise RuntimeError(f"HTTP {r.status_code}")
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1) + 1)


def children(key):
    """枚举某分类节点下的直接子节点（GBIF 路径 /species/{key}/children）。"""
    offset = 0
    while True:
        batch = get(f"{BASE}/species/{key}/children", limit=1000, offset=offset)
        yield from batch.get("results", [])
        if batch.get("endOfRecords"):
            break
        offset += len(batch.get("results", []))


def main(with_species):
    # 1) Araneae 下的所有科
    families = []
    for c in children(ARANEAE_KEY):
        if c.get("rank") == "FAMILY" and c.get("taxonomicStatus") == "ACCEPTED":
            families.append((c["key"], c.get("canonicalName") or c.get("scientificName")))
    print(f"共 {len(families)} 个科")

    genera = {}
    species = {}
    queue = [(key, fam, None) for key, fam in families]
    done = 0
    while queue:
        key, fam, subfam = queue.pop(0)
        for c in children(key):
            status = c.get("taxonomicStatus")
            rank = c.get("rank")
            name = c.get("canonicalName") or ""
            if status not in (None, "ACCEPTED") and rank != "GENUS":
                pass  # 属级允许同物异名（罕见），种级单独处理
            if rank == "SUBFAMILY":
                queue.append((c["key"], fam, c.get("canonicalName")))
            elif rank == "GENUS":
                if name:
                    g = name.split()[0]
                    genera[g.lower()] = {"g": g, "f": fam, "sf": subfam}
                if with_species:
                    queue.append((c["key"], fam, c.get("canonicalName")))
            elif with_species and rank in ("SPECIES", "SUBSPECIES"):
                if name and len(name.split()) >= 2:
                    is_syn = status and "SYNONYM" in status.upper()
                    acc_ref = (c.get("accepted") or "").strip()
                    accepted = " ".join(acc_ref.split()[:2]) if (is_syn and acc_ref) else name
                    species[name.lower()] = {
                        "n": name,                      # 该名称（可能是异名）
                        "acc": accepted,                # 当前接受名
                        "g": name.split()[0],           # 该名称的属
                        "f": fam, "sf": subfam,
                        "syn": bool(is_syn),
                    }
        done += 1
        if done % 40 == 0:
            print(f"  已处理 {done} 个节点，属 {len(genera)}，种 {len(species)} ...")
        time.sleep(0.03)

    data = sorted(genera.values(), key=lambda x: x["g"].lower())
    with open(OUT_GEN, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"属名录完成：{len(data)} 个属 -> {OUT_GEN}")

    if with_species:
        sp = sorted(species.values(), key=lambda x: x["n"].lower())
        with open(OUT_SPC, "w", encoding="utf-8") as f:
            json.dump(sp, f, ensure_ascii=False)
        n_syn = sum(1 for x in sp if x["syn"])
        print(f"种名录完成：{len(sp)} 个种名（含异名 {n_syn} 个）-> {OUT_SPC}")


if __name__ == "__main__":
    try:
        main(with_species="--species" in sys.argv)
    except Exception as e:
        print(f"拉取失败：{e}", file=sys.stderr)
        sys.exit(1)
