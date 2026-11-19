import pytest

from grafter.charter import Charter, UnsafePathError, normalise_path


def make(protected: list[str] | None = None, review: list[str] | None = None) -> Charter:
    return Charter(
        solution="x",
        purpose="y",
        core_domains=["a"],
        allowed_change_types=["bugfix"],
        protected_paths=protected or [],
        review_paths=review or [],
    )


# --- normalise_path ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auth/x", "auth/x"),
        ("./auth/x", "auth/x"),
        ("auth\\x", "auth/x"),
        ("auth//x", "auth/x"),
        ("src/../auth/x", "auth/x"),
        ("a/./b/", "a/b"),
        ("Auth/Login.cs", "Auth/Login.cs"),
    ],
)
def test_normalise_path(raw, expected):
    assert normalise_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "a/..",
        "/auth/x",
        "\\auth\\x",
        "C:/auth/x",
        "C:auth/x",
        "c:\\auth\\x",
        "//server/share/x",
        "\\\\server\\share\\x",
        "../outside",
        "src/../../outside",
        "auth/\x00x",
        "auth./x",  # Windows strips trailing dots: same as auth/x
        "auth /x",  # Windows strips trailing spaces: same as auth/x
        "auth/x:stream",  # NTFS alternate data stream
    ],
)
def test_normalise_path_rejects_unsafe(raw):
    with pytest.raises(UnsafePathError):
        normalise_path(raw)


def test_unsafe_path_error_is_value_error():
    assert issubclass(UnsafePathError, ValueError)


# --- directory patterns cannot be bypassed ---------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "auth/x",
        "./auth/x",
        "auth\\x",
        "AUTH/x",
        "Auth/Login.cs",
        "src/../auth/x",
        "auth//x",
        "auth/deep/nested/file.cs",
        # unsafe paths fail closed
        "/auth/x",
        "C:/auth/x",
        "../outside",
        "",
    ],
)
def test_auth_dir_is_protected(path):
    assert make(["auth/"]).is_protected(path)


@pytest.mark.parametrize("path", ["src/lots.cs", "authors/x", "src/auth/x", "auth.cs"])
def test_auth_dir_does_not_overmatch(path):
    assert not make(["auth/"]).is_protected(path)


def test_dir_pattern_also_matches_the_bare_name():
    # Decision: `auth/` protects the directory itself too, so a change that replaces
    # or deletes the directory (reported as the path `auth`) is blocked. Fail closed.
    assert make(["auth/"]).is_protected("auth")


def test_plain_pattern_is_exact_path():
    c = make(["auth"])
    assert c.is_protected("auth")
    assert c.is_protected("AUTH")
    assert not c.is_protected("auth/x")
    assert not c.is_protected("authors")


def test_pattern_is_normalised():
    c = make(["./Auth\\"])
    assert c.protected_paths[0] == "Auth/"
    assert c.is_protected("auth/x")


def test_unsafe_pattern_rejected():
    with pytest.raises(ValueError):
        make(["../outside/"])
    with pytest.raises(ValueError):
        make(review=["/etc/"])


# --- globs -----------------------------------------------------------------


def test_double_star_secrets_json():
    c = make(["**/secrets/*.json"])
    assert c.is_protected("secrets/a.json")
    assert c.is_protected("app/config/secrets/a.json")
    assert c.is_protected("App\\Secrets\\A.JSON")
    assert not c.is_protected("secrets/sub/a.json")  # `*` does not cross `/`
    assert not c.is_protected("secrets/a.yaml")
    assert not c.is_protected("mysecrets/a.json")


def test_star_pem_is_root_only():
    c = make(["*.pem"])
    assert c.is_protected("key.pem")
    assert c.is_protected("KEY.PEM")
    assert not c.is_protected("certs/key.pem")  # use `**/*.pem` for any depth
    assert make(["**/*.pem"]).is_protected("certs/key.pem")


def test_double_star_in_middle():
    c = make(["db/**/seed.sql"])
    assert c.is_protected("db/seed.sql")
    assert c.is_protected("db/a/seed.sql")
    assert c.is_protected("db/a/b/seed.sql")
    assert not c.is_protected("db/a/seed.sql.bak")
    assert not c.is_protected("dbx/seed.sql")


def test_question_mark():
    c = make(["v?.txt"])
    assert c.is_protected("v1.txt")
    assert not c.is_protected("v10.txt")
    assert not c.is_protected("v/.txt")


def test_trailing_double_star():
    c = make(["db/**"])
    assert c.is_protected("db/a")
    assert c.is_protected("db/a/b")
    assert not c.is_protected("dbx/a")


def test_regex_chars_are_literal():
    c = make(["a+b(1).txt"])
    assert c.is_protected("a+b(1).txt")
    assert not c.is_protected("aab1.txt")


# --- review paths ----------------------------------------------------------


def test_needs_review_normalises_and_fails_closed():
    c = make(review=["db/migrations/"])
    assert c.needs_review("DB\\Migrations\\001.sql")
    assert c.needs_review("./db/x/../migrations/001.sql")
    assert c.needs_review("/db/migrations/001.sql")
    assert c.needs_review("")
    assert not c.needs_review("db/seed.sql")


def test_overlap_is_case_and_form_insensitive():
    with pytest.raises(ValueError):
        make(["db/"], ["DB\\"])
