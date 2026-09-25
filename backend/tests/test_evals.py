"""Regression gate: the offline evaluation suites must meet their thresholds.

Runs the eval runner in a subprocess (it configures its own hermetic
environment) so thresholds are enforced by `unittest discover`.
"""

import _env  # noqa: F401  (must be first)

import json
import subprocess
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


class EvaluationSuiteRegressionTests(unittest.TestCase):
    def test_router_rag_agent_and_security_suites_meet_thresholds(self):
        completed = subprocess.run(
            [sys.executable, "-m", "evals.run", "--suite", "router", "--suite", "rag", "--suite", "agent", "--suite", "security"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        report = json.loads((BACKEND / "evals" / "results" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, report.get("failures") or completed.stderr[-2000:])
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["security"]["leaks"], 0)
        self.assertEqual(report["summary"]["security"]["block_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
