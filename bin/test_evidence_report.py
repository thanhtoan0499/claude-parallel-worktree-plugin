#!/usr/bin/env python3
"""Checks for evidence_report.py — the guard rails, mostly.

The renderer is the easy half; validate() is the half that has to hold, because it is the only
thing standing between "nine files on a ticket" and "nine files nobody can connect to a
requirement". Every rule below corresponds to a way a report can look finished and prove nothing,
and each is asserted to FIRE — a validator that silently passes everything is the failure mode
this whole file exists to catch.
"""

import json

from evidence_report import CHECKLIST_KEYS, MAIN_RE, build, bundle, render, validate


def _manifest(tmp_path, **over):
    """A minimal manifest that validates, plus the files it points at."""
    (tmp_path / "log.txt").write_text("RED then GREEN", encoding="utf-8")
    base = {
        "ticket": 6541,
        "type": "bug",
        "title": "Title ra câu động từ",
        "requirement": {"source": "AC-B3.2", "text": "phải là cụm danh từ"},
        "changed": ["services/gateway/.../title_generation.py — cắt động từ"],
        "results": [{
            "req": "Title không bắt đầu bằng động từ",
            "verdict": "đạt",
            "evidence": [{"file": "log.txt", "proves": "gỡ fix ra thì test đỏ"}],
        }],
        "red_green": {"red": "FAILED", "green": "PASSED"},
        "checklist": dict.fromkeys(CHECKLIST_KEYS, True),
    }
    base.update(over)
    return base


def _write(tmp_path, manifest):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_a_complete_manifest_validates(tmp_path):
    assert validate(_manifest(tmp_path), tmp_path) == []


def test_pass_verdict_without_evidence_is_refused(tmp_path):
    m = _manifest(tmp_path)
    m["results"][0]["evidence"] = []
    assert any("không có bằng chứng" in p for p in validate(m, tmp_path))


def test_evidence_without_a_proves_line_is_refused(tmp_path):
    """The whole point of the format. A filename with no sentence under it is exactly the state
    the board was already in."""
    m = _manifest(tmp_path)
    m["results"][0]["evidence"][0]["proves"] = "   "
    assert any("proves" in p for p in validate(m, tmp_path))


def test_evidence_pointing_at_a_missing_file_is_refused(tmp_path):
    m = _manifest(tmp_path)
    m["results"][0]["evidence"][0]["file"] = "khong-co.txt"
    assert any("không tìm thấy tệp" in p for p in validate(m, tmp_path))


def test_code_change_without_red_green_is_refused(tmp_path):
    m = _manifest(tmp_path)
    del m["red_green"]
    assert any("D62-5" in p for p in validate(m, tmp_path))


def test_doc_ticket_does_not_owe_red_green(tmp_path):
    """A written deliverable has no test to turn red — demanding one would just teach people to
    paste something meaningless into the field."""
    m = _manifest(tmp_path, type="doc")
    del m["red_green"]
    assert validate(m, tmp_path) == []


def test_ui_change_without_a_screenshot_is_refused(tmp_path):
    m = _manifest(tmp_path, changed=["apps/web/src/pages/x.tsx — đổi nút"])
    assert any("ảnh chụp màn hình" in p for p in validate(m, tmp_path))


def test_unchecked_checklist_item_needs_a_reason(tmp_path):
    m = _manifest(tmp_path)
    m["checklist"]["D62-1"] = ""
    assert any("D62-1" in p for p in validate(m, tmp_path))
    m["checklist"]["D62-1"] = "CI xanh, chưa ai duyệt"
    assert validate(m, tmp_path) == []


def test_missing_checklist_key_is_refused(tmp_path):
    m = _manifest(tmp_path)
    del m["checklist"]["D71-5"]
    assert any("D71-5" in p for p in validate(m, tmp_path))


def test_unknown_verdict_is_refused(tmp_path):
    m = _manifest(tmp_path)
    m["results"][0]["verdict"] = "ok"
    assert any("verdict" in p for p in validate(m, tmp_path))


def test_every_problem_is_reported_not_just_the_first(tmp_path):
    """One round trip per mistake is how a submission gate becomes something people route around."""
    m = _manifest(tmp_path)
    m["results"][0]["evidence"] = []
    del m["red_green"]
    m["checklist"]["D62-2"] = ""
    assert len(validate(m, tmp_path)) >= 3


def test_build_raises_and_writes_nothing_when_invalid(tmp_path):
    m = _manifest(tmp_path)
    m["results"][0]["evidence"] = []
    path = _write(tmp_path, m)
    try:
        build(path)
    except ValueError as exc:
        assert "chưa nộp được" in str(exc)
    else:
        raise AssertionError("build() rendered an invalid manifest")
    assert not (tmp_path / "report-AB6541.html").exists()


def test_build_writes_a_self_contained_page(tmp_path):
    out = build(_write(tmp_path, _manifest(tmp_path)))
    html = out.read_text(encoding="utf-8")
    assert out.name == "report-AB6541.html"
    assert html.startswith("<!doctype html>")
    assert 'class="rpt"' in html          # the board embeds the fragment under this scope
    assert "gỡ fix ra thì test đỏ" in html  # the proves line reaches the page


def test_render_is_embeddable_as_a_fragment(tmp_path):
    """The board cannot link to the report (ADO force-downloads .html), so it lifts <main> out and
    drops it into its own page. That only works while every style stays scoped to .rpt."""
    html = render(_manifest(tmp_path), tmp_path)
    fragment = MAIN_RE.search(html)
    assert fragment and fragment.group(0).startswith("<main>")
    assert "<style>" not in fragment.group(0)


def test_evidence_urls_become_links_when_known(tmp_path):
    m = _manifest(tmp_path)
    plain = render(m, tmp_path)
    linked = render(m, tmp_path, {"log.txt": "https://dev.azure.com/agentiqai/x/_apis/wit/attachments/abc"})
    assert "<a href" not in plain.split("log.txt")[0][-80:]
    assert "download=false" in linked


def test_report_titles_carry_the_ticket(tmp_path):
    html = render(_manifest(tmp_path), tmp_path)
    assert "AB#6541" in html


def test_the_board_bundle_carries_no_image_bytes(tmp_path):
    """The board's data travels through a `claude -p` prompt, so a bundle carrying base64
    screenshots comes back recomposed rather than copied — measured 2026-09-10, when the first
    such bundle published with `evidence` emptied and two invented keys. Prose survives; bytes
    do not."""
    (tmp_path / "shot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)  # header is enough — nothing should read it
    m = _manifest(tmp_path)
    m["results"][0]["evidence"].append({"file": "shot.png", "proves": "màn hình sau khi sửa"})
    out = json.dumps(bundle(m, tmp_path))
    assert "data:image" not in out
    assert len(out) < 8000, "the bundle is carrying something that is not prose"


def test_the_board_bundle_keeps_the_link_to_the_original(tmp_path):
    m = _manifest(tmp_path)
    out = bundle(m, tmp_path, {"log.txt": "https://dev.azure.com/agentiqai/x/_apis/wit/attachments/abc"})
    assert out["results"][0]["evidence"][0]["href"].endswith("download=false")
