#!/usr/bin/env python3
"""attach_evidence.py — puts verification evidence where the board's evidence rule (see
board_state.py's forthcoming evidence_drift()) expects to find it: attached to the ADO ticket,
and on the PR when one exists.

    attach_evidence.py --ticket 8309 --file shot.png [--pr 719] [--note "..."] [--apply]

Two REST calls, both authenticated the same way `az devops login` already works today:
  1. POST .../_apis/wit/attachments?fileName=... with the raw file bytes -> an attachment url.
  2. PATCH the work item with a JSON-Patch `add` of an AttachedFile relation pointing at it.
When --pr is given, a third call posts a PR comment naming the evidence and linking the ADO url,
so a reviewer sees the proof without leaving the PR.

Auth: a PAT already stored at ~/.azure/azuredevops/personalAccessTokens under
`[azdevops-cli:<org>]`, key `personal access token` — read fresh at call time by read_pat(),
never cached beyond one call, never printed, never placed in a log line, a commit, or a test
fixture. `curl -u ":$PAT"` is the working shape.

dry-run is the default and touches NOTHING — it does not even read the PAT file. --apply is
required to actually upload/link/comment.
"""

import configparser
import json
import os
import pathlib
import subprocess
from urllib.parse import quote

from dashboard import _ADO_ORG, _ADO_PROJECT

DEFAULT_PAT_PATH = os.path.expanduser("~/.azure/azuredevops/personalAccessTokens")


def read_pat(org: str = _ADO_ORG, path: str = DEFAULT_PAT_PATH) -> str:
    """The az devops CLI's own stored PAT for `org`, read fresh every call — never module-cached,
    never logged. Raises (NoSectionError/NoOptionError) on a missing/misconfigured file rather
    than handing curl an empty string, which would fail as an opaque 401 with no clue why.
    """
    parser = configparser.ConfigParser()
    parser.read(path)
    return parser.get(f"azdevops-cli:{org}", "personal access token")


def upload_attachment(file_path: str, *, org: str = _ADO_ORG, project: str = _ADO_PROJECT,
                       pat: str, run=subprocess.run) -> str:
    """POST the raw bytes to ADO's attachment endpoint. Returns the attachment's own `url` — the
    one thing link_attachment() needs to point the work item at it.
    """
    filename = os.path.basename(file_path)
    url = f"{org}/{project}/_apis/wit/attachments?fileName={quote(filename)}&api-version=7.1"
    result = run(
        ["curl", "-sS", "-f", "-u", f":{pat}",
         "-H", "Content-Type: application/octet-stream",
         "--data-binary", f"@{file_path}", url],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl upload exited {result.returncode}: {(result.stderr or '').strip()[:300]}")
    return json.loads(result.stdout)["url"]


def link_attachment(ticket, attachment_url: str, note: str | None, *,
                     org: str = _ADO_ORG, project: str = _ADO_PROJECT,
                     pat: str, run=subprocess.run) -> None:
    """PATCH the work item with a JSON-Patch `add` of an AttachedFile relation."""
    patch = json.dumps([{
        "op": "add", "path": "/relations/-",
        "value": {"rel": "AttachedFile", "url": attachment_url, "attributes": {"comment": note or ""}},
    }])
    url = f"{org}/{project}/_apis/wit/workitems/{ticket}?api-version=7.1"
    result = run(
        ["curl", "-sS", "-f", "-u", f":{pat}", "-X", "PATCH",
         "-H", "Content-Type: application/json-patch+json",
         "-d", patch, url],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl link exited {result.returncode}: {(result.stderr or '').strip()[:300]}")


def comment_on_pr(pr, body: str, run=subprocess.run, cwd: str | None = None) -> None:
    """`gh pr comment` — so a reviewer sees the proof without leaving the PR.

    `cwd` is the evidence file's own directory, because `gh` resolves a PR number against the
    repo it is standing in. Run from anywhere else — this board's repo, say — and a valid PR
    number resolves against the wrong project and fails, leaving the ticket holding evidence the
    PR never hears about (which is what happened the first time this ran for real, AB#8382).
    """
    result = run(["gh", "pr", "comment", str(pr), "--body", body],
                 capture_output=True, text=True, timeout=30, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr comment exited {result.returncode}: {(result.stderr or '').strip()[:300]}")


def _pr_comment_body(ticket, filename: str, attachment_url: str, note: str | None) -> str:
    body = f"Evidence attached to AB#{ticket}: {filename}\n{attachment_url}"
    return body + f"\n\n{note}" if note else body


def attach_evidence(ticket, file_path: str, pr=None, note: str | None = None, dry_run: bool = True, *,
                     org: str = _ADO_ORG, project: str = _ADO_PROJECT,
                     read_pat=read_pat, upload=upload_attachment, link=link_attachment,
                     comment=comment_on_pr) -> dict:
    """Upload -> link -> (if `pr`) comment. dry-run announces the plan and calls none of the four
    injected steps — not even read_pat, so a dry run never so much as opens the credential file.
    """
    filename = os.path.basename(file_path)
    if dry_run:
        msg = f"[dry-run] would upload {filename} and attach it to AB#{ticket}"
        if pr:
            msg += f", and comment on PR #{pr}"
        print(msg)
        return {"ticket": str(ticket), "file": filename, "pr": str(pr) if pr else None,
                "applied": False, "url": None}

    pat = read_pat(org=org)
    url = upload(file_path, org=org, project=project, pat=pat)
    link(ticket, url, note, org=org, project=project, pat=pat)
    if pr:
        # The evidence file's own directory: `gh` needs to be standing in the repo the PR
        # belongs to, and that is the only repo the file could have come out of.
        comment(pr, _pr_comment_body(ticket, filename, url, note),
                cwd=str(pathlib.Path(file_path).resolve().parent))
    print(f"attach_evidence: attached {filename} to AB#{ticket}: {url}")
    return {"ticket": str(ticket), "file": filename, "pr": str(pr) if pr else None,
            "applied": True, "url": url}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticket", required=True, help="ADO work item id")
    parser.add_argument("--file", required=True, help="Path to the evidence file to upload")
    parser.add_argument("--pr", help="PR number to also comment on")
    parser.add_argument("--note", help="Comment recorded on the AttachedFile relation")
    parser.add_argument("--apply", action="store_true",
                         help="Actually upload/link/comment. Without this flag, prints the plan and touches nothing.")
    args = parser.parse_args(argv)

    try:
        # Looked up by (global) name here rather than relied on as attach_evidence()'s own
        # keyword defaults, which are bound once at function-definition time — this indirection
        # is what lets a test monkeypatch this module's functions and have main() see it.
        attach_evidence(
            args.ticket, args.file, pr=args.pr, note=args.note, dry_run=not args.apply,
            read_pat=read_pat, upload=upload_attachment, link=link_attachment, comment=comment_on_pr,
        )
    except Exception as exc:
        print(f"attach_evidence: failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
