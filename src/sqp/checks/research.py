"""Research boundary (decision 0022): study code reads data only through `sqp.data.pit`.

Study code lives in `src/sqp/research/studies/`. The runner hands each study a
`PointInTime` object and nothing else; this check makes sure a study cannot reach
around it. It is an allow-list at three levels:

1. modules: only those listed in ALLOWED_MODULES may be imported, by exact name;
2. names: `from m import n` must name something the checker itself finds in `m`
   (an unknown name fails closed), and it must not be a module off the list or a
   dangerous built-in (`dataclasses.sys`, `typing.operator`, `polars.os` are refused);
3. attribute chains on imported names (`pl.os.system`) are resolved the same way,
   step by step, as far as the checker can follow them.

On top of that, some names are refused wherever they appear: private names
(`pit._store`), names of functions that read or write files, dynamic access and
execution (`getattr`, `eval`, `open`), names of modules that reach the rest of the
process (`sys`, `os`, `operator`), and frame attributes (`gi_frame.f_globals`).

The checker resolves names by importing only modules on the allow-list, never the
pull request's code. For sqp modules it accepts only names defined in that module
itself, so a pull request cannot widen what a study may import by adding a
re-export next to it.

Like the point-in-time interface itself, this stops honest mistakes and makes a
deliberate bypass visible in review; it is not a sandbox. The hard boundary is
still the bucket's credentials (decision 0022).
"""

from __future__ import annotations

import ast
import builtins
import importlib
import re
import types
from pathlib import PurePosixPath

from sqp.checks import Violation
from sqp.config import Governance
from sqp.repo import Repo

RULE = "research-boundary"
STUDIES = "src/sqp/research/studies/"
PACKAGE = "sqp.research.studies"

# Modules a study may import, by exact name (submodules are not implied).
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
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
        "polars.selectors",
    }
)
# sqp modules a study may take names from, and which. None = any function or class
# defined in that module itself (not re-exported into it).
SQP_NAMES: dict[str, frozenset[str] | None] = {
    "sqp.data.pit": frozenset({"PointInTime", "AccessRefused", "IntegrityError", "Segment"}),
    "sqp.stats": None,
}
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
_DANGEROUS_OBJECTS = tuple(getattr(builtins, n) for n in sorted(FORBIDDEN_NAMES) if n[0] != "_")
# Names that read files, run text as a query that can itself read files (polars SQL
# has read_parquet() table functions), evaluate annotation strings as code, or are
# modules that reach the rest of the process. Refused even where the checker cannot
# tell what object they belong to.
OTHER_NAMES = frozenset(
    {
        "sql",
        "sql_expr",
        "SQLContext",
        "plugins",
        "register_plugin_function",
        "ForwardRef",
        "evaluate_forward_ref",
        "get_type_hints",
        "builtins",
        "bltns",
        "sys",
        "os",
        "io",
        "inspect",
        "operator",
        "types",
        "importlib",
        "subprocess",
        "pathlib",
        "shutil",
        "tempfile",
        "glob",
        "gc",
        "ctypes",
        "contextlib",
        "socket",
        "pickle",
        "marshal",
        "codecs",
        "urllib",
        "http",
        "dependencies",
    }
)
_IO_NAME = re.compile(r"^(read|scan|write|sink|load|save|serialize|deserialize)(_|$)")
# Frames and code objects reach every module's globals without a single underscore:
# (x for x in ()).gi_frame.f_globals["__builtins__"]["open"].
_FRAME_NAME = re.compile(r"^(gi|cr|ag|tb|f|co)_[a-z]+$")
_MISSING = object()


def _bad_name(name: str) -> str | None:
    if name.startswith("_"):
        return "以下划线开头的名字（私有属性或解释器内部）"
    if name in FORBIDDEN_NAMES:
        return "动态访问或执行的内置函数"
    if _IO_NAME.match(name):
        return "读写文件的函数"
    if name in OTHER_NAMES:
        return "能读文件、把文字当代码执行，或者能拿到别的模块"
    if _FRAME_NAME.match(name):
        return "解释器的栈帧和代码对象"
    return None


def _bad_object(obj: object) -> str | None:
    if isinstance(obj, types.ModuleType) and obj.__name__ not in ALLOWED_MODULES:
        return f"它是不在白名单上的模块 {obj.__name__}"
    if any(obj is d for d in _DANGEROUS_OBJECTS):
        return "它是动态访问或执行的内置函数"
    return None


