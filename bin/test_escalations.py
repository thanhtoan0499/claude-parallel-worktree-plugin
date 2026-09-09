#!/usr/bin/env python3
"""assert-based checks for the escalation queue and tier classifier. Run: python3 bin/test_escalations.py"""

import json
import os
import tempfile

from escalations import (
    append,
    classify,
    current_state,
    is_undeliverable,
    new_record,
    normalize_options,
    read_all,
    record_answer,
    record_dismiss,
)


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return path


def test_new_record_has_required_fields():
    r = new_record("sess-1", "red_tests", "Tests fail, retry or reassign?")
    for key in (
        "id",
        "ts",
        "session_id",
        "kind",
        "question",
        "options",
        "evidence",
        "tier",
        "status",
        "decided_by",
        "answer",
        "answered_at",
    ):
        assert key in r, f"missing {key}"
    assert r["session_id"] == "sess-1"
    assert r["kind"] == "red_tests"
    assert r["status"] == "open"
    assert r["tier"] is None
    assert r["answer"] is None
    assert r["options"] == []
    assert r["evidence"] == {}


def test_new_record_ids_are_unique():
    a = new_record("s", "k", "q")
    b = new_record("s", "k", "q")
    assert a["id"] != b["id"]


def test_normalize_options_keeps_a_list_of_strings():
    assert normalize_options(["Approve", "Reject"]) == ["Approve", "Reject"]


def test_normalize_options_wraps_a_bare_string():
    # The live bug: a record authored with options="Approve" instead of ["Approve"]. Must
    # become a one-element list, never iterated character by character.
    assert normalize_options("Approve") == ["Approve"]


def test_normalize_options_drops_non_string_items_from_a_list():
    assert normalize_options(["Approve", None, 3, {"x": 1}, "Reject"]) == ["Approve", "Reject"]


def test_normalize_options_empty_for_a_dict():
    assert normalize_options({"Approve": True}) == []


def test_normalize_options_empty_for_none():
    assert normalize_options(None) == []


def test_append_then_read_all_roundtrips():
    path = _tmp()
    try:
        a = new_record("s1", "k1", "q1")
        b = new_record("s2", "k2", "q2")
        append(path, a)
        append(path, b)
        got = read_all(path)
        assert [r["id"] for r in got] == [a["id"], b["id"]]
        assert got[0]["question"] == "q1"
    finally:
        os.unlink(path)


def test_read_all_missing_file_is_empty():
    assert read_all("/nonexistent/path/queue.jsonl") == []


def test_read_all_skips_malformed_lines():
    path = _tmp()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(new_record("s", "k", "good")) + "\n")
            f.write("not json at all\n")
            f.write("\n")
        got = read_all(path)
        assert len(got) == 1
        assert got[0]["question"] == "good"
    finally:
        os.unlink(path)


def test_read_all_skips_non_object_json_lines():
    # Valid JSON that isn't an object reads back as an int/str/None/list, and every consumer
    # downstream calls .get() on it. Skipping it here is what keeps them from crashing.
    path = _tmp()
    try:
        good = new_record("s", "k", "good")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n")
            f.writelines(junk + "\n" for junk in ("123", '"done"', "null", "[]"))
        got = read_all(path)
        assert len(got) == 1, f"non-object lines must be skipped, got {got}"
        assert got[0]["question"] == "good"
        state = current_state(path)  # must not raise
        assert [r["id"] for r in state] == [good["id"]]
    finally:
        os.unlink(path)


def test_tier2_mechanical_kinds():
    for kind in ("red_tests", "looping", "pick_implementation", "scope_question"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier2", f"{kind} should be tier2, got {tier} ({reason})"
        assert reason


def test_tier3_always_human_kinds():
    for kind in ("irreversible", "push_or_pr", "credentials", "spec_ambiguity", "no_convergence", "worktree_collision"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier3", f"{kind} should be tier3, got {tier} ({reason})"
        assert reason


def test_clean_diff_is_tier2():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "changed_files": ["bin/dashboard.py"],
            "migration": False,
        },
    )
    tier, reason = classify(r)
    assert tier == "tier2", reason


def test_diff_with_red_tests_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "red",
            "deps_added": [],
            "changed_files": ["bin/dashboard.py"],
            "migration": False,
        },
    )
    assert classify(r)[0] == "tier3"


def test_diff_adding_dependency_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": ["requests"],
            "changed_files": ["bin/x.py"],
            "migration": False,
        },
    )
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "depend" in reason.lower()


