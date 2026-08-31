import unittest
from app.db_adapter import *
class PostgreSQLAdapterTest(unittest.TestCase):
 def test_backend(self):
  self.assertEqual(normalize_backend_name("postgresql"),"postgres")
  with self.assertRaises(ValueError): normalize_backend_name("sqlite")
 def test_placeholders(self): self.assertEqual(placeholders(3),"%s, %s, %s")
 def test_insert_conflict(self): self.assertIn("ON CONFLICT (name) DO NOTHING",insert_ignore_statement("projects",["name"],["name"]))
 def test_returning(self): self.assertEqual(prepare_insert_returning_id("INSERT INTO x(a) VALUES (%s)"),"INSERT INTO x(a) VALUES (%s) RETURNING id")
 def test_boolean(self): self.assertIs(to_db_bool(1),True)
 def test_rows(self): self.assertEqual(row_to_dict({"id":1}),{"id":1})
