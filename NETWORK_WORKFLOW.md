# Inventory Workflow

## 1. Import
Provide a CSV with a `domain` column.

## 2. Clean
```bash
python -m app.cli clean imports/raw.csv imports/clean_domains.csv
```

The cleaner:
- lowercases
- strips protocols and paths
- removes www
- validates syntax
- removes duplicates

## 3. Prepare source and target lists
The same cleaned inventory may initially be copied into both roles, or separate lists can be used.

## 4. Dry-run assignment
```bash
python -m app.cli plan --sources imports/source_sites.csv --targets imports/target_inventory.csv
```

Default rule: 10 targets reused for 10 consecutive source sites, then rotate to the next group.

## 5. Review
Inspect `exports/network_assignments.csv`. No server access or website changes occur in this phase.
