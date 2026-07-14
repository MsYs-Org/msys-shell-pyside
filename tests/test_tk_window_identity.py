from __future__ import annotations

import ast
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "msys_shell_pyside"


def _tk_constructors(path: Path) -> tuple[list[tuple[str, ast.Call]], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constructors: list[tuple[str, ast.Call]] = []
    identity_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and "MSYS_WINDOW_IDENTITY" in ast.unparse(value):
                identity_names.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"Tk", "Toplevel"}:
            constructors.append((node.func.attr, node))
    return constructors, identity_names


class TkWindowIdentityContractTests(unittest.TestCase):
    def test_every_tk_window_has_the_manifest_identity_at_creation(self) -> None:
        """WM_CLASS cannot be repaired after a Tk window has been created."""

        windows: list[str] = []
        violations: list[str] = []
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            constructors, identity_names = _tk_constructors(path)
            for constructor, call in constructors:
                windows.append(f"{path.name}:{call.lineno}:{constructor}")
                required_keyword = "className" if constructor == "Tk" else "class_"
                identity = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == required_keyword
                    ),
                    None,
                )
                uses_identity = identity is not None and (
                    "MSYS_WINDOW_IDENTITY" in ast.unparse(identity)
                    or isinstance(identity, ast.Name) and identity.id in identity_names
                )
                if not uses_identity:
                    violations.append(
                        f"{path.name}:{call.lineno} {constructor} requires "
                        f"{required_keyword}=MSYS_WINDOW_IDENTITY"
                    )

        self.assertTrue(windows, "the shell package should create Tk windows")
        self.assertFalse(violations, "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