def test_diff_with_migration_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "changed_files": ["bin/x.py"],
            "migration": True,
        },
    )
    assert classify(r)[0] == "tier3"


def test_diff_touching_sensitive_path_is_tier3():
    for path in ("services/gateway/auth/token.py", "config/.env", "app/secrets.yaml", "migrations/0042_add.py"):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={
                "tests": "green",
                "deps_added": [],
                "changed_files": ["bin/ok.py", path],
                "migration": False,
            },
        )
        tier, reason = classify(r)
        assert tier == "tier3", f"{path} should force tier3 ({reason})"


def _clean_diff_evidence(**extra) -> dict:
    return {"tests": "green", "deps_added": [], "changed_files": ["bin/x.py"], "migration": False, **extra}


def test_target_branch_main_is_tier3():
    # Evidence, not the self-declared kind, decides: a diff_review that looks clean but targets
    # main is still "anything touching main", so the manager must not be able to approve it.
    for branch in ("main", "master", "MAIN", " main "):
        r = new_record("s", "diff_review", "merge?", evidence=_clean_diff_evidence(branch=branch))
        tier, reason = classify(r)
        assert tier == "tier3", f"branch {branch!r} must be tier3, got {reason}"
        assert "branch" in reason


def test_feature_branch_stays_tier2():
    for branch in ("feature/x", "fix/thing", "dev"):
        r = new_record("s", "diff_review", "merge?", evidence=_clean_diff_evidence(branch=branch))
        tier, reason = classify(r)
        assert tier == "tier2", f"branch {branch!r} must stay tier2, got {reason}"


def test_tier3_wins_ties():
    # A mechanical kind that nonetheless carries an irreversible flag stays tier3.
    r = new_record("s", "red_tests", "q", evidence={"irreversible": True})
    assert classify(r)[0] == "tier3"


def test_unknown_kind_defaults_to_tier3():
    tier, reason = classify(new_record("s", "something_new", "q"))
    assert tier == "tier3"
    assert "unknown" in reason.lower()


def test_diff_must_disclose_every_gated_field():
    # Only tests disclosed — the other three are simply absent.
    r = new_record("s", "diff_review", "merge?", evidence={"tests": "green"})
    tier, reason = classify(r)
    assert tier == "tier3", "an undisclosed field must not read as compliance"
    assert "disclose" in reason

    for missing in ("deps_added", "migration", "changed_files"):
        ev = {"tests": "green", "deps_added": [], "migration": False, "changed_files": ["bin/x.py"]}
        del ev[missing]
        tier, reason = classify(new_record("s", "diff_review", "merge?", evidence=ev))
        assert tier == "tier3", f"missing {missing} must force tier3"
        assert missing in reason


def test_changed_files_as_string_still_scanned():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "migration": False,
            "changed_files": "services/gateway/auth/token.py",
        },
    )
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "sensitive" in reason


def test_changed_files_as_dict_still_scanned():
    # A dict wrapping the list is worker-authored evidence too. Iterating it yields keys,
    # never the nested paths, so an unscanned shape passes every secret file as clean.
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "migration": False,
            "changed_files": {"files": ["services/auth/login.py"]},
        },
    )
    tier, reason = classify(r)
    assert tier == "tier3", reason
    assert "sensitive" in reason


def test_clean_changed_files_shapes_stay_tier2():
    # The fail-closed scan must not turn every unusual shape into a false positive.
    for shape in ("bin/ok.py", ["bin/ok.py", "README.md"], {"files": ["bin/ok.py", "README.md"]}):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={"tests": "green", "deps_added": [], "migration": False, "changed_files": shape},
        )
        tier, reason = classify(r)
        assert tier == "tier2", f"clean {shape!r} must stay tier2, got {reason}"


def test_changed_files_as_non_list_scalar_is_tier3():
    # A number/bool isn't a file list at all. _sensitive() can scan its str() form and find no
    # marker, but "no marker" is not the same as "a disclosed, clean file list" — this must not
    # read as compliance any more than an outright missing field does.
    for shape in (42, 3.14, True, 1, -1):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={"tests": "green", "deps_added": [], "migration": False, "changed_files": shape},
        )
        tier, reason = classify(r)
        assert tier == "tier3", f"changed_files={shape!r} must not read as a clean file list, got {reason}"


