import copy, contextlib, io, json, tempfile, unittest
from pathlib import Path

from scripts.audit_repository_postgres_coverage import audit, main

ROOT = Path(__file__).resolve().parents[1]
BASE_MANIFEST = ROOT / "docs/postgres/repository_method_coverage.json"

REPO = '''
Path("SIDE_EFFECT").write_text("bad")
class Repository:
    def read_one(self):
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (1,)).fetchall()
    def write_one(self):
        self.conn.execute("UPDATE items SET value = 1")
        self.conn.commit()
    def calls_write(self):
        return self.write_one()
'''
SMOKE = 'SMOKE_METHODS = ("read_one",)\n'
MANIFEST = {"schema_version":1,"deferred_read_only":{},"write_or_mutating":{"write_one":{"reason":"writes","mutation_kind":"update"},"calls_write":{"reason":"calls write","mutation_kind":"mixed_read_write"}},"infrastructure_or_mixed":{}}

class RepositoryPostgresCoverageAuditTests(unittest.TestCase):
    def write_case(self, repo=REPO, smoke=SMOKE, manifest=None, app_files=None):
        d=tempfile.TemporaryDirectory(); root=Path(d.name); (root/'app').mkdir(); (root/'scripts').mkdir(); (root/'docs/postgres').mkdir(parents=True)
        (root/'app/repository.py').write_text(repo); (root/'scripts/postgres_repository_smoke.py').write_text(smoke)
        (root/'docs/postgres/repository_method_coverage.json').write_text(json.dumps(MANIFEST if manifest is None else manifest, sort_keys=True))
        for name, text in (app_files or {}).items():
            path=root/'app'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return d, root
    def run_case(self, **kw):
        d, root = self.write_case(**kw); self.addCleanup(d.cleanup)
        return audit(root/'app/repository.py', root/'scripts/postgres_repository_smoke.py', root/'docs/postgres/repository_method_coverage.json')

    def test_actual_repository_baseline(self):
        s=audit()
        self.assertEqual('ok', s['status']); self.assertEqual([], s['unclassified']); self.assertEqual([], s['duplicates'])
        self.assertEqual([], s['stale_manifest_entries']); self.assertEqual([], s['unknown_smoke_methods'])
        self.assertEqual([], s['duplicate_smoke_methods']); self.assertEqual([], s['smoke_write_suspects'])
        self.assertEqual([], s['deferred_write_suspects']); self.assertEqual(s['repository_public_methods_count'], s['classified_methods_count'])
        self.assertEqual(112, s['repository_public_methods_count']); self.assertEqual(57, s['smoke_covered_read_count'])
        self.assertEqual(4, s['deferred_read_only_count']); self.assertEqual(50, s['write_or_mutating_count']); self.assertEqual(1, s['infrastructure_or_mixed_count']); self.assertEqual(93.44, s['read_surface_coverage_percent'])

    def test_unclassified_public_method(self):
        s=self.run_case(repo=REPO+'\n    def newly_added_method(self):\n        return []\n')
        self.assertEqual('failed', s['status']); self.assertIn('newly_added_method', s['unclassified'])

    def test_duplicate_classification(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['write_one']={"reason":"bad","blockers":[],"recommended_batch":"x"}
        s=self.run_case(manifest=m); self.assertEqual('failed', s['status']); self.assertIn('write_one', s['duplicate_classifications'])

    def test_stale_manifest_entry(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['missing']={"reason":"stale","mutation_kind":"update"}
        s=self.run_case(manifest=m); self.assertIn('missing', s['stale_manifest_entries'])

    def test_unknown_and_duplicate_smoke_methods(self):
        s=self.run_case(smoke='SMOKE_METHODS=("read_one","missing","read_one")\n')
        self.assertIn('missing', s['unknown_smoke_methods']); self.assertIn('read_one', s['duplicate_smoke_methods'])

    def test_obvious_write_in_smoke_and_deferred(self):
        s=self.run_case(smoke='SMOKE_METHODS=("write_one",)\n')
        self.assertIn('write_one', s['smoke_write_suspects'])
        m=copy.deepcopy(MANIFEST); m['write_or_mutating'].pop('write_one'); m['deferred_read_only']['write_one']={"reason":"bad","blockers":[],"recommended_batch":"x"}
        s=self.run_case(manifest=m, smoke='SMOKE_METHODS=("read_one",)\n')
        self.assertIn('write_one', s['deferred_write_suspects'])

    def test_valid_read_only_method_and_transitive_write(self):
        self.assertEqual('ok', self.run_case()['status'])
        m=copy.deepcopy(MANIFEST); m['write_or_mutating'].pop('calls_write'); m['deferred_read_only']['calls_write']={"reason":"bad","blockers":[],"recommended_batch":"x"}
        s=self.run_case(manifest=m); self.assertIn('calls_write', s['deferred_write_suspects'])

    def test_direct_runtime_sql_census(self):
        app={'server.py':'def f(conn, sql):\n conn.execute("SELECT 1")\n conn.execute("INSERT INTO x VALUES (1)")\n conn.execute("UPDATE x SET y=1")\n conn.execute("PRAGMA table_info(x)")\n conn.execute(sql)\n'}
        s=self.run_case(app_files=app); calls=s['direct_runtime_sql_summary']['calls']; ops=[c['operation'] for c in calls]
        for op in ['select','insert','update','pragma','dynamic_or_unknown']: self.assertIn(op, ops)
        self.assertTrue(all(c['function']=='f' for c in calls))

    def test_recursive_runtime_census_excludes_non_runtime_directories(self):
        app={
            'nested/worker.py': 'def f(conn):\n conn.execute("SELECT 1")\n',
            'data/ignored.py': 'def f(conn):\n conn.execute("UPDATE ignored SET value = 1")\n',
            'backups/ignored.py': 'def f(conn):\n conn.execute("DELETE FROM ignored")\n',
            'logs/ignored.py': 'def f(conn):\n conn.execute("INSERT INTO ignored VALUES (1)")\n',
            '__pycache__/ignored.py': 'def f(conn):\n conn.execute("PRAGMA cache_size")\n',
        }
        s=self.run_case(app_files=app)
        direct=s['direct_runtime_sql_summary']
        self.assertIn('app/nested/worker.py', direct['files_with_direct_sql'])
        self.assertNotIn('app/data/ignored.py', direct['files_with_direct_sql'])
        self.assertNotIn('app/backups/ignored.py', direct['files_with_direct_sql'])
        self.assertNotIn('app/logs/ignored.py', direct['files_with_direct_sql'])
        self.assertNotIn('app/__pycache__/ignored.py', direct['files_with_direct_sql'])

    def test_manifest_rejects_unknown_or_missing_top_level_keys(self):
        m=copy.deepcopy(MANIFEST); m['unexpected']={}
        with self.assertRaisesRegex(Exception, 'unknown top-level keys'):
            self.run_case(manifest=m)
        m=copy.deepcopy(MANIFEST); del m['write_or_mutating']
        with self.assertRaisesRegex(Exception, 'missing top-level keys'):
            self.run_case(manifest=m)

    def test_manifest_rejects_invalid_category_and_metadata_shapes(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']=[]
        with self.assertRaisesRegex(Exception, 'deferred_read_only must be object'):
            self.run_case(manifest=m)
        m=copy.deepcopy(MANIFEST); m['infrastructure_or_mixed']['transaction']='not metadata'
        with self.assertRaisesRegex(Exception, 'transaction must be object'):
            self.run_case(manifest=m)
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['write_one']={'reason':'writes'}
        with self.assertRaisesRegex(Exception, 'missing metadata keys: mutation_kind'):
            self.run_case(manifest=m)
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_one']={'reason':'deferred','blockers':'not-list','recommended_batch':'x'}
        with self.assertRaisesRegex(Exception, 'blockers must be list'):
            self.run_case(manifest=m)

    def test_manifest_rejects_unknown_metadata_keys_and_empty_strings(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['write_one']['extra']='bad'
        with self.assertRaisesRegex(Exception, 'unknown metadata keys: extra'):
            self.run_case(manifest=m)
        m=copy.deepcopy(MANIFEST); m['infrastructure_or_mixed']['transaction']={'reason':' '}
        with self.assertRaisesRegex(Exception, 'reason must be non-empty string'):
            self.run_case(manifest=m)

    def test_deterministic_no_execution_and_read_only_filesystem(self):
        d, root=self.write_case(app_files={'server.py':'def f(conn):\n conn.execute("SELECT 1")\n'}); self.addCleanup(d.cleanup)
        files=[root/'app/repository.py',root/'scripts/postgres_repository_smoke.py',root/'docs/postgres/repository_method_coverage.json',root/'app/server.py']
        before={p:p.read_text() for p in files}
        one=audit(files[0],files[1],files[2]); two=audit(files[0],files[1],files[2])
        self.assertEqual(json.dumps(one,sort_keys=True), json.dumps(two,sort_keys=True))
        self.assertFalse((root/'SIDE_EFFECT').exists())
        self.assertEqual(before, {p:p.read_text() for p in files})

    def test_audit_never_executes_repository_marker(self):
        marker='AUDIT_MUST_NEVER_EXECUTE_THIS_MODULE'
        repo=f'raise RuntimeError("{marker}")\n'+REPO
        self.assertEqual('ok', self.run_case(repo=repo)['status'])

    def test_cli_exit_codes_are_zero_one_and_two(self):
        d, root=self.write_case(); self.addCleanup(d.cleanup)
        args=['--repository-file', str(root/'app/repository.py'), '--smoke-script', str(root/'scripts/postgres_repository_smoke.py'), '--manifest', str(root/'docs/postgres/repository_method_coverage.json')]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(args))
        (root/'scripts/postgres_repository_smoke.py').write_text('SMOKE_METHODS = ("missing",)\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, main(args))
        (root/'docs/postgres/repository_method_coverage.json').write_text('{}')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, main(args))

if __name__ == '__main__':
    unittest.main()
