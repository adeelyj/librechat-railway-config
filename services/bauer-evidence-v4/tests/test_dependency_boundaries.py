from __future__ import annotations

import ast
import unittest
from pathlib import Path


class DependencyBoundaryTests(unittest.TestCase):
    def test_core_contracts_are_host_neutral(self) -> None:
        contract_root = (
            Path(__file__).resolve().parents[1] / "bauer_evidence_v4" / "contracts"
        )
        forbidden = ("librechat", "onix", "mongo", "adapter")
        for path in contract_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(
                [name for name in imports if any(token in name.lower() for token in forbidden)],
                path,
            )

    def test_adapters_do_not_import_canonical_storage(self) -> None:
        root = Path(__file__).resolve().parents[3] / "adapters"
        forbidden = ("canonical", "storage", "postgres", "object_store")
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(
                [name for name in imports if any(token in name.lower() for token in forbidden)],
                path,
            )


if __name__ == "__main__":
    unittest.main()
