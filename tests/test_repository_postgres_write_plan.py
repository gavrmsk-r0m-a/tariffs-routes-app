import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_repository_postgres_write_plan import ROOT, audit


class RepositoryPostgresWritePlanTests(unittest.TestCase):
    def setUp(self):
        self.coverage = ROOT / "docs/postgres/repository_method_coverage.json"
        self.plan_path = ROOT / "docs/postgres/repository_write_surface_plan.json"
        self.repository = ROOT / "app/repository.py"
        self.plan = json.loads(self.plan_path.read_text())

    def run_plan(self, plan):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"; path.write_text(json.dumps(plan))
            return audit(self.repository, self.coverage, path)

    def test_actual_plan_passes_and_is_deterministic(self):
        first = audit(self.repository, self.coverage, self.plan_path)
        self.assertEqual(first, audit(self.repository, self.coverage, self.plan_path))
        self.assertEqual("ok", first["status"]); self.assertEqual(50, first["planned_write_methods_count"])
        self.assertEqual([], first["missing_write_methods"]); self.assertEqual([], first["duplicate_planned_methods"])
        self.assertEqual([], first["stale_planned_methods"]); self.assertEqual([], first["non_write_methods_in_plan"])
        self.assertEqual("write_test_harness_and_transaction_foundation", first["recommended_next_batch"])

    def test_missing_duplicate_stale_read_unknown_empty_and_required_fail(self):
        cases=[]
        missing=copy.deepcopy(self.plan); n=next(iter(missing["methods"])); del missing["methods"][n]; missing["batches"][self.plan["methods"][n]["batch"]]["methods"].remove(n); cases.append(missing)
        duplicate=copy.deepcopy(self.plan); n=next(iter(duplicate["methods"])); duplicate["batches"][next(k for k in duplicate["batches"] if k != duplicate["methods"][n]["batch"])]["methods"].append(n); cases.append(duplicate)
        stale=copy.deepcopy(self.plan); stale["methods"]["gone"] = copy.deepcopy(next(iter(stale["methods"].values()))); stale["batches"][stale["methods"]["gone"]["batch"]]["methods"].append("gone"); cases.append(stale)
        unknown=copy.deepcopy(self.plan); unknown["methods"][next(iter(unknown["methods"]))]["batch"]="gone"; cases.append(unknown)
        empty=copy.deepcopy(self.plan); empty["batches"]["write_test_harness_and_transaction_foundation"]["methods"]=[]; cases.append(empty)
        required=copy.deepcopy(self.plan); del required["methods"][next(iter(required["methods"]))]["transaction_contract"]; cases.append(required)
        for case in cases: self.assertEqual("failed", self.run_plan(case)["status"])

    def test_dynamic_sql_is_reported(self):
        self.assertTrue(audit(self.repository, self.coverage, self.plan_path)["dynamic_sql_methods"])
