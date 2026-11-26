"""Cloud Run entry point of the Binance recorder (decision 0027).

The job's image is the stock python:3.12-slim from Docker Hub, and its command only
fetches this file from main and runs it (scripts/gcp/setup-recorder.sh). Each run then
downloads main at one commit, installs the pinned uv, and runs `sqp record binance-um`
with the locked dependencies. What runs is exactly main: merging a PR into main is the
deployment, and main only changes through reviewed, green PRs (decision 0027, item 3).

Standard library only: the image has nothing else.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO = "zhihong1357/self-quant-pro"
UV = "0.11.32"


def _get(url: str, timeout: float = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "self-quant-pro-recorder"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def main_commit() -> str:
    """The commit main points at now, so the code downloaded is one fixed snapshot."""
    body = _get(f"https://api.github.com/repos/{REPO}/commits/main", timeout=30)
    sha = json.loads(body)["sha"]
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("GitHub 返回的提交号不对")
    return sha


def main() -> int:
    sha = main_commit()
    print(f"录制开始：main 的提交 {sha}", flush=True)
    work = tempfile.mkdtemp(prefix="sqp-")
    archive = _get(f"https://codeload.github.com/{REPO}/zip/{sha}")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        bundle.extractall(work)
    root = os.path.join(work, f"{REPO.split('/')[1]}-{sha}")
    pip = [sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", f"uv=={UV}"]
    subprocess.run(pip, check=True)
    env = dict(os.environ, UV_CACHE_DIR=os.path.join(work, ".uv-cache"), SQP_CODE_COMMIT=sha)
    command = ["uv", "run", "--locked", "--no-dev", "--directory", root]
    command += ["sqp", "record", "binance-um", "--repo", root]
    if os.environ.get("SQP_PROBE") == "1":  # setup-recorder.sh's first, two-minute run
        command.append("--probe")
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main())
