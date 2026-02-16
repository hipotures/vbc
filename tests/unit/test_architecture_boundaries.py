import ast
from pathlib import Path


def test_pipeline_layer_does_not_import_ui_layer():
    """Pipeline layer must not import from UI layer directly."""
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_dir = repo_root / "vbc" / "pipeline"

    violations = []
    for py_file in pipeline_dir.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        rel_path = py_file.relative_to(repo_root)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name == "vbc.ui" or name.startswith("vbc.ui."):
                        violations.append(f"{rel_path}:{node.lineno} imports {name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "vbc.ui" or module.startswith("vbc.ui."):
                    violations.append(f"{rel_path}:{node.lineno} imports from {module}")

    assert not violations, "Pipeline layer must not import UI layer:\n" + "\n".join(violations)
