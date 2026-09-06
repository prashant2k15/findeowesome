import csv
from app.assignment_export import export_assignments

def test_export(tmp_path):
    out=tmp_path/"assignments.csv"
    rows=export_assignments(["a.com"],["x.com","y.com"],out,2,10)
    assert len(rows)==1
    with open(out) as f:
        data=list(csv.DictReader(f))
    assert data[0]["targets"]=="x.com|y.com"