def _package_of(path: str) -> list[str]:
    """The package a file's relative imports start from, as dotted parts."""
    parts = list(PurePosixPath(path).relative_to("src").with_suffix("").parts)
    parts.pop()  # a module's own name, or "__init__" for a package
    return parts


def _resolve(path: str, node: ast.ImportFrom) -> str | None:
    """Absolute module of a `from ... import`; None if it climbs above the top package."""
    if not node.level:
        return node.module or ""
    package = _package_of(path)
    if node.level - 1 >= len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _internal(module: str) -> bool:
    return module == PACKAGE or module.startswith(PACKAGE + ".")


def _from_import(module: str, name: str) -> tuple[object, str | None]:
    """The object `from module import name` gives, and why it is refused (or None)."""
    if module in SQP_NAMES:
        allowed = SQP_NAMES[module]
        obj = getattr(importlib.import_module(module), name, _MISSING)
        own = getattr(obj, "__module__", None) == module
        if (allowed is not None and name not in allowed) or not own:
            return obj, f"不能从 {module} 导入 {name}"
        return obj, None
    if module == "__future__":
        return _MISSING, None
    if module not in ALLOWED_MODULES:
        return _MISSING, f"研究代码不能导入 {module}.{name}（只能通过 sqp.data.pit 读数据）"
    obj = getattr(importlib.import_module(module), name, _MISSING)
    if obj is _MISSING:
        return obj, f"{module} 里没有 {name}（不认识的名字一律不放行）"
    if reason := _bad_object(obj):
        return obj, f"不能从 {module} 导入 {name}：{reason}"
    return obj, None


def _scan(path: str, source: str) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Violation(RULE, path, f"第 {exc.lineno} 行：无法解析（{exc.msg}）")]
    found = []
    bound: dict[str, object] = {}  # local name -> the object it was imported as

    def flag(node: ast.AST, message: str) -> None:
        found.append(Violation(RULE, path, f"第 {node.lineno} 行：{message}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES or alias.name == "__future__":
                    flag(node, f"研究代码不能 import {alias.name}（只能通过 sqp.data.pit 读数据）")
                elif alias.asname:
                    bound[alias.asname] = importlib.import_module(alias.name)
                else:
                    top = alias.name.split(".")[0]
                    bound[top] = importlib.import_module(top)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(path, node)
            if module is None:
                flag(node, "相对导入超出了顶层包")
                continue
            for alias in node.names:
                if alias.name == "*":
                    flag(node, f"不能 from {module} import *")
                elif reason := _bad_name(alias.name):
                    flag(node, f"不能导入 {alias.name}：{reason}")
                elif not _internal(module):  # other study code is checked by this rule
                    obj, reason = _from_import(module, alias.name)
                    if reason:
                        flag(node, reason)
                    elif obj is not _MISSING:
                        bound[alias.asname or alias.name] = obj
        elif isinstance(node, ast.Attribute):
            if reason := _bad_name(node.attr):
                flag(node, f"不能访问 .{node.attr}：{reason}")
        elif isinstance(node, ast.Name) and (reason := _bad_name(node.id)):
            flag(node, f"研究代码不能用 {node.id}：{reason}")
    return found + _chains(path, tree, bound)


def _chains(path: str, tree: ast.AST, bound: dict[str, object]) -> list[Violation]:
    """Follow `name.attr.attr` from imported names; refuse modules off the list and
    dangerous built-ins found on the way (pl.os, dataclasses.inspect)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or isinstance(node.ctx, ast.Del):
            continue
        chain: list[str] = []
        inner: ast.AST = node
        while isinstance(inner, ast.Attribute):
            chain.append(inner.attr)
            inner = inner.value
        if not (isinstance(inner, ast.Name) and inner.id in bound):
            continue
        obj, steps = bound[inner.id], [inner.id]
        for attr in reversed(chain):
            obj = getattr(obj, attr, _MISSING)
            steps.append(attr)
            if obj is _MISSING:
                break
            if reason := _bad_object(obj):
                where = ".".join(steps)
                found.append(
                    Violation(RULE, path, f"第 {node.lineno} 行：不能用 {where}：{reason}")
                )
                break
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
