"""宿主日志接口兼容性契约测试。"""

import ast
from pathlib import Path


def test_python_runtime_uses_only_host_logger_methods() -> None:
    """运行时代码不得调用 MoviePilot 日志器不存在的接口。"""

    root = Path(__file__).resolve().parents[1]
    forbidden_methods = {"exception", "warn", "bind"}
    forbidden_keywords = {"exc_info", "extra", "stack_info"}
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                if node.func.attr in forbidden_methods:
                    violations.append(f"{path.relative_to(root)}:{node.lineno} logger.{node.func.attr}")
                violations.extend(
                    f"{path.relative_to(root)}:{node.lineno} {keyword.arg}="
                    for keyword in node.keywords
                    if keyword.arg in forbidden_keywords
                )

    assert violations == []
