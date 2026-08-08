from scripts.secret_scan import scan


def test_git_candidate_files_do_not_contain_secrets() -> None:
    assert scan() == []
