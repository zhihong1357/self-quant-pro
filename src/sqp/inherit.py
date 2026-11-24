"""Snapshot, and later re-verify, what generation 3 inherits from earlier generations.

`scan` reads clean checkouts of the source repositories and writes
registry/inherited/: a manifest (pinned commits, file hashes), copies of the trial
ledgers, and trials.json (every inherited trial, classified, with the data dates it
touched). `verify` fetches the pinned commits into a temporary directory, scans again
and compares byte for byte.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
OUT_DIR = "inherited"
NOTE = (
    "由 sqp inherit scan 生成，不要手改。data.first/last 是试验结果里出现过的日期范围"
    "（启发式，偏保守），last 不晚于记录时间。counted 表示计入多重检验的试验次数。"
)


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def _match(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def _collect_dates(obj: object, acc: list[str]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_dates(value, acc)
    elif isinstance(obj, list):
        for value in obj:
            _collect_dates(value, acc)
    elif isinstance(obj, str):
        match = DATE.match(obj)
        if match:
            acc.append(match.group(1))


def _span(obj: object, recorded_at: str) -> dict | None:
    dates: list[str] = []
    _collect_dates(obj, dates)
    if not dates:
        return None
    cap = recorded_at[:10]
    last = min(max(dates), cap)
    return {"first": min(min(dates), last), "last": last}


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _myquant(src: Path, files: list[str], studies: dict) -> list[dict]:
    records = []
    for file in files:
        study = file.split("/")[1]
        if study not in studies:
            raise RuntimeError(f"myquant 的研究 {study} 没有在 config/inheritance.toml 里分类")
        label = studies[study]
        for row in _lines(src / file):
            result = row.get("result", {})
            records.append(
                {
                    "source": "myquant",
                    "study": study,
                    "trial_id": row["trial_id"],
                    "hypothesis_id": row["hypothesis_id"],
                    "status": row["status"],
                    "recorded_at": row["recorded_at"],
                    "domain": label["domain"],
                    "family": label["family"],
                    "counted": row["status"] == "completed"
                    and "metrics" in result
                    and label["domain"] != "synthetic",
                    "data": _span(result, row["recorded_at"]),
                    "fingerprint": row.get("record_hash"),
                }
            )
    return records


def _finance_research(src: Path, files: list[str], studies: dict) -> list[dict]:
    universes = studies["universes"]
    records = []
    for file in files:
        for row in _lines(src / file):
            hypothesis = row["hypothesis"]
            card_path = src / "registry" / "hypotheses" / f"{hypothesis}.toml"
            card = tomllib.loads(card_path.read_text("utf-8"))
            universe = card.get("universe") or card.get("scope", {}).get("universe", "")
            if universe not in universes:
                raise RuntimeError(f"finance-research 的研究宇宙 {universe} 没有分类")
            domain = universes[universe]
            records.append(
                {
                    "source": "finance-research",
                    "study": hypothesis,
                    "trial_id": row["trial_id"],
                    "hypothesis_id": hypothesis,
                    "status": "completed" if row.get("metrics") else "unknown",
                    "recorded_at": row["recorded_at"],
                    "domain": domain,
                    "family": f"finance-research.{hypothesis}",
                    "counted": bool(row.get("metrics")) and domain != "synthetic",
                    "segment": row.get("segment"),
                    "data": _span(row.get("manifest", {}), row["recorded_at"]),
                    "fingerprint": row.get("result_fingerprint"),
                }
            )
    return records


PARSERS = {"myquant": _myquant, "finance-research": _finance_research}


def _summary(records: list[dict]) -> dict:
    by_domain: dict[str, int] = {}
    families: dict[str, dict] = {}
    for record in records:
        if record["counted"]:
            domains = ["crypto", "tradfi"] if record["domain"] == "mixed" else [record["domain"]]
            for domain in domains:
                by_domain[domain] = by_domain.get(domain, 0) + 1
        family = families.setdefault(
            record["family"],
            {"domain": record["domain"], "trials": [], "counted": 0, "first": None, "last": None},
        )
        family["trials"].append(f"{record['source']}:{record['trial_id']}")
        family["counted"] += int(record["counted"])
        if record["data"]:
            firsts = [d for d in (family["first"], record["data"]["first"]) if d]
            lasts = [d for d in (family["last"], record["data"]["last"]) if d]
            family["first"], family["last"] = min(firsts), max(lasts)
    return {"counted_by_domain": dict(sorted(by_domain.items())), "families": families}


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")


def scan(config: dict, sources: dict[str, Path], out: Path) -> None:
    if out.name != OUT_DIR:
        raise ValueError(f"输出目录必须叫 {OUT_DIR}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest: dict = {"generated_by": "sqp inherit scan", "sources": []}
    records: list[dict] = []
    for source in config["source"]:
        name = source["name"]
        src = sources[name]
        if _git(src, "status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError(f"{name} 的工作区有未提交的改动，先还原再扫描")
        tracked = sorted(p for p in _git(src, "ls-files", "-z").split("\0") if p)
        entries = []
        for path in (p for p in tracked if _match(p, source["pin"])):
            data = (src / path).read_bytes()
            entries.append(
                {
                    "path": path,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "lines": data.count(b"\n"),
                }
            )
            if _match(path, source.get("vendor", [])):
                target = out / name / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        trial_files = [p for p in tracked if _match(p, source.get("trials", []))]
        studies = config.get("studies", {}).get(name, {})
        records += PARSERS[source["format"]](src, trial_files, studies)
        manifest["sources"].append(
            {
                "name": name,
                "url": source["url"],
                "generation": source["generation"],
                "role": source["role"],
                "commit": _git(src, "rev-parse", "HEAD").strip(),
                "files": entries,
            }
        )
    records.sort(key=lambda r: (r["source"], r["study"], r["trial_id"]))
    _write_json(out / "manifest.json", manifest)
    _write_json(
        out / "trials.json", {"note": NOTE, "summary": _summary(records), "records": records}
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def verify(repo_root: Path) -> list[str]:
    """Problems found when regenerating registry/inherited/ from the pinned commits."""
    config = tomllib.loads((repo_root / "config" / "inheritance.toml").read_text("utf-8"))
    committed = repo_root / "registry" / OUT_DIR
    manifest = json.loads((committed / "manifest.json").read_text("utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        sources = {}
        for source in manifest["sources"]:
            checkout = Path(tmp) / "src" / source["name"]
            checkout.mkdir(parents=True)
            _git(checkout, "init", "-q")
            _git(checkout, "fetch", "-q", "--depth", "1", source["url"], source["commit"])
            _git(checkout, "checkout", "-q", "FETCH_HEAD")
            sources[source["name"]] = checkout
        regenerated = Path(tmp) / "out" / OUT_DIR
        scan(config, sources, regenerated)
        old, new = _tree(committed), _tree(regenerated)
    problems = [f"仓库里多出：{p}" for p in sorted(set(old) - set(new))]
    problems += [f"仓库里缺少：{p}" for p in sorted(set(new) - set(old))]
    problems += [f"内容不一致：{p}" for p in sorted(set(old) & set(new)) if old[p] != new[p]]
    return problems
