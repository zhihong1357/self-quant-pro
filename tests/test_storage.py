"""Append-only storage: both backends refuse a second write, and the self-check says so."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Forbidden, PreconditionFailed

from sqp import cli
from sqp.storage import GcsStore, LocalStore, ObjectExists, load_config, self_check

ROOT = Path(__file__).resolve().parents[1]


def test_local_store_is_create_only(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "lake")
    store.put("raw/a/b.zip", b"one")
    assert store.get("raw/a/b.zip") == b"one"
    assert store.exists("raw/a/b.zip")
    with pytest.raises(ObjectExists):
        store.put("raw/a/b.zip", b"two")
    assert store.get("raw/a/b.zip") == b"one"
    assert store.keys("raw/") == ["raw/a/b.zip"]
    assert not list((tmp_path / "lake" / "raw" / "a").glob(".tmp-*"))


@pytest.mark.parametrize("key", ["", "/abs", "a//b", "a/../b", "./a", "a\\b", "a/.tmp-x"])
def test_bad_keys_are_refused(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        LocalStore(tmp_path).put(key, b"x")


class FakeBucket:
    """In-memory bucket. `can_delete` stands for the project owner; the workflow
    identity has create + read only. `iam_first` makes IAM refuse a taken name
    before the generation precondition is looked at."""

    def __init__(self, *, can_delete: bool = False, iam_first: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.can_delete = can_delete
        self.iam_first = iam_first

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str = "") -> list[SimpleNamespace]:
        return [SimpleNamespace(name=n) for n in self.objects if n.startswith(prefix)]


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data: bytes, if_generation_match: int | None = None) -> None:
        taken = self.name in self.bucket.objects
        if taken and self.bucket.iam_first and not self.bucket.can_delete:
            raise Forbidden("overwrite needs storage.objects.delete")
        if taken and if_generation_match == 0:
            raise PreconditionFailed("generation mismatch")
        if taken and not self.bucket.can_delete:
            raise Forbidden("overwrite needs storage.objects.delete")
        self.bucket.objects[self.name] = data

    def download_as_bytes(self) -> bytes:
        return self.bucket.objects[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def delete(self) -> None:
        if not self.bucket.can_delete:
            raise Forbidden("no storage.objects.delete")
        del self.bucket.objects[self.name]


def gcs(bucket: FakeBucket) -> GcsStore:
    return GcsStore("bucket", "project", client=SimpleNamespace(bucket=lambda _name: bucket))


@pytest.mark.parametrize("iam_first", [False, True])
def test_gcs_store_never_overwrites(iam_first: bool) -> None:
    store = gcs(FakeBucket(iam_first=iam_first))
    store.put("raw/x.zip", b"one")
    with pytest.raises(ObjectExists):
        store.put("raw/x.zip", b"two")
    assert store.get("raw/x.zip") == b"one"


def test_gcs_permission_error_on_a_new_name_is_not_hidden() -> None:
    bucket = FakeBucket()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise Forbidden("no storage.objects.create")

    store = gcs(bucket)
    bucket.blob = lambda name: SimpleNamespace(upload_from_string=refuse, exists=lambda: False)
    with pytest.raises(Forbidden):
        store.put("raw/new.zip", b"x")


def test_self_check_passes_for_the_workflow_identity() -> None:
    bucket = FakeBucket()
    steps = self_check(gcs(bucket), expect_append_only=True)
    assert [s.ok for s in steps] == [True] * 6
    assert len(bucket.objects) == 1  # the marker stays


def test_self_check_flags_an_identity_that_can_delete() -> None:
    steps = self_check(gcs(FakeBucket(can_delete=True)), expect_append_only=True)
    failed = [s.name for s in steps if not s.ok]
    assert failed == ["覆盖被拒绝（权限层）", "删除被拒绝（权限层）"]


def test_destructive_probes_only_touch_check_markers() -> None:
    store = gcs(FakeBucket(can_delete=True))
    store.put("raw/x.zip", b"one")
    with pytest.raises(ValueError):
        store.iam_denies_delete("raw/x.zip")
    with pytest.raises(ValueError):
        store.iam_denies_overwrite("raw/x.zip")


def test_append_only_check_needs_the_bucket(tmp_path: Path) -> None:
    steps = self_check(LocalStore(tmp_path), expect_append_only=True)
    assert steps[-1].ok is False


def test_cli_checks_a_local_directory(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["storage", "check", "--local", str(tmp_path)]) == 0
    assert "本机目录自检全部通过" in capsys.readouterr().out


def test_storage_config_is_complete() -> None:
    cfg = load_config(ROOT)
    assert cfg["bucket"] and cfg["project_id"]
    assert cfg["workload_identity_provider"].startswith("projects/")
