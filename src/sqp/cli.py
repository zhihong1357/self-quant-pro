"""Command line: `sqp check`, `sqp power`, `sqp ledger append`, `sqp inherit scan|verify`,
`sqp storage check`."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from sqp import inherit, ledger, stats, storage
from sqp.checks import run_all
from sqp.repo import GitError, Repo


def _check(args: argparse.Namespace) -> int:
    try:
        repo = Repo.open(args.repo, args.base)
    except GitError as exc:
        print(f"无法打开仓库：{exc}")
        return 2
    violations = run_all(repo)
    for violation in violations:
        print(violation)
    if violations:
        print(f"\n共 {len(violations)} 处违规。规则说明见 docs/architecture.md 第 5 节。")
        return 1
    base = repo.base[:8] if repo.base else "无（跳过与基准比对的检查）"
    print(f"全部通过：{len(repo.files())} 个文件，比对基准 {base}。")
    return 0


def _power(args: argparse.Namespace) -> int:
    years = args.days / args.days_per_year
    hurdle = stats.min_detectable_sharpe(years, args.t, args.power)
    print(
        f"样本 {args.days:g} 天（{years:.2f} 年）：要有 {args.power:.0%} 的把握得到 "
        f"t ≥ {args.t:g}，年化夏普至少约 {hurdle:.2f}。"
    )
    if args.trials > 1:
        noise = stats.expected_max_sharpe(args.trials, years)
        print(f"累计试了 {args.trials} 次：纯噪声里最好的那个，年化夏普预期约 {noise:.2f}。")
    print("（正态近似，是下限；肥尾和自相关会让真实门槛更高。）")
    return 0


def _ledger_append(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload)
    if not isinstance(payload, dict):
        print("记录必须是 JSON 对象")
        return 2
    record = ledger.append(Path(args.file), payload)
    print(f"已追加第 {record['seq']} 条：{record['hash'][:12]}")
    return 0


def _inherit_scan(args: argparse.Namespace) -> int:
    root = Path(args.repo).resolve()
    config = tomllib.loads((root / "config" / "inheritance.toml").read_text("utf-8"))
    sources = {}
    for item in args.source:
        name, _, path = item.partition("=")
        sources[name] = Path(path).resolve()
    inherit.scan(config, sources, root / "registry" / inherit.OUT_DIR)
    print("已生成 registry/inherited/")
    return 0


def _inherit_verify(args: argparse.Namespace) -> int:
    problems = inherit.verify(Path(args.repo).resolve())
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("继承记录与固定提交逐字节一致。")
    return 0


def _storage_check(args: argparse.Namespace) -> int:
    if args.local:
        store: storage.Store = storage.LocalStore(Path(args.local))
        where = "本机目录"
    else:
        try:
            cfg = storage.load_config(Path(args.repo))
            store = storage.GcsStore(cfg["bucket"], cfg["project_id"])
        except Exception as exc:
            print(f"连不上谷歌云存储：{type(exc).__name__}: {exc}"[:300])
            return 2
        where = "谷歌云存储桶"
    steps = storage.self_check(store, expect_append_only=args.expect_append_only)
    for step in steps:
        line = f"{'通过' if step.ok else '失败'}  {step.name}"
        if not step.ok and step.detail:
            line += f"：{step.detail}"
        print(line)
    failed = sum(not step.ok for step in steps)
    if failed:
        print(f"\n{where}自检失败：{failed} 项。")
        return 1
    print(f"\n{where}自检全部通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqp", description="self-quant-pro：治理检查与研究工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="运行全部治理检查")
    p.add_argument("--repo", default=".", help="要检查的仓库，默认当前目录")
    p.add_argument("--base", help="比对基准：PR 的目标提交，或推送之前的提交")
    p.set_defaults(func=_check)

    p = sub.add_parser("power", help="注册前的功效与多重检验门槛")
    p.add_argument("--days", type=float, required=True, help="样本长度（天）")
    p.add_argument("--days-per-year", type=float, default=365.0, help="加密 365，股票 252")
    p.add_argument("--t", type=float, default=3.0, help="t 统计量门槛")
    p.add_argument("--power", type=float, default=0.8, help="想要的把握")
    p.add_argument("--trials", type=int, default=0, help="累计试验次数（含继承的）")
    p.set_defaults(func=_power)

    p = sub.add_parser("ledger", help="账本工具")
    ledger_sub = p.add_subparsers(dest="ledger_command", required=True)
    a = ledger_sub.add_parser("append", help="在账本末尾追加一条记录")
    a.add_argument("file", help="账本文件，例如 registry/ledger/approvals.jsonl")
    a.add_argument("payload", help="一条 JSON 对象")
    a.set_defaults(func=_ledger_append)

    p = sub.add_parser("inherit", help="继承记录")
    inherit_sub = p.add_subparsers(dest="inherit_command", required=True)
    s = inherit_sub.add_parser("scan", help="从前代仓库的干净副本生成 registry/inherited/")
    s.add_argument("--source", action="append", required=True, metavar="名字=路径")
    s.add_argument("--repo", default=".")
    s.set_defaults(func=_inherit_scan)
    v = inherit_sub.add_parser("verify", help="按固定提交重新生成并逐字节比对（需要联网）")
    v.add_argument("--repo", default=".")
    v.set_defaults(func=_inherit_verify)

    p = sub.add_parser("storage", help="存储工具")
    storage_sub = p.add_subparsers(dest="storage_command", required=True)
    c = storage_sub.add_parser("check", help="自检：写一个小标记文件，确认只增不改")
    c.add_argument("--repo", default=".", help="读哪个仓库的 config/storage.toml")
    c.add_argument("--local", help="检查这个本机目录，而不是谷歌云存储桶")
    c.add_argument(
        "--expect-append-only",
        action="store_true",
        help="同时确认权限层拒绝覆盖和删除；只在 GitHub 的数据工作流里用",
    )
    c.set_defaults(func=_storage_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
