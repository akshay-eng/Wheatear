from wheatear.source_fetch import looks_like_url, parse_github_url


def test_looks_like_url_recognizes_https():
    assert looks_like_url("https://github.com/akshay-eng/Wheatear") is True


def test_looks_like_url_recognizes_ssh_form():
    assert looks_like_url("git@github.com:akshay-eng/Wheatear.git") is True


def test_looks_like_url_rejects_local_paths():
    assert looks_like_url("/Users/akshay/exports/my-agent") is False
    assert looks_like_url("./relative/path") is False
    assert looks_like_url("my-agent-clone") is False


def test_parse_github_url_plain_repo_adds_git_suffix():
    repo_url, branch, subpath = parse_github_url("https://github.com/akshay-eng/Wheatear")
    assert repo_url == "https://github.com/akshay-eng/Wheatear.git"
    assert branch is None
    assert subpath == ""


def test_parse_github_url_already_has_git_suffix():
    repo_url, branch, subpath = parse_github_url("https://github.com/akshay-eng/Wheatear.git")
    assert repo_url == "https://github.com/akshay-eng/Wheatear.git"


def test_parse_github_tree_url_extracts_branch_and_subpath():
    repo_url, branch, subpath = parse_github_url(
        "https://github.com/akshay-eng/exports/tree/main/hr-bot-export"
    )
    assert repo_url == "https://github.com/akshay-eng/exports.git"
    assert branch == "main"
    assert subpath == "hr-bot-export"


def test_parse_github_tree_url_with_nested_subpath():
    repo_url, branch, subpath = parse_github_url(
        "https://github.com/org/repo/tree/feature-branch/some/nested/folder"
    )
    assert branch == "feature-branch"
    assert subpath == "some/nested/folder"


def test_parse_github_tree_url_with_no_subpath_just_branch():
    repo_url, branch, subpath = parse_github_url("https://github.com/org/repo/tree/main")
    assert branch == "main"
    assert subpath == ""


def test_parse_non_github_url_passed_through_unchanged():
    repo_url, branch, subpath = parse_github_url("https://gitlab.com/org/repo.git")
    assert repo_url == "https://gitlab.com/org/repo.git"
    assert branch is None
    assert subpath == ""
