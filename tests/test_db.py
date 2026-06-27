import os
import sqlite3
import tempfile
import unittest

from app.db import connect, SQLITE_BUSY_TIMEOUT_MS


class SQLiteConnectionSettingsTest(unittest.TestCase):
    def test_connect_applies_wal_busy_timeout_and_foreign_keys(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        try:
            conn = connect(tmp.name)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], SQLITE_BUSY_TIMEOUT_MS)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                conn.close()
        finally:
            os.unlink(tmp.name)
            for suffix in ("-wal", "-shm"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)
