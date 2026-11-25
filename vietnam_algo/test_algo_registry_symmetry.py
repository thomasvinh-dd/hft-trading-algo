"""Each registered strategy should live in its own module under
vietnam_algo.algos, not be inlined in registry.py.

Background: when we restructured the package on 2026-09-13, every
file at the package root was moved into a top-level sub-package
(bots/, algos/, libs/, …). The momentum adapter was small enough that
it stayed inside ``registry.py``, but that broke the symmetry:

  - autofin_signal.py     ~1700 lines, full decision logic
  - momentum_rotation.py  ~80 lines, thin wrapper around the
                          legacy backtest pipeline
  - registry.py           the lookup table + the momentum class

Adding a new algorithm should be a 2-file change (drop a file under
algos/, append to STRATEGY_REGISTRY). Inlining an adapter into
registry.py hides the pattern — this test catches that.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vietnam_algo.algos.registry import STRATEGY_REGISTRY


ALGOS_DIR = Path(__file__).resolve().parents[1] / "algos"


class RegistrySymmetryTests(unittest.TestCase):
    def test_each_strategy_has_its_own_module(self) -> None:
        """Every entry in STRATEGY_REGISTRY must be defined in a
        sibling module under ``algos/`` (not inlined in registry.py).

        We look at the registry.py AST and verify no class definition
        whose ``__module__ == 'vietnam_algo.algos.registry'`` matches
        a registered strategy. Adapter base classes (``StrategyAdapter``
        itself, ``_EmptyStrategy``) are fine — they're scaffolding.
        """
        tree = ast.parse((ALGOS_DIR / "registry.py").read_text())
        inlined_classes: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                inlined_classes.add(node.name)

        registered_names = set(STRATEGY_REGISTRY.keys())
        # Allow the scaffolding classes defined here:
        #   - StrategyAdapter re-export is fine (lives in base.py though)
        #   - _EmptyStrategy is for tests
        allowed_in_registry = {"StrategyAdapter", "_EmptyStrategy"}
        leaked = registered_names & inlined_classes - allowed_in_registry
        self.assertEqual(
            leaked, set(),
            f"Strategy adapters {leaked} are defined inside registry.py "
            "but should live in their own module under algos/. "
            "Extract each to vietnam_algo/algos/<name>.py and re-export "
            "from registry.py.",
        )

    def test_algos_dir_contains_one_module_per_registered_strategy(self) -> None:
        """Each registered strategy should have a corresponding file
        under algos/ (by name)."""
        for name in STRATEGY_REGISTRY:
            expected = ALGOS_DIR / f"{name}.py"
            self.assertTrue(
                expected.exists(),
                f"Expected module for strategy {name!r} at {expected}",
            )

    def test_registry_imports_each_strategy_module(self) -> None:
        """registry.py should explicitly import each strategy module
        so that simply importing the registry wires up all adapters."""
        src = (ALGOS_DIR / "registry.py").read_text()
        for name in STRATEGY_REGISTRY:
            self.assertIn(
                f"from .{name}",
                src,
                f"registry.py must import the {name!r} adapter explicitly",
            )


if __name__ == "__main__":
    unittest.main()
