"""Guards against the 2026-11-01 stale-price-deploy incident.

The local checkout used to track vietnam_algo/data/prices/*.json (they
were committed in ed16cff "init strategy algo", before .gitignore
covered vietnam_algo/data/). A plain `rsync -avz` then pushed those
stale local copies over the VPS's freshly-synced price files, and the
VPS market data went from 2026-10-30 back to 2026-08-13.

Two things must hold:
  1. Price files must not be tracked by git.
  2. The deploy script must exclude the runtime data directories.

This test guards both.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git_ls_files(pattern: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", pattern],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


class PriceFilesNotTrackedTests(unittest.TestCase):
    """Price history belongs to the VPS, not to git."""

    def test_price_files_are_not_tracked(self):
        tracked = _git_ls_files("vietnam_algo/data/prices/")
        self.assertEqual(
            tracked, [],
            f"{len(tracked)} price file(s) are tracked by git — they will "
            f"overwrite the VPS's freshly-synced data on the next deploy. "
            f"Run: git rm --cached -r vietnam_algo/data/prices/",
        )

    def test_price_dir_is_gitignored(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(
            "vietnam_algo/data/prices/",
            gi,
            ".gitignore must list vietnam_algo/data/prices/ explicitly",
        )


class DeployScriptTests(unittest.TestCase):
    """scripts/deploy.sh must never sync runtime data to the VPS."""

    def setUp(self):
        self.script = ROOT / "scripts" / "deploy.sh"
        if not self.script.exists():
            self.skipTest("scripts/deploy.sh not present")
        self.src = self.script.read_text(encoding="utf-8")

    def _exclude_args(self) -> list[str]:
        m = re.search(r"EXCLUDES=\((.*?)\)", self.src, re.DOTALL)
        self.assertIsNotNone(m, "EXCLUDES array not found in deploy.sh")
        return re.findall(r"--exclude='([^']+)'", m.group(1))

    def test_excludes_vietnam_algo_data(self):
        excludes = self._exclude_args()
        self.assertIn(
            "vietnam_algo/data/", excludes,
            "deploy.sh must exclude vietnam_algo/data/ (price files, live state)",
        )

    def test_excludes_darvas_data(self):
        excludes = self._exclude_args()
        self.assertIn(
            "darvas_box_strategy/data/", excludes,
            "deploy.sh must exclude darvas_box_strategy/data/ (web cache, "
            "bot state, position tracker)",
        )

    def test_excludes_env_and_logs(self):
        excludes = self._exclude_args()
        for pattern in (".git", "__pycache__", "*.log", "*.env"):
            self.assertIn(pattern, excludes, f"deploy.sh must exclude {pattern}")

    def test_delete_is_opt_in(self):
        """--delete must be opt-in, never the default.

        The VPS hosts files that are not in the local checkout
        (postgres/initdb.d/*.sql, operator helper scripts). A blanket
        --delete would wipe them.
        """
        self.assertIn(
            "--delete", self.src,
            "deploy.sh should support --delete",
        )
        self.assertIn(
            'DELETE=""', self.src,
            "--delete must default to OFF",
        )
        self.assertIn(
            "--delete is OFF", self.src,
            "deploy.sh should warn that --delete is off by default",
        )

    def test_delete_flag_not_in_default_rsync_args(self):
        """The default rsync invocation must not pass --delete."""
        m = re.search(r"RSYNC_ARGS=\((.*?)\)", self.src, re.DOTALL)
        self.assertIsNotNone(m, "RSYNC_ARGS not found in deploy.sh")
        self.assertNotIn(
            "--delete", m.group(1),
            "--delete must be added conditionally, not in the default args",
        )

    def test_documented_incident(self):
        """The script should explain WHY it excludes the data dirs."""
        self.assertIn(
            "2026-11-01", self.src,
            "deploy.sh should document the incident that motivated the "
            "excludes, so nobody removes them later",
        )


if __name__ == "__main__":
    unittest.main()
