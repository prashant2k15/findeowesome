"""Domain inventory import, validation and export utilities."""
from __future__ import annotations
import csv,re
from pathlib import Path
from urllib.parse import urlparse

DOMAIN_RE=re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

def normalize_domain(value:str)->str:
    value=(value or "").strip().lower()
    if not value:return ""
    if "://" not in value:value="//"+value
    host=urlparse(value).hostname or ""
    return host[4:] if host.startswith("www.") else host

def is_valid_domain(domain:str)->bool:
    return bool(DOMAIN_RE.match(domain)) and len(domain)<=253

def clean_domains(values):
    seen=set(); valid=[]; invalid=[]
    for value in values:
        domain=normalize_domain(str(value))
        if not domain:continue
        if not is_valid_domain(domain):
            invalid.append(str(value)); continue
        if domain not in seen:
            seen.add(domain); valid.append(domain)
    return valid,invalid

def read_csv(path: str|Path, column="domain"):
    with open(path,newline="",encoding="utf-8-sig") as f:
        return [row.get(column,"") for row in csv.DictReader(f)]

def write_csv(path, domains):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["domain"]); w.writerows([[d] for d in domains])
