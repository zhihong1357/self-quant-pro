"""Research boundary (decision 0022): study code reads data only through `sqp.data.pit`.

Study code lives in `src/sqp/research/studies/`. The runner hands each study a
`PointInTime` object and nothing else; this check makes sure a study cannot reach
around it. Imports are an allow-list, not a block-list, so a new way to read files
or the network is refused until someone adds it here, in review.

Like the point-in-time interface itself, this stops honest mistakes and makes a
deliberate bypass visible in review; it is not a sandbox. The hard boundary is
still the bucket's credentials (decision 0022).
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo

RULE = "research-boundary"
STUDIES = "src/sqp/research/studies/"
PACKAGE = "sqp.research.studies"

# Modules a study may import whole (`import x`, `from x import y`), with submodules.
ALLOWED_MODULES = (
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "itertools",
    "math",
    "random",
    "statistics",
    "typing",
    "polars",
    "sqp.stats",
    PACKAGE,
)
# Names a study may take from the point-in-time module, and nothing else from sqp.data.
PIT_MODULE = "sqp.data.pit"
PIT_NAMES = frozenset({"PointInTime", "AccessRefused", "IntegrityError", "Segment"})
# Built-ins that open files, run strings as code, or reach attributes by name.
FORBIDDEN_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
# Attribute and imported names that read or write files (polars' read_parquet, scan_csv,
# write_parquet, deserialize, ...) or run text as a query that can itself read files
# (polars SQL has read_parquet() table functions). Names starting with "_" reach
# private state (pit._store) or the interpreter (__class__, __globals__).
_IO_NAME = re.compile(r"^(read|scan|write|sink|load|save|serialize|deserialize)(_|$)")
OTHER_NAMES = frozenset(
    {
        "sql",
        "SQLContext",
        "plugins",
        "register_plugin_function",
        "ForwardRef",
        "evaluate_forward_ref",
        "get_type_hints",  # evaluates annotation strings as code
    }
)
# Frames and code objects reach every module's globals without a single underscore:
# (x for x in ()).gi_frame.f_globals["__builtins__"]["open"].
_FRAME_NAME = re.compile(r"^(gi|cr|ag|tb|f|co)_[a-z]+$")


def _allowed_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in ALLOWED_MODULES)


def _bad_name(name: str) -> str | None:
    if name.startswith("_"):
        return "以下划线开头的名字（私有属性或解释器内部）"
    if _IO_NAME.match(name):
        return "读写文件的函数"
    if name in OTHER_NAMES:
        return "能读文件或把文字当代码执行"
    if _FRAME_NAME.match(name):
        return "解释器的栈帧和代码对象"
    return None


def _module_of(path: str) -> str:
    """Dotted module name of a file under src/, e.g. sqp.research.studies.h0001."""
    parts = list(PurePosixPath(path).relative_to("src").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(path: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = _module_of(path).split(".")
    if not path.endswith("__init__.py"):
        package.pop()  # a module's relative imports start from its package
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _scan(path: str, source: str) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Violation(RULE, path, f"第 {exc.lineno} 行：无法解析（{exc.msg}）")]
    found = []

    def flag(node: ast.AST, message: str) -> None:
        found.append(Violation(RULE, path, f"第 {node.lineno} 行：{message}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqp" or alias.name.startswith("sqp."):
                    flag(node, f"不能 import {alias.name}：sqp 的模块只能用 from … import 取名字")
                elif not _allowed_module(alias.name):
                    flag(node, f"研究代码不能导入 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(path, node)
            for alias in node.names:
                if alias.name == "*":
                    flag(node, f"不能 from {module} import *")
                    continue
                if module == PIT_MODULE:
                    if alias.name not in PIT_NAMES:
                        allowed = "、".join(sorted(PIT_NAMES))
                        flag(node, f"从 {PIT_MODULE} 只能取 {allowed}，不能取 {alias.name}")
                    continue
                full = f"{module}.{alias.name}"
                if not (_allowed_module(module) or (module == "sqp" and full == "sqp.stats")):
                    flag(node, f"研究代码不能导入 {full}（只能通过 {PIT_MODULE} 读数据）")
                elif reason := _bad_name(alias.name):
                    flag(node, f"不能导入 {alias.name}：{reason}")
        elif isinstance(node, ast.Attribute):
            if reason := _bad_name(node.attr):
                flag(node, f"不能访问 .{node.attr}：{reason}")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id in OTHER_NAMES):
            flag(node, f"研究代码不能用 {node.id}")
    return found


def check(repo: Repo, cfg: Governance, paths: list[str]) -> list[Violation]:
    found = []
    for path in paths:
        if not path.startswith(STUDIES):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".md":
            continue
        if suffix != ".py":
            found.append(Violation(RULE, path, "研究代码目录里只能放 .py 和 .md 文件"))
            continue
        data = repo.read(path)
        if data is not None:
            found += _scan(path, data.decode("utf-8", "replace"))
    return found
