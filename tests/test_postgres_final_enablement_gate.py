import unittest
from unittest.mock import patch
from app.db import DbConfig, connect_database, load_db_config
class FinalEnablementGateTests(unittest.TestCase):
 def test_postgres_is_unconditionally_runtime_backend(self):
  config=load_db_config({"DB_BACKEND":"postgres","DATABASE_URL":"postgresql://db/app"})
  with patch("app.db.connect_postgres",return_value="connected") as connect:
   self.assertEqual(connect_database(config),"connected")
  connect.assert_called_once()
 def test_old_guard_cannot_select_fallback(self):
  config=DbConfig("postgres","postgresql://db/app")
  with patch("app.db.connect_postgres",side_effect=OSError("down")):
   with self.assertRaises(OSError): connect_database(config,{"POSTGRES_RUNTIME_ENABLED":"0"})
