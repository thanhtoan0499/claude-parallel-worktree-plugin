#!/usr/bin/env python3
"""evidence_report.py — turns a ticket's scattered evidence files into one readable report.

    evidence_report.py --manifest report.json [--out report-AB6541.html] [--pr 732] [--apply]

The problem it exists to solve: a ticket accumulates nine attachments with names like
`d62-5_red_green_revert.txt` and `verify-goal-verbfirst.png`, and nobody — not the TL, not QC,
not the CTO — can tell from the board what the ticket asked for or which file proves what. The
files are individually fine and collectively useless.

So the unit of delivery stops being "a pile of attachments" and becomes ONE document that reads
end to end: what the ticket required, what changed, a row per requirement with its verdict, and
under every single piece of evidence a sentence saying WHAT IT PROVES. That sentence is the
whole point — it is the only thing that connects a filename to a requirement, and validate()
refuses to render a report without one on every file.

The engineer writes `report.json` next to their evidence files; this renders it. Images are
downscaled to WebP and inlined as data URIs, so the output is a single self-contained file that
opens anywhere — needed because ADO serves an .html attachment as
`content-type: application/octet-stream` + `content-disposition: attachment` no matter what
query string you hand it (measured 2026-09-10; the `?fileName=x.png&download=false` trick that
makes PNGs preview inline does NOT extend to html). Clicking an inlined image still opens the
full-resolution original on ADO.

Schema → docs/evidence-report-template.md. dry-run is the default; --apply attaches.
"""

import base64
import hashlib
import html
import io
import json
import mimetypes
import pathlib
import re
import urllib.parse

# The board shows the report inline (ADO force-downloads an .html attachment, so a
# plain link is useless there). It lifts this out of the standalone file and drops it
# inside a <div class="rpt">, which is why every selector in CSS is scoped to .rpt.
MAIN_RE = re.compile(r"<main>.*</main>", re.S)

VERDICTS = ("đạt", "đạt một phần", "chưa chứng minh", "không đạt")
PASS_VERDICT = "đạt"
DOC_TYPES = ("bug", "task", "doc")
CHECKLIST_KEYS = ("D62-1", "D62-2", "D62-3", "D62-4", "D62-5", "D71-5")
CHECKLIST_TEXT = {
    "D62-1": "CI xanh + ≥1 approve",
    "D62-2": "Test đi kèm, chạy trong CI",
    "D62-3": "PR ghi AC được phủ",
    "D62-4": "Bảng AC + bằng chứng trong vé",
    "D62-5": "Có test đỏ khi gỡ thay đổi",
    "D71-5": "Bảng self-test có trước khi sang QC",
}
# A diff here means a human looked at a screen, so the report owes a screenshot.
UI_PATHS = ("apps/web/", "packages/ui/")
# Prose is plain text, deliberately. The board renders this same manifest through its own h()
# helper, which builds text nodes and has no markup path at all — so a tag here would render as
# a tag in the html file and as literal "<b>" on the board. Rejecting it keeps one manifest
# meaning one thing in both places.
PROSE_MARKUP = "<"
# sha256 -> "/_blob/<id>", written by whoever uploaded the image to the board artifact's asset
# store. Keyed by content, not filename: the same screenshot submitted from two worktrees is one
# asset, and a file that changes gets a new key rather than silently reusing a stale picture.
ASSET_MAP_PATH = pathlib.Path.home() / ".config" / "board-mirror" / "asset-map.json"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_IMAGE_WIDTH = 1100


# ---------------------------------------------------------------- validation

def _evidence_of(manifest) -> list[tuple[str, dict]]:
    """Every evidence entry in the manifest, tagged with where it came from, so an error message
    can say *which* row is missing a `proves` line rather than just that one is."""
    out = []
    req = manifest.get("requirement") or {}
    for e in req.get("evidence") or []:
        out.append(("requirement", e))
    for i, r in enumerate(manifest.get("results") or []):
        for e in r.get("evidence") or []:
            out.append((f"results[{i}] {str(r.get('req'))[:40]}", e))
    return out


