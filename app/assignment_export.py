from __future__ import annotations
import csv
from pathlib import Path
from .network_assignment import build_assignments

def export_assignments(source_domains,target_domains,output,links_per_website=10,websites_per_batch=10):
    rows=build_assignments(source_domains,target_domains,links_per_website=links_per_website,websites_per_batch=websites_per_batch)
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with open(output,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["source_site","batch","target_count","targets"])
        w.writeheader()
        for r in rows:
            w.writerow({"source_site":r.source_site,"batch":r.batch,"target_count":len(r.targets),"targets":"|".join(r.targets)})
    return rows
