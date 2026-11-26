"""Command line: `sqp check`, `sqp power`, `sqp ledger append`, `sqp inherit scan|verify`,
`sqp storage check`, `sqp data sync|build|profile`, `sqp trial run`."""

from __future__ import annotations

import argparse
import builtins
import json
import os
import secrets
import sys
import tomllib
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqp import inherit, ledger, stats, storage
from sqp.checks import run_all
from sqp.data import binance_api, build, gen1, profile, vision
from sqp.data.pit import AccessRefused, IntegrityError, PointInTime
from sqp.repo import GitError, Repo
from sqp.research import run


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


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


def _store_factory(args: argparse.Namespace) -> tuple[Callable[[], storage.Store], int]:
    """A store maker for this command, and the size the store may grow to."""
    if args.local:
        local = storage.LocalStore(Path(args.local))
        return (lambda: local), 10**15  # this machine's disk is not billed
    cfg = storage.load_config(Path(args.repo))
    limit = storage.max_bucket_bytes(Path(args.repo))
    return (lambda: storage.GcsStore(cfg["bucket"], cfg["project_id"])), limit


def _record_binance(args: argparse.Namespace) -> int:
    api = binance_api
    client = api.Client(api.http_get)
    try:
        if args.probe:
            status = api.probe(client)
            print(f"Binance 合约接口的状态码：{status}（200 表示这里能录）")
            return 0 if status == 200 else 1
        factory, limit = _store_factory(args)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        commit = os.environ.get("SQP_CODE_COMMIT", "")  # set by scripts/recorder/bootstrap.py
        report = api.record(client, factory(), now_ms=now_ms, limit_bytes=limit, code_commit=commit)
    except api.Unreachable as exc:
        print(f"Binance 合约接口没有放行，状态码 {exc}（451 表示这个地区被拒绝）。什么都没写。")
        return 1
    except api.Banned:
        print("Binance 暂时封了这个地址（418），已经停下。等下一次定时运行再试。")
        return 2
    except api.NotFinal as exc:
        print(f"合约列表或快照没取到，这次没有录历史数据：{exc}"[:300])
        return 1
    print(f"USDT 永续合约：{report.symbols} 个；请求 {client.requests} 次")
    print(f"写入：{'、'.join(report.written) or '无'}")
    if report.nothing_new:
        print(f"没有新数据：{'、'.join(report.nothing_new)}")
    if report.healed:
        print(f"补写了上次没写成的清单：{'、'.join(report.healed)}")
    if report.refused:
        print(f"接口明确拒绝、不会再试的请求：{report.refused} 个（例如合约已下架）")
    print(f"本次写入约 {_mb(report.stored_bytes)}")
    if report.failed:
        print(f"没写成、下次重试：{len(report.failed)} 项")
        for line in report.failed:
            print(f"  {line}")
    return 1 if report.failed else 0