def test_tier3_evidence_beats_any_tier2_kind():
    for ev, why in (
        ({"deps_added": ["requests"]}, "dependency"),
        ({"migration": True}, "migration"),
        ({"changed_files": ["services/auth/token.py"]}, "sensitive path"),
    ):
        for kind in ("red_tests", "looping", "pick_implementation", "scope_question"):
            tier, reason = classify(new_record("s", kind, "q", evidence=ev))
            assert tier == "tier3", f"{kind} + {why} must be tier3, got {reason}"


def test_secret_and_migration_shapes_are_sensitive():
    for path in (
        "deploy/private_key.pem",
        "infra/id_rsa",
        "etc/passwd",
        "app/.netrc",
        "app/.npmrc",
        "keystore/prod.jks",
        "db/schema.sql",
        "alembic/versions/0042_add.py",
    ):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={
                "tests": "green",
                "deps_added": [],
                "migration": False,
                "changed_files": ["bin/ok.py", path],
            },
        )
        tier, reason = classify(r)
        assert tier == "tier3", f"{path} should be sensitive ({reason})"


def test_cost_anomaly_names_itself():
    tier, reason = classify(new_record("s", "cost_anomaly", "spend spiked"))
    assert tier == "tier3"
    assert "cost_anomaly" in reason
    assert "unknown" not in reason.lower()


def test_deps_added_as_string_reads_cleanly():
    r = new_record("s", "diff_review", "merge?", evidence={"deps_added": "requests"})
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "requests" in reason
    assert "r, e, q" not in reason


def test_explicitly_null_evidence_is_not_disclosure():
    for null_key in ("deps_added", "migration", "changed_files"):
        ev = {"tests": "green", "deps_added": [], "migration": False, "changed_files": ["bin/x.py"]}
        ev[null_key] = None
        tier, reason = classify(new_record("s", "diff_review", "merge?", evidence=ev))
        assert tier == "tier3", f"{null_key}=None must not read as disclosed"
        assert null_key in reason


def test_record_answer_appends_and_marks_answered():
    path = _tmp()
    try:
        r = new_record("s1", "red_tests", "retry?")
        append(path, r)
        updated = record_answer(path, r["id"], "retry once", "manager")
        assert updated is not None
        assert updated["status"] == "answered"
        assert updated["answer"] == "retry once"
        assert updated["decided_by"] == "manager"
        assert updated["answered_at"] is not None
        # append-only: the original line is still there, the update is a second line
        assert len(read_all(path)) == 2
    finally:
        os.unlink(path)


def test_record_answer_unknown_id_returns_none():
    path = _tmp()
    try:
        append(path, new_record("s", "k", "q"))
        assert record_answer(path, "nope", "x", "manager") is None
        assert len(read_all(path)) == 1
    finally:
        os.unlink(path)


def test_current_state_folds_to_latest_per_id():
    path = _tmp()
    try:
        a = new_record("s1", "red_tests", "q1")
        b = new_record("s2", "diff_review", "q2")
        append(path, a)
        append(path, b)
        record_answer(path, a["id"], "go", "human")
        state = current_state(path)
        assert len(state) == 2
        by_id = {r["id"]: r for r in state}
        assert by_id[a["id"]]["status"] == "answered"
        assert by_id[a["id"]]["answer"] == "go"
        assert by_id[b["id"]]["status"] == "open"
    finally:
        os.unlink(path)


def test_current_state_preserves_first_seen_order():
    path = _tmp()
    try:
        ids = []
        for i in range(3):
            r = new_record(f"s{i}", "red_tests", f"q{i}")
            ids.append(r["id"])
            append(path, r)
        record_answer(path, ids[0], "done", "manager")
        assert [r["id"] for r in current_state(path)] == ids
    finally:
        os.unlink(path)


def test_is_undeliverable_true_for_the_delivery_failure_trio():
    r = new_record("s", "push_or_pr", "push?")
    r["status"] = "needs_human"
    r["answer"] = "yes"
    r["delivery_attempts"] = 3
    assert is_undeliverable(r) is True


def test_is_undeliverable_false_for_a_genuine_open_question_flipped_to_needs_human():
    # process_open's other branch: the manager punts on an open record with no answer of its own.
    r = new_record("s", "push_or_pr", "push?")
    r["status"] = "needs_human"
    assert r["answer"] is None
    assert is_undeliverable(r) is False


