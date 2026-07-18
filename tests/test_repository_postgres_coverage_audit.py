import copy, io, json, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.audit_repository_postgres_coverage import ConfigError, audit, main, read_manifest, runtime_census

ROOT = Path(__file__).resolve().parents[1]
BASE_MANIFEST = ROOT / "docs/postgres/repository_method_coverage.json"

SMOKE = 'SMOKE_METHODS = ("read_one",)\n'
MANIFEST = {"schema_version":1,"deferred_read_only":{},"write_or_mutating":{"write_one":{"reason":"writes","mutation_kind":"update"},"calls_write":{"reason":"calls write","mutation_kind":"mixed_read_write"}},"infrastructure_or_mixed":{}}


def repo_source(marker: Path | None = None):
    side_effect = ""
    if marker is not None:
        side_effect = f"from pathlib import Path\nPath({str(marker)!r}).write_text(\"bad\")\n"
    return side_effect + '''class Repository:
    def read_one(self):
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (1,)).fetchall()
    def write_one(self):
        self.conn.execute("UPDATE items SET value = 1")
        self.conn.commit()
    def calls_write(self):
        return self.write_one()
'''

class RepositoryPostgresCoverageAuditTests(unittest.TestCase):
    def write_case(self, repo=None, smoke=SMOKE, manifest=None, app_files=None, manifest_text=None):
        d=tempfile.TemporaryDirectory(); root=Path(d.name); (root/'app').mkdir(); (root/'scripts').mkdir(); (root/'docs/postgres').mkdir(parents=True)
        (root/'app/repository.py').write_text(repo if repo is not None else repo_source()); (root/'scripts/postgres_repository_smoke.py').write_text(smoke)
        text = json.dumps(MANIFEST if manifest is None else manifest, sort_keys=True) if manifest_text is None else manifest_text
        (root/'docs/postgres/repository_method_coverage.json').write_text(text)
        for name, text in (app_files or {}).items():
            target=root/'app'/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text)
        return d, root
    def run_case(self, **kw):
        d, root = self.write_case(**kw); self.addCleanup(d.cleanup)
        return audit(root/'app/repository.py', root/'scripts/postgres_repository_smoke.py', root/'docs/postgres/repository_method_coverage.json')
    def assert_manifest_error(self, manifest=None, expected='', manifest_text=None):
        d, root=self.write_case(manifest=manifest, manifest_text=manifest_text); self.addCleanup(d.cleanup)
        path=root/'docs/postgres/repository_method_coverage.json'
        with self.assertRaisesRegex(ConfigError, expected): read_manifest(path)
        buf=io.StringIO()
        with redirect_stdout(buf):
            code=main(['--repository-file', str(root/'app/repository.py'), '--smoke-script', str(root/'scripts/postgres_repository_smoke.py'), '--manifest', str(path), '--format', 'json'])
        self.assertEqual(2, code)
        payload=json.loads(buf.getvalue())
        self.assertEqual('error', payload['status'])
        self.assertRegex(payload['errors'][0], expected)

    def test_actual_repository_baseline(self):
        s=audit(); self.assertEqual('ok', s['status']); self.assertEqual([], s['unclassified']); self.assertEqual([], s['duplicates'])
        self.assertEqual([], s['stale_manifest_entries']); self.assertEqual([], s['unknown_smoke_methods']); self.assertEqual([], s['duplicate_smoke_methods'])
        self.assertEqual([], s['smoke_write_suspects']); self.assertEqual([], s['deferred_write_suspects']); self.assertEqual(s['repository_public_methods_count'], s['classified_methods_count'])
        self.assertEqual(112, s['repository_public_methods_count']); self.assertEqual(54, s['smoke_covered_read_count']); self.assertEqual(7, s['deferred_read_only_count'])
        self.assertEqual(50, s['write_or_mutating_count']); self.assertEqual(1, s['infrastructure_or_mixed_count']); self.assertEqual(88.52, s['read_surface_coverage_percent'])
    def test_valid_current_repository_manifest_schema(self): self.assertIsInstance(read_manifest(BASE_MANIFEST), dict)
    def test_invalid_json(self): self.assert_manifest_error(manifest_text='{', expected='cannot read manifest')
    def test_top_level_list(self): self.assert_manifest_error(manifest_text='[]', expected='top-level value')
    def test_top_level_string(self): self.assert_manifest_error(manifest_text='"bad"', expected='top-level value')
    def test_top_level_null(self): self.assert_manifest_error(manifest_text='null', expected='top-level value')
    def test_schema_version_true(self):
        m=copy.deepcopy(MANIFEST); m['schema_version']=True; self.assert_manifest_error(m, 'schema_version')
    def test_unknown_schema_version(self):
        m=copy.deepcopy(MANIFEST); m['schema_version']=2; self.assert_manifest_error(m, 'schema_version')
    def test_missing_required_category(self):
        m=copy.deepcopy(MANIFEST); del m['deferred_read_only']; self.assert_manifest_error(m, 'deferred_read_only')
    def test_unknown_top_level_key(self):
        m=copy.deepcopy(MANIFEST); m['extra']={}; self.assert_manifest_error(m, 'extra')
    def test_category_as_list(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']=[]; self.assert_manifest_error(m, 'deferred_read_only must be object')
    def test_method_metadata_as_string(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['write_one']='bad'; self.assert_manifest_error(m, 'write_or_mutating.write_one metadata')
    def test_empty_method_name(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['']={"reason":"x","mutation_kind":"update"}; self.assert_manifest_error(m, 'write_or_mutating method name')
    def test_deferred_entry_without_reason(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"blockers":["x"],"recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.reason')
    def test_deferred_entry_empty_reason(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":" ","blockers":["x"],"recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.reason')
    def test_blockers_as_string(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":"x","blockers":"x","recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.blockers must be a list')
    def test_blockers_empty_list(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":"x","blockers":[],"recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.blockers')
    def test_blockers_empty_element(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":"x","blockers":[""],"recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.blockers')
    def test_blockers_duplicate_element(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":"x","blockers":["x","x"],"recommended_batch":"b"}; self.assert_manifest_error(m, 'deferred_read_only.read_two.blockers contains duplicate')
    def test_empty_recommended_batch(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['read_two']={"reason":"x","blockers":["x"],"recommended_batch":" "}; self.assert_manifest_error(m, 'deferred_read_only.read_two.recommended_batch')
    def test_write_entry_without_mutation_kind(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['write_one']={"reason":"x"}; self.assert_manifest_error(m, 'write_or_mutating.write_one.mutation_kind')
    def test_unknown_mutation_kind(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['write_one']={"reason":"x","mutation_kind":"bad"}; self.assert_manifest_error(m, 'write_or_mutating.write_one.mutation_kind')
    def test_infrastructure_entry_as_string(self):
        m=copy.deepcopy(MANIFEST); m['infrastructure_or_mixed']['transaction']='bad'; self.assert_manifest_error(m, 'infrastructure_or_mixed.transaction metadata')
    def test_infrastructure_entry_empty_reason(self):
        m=copy.deepcopy(MANIFEST); m['infrastructure_or_mixed']['transaction']={"reason":""}; self.assert_manifest_error(m, 'infrastructure_or_mixed.transaction.reason')

    def test_unclassified_public_method(self):
        s=self.run_case(repo=repo_source()+'\n    def newly_added_method(self):\n        return []\n'); self.assertEqual('failed', s['status']); self.assertIn('newly_added_method', s['unclassified'])
    def test_duplicate_classification(self):
        m=copy.deepcopy(MANIFEST); m['deferred_read_only']['write_one']={"reason":"bad","blockers":["x"],"recommended_batch":"x"}; s=self.run_case(manifest=m); self.assertEqual('failed', s['status']); self.assertIn('write_one', s['duplicate_classifications'])
    def test_stale_manifest_entry(self):
        m=copy.deepcopy(MANIFEST); m['write_or_mutating']['missing']={"reason":"stale","mutation_kind":"update"}; s=self.run_case(manifest=m); self.assertIn('missing', s['stale_manifest_entries'])
    def test_unknown_and_duplicate_smoke_methods(self):
        s=self.run_case(smoke='SMOKE_METHODS=("read_one","missing","read_one")\n'); self.assertIn('missing', s['unknown_smoke_methods']); self.assertIn('read_one', s['duplicate_smoke_methods'])
    def test_obvious_write_in_smoke_and_deferred(self):
        s=self.run_case(smoke='SMOKE_METHODS=("write_one",)\n'); self.assertIn('write_one', s['smoke_write_suspects'])
        m=copy.deepcopy(MANIFEST); m['write_or_mutating'].pop('write_one'); m['deferred_read_only']['write_one']={"reason":"bad","blockers":["x"],"recommended_batch":"x"}
        s=self.run_case(manifest=m, smoke='SMOKE_METHODS=("read_one",)\n'); self.assertIn('write_one', s['deferred_write_suspects'])
    def test_valid_read_only_method_and_transitive_write(self):
        self.assertEqual('ok', self.run_case()['status'])
        m=copy.deepcopy(MANIFEST); m['write_or_mutating'].pop('calls_write'); m['deferred_read_only']['calls_write']={"reason":"bad","blockers":["x"],"recommended_batch":"x"}
        s=self.run_case(manifest=m); self.assertIn('calls_write', s['deferred_write_suspects'])
    def test_direct_runtime_sql_census(self):
        app={'server.py':'def f(conn, sql):\n conn.execute("SELECT 1")\n conn.execute("INSERT INTO x VALUES (1)")\n conn.execute("UPDATE x SET y=1")\n conn.execute("PRAGMA table_info(x)")\n conn.execute(sql)\n'}
        s=self.run_case(app_files=app); ops=[c['operation'] for c in s['direct_runtime_sql_summary']['calls']]
        for op in ['select','insert','update','pragma','dynamic_or_unknown']: self.assertIn(op, ops)
    def test_recursive_runtime_sql_census_detects_nested_files(self):
        s=self.run_case(app_files={'services/example.py':'def f(conn, sql):\n conn.execute("SELECT 1")\n conn.execute("INSERT INTO x VALUES (1)")\n conn.execute(sql)\n'})
        calls=s['direct_runtime_sql_summary']['calls']; self.assertTrue(any(f.endswith('app/services/example.py') for f in s['direct_runtime_sql_summary']['files_with_direct_sql'])); self.assertEqual(['select','insert','dynamic_or_unknown'], [c['operation'] for c in calls])
    def test_recursive_runtime_sql_census_excludes_service_directories(self):
        files={f'services/{d}/ignored.py':'def f(conn):\n conn.execute("SELECT 1")\n' for d in ('__pycache__','data','backups','logs','venv')}
        s=self.run_case(app_files=files); self.assertEqual([], s['direct_runtime_sql_summary']['calls'])
    def test_runtime_census_excludes_repository_file_by_resolved_path(self):
        d, root=self.write_case(); self.addCleanup(d.cleanup)
        nested=root/'app/services/repository_link.py'; nested.parent.mkdir(parents=True); nested.symlink_to(root/'app/repository.py')
        calls=runtime_census(root/'app', nested)['calls']; self.assertFalse(any(c['file'].endswith('repository_link.py') for c in calls))
    def test_runtime_census_deterministic_ordering(self):
        app={'services/z.py':'def z(conn):\n conn.execute("SELECT 1")\n','services/a.py':'def a(conn):\n conn.executemany("INSERT INTO x VALUES (?)", [])\n','services/m.py':'def m(conn):\n conn.execute("SELECT 1")\n conn.execute("SELECT 2")\n'}
        s=self.run_case(app_files=app); calls=s['direct_runtime_sql_summary']['calls']; self.assertEqual(calls, sorted(calls, key=lambda c:(c['file'], c['line'], c['api'], c['context'])))
    def test_deterministic_no_execution_and_read_only_filesystem(self):
        d=tempfile.TemporaryDirectory(); root=Path(d.name); self.addCleanup(d.cleanup); marker=root/'SIDE_EFFECT_MARKER'
        d2, case_root=self.write_case(repo=repo_source(marker), app_files={'server.py':'def f(conn):\n conn.execute("SELECT 1")\n'}); self.addCleanup(d2.cleanup)
        files=[case_root/'app/repository.py',case_root/'scripts/postgres_repository_smoke.py',case_root/'docs/postgres/repository_method_coverage.json',case_root/'app/server.py']
        before={p:p.read_text() for p in files}; one=audit(files[0],files[1],files[2]); two=audit(files[0],files[1],files[2])
        self.assertEqual(json.dumps(one,sort_keys=True), json.dumps(two,sort_keys=True)); self.assertFalse(marker.exists()); self.assertEqual(before, {p:p.read_text() for p in files})

if __name__ == '__main__': unittest.main()
