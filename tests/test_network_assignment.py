from app.network_assignment import build_assignments


def test_ten_targets_reused_for_ten_sites():
    sources=[f"site{i}.com" for i in range(1,21)]
    targets=[f"target{i}.com" for i in range(1,21)]
    rows=build_assignments(sources,targets,links_per_website=10,websites_per_batch=10)
    assert all(r.targets==tuple(targets[:10]) for r in rows[:10])
    assert all(r.targets==tuple(targets[10:20]) for r in rows[10:20])


def test_rotation_wraps_when_inventory_is_shorter_than_sources():
    rows=build_assignments(["a.com","b.com","c.com"],["x.com"],links_per_website=1,websites_per_batch=1)
    assert [r.targets for r in rows]==[("x.com",),("x.com",),("x.com",)]


def test_source_is_not_assigned_to_itself():
    rows=build_assignments(["a.com"],["a.com","b.com"],links_per_website=2,websites_per_batch=1)
    assert rows[0].targets==("b.com",)
