"""Cloud Run entry point of the Binance recorder (decision 0027).

The job's image is the stock python:3.12-slim from Docker Hub, and its command only
fetches this file from main and runs it (scripts/gcp/setup-recorder.sh). Each run then
downloads main as one archive, installs the pinned uv, and runs `sqp record binance-um`
with the locked dependencies. The archive names its own commit (git stores the commit
ID as the zip comment), so no anonymous GitHub API call is needed, and there is no race
between finding the commit and downloading it. What runs is exactly main: merging a PR
into main is the deployment, and main only changes through reviewed, green PRs
(decision 0027, item 3).

Standard library only: the image has nothing else.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = "zhihong1357/self-quant-pro"
UV = "0.11.32"


SHA = re.compile(r"^[0-9a-f]{40}$")


def _get(url: str, timeout: float = 120, attempts: int = 3) -> bytes:
    """GET with a few spaced retries: shared egress addresses meet rate limits now and then."""
    request = urllib.request.Request(url, headers={"User-Agent": "self-quant-pro-recorder"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.read()
        except OSError:  # URLError, HTTPError, timeouts, resets
            if attempt == attempts - 1:
                raise
            time.sleep(15 * 2**attempt)
    raise AssertionError("unreachable")


def commit_of(bundle: zipfile.ZipFile) -> str:
    """The commit an archive was made from: its zip comment, else GitHub's API."""
    comment = bundle.comment.decode("ascii", "replace").strip()
    if SHA.match(comment):
        return comment
    try:
        sha = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/main", 30))["sha"]
    except (OSError, ValueError, KeyError):
        return "unknown"
    return sha if SHA.match(sha) else "unknown"


def main() -> int:
    work = tempfile.mkdtemp(prefix="sqp-")
    archive = _get(f"https://codeload.github.com/{REPO}/zip/refs/heads/main")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        sha = commit_of(bundle)
        tops = {name.split("/", 1)[0] for name in bundle.namelist()}
        if len(tops) != 1:
            raise ValueError("下载的代码包结构不对")
        bundle.extractall(work)
    print(f"录制开始：main 的提交 {sha}", flush=True)
    root = os.path.join(work, tops.pop())
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