def validate(manifest: dict, base_dir) -> list[str]:
    """Every problem with the manifest, not just the first — a report bounced back one line at a
    time costs the engineer a round trip per mistake. Empty list means renderable.
    """
    base = pathlib.Path(base_dir)
    problems: list[str] = []

    for field in ("ticket", "type", "title", "results", "checklist"):
        if not manifest.get(field):
            problems.append(f"thiếu trường bắt buộc: {field}")
    if problems:
        return problems

    if manifest["type"] not in DOC_TYPES:
        problems.append(f"type phải là một trong {DOC_TYPES}, đang là {manifest['type']!r}")

    for i, r in enumerate(manifest.get("results") or []):
        where = f"results[{i}]"
        if not str(r.get("req") or "").strip():
            problems.append(f"{where}: thiếu `req` — không nói rõ yêu cầu nào")
        verdict = r.get("verdict")
        if verdict not in VERDICTS:
            problems.append(f"{where}: verdict phải là một trong {VERDICTS}, đang là {verdict!r}")
        if verdict == PASS_VERDICT and not (r.get("evidence") or []):
            problems.append(f"{where}: ghi 'đạt' mà không có bằng chứng nào")

    for where, e in _evidence_of(manifest):
        name = str(e.get("file") or "").strip()
        if not name:
            problems.append(f"{where}: bằng chứng thiếu `file`")
            continue
        if not str(e.get("proves") or "").strip():
            problems.append(f"{where}: {name} không có câu `proves` — nó chứng minh điều gì?")
        if not (base / name).is_file():
            problems.append(f"{where}: không tìm thấy tệp {name} trong {base}")

    if manifest["type"] in ("bug", "task"):
        rg = manifest.get("red_green") or {}
        if not (str(rg.get("red") or "").strip() and str(rg.get("green") or "").strip()):
            problems.append("thiếu red_green.red / red_green.green — D62-5 đòi một test đỏ khi gỡ thay đổi ra")

    changed = manifest.get("changed") or []
    touches_ui = any(any(p in str(c) for p in UI_PATHS) for c in changed)
    has_shot = any(str(e.get("file", "")).lower().endswith(IMAGE_SUFFIXES) for _, e in _evidence_of(manifest))
    if touches_ui and not has_shot:
        problems.append("diff đụng apps/web hoặc packages/ui mà không có ảnh chụp màn hình nào")

    for where, e in _evidence_of(manifest):
        if PROSE_MARKUP in str(e.get("proves") or ""):
            problems.append(f"{where}: `proves` phải là chữ thường, không dùng thẻ HTML")
    for field, val in (("requirement.text", (manifest.get("requirement") or {}).get("text")),
                       *((f"changed[{i}]", c) for i, c in enumerate(manifest.get("changed") or [])),
                       *((f"blockers[{i}]", b) for i, b in enumerate(manifest.get("blockers") or []))):
        if PROSE_MARKUP in str(val or ""):
            problems.append(f"{field}: phải là chữ thường, không dùng thẻ HTML")

    checklist = manifest.get("checklist") or {}
    for key in CHECKLIST_KEYS:
        if key not in checklist:
            problems.append(f"checklist thiếu {key}")
        elif checklist[key] is not True and not str(checklist[key] or "").strip():
            problems.append(f"checklist[{key}] chưa tick và cũng chưa ghi lý do")

    return problems


# ---------------------------------------------------------------- rendering

