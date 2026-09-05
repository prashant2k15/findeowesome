from app.collectors.github_collector import _likely_list_file, _raw_url


def test_likely_list_file_accepts_backlink_data_files():
    assert _likely_list_file("data/backlink-sites.csv") is True
    assert _likely_list_file("lists/directories.txt") is True
    assert _likely_list_file("README.md") is True


def test_likely_list_file_rejects_irrelevant_or_large_files():
    assert _likely_list_file("src/application.py") is False
    assert _likely_list_file("image.png") is False
    assert _likely_list_file("lists/backlinks.csv", 2_000_001) is False


def test_raw_url_escapes_branch_and_path():
    url = _raw_url("owner/repo", "feature/test", "lists/my sites.csv")
    assert url == "https://raw.githubusercontent.com/owner/repo/feature%2Ftest/lists/my%20sites.csv"