def _data_sync(args: argparse.Namespace) -> int:
    root = Path(args.repo)
    source = vision.load_source(root)
    only = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None
    factory, limit = _store_factory(args)
    store = factory()
    sizes = store.sizes()
    total = sum(sizes.values())
    symbols = vision.resolve_symbols(vision.http_get, source, only)
    remotes = vision.list_upstream(vision.http_get, source, symbols, args.workers)
    stored = {k: v for k, v in sizes.items() if k.startswith(vision.RAW_PREFIX)}
    plan = vision.make_plan(remotes, stored, vision.recorded_etags(store), limit - total)
    sets = "，".join(f"{name} {len(names)} 个" for name, names in symbols.items())
    print(f"合约：{sets}")
    print(f"上游归档：{plan.upstream} 个文件，共 {_mb(plan.upstream_bytes)}")
    print(f"已存：{plan.present} 个；本次要下载：{len(plan.fetch)} 个")
    if plan.changed:
        print(f"上游替换过的已存文件：{len(plan.changed)} 个（不覆盖，记进清单）")
    if plan.capped:
        print(f"超出存储上限、这次不下载：{plan.capped} 个（上限 {_mb(limit)}）")
    if args.dry_run:
        print("只列计划，没有下载。")
        return 0
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    step = max(1, len(plan.fetch) // 10)

    def progress(done: int) -> None:
        if done % step == 0 or done == len(plan.fetch):
            print(f"进度：{done}/{len(plan.fetch)}", flush=True)

    outcome = vision.mirror(
        plan,
        fetch=vision.http_get,
        site=source.site,
        store_factory=factory,
        run_id=run_id,
        workers=args.workers,
        progress=progress,
    )
    print(f"已下载并校验：{outcome.stored} 个，共 {_mb(outcome.stored_bytes)}")
    if outcome.already:
        print(f"下载期间已被写入、跳过：{outcome.already} 个")
    print(f"存储桶合计：约 {_mb(total + outcome.stored_bytes)}")
    if outcome.failed:
        print(f"失败：{len(outcome.failed)} 个。前几个：")
        for line in outcome.failed[:5]:
            print(f"  {line}")
    return 1 if outcome.failed or plan.capped else 0


def _data_gen1(args: argparse.Namespace) -> int:
    root = Path(args.repo)
    branches = gen1.load_branches(root)
    factory, limit = _store_factory(args)
    store = factory()
    room = limit - sum(store.sizes().values())
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    report = gen1.import_branches(
        branches, Path(args.clones), store, run_id=run_id, room=room, now=now
    )
    for line in report.lines:
        print(line)
    print(f"清单：{report.manifest}" if report.manifest else "没有新提交，不写清单。")
    for alarm in report.alarms:
        print(f"报警：{alarm}")
    return 1 if report.alarms else 0


def _data_build(args: argparse.Namespace) -> int:
    source = vision.load_source(Path(args.repo))
    factory, limit = _store_factory(args)
    total = sum(factory().sizes().values())
    outcomes = build.build(source, factory, room=limit - total, workers=args.workers)
    for dataset, outcome in outcomes.items():
        line = f"{dataset}：新建 {outcome.parts} 个分块，{outcome.rows:,} 行，{_mb(outcome.bytes)}"
        if outcome.duplicates:
            line += f"；内容重复、只留一份的归档 {outcome.duplicates} 个"
        print(line)
        if outcome.checksums:
            print(f"  给以前建的分块补写校验值：{outcome.checksums} 个")
        if outcome.capped:
            print(f"  超出存储上限、这次没写：{len(outcome.capped)} 个月份")
    return 1 if any(o.capped for o in outcomes.values()) else 0


def _trial_run(args: argparse.Namespace) -> int:
    root = Path(args.repo)
    factory, _ = _store_factory(args)
    store = factory()
    failed, attempted = 0, set()
    while True:
        # Without a number, run every card that can run; a repeat becomes runnable once
        # its original has finished, so look again until nothing new is runnable.
        try:
            wanted = [args.trial] if args.trial else run.pending(root, store)
        except run.TrialRefused as exc:
            print(f"拒绝：{exc}")
            return 1
        wanted = [tid for tid in wanted if tid not in attempted]
        if not wanted:
            break
        for tid in wanted:
            attempted.add(tid)
            failed += _run_one(root, store, tid)
    if not attempted:
        print("没有等着跑的试验卡。")
    return 1 if failed else 0


def _error_name(exc: BaseException) -> str:
    """The error's type if it is a built-in one or one of the data layer's. A type made
    by study code is not named: its name could be built from data (type("S3.14", ...))."""
    kind = type(exc)
    if getattr(builtins, kind.__name__, None) is kind or kind in (AccessRefused, IntegrityError):
        return kind.__name__
    return "研究代码自己定义的错误类型（名字不打印）"


def _run_one(root: Path, store: storage.Store, tid: str) -> int:
    """Run one card and print only names and fingerprints; 1 if it did not finish cleanly."""
    try:
        done = run.run_trial(root, store, tid)
    except run.TrialRefused as exc:
        print(f"{tid} 拒绝：{exc}")
        return 1
    except Exception as exc:  # only the type: a message may carry numbers (decision 0011)
        print(f"{tid} 出错：{_error_name(exc)}。这次已开始，计入试验次数（决策 0025）")
        for frame in traceback.extract_tb(exc.__traceback__):  # where, never the values
            print(f"  {frame.filename}:{frame.lineno} {frame.name}")
        return 1
    for line in done.lines:
        print(line)
    for alarm in done.alarms:
        print(f"报警：{alarm}")
    return 1 if done.alarms else 0


def _data_profile(args: argparse.Namespace) -> int:
    root = Path(args.repo)
    factory, _ = _store_factory(args)
    store = factory()
    pit = PointInTime(root, store, cache_dir=Path(args.cache) if args.cache else None)
    try:
        key, fingerprint, existed = profile.write_profile(pit, store, root)
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"画像：{key}")
    print(f"指纹：{fingerprint}")
    print(f"输入：{len(pit.inputs)} 个分块")
    print("和已存的画像一模一样：重算一致。" if existed else "已写入；同名的 .md 是给人看的摘要。")
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

    p = sub.add_parser("data", help="数据层")
    data_sub = p.add_subparsers(dest="data_command", required=True)
    d = data_sub.add_parser("sync", help="把 Binance 公开数据站的月度归档原样存进存储（只增不改）")
    d.add_argument("--repo", default=".", help="读哪个仓库的数据源登记和存储配置")
    d.add_argument("--local", help="存进这个本机目录，而不是谷歌云存储桶")
    d.add_argument("--symbols", help="只同步这些永续合约（逗号分隔）及其现货，默认全部")
    d.add_argument("--dry-run", action="store_true", help="只列出计划，不下载")
    d.add_argument("--workers", type=int, default=16, help="并发数")
    d.set_defaults(func=_data_sync)
    g = data_sub.add_parser("gen1", help="把第一代录下的数据连同 git 历史归档进存储（只增不改）")
    g.add_argument("--repo", default=".", help="读哪个仓库的数据源登记和存储配置")
    g.add_argument("--local", help="存进这个本机目录，而不是谷歌云存储桶")
    g.add_argument("--clones", default=".gen1", help="第一代仓库的克隆放在哪里，每个仓库一个子目录")
    g.set_defaults(func=_data_gen1)
    b = data_sub.add_parser("build", help="把存好的归档整理成研究用的月度表（只增不改）")
    b.add_argument("--repo", default=".", help="读哪个仓库的数据源登记和存储配置")
    b.add_argument("--local", help="用这个本机目录，而不是谷歌云存储桶")
    b.add_argument("--workers", type=int, default=16, help="并发数")
    b.set_defaults(func=_data_build)
    f = data_sub.add_parser(
        "profile", help="重算 U1 探索集的数据画像，结果只写进存储（不打印数字）"
    )
    f.add_argument("--repo", default=".", help="读哪个仓库的研究宇宙和存储配置")
    f.add_argument("--local", help="用这个本机目录，而不是谷歌云存储桶")
    f.add_argument("--cache", help="把读过的月度表缓存在这个本机目录")
    f.set_defaults(func=_data_profile)

    p = sub.add_parser("record", help="录制（决策 0027）")
    record_sub = p.add_subparsers(dest="record_command", required=True)
    rb = record_sub.add_parser(
        "binance-um", help="从 Binance U 本位合约的公开行情接口录数据，原样存进存储（只增不改）"
    )
    rb.add_argument("--repo", default=".", help="读哪个仓库的存储配置")
    rb.add_argument("--local", help="存进这个本机目录，而不是谷歌云存储桶")
    rb.add_argument("--probe", action="store_true", help="只测这里能不能连上，打印状态码")
    rb.set_defaults(func=_record_binance)

    p = sub.add_parser("trial", help="试验（决策 0025）：先在 main 上登记，再跑")
    trial_sub = p.add_subparsers(dest="trial_command", required=True)
    r = trial_sub.add_parser("run", help="跑一张已登记的试验卡；不写编号就跑所有还没跑过的")
    r.add_argument("trial", nargs="?", default="", help="试验编号，例如 T-0001")
    r.add_argument("--repo", default=".", help="读哪个仓库的试验账本和存储配置")
    r.add_argument("--local", help="用这个本机目录，而不是谷歌云存储桶")
    r.set_defaults(func=_trial_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
