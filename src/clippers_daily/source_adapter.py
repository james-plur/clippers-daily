from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from .models import Record, RunReport


ALLOWED_IMPORTS = {"re", "json", "datetime", "urllib.parse", "typing", "logging", "httpx", "feedparser", "bs4",
                   "clippers_daily.models", "clippers_daily.collectors"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__", "breakpoint", "input"}


def adapter_dir() -> Path:
    configured = os.getenv("CLIPPERS_ADAPTER_DIR")
    if configured:
        return Path(configured)
    data = Path(os.getenv("CLIPPERS_DATA_DIR", "data"))
    return data / "source_adapters"


def adapter_path(source_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in source_id)
    return adapter_dir() / f"{safe}.py"


def validate_adapter_code(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(not any(name == allowed or name.startswith(allowed + ".") for allowed in ALLOWED_IMPORTS) for name in names):
                raise ValueError(f"适配器禁止导入：{names}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            raise ValueError(f"适配器禁止调用：{node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("适配器禁止访问双下划线属性")
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "collect" for node in tree.body):
        raise ValueError("适配器必须定义 collect(source, runtime)")


def run_source_adapter(source: dict, runtime: dict, path: Path | None = None, timeout: int = 90) -> tuple[list[Record], list[RunReport]] | None:
    path = path or adapter_path(source["id"])
    if not path.is_file():
        return None
    validate_adapter_code(path.read_text(encoding="utf-8"))
    payload = json.dumps({"source": source, "runtime": runtime}, ensure_ascii=False)
    package_root = str(Path(__file__).resolve().parents[1])
    python_path = os.pathsep.join(filter(None, (package_root, os.environ.get("PYTHONPATH", ""))))
    result = subprocess.run([sys.executable, "-m", "clippers_daily.source_adapter", str(path)],
                            input=payload, text=True, capture_output=True, timeout=timeout,
                            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": python_path})
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    data = json.loads(result.stdout)
    records = [Record.model_validate(item) for item in data.get("records", [])]
    reports = [RunReport.model_validate(item) for item in data.get("reports", [])]
    return records, reports


def _execute(path: Path) -> None:
    validate_adapter_code(path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("clippers_repair_adapter", path)
    module = importlib.util.module_from_spec(spec); assert spec.loader
    spec.loader.exec_module(module)
    request = json.load(sys.stdin)
    value = module.collect(request["source"], request["runtime"])
    if isinstance(value, tuple):
        records, reports = value
        value = {"records": [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in records],
                 "reports": [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in reports]}
    print(json.dumps(value, ensure_ascii=False, default=str))


if __name__ == "__main__":
    _execute(Path(sys.argv[1]))
