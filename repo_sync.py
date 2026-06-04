"""Push presets + assignments to GitHub via the Contents API.

Streamlit Cloud containers are ephemeral and have no git binary, so we can't
`git push`. The GitHub Contents API lets us create/update/delete individual
files with just a token — works from any container (and locally too).

Used by the "Commit presets" button so saved presets persist across restarts
and are picked up by the GitHub Actions alert cron.

Requires a fine-grained PAT with "Contents: Read and write" on the repo,
provided via st.secrets["github"]["token"].
"""

from __future__ import annotations

import base64
from pathlib import Path

import requests

API = "https://api.github.com"
TIMEOUT = 20


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_sha(repo: str, path: str, token: str, branch: str) -> str | None:
    r = requests.get(
        f"{API}/repos/{repo}/contents/{path}",
        headers=_headers(token), params={"ref": branch}, timeout=TIMEOUT,
    )
    if r.status_code == 200:
        return r.json().get("sha")
    return None


def put_file(repo: str, path: str, content: bytes, token: str, branch: str, message: str) -> str:
    sha = _get_sha(repo, path, token, branch)
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"{API}/repos/{repo}/contents/{path}",
        headers=_headers(token), json=payload, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return "updated" if sha else "created"


def delete_file(repo: str, path: str, token: str, branch: str, message: str) -> str:
    sha = _get_sha(repo, path, token, branch)
    if not sha:
        return "absent"
    r = requests.delete(
        f"{API}/repos/{repo}/contents/{path}",
        headers=_headers(token),
        json={"message": message, "sha": sha, "branch": branch},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return "deleted"


def _list_repo_strategy_files(repo: str, token: str, branch: str) -> list[str]:
    r = requests.get(
        f"{API}/repos/{repo}/contents/strategies",
        headers=_headers(token), params={"ref": branch}, timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return []
    return [it["name"] for it in r.json() if it.get("type") == "file" and it["name"].endswith(".json")]


def sync_presets(
    repo: str,
    token: str,
    branch: str,
    strategies_dir: Path,
    assignments_file: Path,
) -> dict:
    """Mirror local presets + assignments to the repo.

    - PUT every local strategies/*.json (create or update)
    - DELETE repo strategy files that no longer exist locally
    - PUT strategy_assignments.json

    Returns a summary dict with created/updated/deleted/errors lists.
    """
    results: dict[str, list[str]] = {"created": [], "updated": [], "deleted": [], "errors": []}
    local_files = {p.name for p in strategies_dir.glob("*.json")}

    for p in sorted(strategies_dir.glob("*.json")):
        try:
            status = put_file(
                repo, f"strategies/{p.name}", p.read_bytes(), token, branch,
                f"chore: sync preset {p.name}",
            )
            results[status].append(f"strategies/{p.name}")
        except requests.RequestException as e:
            results["errors"].append(f"strategies/{p.name}: {e}")

    try:
        for name in _list_repo_strategy_files(repo, token, branch):
            if name not in local_files:
                try:
                    delete_file(
                        repo, f"strategies/{name}", token, branch,
                        f"chore: remove preset {name}",
                    )
                    results["deleted"].append(f"strategies/{name}")
                except requests.RequestException as e:
                    results["errors"].append(f"delete strategies/{name}: {e}")
    except requests.RequestException as e:
        results["errors"].append(f"list repo strategies: {e}")

    if assignments_file.exists():
        try:
            status = put_file(
                repo, assignments_file.name, assignments_file.read_bytes(), token, branch,
                "chore: sync strategy assignments",
            )
            results[status].append(assignments_file.name)
        except requests.RequestException as e:
            results["errors"].append(f"{assignments_file.name}: {e}")

    return results
