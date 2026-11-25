"""Append-only object storage: a local directory or the Google Cloud Storage bucket.

Raw data and derived results are written once and never overwritten or deleted
(decision 0015). Both backends refuse a second write to the same key. On the
bucket, IAM also denies overwrite and delete to the workflow identity, so the
guarantee does not rest on this code alone; `self_check` verifies both layers.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

CONFIG_PATH = "config/storage.toml"
CHECK_PREFIX = "_check/"
_TMP_PREFIX = ".tmp-"


class ObjectExists(Exception):
    """The key is taken; append-only storage never overwrites."""


class Store(Protocol):
    def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def keys(self, prefix: str = "") -> list[str]: ...
    def sizes(self, prefix: str = "") -> dict[str, int]: ...


def load_config(repo_root: Path) -> dict[str, str]:
    """The [gcp] table of config/storage.toml."""
    with (repo_root / CONFIG_PATH).open("rb") as f:
        return tomllib.load(f)["gcp"]


def max_bucket_bytes(repo_root: Path) -> int:
    """Downloads stop before the bucket would pass this size (decision 0018)."""
    with (repo_root / CONFIG_PATH).open("rb") as f:
        return int(tomllib.load(f)["limits"]["max_bucket_bytes"])


def _check_key(key: str) -> None:
    parts = key.split("/")
    bad_part = any(p in ("", ".", "..") or p.startswith(_TMP_PREFIX) for p in parts)
    if not key or bad_part or "\\" in key:
        raise ValueError(f"对象名不合法：{key!r}")


class LocalStore:
    """A directory on this machine. Each object is written to a temporary file
    first and then hard-linked into place, which fails if the name is taken, so a
    crash never leaves a half-written object under a real key."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        _check_key(key)
        return self.root / key

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TMP_PREFIX)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.link(tmp, path)
        except FileExistsError as exc:
            raise ObjectExists(key) from exc
        finally:
            os.unlink(tmp)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(self.sizes(prefix))

    def sizes(self, prefix: str = "") -> dict[str, int]:
        if not self.root.is_dir():
            return {}
        found = {
            p.relative_to(self.root).as_posix(): p.stat().st_size
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.startswith(_TMP_PREFIX)
        }
        return {k: v for k, v in found.items() if k.startswith(prefix)}


class GcsStore:
    """The project bucket. Credentials come from the environment: on GitHub
    Actions, the federated identity set up by google-github-actions/auth."""

    def __init__(self, bucket: str, project: str, client: object | None = None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client(project=project)
        self._bucket = client.bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        from google.api_core.exceptions import Forbidden, PreconditionFailed

        _check_key(key)
        try:
            # Generation 0 means "only if no live object has this name".
            self._bucket.blob(key).upload_from_string(
                data, content_type=content_type, if_generation_match=0
            )
        except PreconditionFailed as exc:
            raise ObjectExists(key) from exc
        except Forbidden:
            # When the name is taken, IAM may refuse (no delete permission) before
            # the precondition is evaluated. Only a taken name counts as "exists".
            if self.exists(key):
                raise ObjectExists(key) from None
            raise

    def get(self, key: str) -> bytes:
        _check_key(key)
        return self._bucket.blob(key).download_as_bytes()

    def exists(self, key: str) -> bool:
        _check_key(key)
        return self._bucket.blob(key).exists()

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(self.sizes(prefix))

    def sizes(self, prefix: str = "") -> dict[str, int]:
        return {blob.name: int(blob.size) for blob in self._bucket.list_blobs(prefix=prefix)}

    def iam_denies_overwrite(self, key: str) -> bool:
        """Try a plain overwrite, which IAM should refuse. Only for check markers."""
        from google.api_core.exceptions import Forbidden

        _require_check_key(key)
        try:
            self._bucket.blob(key).upload_from_string(b"overwrite probe\n")
        except Forbidden:
            return True
        return False

    def iam_denies_delete(self, key: str) -> bool:
        """Try a delete, which IAM should refuse. Only for check markers."""
        from google.api_core.exceptions import Forbidden

        _require_check_key(key)
        try:
            self._bucket.blob(key).delete()
        except Forbidden:
            return True
        return False


def _require_check_key(key: str) -> None:
    _check_key(key)
    if not key.startswith(CHECK_PREFIX):
        raise ValueError(f"破坏性探测只能用在 {CHECK_PREFIX} 下的标记文件上：{key!r}")


@dataclass(frozen=True)
class CheckStep:
    name: str
    ok: bool
    detail: str = ""


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def self_check(store: Store, *, expect_append_only: bool = False) -> list[CheckStep]:
    """Write a small marker under _check/ and confirm append-only behaviour.

    The marker stays: nothing in this system deletes objects. With
    `expect_append_only`, also confirm that IAM refuses overwrite and delete,
    which only holds under the workflow identity, not the project owner.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{CHECK_PREFIX}{stamp}-{secrets.token_hex(4)}.txt"
    body = f"sqp storage check {stamp}\n".encode()
    steps: list[CheckStep] = []

    def run(name: str, action) -> bool:
        try:
            ok, detail = action()
        except Exception as exc:  # report every failure, never stop half-way silently
            ok, detail = False, _describe(exc)
        steps.append(CheckStep(name, ok, detail))
        return ok

    def create():
        store.put(key, body)
        return True, ""

    def read_back():
        return store.get(key) == body, "读回的内容和写入的不一致"

    def refuse_repeat():
        try:
            store.put(key, b"second write\n")
        except ObjectExists:
            return True, ""
        return False, "同名对象被写了第二次"

    def listed():
        return key in store.keys(CHECK_PREFIX), "列表里找不到刚写的对象"

    if not run("新建对象", create):
        return steps
    run("读回内容", read_back)
    run("重复写入被拒绝（代码层）", refuse_repeat)
    run("列出对象", listed)
    if expect_append_only:
        if not isinstance(store, GcsStore):
            steps.append(CheckStep("权限层检查", False, "只有谷歌云存储桶有权限层"))
            return steps
        run("覆盖被拒绝（权限层）", lambda: (store.iam_denies_overwrite(key), "覆盖成功了"))
        run("删除被拒绝（权限层）", lambda: (store.iam_denies_delete(key), "删除成功了"))
    return steps