def test_is_undeliverable_false_without_delivery_attempts():
    r = new_record("s", "push_or_pr", "push?")
    r["status"] = "needs_human"
    r["answer"] = "yes"
    assert is_undeliverable(r) is False


def test_is_undeliverable_false_while_still_open():
    r = new_record("s", "push_or_pr", "push?")
    r["answer"] = "yes"
    r["delivery_attempts"] = 3
    assert is_undeliverable(r) is False  # status hasn't flipped to needs_human


def test_is_undeliverable_false_mid_retry_before_status_flips():
    # Delivery has failed once, not yet exhausted — status is still "answered", per _try_deliver.
    r = new_record("s", "push_or_pr", "push?")
    r["status"] = "answered"
    r["answer"] = "yes"
    r["delivery_attempts"] = 1
    assert is_undeliverable(r) is False


def test_record_dismiss_appends_and_marks_dismissed():
    path = _tmp()
    try:
        r = new_record("s1", "push_or_pr", "push?")
        append(path, r)
        updated = record_dismiss(path, r["id"], "cto")
        assert updated is not None
        assert updated["status"] == "dismissed"
        assert updated["decided_by"] == "cto"
        assert updated["answered_at"] is not None
        # append-only: the original line is still there, the update is a second line
        assert len(read_all(path)) == 2
    finally:
        os.unlink(path)


def test_record_dismiss_unknown_id_returns_none():
    path = _tmp()
    try:
        append(path, new_record("s", "k", "q"))
        assert record_dismiss(path, "nope", "cto") is None
        assert len(read_all(path)) == 1
    finally:
        os.unlink(path)


def test_record_dismiss_preserves_the_decided_answer():
    # Retiring a record is not un-deciding it — the answer stays on the record as history.
    path = _tmp()
    try:
        r = new_record("s1", "push_or_pr", "push?")
        append(path, r)
        record_answer(path, r["id"], "yes", "manager")
        updated = record_dismiss(path, r["id"], "cto")
        assert updated["answer"] == "yes"
    finally:
        os.unlink(path)


def test_record_dismiss_no_longer_reads_as_undeliverable():
    # Once dismissed, get_escalations()'s needs_human filter (status == "needs_human") and
    # is_undeliverable() both stop matching — the record is off the panel, not just relabelled.
    path = _tmp()
    try:
        r = new_record("s1", "push_or_pr", "push?")
        append(path, r)
        record_answer(path, r["id"], "yes", "manager")
        undelivered = dict(current_state(path)[0])
        undelivered["status"] = "needs_human"
        undelivered["delivery_attempts"] = 3
        append(path, undelivered)
        assert is_undeliverable(current_state(path)[0]) is True

        record_dismiss(path, r["id"], "cto")
        final = current_state(path)[0]
        assert final["status"] == "dismissed"
        assert is_undeliverable(final) is False
    finally:
        os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")


def test_normalize_kind_accepts_the_canonical_vocabulary_unchanged():
    from escalations import CANONICAL_KINDS, normalize_kind

    for kind in CANONICAL_KINDS:
        assert normalize_kind(kind) == kind


def test_normalize_kind_maps_a_decorated_near_miss_onto_its_canonical_name():
    """The live queue carried `blocked_on_credentials`. The tier stayed safe (unknown defaults
    to a human) but every consumer keying off the kind read a production credentials outage as
    unrecognised — the dashboard scored it P1 where `credentials` scores P0."""
    from escalations import normalize_kind

    assert normalize_kind("blocked_on_credentials") == "credentials"
    assert normalize_kind("credentials_expired") == "credentials"
    assert normalize_kind("Red Tests") == "red_tests"
    assert normalize_kind("  PUSH_OR_PR  ") == "push_or_pr"


def test_normalize_kind_invents_nothing_for_text_that_names_no_kind():
    from escalations import normalize_kind

    for raw in ("", "   ", "something_else", None, 7, ["credentials"]):
        assert normalize_kind(raw) is None


def test_classify_routes_a_decorated_kind_by_its_canonical_name():
    tier, reason = classify(new_record("s", "blocked_on_credentials", "q"))
    assert tier == "tier3"
    assert "credentials" in reason
    assert "unknown" not in reason


def test_classify_still_defaults_an_unrecognisable_kind_to_a_human():
    """Widening what counts as a known kind must not widen what gets auto-decided."""
    tier, reason = classify(new_record("s", "totally_made_up", "q"))
    assert tier == "tier3"
    assert "unknown kind 'totally_made_up'" in reason
