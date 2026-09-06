# Website Inventory Assignment Plan

## Scope
This phase prepares deterministic inventory rotation only. It does not connect
to servers, alter HTML, or deploy links.

## Rotation rule
Default configuration:

- 10 target domains per group
- The same group is assigned to 10 consecutive source websites
- Then the next 10 target domains are assigned to the next 10 websites
- The sequence repeats deterministically when source sites exceed target groups

Example:

| Source websites | Assigned target group |
|---|---|
| 1-10 | targets 1-10 |
| 11-20 | targets 11-20 |
| 21-30 | targets 21-30 |

## Safety properties
- Input domains are normalized and deduplicated.
- A source domain is excluded from its own target group.
- No network or FTP access is required.
- Output is deterministic, making dry-run comparisons reproducible.

## Next phases
1. Import source and target domain CSV files.
2. Generate and review assignment CSV.
3. Add HTML file inventory support.
4. Add backup-aware deployment separately after the assignment logic is validated.
