#!/usr/bin/env python3
"""assert-based checks for attach_evidence.py (BRIEF-EVIDENCE.md Part 4) — dry-run by default,
never leaking the PAT, upload -> link -> optional PR comment. No test ever touches the network or
the real ~/.azure/azuredevops/personalAccessTokens file.
"""

import configparser

from attach_evidence import (
    attach_evidence,
    comment_on_pr,
    link_attachment,
    read_pat,
    upload_attachment,
)

FAKE_PAT = "FAKE-PAT-DO-NOT-USE-1234567890"  # not a real credential — only ever compared, never logged


def _pat_file(tmp_path, org="https://dev.azure.com/agentiqai", token=FAKE_PAT):
    path = tmp_path / "personalAccessTokens"
    parser = configparser.ConfigParser()
    parser[f"azdevops-cli:{org}"] = {"personal access token": token}
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)
    return str(path)


def test_read_pat_parses_the_orgs_own_section(tmp_path):
    path = _pat_file(tmp_path)
    assert read_pat(org="https://dev.azure.com/agentiqai", path=path) == FAKE_PAT


def test_read_pat_never_prints_anything(tmp_path, capsys):
    path = _pat_file(tmp_path)
    read_pat(org="https://dev.azure.com/agentiqai", path=path)
    out = capsys.readouterr()
    assert FAKE_PAT not in out.out and FAKE_PAT not in out.err


def test_read_pat_raises_clearly_when_the_org_has_no_section(tmp_path):
    path = _pat_file(tmp_path, org="https://dev.azure.com/someone-else")
    try:
        read_pat(org="https://dev.azure.com/agentiqai", path=path)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return
    raise AssertionError("a missing PAT section must raise, not hand curl an empty string")


