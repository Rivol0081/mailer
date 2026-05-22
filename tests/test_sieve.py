from mailer.sieve_client import _build_script


def test_empty_keywords():
    s = _build_script([], "trash")
    assert "no keywords" in s


def test_trash_action():
    s = _build_script(["promo", "spam test"], "trash")
    assert 'require ["fileinto", "body"]' in s
    assert '"promo"' in s
    assert '"spam test"' in s
    assert 'fileinto "Trash"' in s


def test_delete_action():
    s = _build_script(["foo"], "delete")
    assert "discard" in s


def test_flag_action():
    s = _build_script(["foo"], "flag")
    assert "imap4flags" in s
    assert "setflag" in s


def test_quote_escaping():
    s = _build_script(['a "quoted" word'], "trash")
    assert '\\"quoted\\"' in s
