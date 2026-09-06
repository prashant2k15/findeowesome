from app.inventory import normalize_domain,clean_domains,is_valid_domain

def test_normalization():
    assert normalize_domain(" HTTPS://WWW.Example.COM/page ")=="example.com"

def test_cleaning_and_dedupe():
    valid,invalid=clean_domains(["Example.com","https://www.example.com/a","bad domain","test.org"])
    assert valid==["example.com","test.org"]
    assert invalid==["bad domain"]

def test_validation():
    assert is_valid_domain("example.com")
    assert not is_valid_domain("not_a_domain")
