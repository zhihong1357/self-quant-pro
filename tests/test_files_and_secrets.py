from __future__ import annotations

FAKE_GOOGLE_KEY = "AIza" + "Sy" + "C" * 33
PEM_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def test_private_key_block(sandbox):
    sandbox.write("deploy/id.txt", PEM_HEADER + "\nabc\n")
    assert "secret-pattern" in sandbox.rules()


def test_hardcoded_secret_assignment(sandbox):
    sandbox.write("research/x.py", 'token = None\nauth_token = "' + "q" * 24 + '"\n')
    assert "secret-pattern" in sandbox.rules()


def test_prose_is_not_a_secret(sandbox):
    sandbox.write("docs/a.md", "risk-adjusted-performance-metrics-for-everyone\n")
    assert sandbox.rules() == set()


def test_a_secret_removed_later_is_still_caught_in_history(sandbox):
    base = sandbox.commit("base")
    sandbox.write("leak.txt", FAKE_GOOGLE_KEY + "\n")
    sandbox.commit("leak")
    sandbox.remove("leak.txt")
    sandbox.commit("remove")
    assert "secret-history" in sandbox.rules(base)


def test_env_file_is_blocked_but_the_template_is_fine(sandbox):
    sandbox.write(".env.example", "GEMINI_API_KEY=\n")
    assert sandbox.rules() == set()
    sandbox.write(".env", "GEMINI_API_KEY=\n")
    assert "secret-file" in sandbox.rules()


def test_service_account_file_is_blocked(sandbox):
    sandbox.write("keys/my-service-account.json", "{}\n")
    assert "secret-file" in sandbox.rules()


def test_data_files_only_in_fixtures(sandbox):
    sandbox.write("tests/fixtures/tiny.csv", "a,b\n1,2\n")
    assert sandbox.rules() == set()
    sandbox.write("prices.csv", "a,b\n1,2\n")
    assert "data-file" in sandbox.rules()


def test_large_files_are_blocked(sandbox):
    (sandbox.root / "big.bin").write_bytes(b"0" * 1_000_001)
    assert "file-size" in sandbox.rules()
