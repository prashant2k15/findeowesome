"""Deterministic inventory-to-site assignment planner.

This module does not connect to FTP or modify websites. It produces a dry-run
mapping that can later be consumed by an approved deployment layer.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable


def clean_domains(values: Iterable[str]) -> list[str]:
    seen=set(); out=[]
    for value in values:
        domain=value.strip().lower().replace("https://","").replace("http://","").strip("/")
        if domain and domain not in seen:
            seen.add(domain); out.append(domain)
    return out


@dataclass(frozen=True)
class Assignment:
    source_site: str
    batch: int
    targets: tuple[str, ...]


def build_assignments(
    source_sites: Iterable[str],
    target_inventory: Iterable[str],
    *,
    links_per_website: int = 10,
    websites_per_batch: int = 10,
) -> list[Assignment]:
    """Assign one target group to N consecutive source websites.

    Example: 10 targets are reused for 10 source sites, then the next 10
    targets are reused for the next 10 sites. Assignment is deterministic.
    """
    if links_per_website < 1 or websites_per_batch < 1:
        raise ValueError("batch sizes must be positive")

    sources=clean_domains(source_sites)
    targets=clean_domains(target_inventory)
    if not sources or not targets:
        return []

    groups=[targets[i:i+links_per_website] for i in range(0,len(targets),links_per_website)]
    result=[]
    for i, source in enumerate(sources):
        batch=i // websites_per_batch
        group=groups[batch % len(groups)]
        filtered=tuple(x for x in group if x != source)
        result.append(Assignment(source, batch+1, filtered))
    return result
