"""Regression gate: every agent and the orchestrator must be deterministic
offline (same input -> same output), including concurrent runs and repeated
runs by one user.

Runs `python -m evals.determinism` in a subprocess (it configures its own
hermetic environment) and writes the report to a temporary file.
"""

import _env  # noqa: F401  (must be first)

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


class DeterminismRegressionTests(unittest.TestCase):
    def test_agents_and_orchestrator_are_deterministic_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "determinism.json"
            completed = subprocess.run(
                [sys.executable, "-m", "evals.determinism", "--runs", "2", "--output", str(output)],
                cwd=BACKEND,
                capture_output=True,
                text=True,
                timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertTrue(output.exists(), completed.stderr[-2000:])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, report.get("non_deterministic") or completed.stderr[-2000:])
        self.assertEqual(report["agents"]["agents_without_case"], [], "every registered agent needs a determinism case")
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
