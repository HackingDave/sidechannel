"""Tests that verify subprocess safety across the codebase."""

import ast
from pathlib import Path

NIGHTWIRE_ROOT = Path(__file__).parent.parent.parent / "nightwire"


def _find_subprocess_calls(filepath: Path):
    """Find all subprocess calls in a Python file and check safety."""
    issues = []
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return issues

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id

            # Check for dangerous subprocess patterns
            if func_name in ("system",):
                issues.append(
                    f"{filepath}:{node.lineno}: os.system() call"
                )

            # Check subprocess calls for shell=True
            if func_name in ("run", "call", "Popen", "check_output", "check_call",
                             "create_subprocess_shell"):
                for kw in node.keywords:
                    if (
                        kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        issues.append(
                            f"{filepath}:{node.lineno}: subprocess with shell=True"
                        )

    return issues


def test_no_shell_true_in_codebase():
    """No subprocess calls should use shell=True."""
    all_issues = []
    for py_file in NIGHTWIRE_ROOT.rglob("*.py"):
        all_issues.extend(_find_subprocess_calls(py_file))
    assert all_issues == [], "Unsafe subprocess calls found:\n" + "\n".join(all_issues)


def test_no_os_system_in_codebase():
    """No os.system() calls should exist (AST-checked to ignore string literals)."""
    all_issues = []
    for py_file in NIGHTWIRE_ROOT.rglob("*.py"):
        issues = _find_subprocess_calls(py_file)
        all_issues.extend(i for i in issues if "os.system()" in i)
    assert all_issues == [], "os.system() calls found:\n" + "\n".join(all_issues)