def test_upload_attachment_posts_the_filename_and_raw_body(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok('{"url": "https://dev.azure.com/agentiqai/_apis/wit/attachments/abc123"}')

    f = tmp_path / "shot.png"
    f.write_bytes(b"fake png bytes")

    url = upload_attachment(str(f), org="https://dev.azure.com/agentiqai", project="AgentIQ",
                             pat=FAKE_PAT, run=fake_run)

    assert url == "https://dev.azure.com/agentiqai/_apis/wit/attachments/abc123"
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "curl"
    assert f"@{f}" in cmd
    joined = " ".join(cmd)
    assert "fileName=shot.png" in joined
    assert "_apis/wit/attachments" in joined
    assert "AgentIQ" in joined


def test_upload_attachment_authenticates_with_the_given_pat_and_never_logs_it():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok('{"url": "https://x/attachments/1"}')

    upload_attachment("/tmp/shot.png", pat=FAKE_PAT, run=fake_run)

    assert f":{FAKE_PAT}" in calls[0], "curl must authenticate with -u :$PAT"


def test_upload_attachment_raises_on_a_failed_curl_call():
    def fake_run(cmd, **kwargs):
        return _fail(22, "curl: (22) The requested URL returned error: 401")

    try:
        upload_attachment("/tmp/shot.png", pat=FAKE_PAT, run=fake_run)
    except RuntimeError as exc:
        assert FAKE_PAT not in str(exc)
        return
    raise AssertionError("a failed upload must raise, not return a fabricated url")


def test_link_attachment_sends_a_json_patch_add_of_an_attachedfile_relation():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok("{}")

    link_attachment("8309", "https://dev.azure.com/agentiqai/_apis/wit/attachments/abc123",
                     "verified via probe", org="https://dev.azure.com/agentiqai", project="AgentIQ",
                     pat=FAKE_PAT, run=fake_run)

    cmd = calls[0]
    assert "-X" in cmd and "PATCH" in cmd
    body = cmd[cmd.index("-d") + 1]
    assert "AttachedFile" in body
    assert "abc123" in body
    assert "verified via probe" in body
    assert "workitems/8309" in " ".join(cmd)


def test_link_attachment_raises_on_a_failed_curl_call():
    def fake_run(cmd, **kwargs):
        return _fail(22, "404 not found")

    try:
        link_attachment("8309", "https://x/attachments/1", None, pat=FAKE_PAT, run=fake_run)
    except RuntimeError:
        return
    raise AssertionError("a failed link must raise")


def test_comment_on_pr_posts_via_gh_with_the_evidence_link():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok("")

    comment_on_pr("719", "Evidence attached to AB#8309: shot.png\nhttps://x/attachments/1", run=fake_run)

    cmd = calls[0]
    assert cmd[:3] == ["gh", "pr", "comment"]
    assert "719" in cmd
    assert "https://x/attachments/1" in cmd[cmd.index("--body") + 1]


def test_comment_on_pr_raises_on_failure():
    def fake_run(cmd, **kwargs):
        return _fail(1, "GraphQL: PR not found")

    try:
        comment_on_pr("719", "body", run=fake_run)
    except RuntimeError:
        return
    raise AssertionError("a failed gh pr comment must raise")


# ---------------------------------------------------------------------------
# attach_evidence() — the orchestrator. dry-run is the default; --apply is required to touch
# anything real.
# ---------------------------------------------------------------------------


def _no_call(name):
    def _raise(*a, **k):
        raise AssertionError(f"{name} must not be called")
    return _raise


def test_attach_evidence_dry_run_is_the_default_and_touches_nothing(capsys):
    result = attach_evidence(
        "8309", "/tmp/shot.png", pr="719", note="verified",
        read_pat=_no_call("read_pat"), upload=_no_call("upload"),
        link=_no_call("link"), comment=_no_call("comment"),
    )
    assert result["applied"] is False
    assert result["url"] is None
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert "8309" in out and "shot.png" in out and "719" in out


def test_attach_evidence_dry_run_never_even_reads_the_pat():
    """dry-run must print what it would do and touch NOTHING — not even the PAT file."""
    attach_evidence("8309", "/tmp/shot.png", read_pat=_no_call("read_pat"),
                     upload=_no_call("upload"), link=_no_call("link"), comment=_no_call("comment"))


def test_attach_evidence_apply_uploads_then_links_then_returns_the_url():
    calls = []
    result = attach_evidence(
        "8309", "/tmp/shot.png", note="verified", dry_run=False,
        read_pat=lambda org=None, path=None: FAKE_PAT,
        upload=lambda file_path, **kw: (calls.append(("upload", kw.get("pat"))), "https://x/attachments/1")[1],
        link=lambda ticket, url, note, **kw: calls.append(("link", ticket, url, note)),
        comment=_no_call("comment"),
    )
    assert calls == [("upload", FAKE_PAT), ("link", "8309", "https://x/attachments/1", "verified")]
    assert result == {"ticket": "8309", "file": "shot.png", "pr": None, "applied": True,
                       "url": "https://x/attachments/1"}


def test_attach_evidence_apply_comments_on_the_pr_when_given():
    calls = []
    attach_evidence(
        "8309", "/tmp/shot.png", pr="719", dry_run=False,
        read_pat=lambda org=None, path=None: FAKE_PAT,
        upload=lambda file_path, **kw: "https://x/attachments/1",
        link=lambda *a, **k: None,
        comment=lambda pr, body: calls.append((pr, body)),
    )
    assert len(calls) == 1
    pr, body = calls[0]
    assert pr == "719"
    assert "shot.png" in body and "https://x/attachments/1" in body


def test_attach_evidence_apply_skips_the_pr_comment_when_none_given():
    attach_evidence(
        "8309", "/tmp/shot.png", dry_run=False,
        read_pat=lambda org=None, path=None: FAKE_PAT,
        upload=lambda file_path, **kw: "https://x/attachments/1",
        link=lambda *a, **k: None,
        comment=_no_call("comment"),
    )


def test_attach_evidence_apply_prints_the_attachment_url_and_nothing_of_the_pat(capsys):
    attach_evidence(
        "8309", "/tmp/shot.png", dry_run=False,
        read_pat=lambda org=None, path=None: FAKE_PAT,
        upload=lambda file_path, **kw: "https://x/attachments/1",
        link=lambda *a, **k: None,
        comment=_no_call("comment"),
    )
    out = capsys.readouterr().out
    assert "https://x/attachments/1" in out
    assert FAKE_PAT not in out


# ---------------------------------------------------------------------------
# main() — CLI wiring, monkeypatched at the module level (main() looks these names up at call
# time, so monkeypatch.setattr on the module reaches it — unlike attach_evidence()'s own
# keyword-injected defaults, which are bound once at definition time).
# ---------------------------------------------------------------------------


def test_main_defaults_to_dry_run_without_apply(monkeypatch, capsys):
    import attach_evidence as mod

    monkeypatch.setattr(mod, "read_pat", _no_call("read_pat"))
    monkeypatch.setattr(mod, "upload_attachment", _no_call("upload_attachment"))
    monkeypatch.setattr(mod, "link_attachment", _no_call("link_attachment"))
    monkeypatch.setattr(mod, "comment_on_pr", _no_call("comment_on_pr"))

    rc = mod.main(["--ticket", "8309", "--file", "/tmp/shot.png"])

    assert rc == 0
    assert "dry-run" in capsys.readouterr().out.lower()


def test_main_apply_flag_actually_calls_through(monkeypatch, capsys):
    import attach_evidence as mod

    calls = []
    monkeypatch.setattr(mod, "read_pat", lambda org=None, path=None: FAKE_PAT)
    monkeypatch.setattr(mod, "upload_attachment", lambda file_path, **kw: "https://x/attachments/9")
    monkeypatch.setattr(mod, "link_attachment", lambda *a, **k: calls.append("linked"))
    monkeypatch.setattr(mod, "comment_on_pr", _no_call("comment_on_pr"))

    rc = mod.main(["--ticket", "8309", "--file", "/tmp/shot.png", "--apply"])

    assert rc == 0
    assert calls == ["linked"]
    assert "https://x/attachments/9" in capsys.readouterr().out


def test_main_returns_nonzero_on_failure_without_a_traceback_leaking_anything(monkeypatch, capsys):
    import attach_evidence as mod

    def boom(org=None, path=None):
        raise RuntimeError("no PAT configured")

    monkeypatch.setattr(mod, "read_pat", boom)
    rc = mod.main(["--ticket", "8309", "--file", "/tmp/shot.png", "--apply"])
    assert rc != 0
    assert FAKE_PAT not in capsys.readouterr().out


def _ok(stdout):
    import subprocess
    return subprocess.CompletedProcess(["curl"], 0, stdout, "")


def _fail(code, stderr):
    import subprocess
    return subprocess.CompletedProcess(["curl"], code, "", stderr)