def _data_uri(path: pathlib.Path) -> str:
    """WebP at MAX_IMAGE_WIDTH when Pillow is around, raw bytes when it is not. A worker venv
    without Pillow gets a fatter file, never a broken one — the report matters more than its size.
    """
    raw = path.read_bytes()
    try:
        from PIL import Image
    except ImportError:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    im = Image.open(io.BytesIO(raw))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.width > MAX_IMAGE_WIDTH:
        im = im.resize((MAX_IMAGE_WIDTH, int(im.height * MAX_IMAGE_WIDTH / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80, method=4)
    return f"data:image/webp;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _ado_href(name: str, urls: dict) -> str | None:
    """The attachment's own ADO url, with the query string that makes ADO answer `image/png`
    instead of forcing a download — so clicking an inlined thumbnail opens the full original."""
    url = urls.get(name)
    if not url:
        return None
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}fileName={urllib.parse.quote(name)}&download=false"


def _evidence_html(e: dict, base: pathlib.Path, urls: dict) -> str:
    name = e["file"]
    proves = html.escape(e.get("proves", ""))
    href = _ado_href(name, urls)
    if name.lower().endswith(IMAGE_SUFFIXES):
        img = f'<img src="{_data_uri(base / name)}" alt="{html.escape(proves)[:120]}">'
        if href:
            img = f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{img}</a>'
        return f"<figure>{img}<figcaption>{proves}</figcaption></figure>"
    label = (f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{html.escape(name)}</a>'
             if href else f"<code>{html.escape(name)}</code>")
    return f'<li>{label}<div class="what">{proves}</div></li>'


def _split_evidence(entries, base, urls) -> tuple[str, str]:
    """Files become a list, images become figures under it — mixing the two in one <ul> reads
    badly at every width."""
    files = [e for e in entries if not str(e.get("file", "")).lower().endswith(IMAGE_SUFFIXES)]
    shots = [e for e in entries if str(e.get("file", "")).lower().endswith(IMAGE_SUFFIXES)]
    lis = "".join(_evidence_html(e, base, urls) for e in files)
    figs = "".join(_evidence_html(e, base, urls) for e in shots)
    return (f'<ul class="ev">{lis}</ul>' if lis else ""), figs


CSS = """
.rpt{--bg:#fbfbfa;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;
--ok:#1a7f4b;--okbg:#e7f5ec;--warn:#9a6400;--warnbg:#fdf3df;--bad:#b3261e;--badbg:#fbe9e7;--acc:#2b5fa8;
background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media(prefers-color-scheme:dark){.rpt{--bg:#16171a;--fg:#e8e8e4;--muted:#9a9a94;
--line:#2e3034;--card:#1d1f23;--ok:#6fd39b;--okbg:#16301f;--warn:#e0b25a;--warnbg:#332816;
--bad:#f2938c;--badbg:#3a1c1a;--acc:#83b0ef}}
.rpt *{box-sizing:border-box}
.rpt main{max-width:840px;margin:0 auto;padding:34px 22px 60px}
.rpt h1{font-size:21px;line-height:1.3;margin:0 0 4px;color:var(--fg)}
.rpt h2{font-size:15px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line);
text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.rpt .sub{color:var(--muted);font-size:13px;margin:0 0 18px}
.rpt .meta{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12.5px;color:var(--muted);
border:1px solid var(--line);background:var(--card);border-radius:8px;padding:11px 15px}
.rpt .meta b{color:var(--fg);font-weight:600}
.rpt table{width:100%;border-collapse:collapse;font-size:13.5px;margin:0}
.rpt th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
font-weight:600;padding:0 9px 6px 0;border-bottom:1px solid var(--line);background:none}
.rpt td{padding:10px 9px 10px 0;border-bottom:1px solid var(--line);vertical-align:top;background:none}
.rpt td.req{width:29%;font-weight:600}
.rpt .v{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
.rpt .v.ok{background:var(--okbg);color:var(--ok)}.rpt .v.warn{background:var(--warnbg);color:var(--warn)}
.rpt .v.bad{background:var(--badbg);color:var(--bad)}
.rpt ul.ev{margin:0;padding:0;list-style:none}
.rpt ul.ev li{margin-bottom:9px}.rpt ul.ev li:last-child{margin-bottom:0}
.rpt ul.ev a,.rpt ul.ev code{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--acc)}
.rpt .what{color:var(--muted);font-size:12.5px;margin-top:2px}
.rpt pre{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:11px 13px;
overflow-x:auto;font-size:11.5px;line-height:1.5;margin:6px 0 0;color:var(--fg);white-space:pre}
.rpt .cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:700px){.rpt .cols{grid-template-columns:1fr}.rpt td.req{width:auto}}
.rpt .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.rpt .lbl.r{color:var(--bad)}.rpt .lbl.g{color:var(--ok)}
.rpt td.code{font-family:ui-monospace,Menlo,monospace;font-weight:600;width:66px}
.rpt td.why{color:var(--muted);font-size:12.5px}
.rpt blockquote{margin:0 0 12px;padding:9px 14px;border-left:3px solid var(--acc);background:var(--card);font-size:14px}
.rpt figure{margin:12px 0 0}
.rpt figure img{width:100%;border:1px solid var(--line);border-radius:7px;display:block}
.rpt figcaption{color:var(--muted);font-size:12px;margin-top:5px}
.rpt code{font-family:ui-monospace,Menlo,monospace;font-size:.9em}
.rpt a{color:var(--acc)}.rpt p{margin:8px 0}
.rpt ul.plain{margin:6px 0;padding-left:19px}.rpt ul.plain li{margin-bottom:6px}
"""

_TONE = {"đạt": "ok", "đạt một phần": "warn", "chưa chứng minh": "warn", "không đạt": "bad"}


def render(manifest: dict, base_dir, urls: dict | None = None) -> str:
    """The manifest as one self-contained page. `urls` maps a filename to its ADO attachment url
    (from a previous attach run) so inlined images can link to their full-size original; without
    it the report still renders, just without those links."""
    base, urls = pathlib.Path(base_dir), (urls or {})
    m, esc = manifest, html.escape

    bits = []
    for label, key, mono in (("PR", "pr", False), ("Commit", "commit", True),
                             ("Người chạy", "run_by", False), ("Lúc", "run_at", False),
                             ("Môi trường", "env", True)):
        val = m.get(key)
        if not val:
            continue
        shown = f"<code>{esc(str(val))}</code>" if mono else esc(str(val))
        if key == "pr" and m.get("pr_url"):
            shown = f'<a href="{esc(m["pr_url"])}" target="_blank" rel="noopener">#{esc(str(val))}</a>'
        bits.append(f"<span><b>{label}</b> {shown}</span>")

    req = m.get("requirement") or {}
    req_block = ""
    if req.get("text"):
        src = f"<b>{esc(req['source'])}</b> — " if req.get("source") else ""
        req_lis, req_figs = _split_evidence(req.get("evidence") or [], base, urls)
        req_block = (f"<h2>Vé yêu cầu gì</h2><blockquote>{src}{esc(req['text'])}</blockquote>"
                     f"{req_lis}{req_figs}")

    changed = "".join(f"<li>{esc(c)}</li>" for c in m.get("changed") or [])
    changed_block = f'<h2>Đã sửa gì</h2><ul class="plain">{changed}</ul>' if changed else ""

    rows = []
    for r in m.get("results") or []:
        lis, figs = _split_evidence(r.get("evidence") or [], base, urls)
        note = f'<div class="what">{esc(r["note"])}</div>' if r.get("note") else ""
        rows.append(f'<tr><td class="req">{esc(r["req"])}</td>'
                    f'<td><span class="v {_TONE.get(r["verdict"], "warn")}">{esc(r["verdict"])}</span></td>'
                    f"<td>{lis}{figs}{note}</td></tr>")
    head = "Bằng chứng — chứng minh điều gì" if m["type"] != "doc" else "Nội dung — ở đâu"
    results_block = (f"<h2>Kết quả</h2><table><thead><tr><th>{'Yêu cầu' if m['type'] != 'doc' else 'Mục'}</th>"
                     f"<th></th><th>{head}</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")

    rg, rg_block = m.get("red_green") or {}, ""
    if rg.get("red") and rg.get("green"):
        how = f'<p class="what">{esc(rg["how"])}</p>' if rg.get("how") else ""
        rg_block = (f"<h2>Gỡ thay đổi ra thì test đỏ</h2>{how}"
                    f'<div class="cols"><div><div class="lbl r">đã gỡ</div><pre>{esc(rg["red"])}</pre></div>'
                    f'<div><div class="lbl g">khôi phục</div><pre>{esc(rg["green"])}</pre></div></div>')

    blockers = "".join(f"<li>{esc(b)}</li>" for b in m.get("blockers") or [])
    blockers_block = f'<h2>Còn vướng · cần quyết</h2><ul class="plain">{blockers}</ul>' if blockers else ""

    ck = []
    for key in CHECKLIST_KEYS:
        val = (m.get("checklist") or {}).get(key)
        done = val is True
        ck.append(f'<tr><td class="code">{key}</td><td>{esc(CHECKLIST_TEXT[key])}</td>'
                  f'<td><span class="v {"ok" if done else "warn"}">{"xong" if done else "chưa"}</span></td>'
                  f'<td class="why">{"" if done else esc(str(val))}</td></tr>')

    type_label = {"bug": "Bug", "task": "Task", "doc": "Tài liệu"}[m["type"]]
    sub = " · ".join(x for x in (type_label, m.get("sprint"), m.get("subtitle")) if x)
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Báo cáo AB#{esc(str(m['ticket']))}</title>
<style>html,body{{margin:0}}{CSS}</style></head><body class="rpt"><main>
<h1>AB#{esc(str(m['ticket']))} — {esc(m['title'])}</h1>
<p class="sub">{esc(sub)}</p>
<div class="meta">{''.join(bits)}</div>
{req_block}{changed_block}{results_block}{rg_block}{blockers_block}
<h2>Checklist DoD</h2><table><tbody>{''.join(ck)}</tbody></table>
</main></body></html>"""


def read_asset_map(path=None) -> dict[str, str]:
    """The image-to-asset-url map, or {} when there is none. Never raises: a missing or malformed
    map means screenshots render as links on the board, which is the state this file shipped in
    before assets existed — a degraded report beats no report."""
    try:
        return json.loads(pathlib.Path(path or ASSET_MAP_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def bundle(manifest: dict, base_dir, urls: dict | None = None, asset_map: dict | None = None) -> dict:
    """The same report as `render()`, but as data the board can build with its own h() helper.

    The board cannot take the html: assigning innerHTML fails SILENTLY inside the artifact
    sandbox, which would leave a blank cell with no error anywhere.

    Text and short urls only, never image BYTES. The board's data reaches the artifact through a
    `claude -p` session's prompt (bin/systemd/run-board-mirror.sh), so every byte here passes
    through a model's context on the way. A few KB of prose survives that verbatim; 135 KB of
    base64 does not — on 2026-09-10 the first bundle carrying screenshots came out the far end as
    a document the model had recomposed, with `evidence` emptied and two invented keys. Images
    therefore stay as `href` links to the ADO original (which opens fine in a tab) and are
    embedded only in the standalone html a person downloads. On the board a screenshot is served
    from the artifact's own asset store instead — same origin, so the sandbox's image-host block
    does not apply — and this only carries the short "/_blob/<id>" url that store hands back.
    Images with no asset yet are listed under `assets_missing` for whoever runs the uploads.
    """
    base, urls = pathlib.Path(base_dir), (urls or {})
    assets = read_asset_map() if asset_map is None else asset_map
    out = json.loads(json.dumps(manifest))  # never mutate the caller's manifest
    for entries in ([(out.get("requirement") or {}).get("evidence") or []]
                    + [r.get("evidence") or [] for r in out.get("results") or []]):
        for e in entries:
            name = e.get("file", "")
            if urls.get(name):
                e["href"] = _ado_href(name, urls)
            # An "/_blob/<id>" url is a few dozen characters, so it crosses the prompt intact
            # where the image itself never could. An image with no asset yet simply stays a link.
            if name.lower().endswith(IMAGE_SUFFIXES):
                digest = hashlib.sha256((base / name).read_bytes()).hexdigest()
                if assets.get(digest):
                    e["src"] = assets[digest]
                else:
                    out.setdefault("assets_missing", []).append(name)
    out["checklist_text"] = CHECKLIST_TEXT
    return out


def drop_previous_reports(ticket, *, run=None) -> int:
    """Remove the ticket's earlier report attachments, so resubmitting does not leave a pile of
    near-identical files for QC to guess between. Returns how many were dropped.

    Best-effort: a failed cleanup must never stop the new report from being attached — an extra
    stale copy is untidy, a missing report is a blocked handover.
    """
    import subprocess

    from attach_evidence import read_pat
    from dashboard import _ADO_ORG, _ADO_PROJECT

    run = run or subprocess.run
    pat = read_pat()
    base = f"{_ADO_ORG}/{_ADO_PROJECT}/_apis/wit/workitems/{ticket}"
    try:
        item = json.loads(run(["curl", "-sS", "-u", f":{pat}", f"{base}?$expand=relations&api-version=7.1"],
                              capture_output=True, text=True, timeout=30).stdout)
        stale = [i for i, r in enumerate(item.get("relations") or [])
                 if r.get("rel") == "AttachedFile"
                 and str((r.get("attributes") or {}).get("name") or "").startswith(f"report-AB{ticket}")]
        if not stale:
            return 0
        # Descending, and guarded by the revision we read: an index removed first would shift
        # every later one, and a concurrent edit would make all of them point at the wrong file.
        patch = [{"op": "test", "path": "/rev", "value": item["rev"]}] + \
                [{"op": "remove", "path": f"/relations/{i}"} for i in sorted(stale, reverse=True)]
        out = run(["curl", "-sS", "-u", f":{pat}", "-X", "PATCH",
                   "-H", "Content-Type: application/json-patch+json", "-d", json.dumps(patch),
                   f"{base}?api-version=7.1"], capture_output=True, text=True, timeout=30).stdout
        return len(stale) if "rev" in json.loads(out) else 0
    except Exception:
        return 0


def fetch_attachment_urls(ticket) -> dict[str, str]:
    """filename -> its ADO attachment url, so an inlined screenshot links to the full-size
    original and a log links to the copy already on the ticket.

    Best-effort by design: a failed read renders the same report with plain filenames instead of
    hyperlinks. A missing link is a cosmetic loss; refusing to produce the report over it would
    put a network outage between an engineer and submitting their work.
    """
    try:
        from dashboard import get_ado_attachments

        rows = get_ado_attachments([str(ticket)]).get(str(ticket)) or []
        return {r["name"]: r["url"] for r in rows if r.get("name") and r.get("url")}
    except Exception:
        return {}


# ---------------------------------------------------------------- cli

def build(manifest_path, out_path=None, *, urls: dict | None = None) -> pathlib.Path:
    """Validate then render. Raises ValueError listing every problem, so a bad manifest never
    silently becomes a thin report that looks complete."""
    path = pathlib.Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    problems = validate(manifest, base)
    if problems:
        raise ValueError("báo cáo chưa nộp được:\n  - " + "\n  - ".join(problems))
    out = pathlib.Path(out_path) if out_path else base / f"report-AB{manifest['ticket']}.html"
    out.write_text(render(manifest, base, urls), encoding="utf-8")
    # The board's copy, attached alongside: same content, shaped for h(). Named off the html so
    # dashboard.py finds it by pattern without needing anything else from the manifest.
    out.with_suffix(".json").write_text(
        json.dumps(bundle(manifest, base, urls), ensure_ascii=False), encoding="utf-8")
    return out


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="Path to report.json")
    parser.add_argument("--out", help="Output html (default: report-AB<ticket>.html beside the manifest)")
    parser.add_argument("--pr", help="PR number to also comment on when attaching")
    parser.add_argument("--check", action="store_true", help="Validate only — render nothing")
    parser.add_argument("--offline", action="store_true",
                        help="Skip the ADO lookup that turns evidence filenames into links")
    parser.add_argument("--apply", action="store_true", help="Attach the rendered report to the ticket")
    args = parser.parse_args(argv)

    path = pathlib.Path(args.manifest).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"evidence_report: không đọc được {path}: {exc}")
        return 1

    problems = validate(manifest, path.parent)
    if problems:
        print("evidence_report: báo cáo chưa nộp được:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if args.check:
        print(f"evidence_report: manifest hợp lệ ({len(manifest.get('results') or [])} yêu cầu)")
        return 0

    urls = {} if args.offline else fetch_attachment_urls(manifest["ticket"])
    out = build(path, args.out, urls=urls)
    print(f"evidence_report: đã ghi {out} ({out.stat().st_size // 1024} KB)")

    if args.apply:
        from attach_evidence import attach_evidence

        note = f"Báo cáo xác minh AB#{manifest['ticket']} (D62-4)"
        dropped = drop_previous_reports(manifest["ticket"])
        if dropped:
            print(f"evidence_report: gỡ {dropped} bản báo cáo cũ khỏi vé")
        # The html is what a person opens; the .json is what the board renders. Only the html gets
        # a PR comment — two links to the same report on one PR is noise.
        attach_evidence(manifest["ticket"], str(out), pr=args.pr or manifest.get("pr"),
                        note=note, dry_run=False)
        attach_evidence(manifest["ticket"], str(out.with_suffix(".json")),
                        note=note + " (bản cho bảng điều phối)", dry_run=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
