import unittest
from scripts.audit_postgres_production_gate import audit
class ProductionGateTest(unittest.TestCase):
 def test_gate_ready_and_postgres_only(self):
  result=audit(); self.assertEqual(result["status"],"ready"); self.assertEqual(result["security_gate"],"ok"); self.assertFalse(result["sqlite_fallback"]); self.assertEqual(result["blockers"],[])
