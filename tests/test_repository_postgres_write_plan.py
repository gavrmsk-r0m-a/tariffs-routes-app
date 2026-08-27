import copy,json,tempfile,unittest
from pathlib import Path
from scripts.audit_repository_postgres_write_plan import ROOT,audit,main
class WritePlanTests(unittest.TestCase):
 def setUp(self): self.plan=json.loads((ROOT/'docs/postgres/repository_write_surface_plan.json').read_text()); self.c=ROOT/'docs/postgres/repository_method_coverage.json'; self.r=ROOT/'app/repository.py'
 def execute_plan(self,p):
  with tempfile.TemporaryDirectory() as d:
   q=Path(d)/'p.json'; q.write_text(json.dumps(p)); return audit(self.r,self.c,q)
 def bad(self,p): self.assertEqual('failed',self.execute_plan(p)['status'])
 def name(self): return next(iter(self.plan['methods']))
 def test_actual_baseline_write_plan_passes(self):
  summary=self.execute_plan(self.plan); self.assertEqual('ok',summary['status']); self.assertEqual(51,summary['rollback_smoke_covered_methods_count'])
 def test_stage66f_all_write_methods_are_rollback_smoked(self):
  self.assertEqual(set(self.plan['methods']), set(self.plan['rollback_smoke_covered_methods']))
  for name in ('create_calling_company','update_calling_company','update_company_routing_setting_comment','update_calling_company_import_fields'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
 def test_invalid_rollback_smoke_tracking_fails(self):
  for value in ([], ['set_app_setting_value','set_app_setting_value'], ['list_countries'], ['stale_method'], ['set_hlr_limit_override','set_app_setting_value','delete_app_setting_value','upsert_hlr_daily_usage']):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods']=value; self.bad(p)
 def test_dictionary_rollback_method_in_wrong_batch_fails(self):
  p=copy.deepcopy(self.plan); p['methods']['ensure_project_exists']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
 def test_stage63_routing_event_smoke_rules(self):
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove('deactivate_routing_event'); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove('update_routing_event'); self.bad(p)
  p=copy.deepcopy(self.plan); p['methods']['deactivate_routing_event']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
  p=copy.deepcopy(self.plan); p['methods']['update_routing_event']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
  for name in ('deactivate_routing_event','update_routing_event'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  for name in ('list_countries','_sync_company_routing_comment_from_event','_active_company_routing_setting'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
 def test_stage65a_company_routing_rollback_rules(self):
  for name in ('create_company_routing_setting','update_company_routing_setting','deactivate_company_routing_setting'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('create_calling_company','update_calling_company','update_company_routing_setting_comment','_change_log'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('create_company_routing_setting'); self.bad(p)
 def test_stage65b_create_routing_event_is_complete(self):
  p=copy.deepcopy(self.plan); p['methods']['create_routing_event']['notes']='missing Stage 65B completion marker'; self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove('create_routing_event'); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('_server_priority_affected_servers'); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('update_company_routing_setting_comment'); self.bad(p)
 def test_stage66a_route_import_rules(self):
  for name in ('create_route','update_route','update_route_import_fields'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('update_phone_number_import_fields_with_history','update_calling_company_import_fields','_change_log','list_routes'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('create_route'); self.bad(p)
 def test_stage66b_phone_import_rules(self):
  for name in ('create_phone_number','update_phone_number','record_phone_update_history','update_phone_number_import_fields_with_history'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('update_calling_company_import_fields','create_tariff','add_phone_to_route','_change_log','list_phone_numbers'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('create_phone_number'); self.bad(p)
 def test_stage66c_route_phone_link_rules(self):
  for name in ('add_phone_to_route','add_phone_to_route_by_number','remove_phone_links_from_route'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('update_calling_company_import_fields','create_tariff','create_currency_rate','_change_log','list_routes'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('add_phone_to_route'); self.bad(p)
 def test_stage66d_tariff_lifecycle_rules(self):
  for name in ('create_tariff','update_tariff','set_tariff_active'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('update_calling_company_import_fields','_change_log','list_tariffs'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('create_tariff'); self.bad(p)
 def test_stage66e_currency_rate_lifecycle_rules(self):
  for name in ('create_currency_rate','log_currency_rate_change','recalculate_current_tariffs_for_currency_rate'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove(name); self.bad(p)
  for name in ('create_calling_company','update_calling_company','update_company_routing_setting_comment','update_calling_company_import_fields','_change_log','list_currency_rates'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append('create_currency_rate'); self.bad(p)
 def test_missing_stage59_rollback_method_fails(self):
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove('update_dictionary_snapshots'); self.bad(p)
 def test_stage57_server_in_wrong_batch_fails(self):
  p=copy.deepcopy(self.plan); p['methods']['create_server']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
 def test_private_and_deferred_dictionary_methods_cannot_be_smoked(self):
  for name in ('_route_prefix_id','_current_tariff','_change_log','_server_route_priority_summary'):
   p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].append(name); self.bad(p)
 def test_stage58_change_reason_in_wrong_batch_fails(self):
  p=copy.deepcopy(self.plan); p['methods']['create_change_reason']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
 def test_update_change_reason_write_plan_and_rollback_rules(self):
  self.assertIn('update_change_reason', self.plan['methods'])
  self.assertEqual('dictionary_and_snapshot_writes', self.plan['methods']['update_change_reason']['batch'])
  self.assertIn('update_change_reason', self.plan['rollback_smoke_covered_methods'])
  p=copy.deepcopy(self.plan); p['rollback_smoke_covered_methods'].remove('update_change_reason'); self.bad(p)
  p=copy.deepcopy(self.plan); p['methods']['update_change_reason']['batch']='app_settings_and_admin_low_risk'; self.bad(p)
 def test_missing_rollback_smoke_tracking_is_config_error(self):
  p=copy.deepcopy(self.plan); del p['rollback_smoke_covered_methods']
  with tempfile.TemporaryDirectory() as d:
   q=Path(d)/'p.json'; q.write_text(json.dumps(p))
   with self.assertRaises(Exception): audit(self.r,self.c,q)
 def test_deterministic_output(self): self.assertEqual(self.execute_plan(self.plan),self.execute_plan(self.plan))
 def test_missing_write_method_fails(self):
  p=copy.deepcopy(self.plan); n=self.name(); del p['methods'][n]; p['batches'][self.plan['methods'][n]['batch']]['methods'].remove(n); self.bad(p)
 def test_duplicate_write_method_fails(self):
  p=copy.deepcopy(self.plan); n=self.name(); p['batches']['app_settings_and_admin_low_risk']['methods'].append(n); self.bad(p)
 def test_stale_method_fails(self):
  p=copy.deepcopy(self.plan); p['methods']['stale']=copy.deepcopy(p['methods'][self.name()]); p['batches'][p['methods']['stale']['batch']]['methods'].append('stale'); self.bad(p)
 def test_read_method_fails(self):
  p=copy.deepcopy(self.plan); p['methods']['list_countries']=copy.deepcopy(p['methods'][self.name()]); p['batches'][p['methods']['list_countries']['batch']]['methods'].append('list_countries'); self.bad(p)
 def test_unknown_batch_fails(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['batch']='missing'; self.bad(p)
 def test_empty_batch_fails(self): p=copy.deepcopy(self.plan); p['batches']['write_test_harness_and_transaction_foundation']['methods']=[]; self.bad(p)
 def test_missing_required_batch_field_fails(self): p=copy.deepcopy(self.plan); del p['batches']['app_settings_and_admin_low_risk']['rationale']; self.bad(p)
 def test_missing_required_method_field_fails(self): p=copy.deepcopy(self.plan); del p['methods'][self.name()]['transaction_contract']; self.bad(p)
 def test_invalid_mutation_kind_fails(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['mutation_kind']='bad'; self.bad(p)
 def test_invalid_transaction_contract_fails(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['transaction_contract']='bad'; self.bad(p)
 def test_invalid_risk_fails(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['risk']='bad'; self.bad(p)
 def test_duplicate_blockers_fail(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['sqlite_postgres_blockers']*=2; self.bad(p)
 def test_duplicate_side_effects_fail(self): p=copy.deepcopy(self.plan); p['methods'][self.name()]['side_effects']*=2; self.bad(p)
 def test_commit_and_rollback_acknowledgement_fail(self):
  p=copy.deepcopy(self.plan); p['methods']['set_app_setting_value']['current_commit_behavior']='never commits'; self.bad(p)
 def test_transitive_call_without_dependency_fails(self):
  self.assertEqual([], self.plan['methods']['set_hlr_limit_override']['dependencies'])
  self.assertEqual(['add_phone_to_route'], self.plan['methods']['add_phone_to_route_by_number']['dependencies'])
  p=copy.deepcopy(self.plan); p['methods']['add_phone_to_route_by_number']['dependencies']=[]; self.bad(p)
 def test_dependency_cycle_fails(self):
  p=copy.deepcopy(self.plan); a='app_settings_and_admin_low_risk'; b='dictionary_and_snapshot_writes'; p['batches'][a]['prerequisites'].append(b); p['batches'][b]['prerequisites'].append(a); self.bad(p)
 def test_dynamic_sql_is_surfaced(self): self.assertTrue(self.execute_plan(self.plan)['dynamic_sql_methods'])
 def test_bad_schema_versions_are_config_errors(self):
  for v in (2,True):
   p=copy.deepcopy(self.plan); p['schema_version']=v
   with tempfile.TemporaryDirectory() as d:
    q=Path(d)/'p.json'; q.write_text(json.dumps(p));
    with self.assertRaises(Exception): audit(self.r,self.c,q)
 def test_malformed_and_non_object_are_config_errors(self):
  with tempfile.TemporaryDirectory() as d:
   q=Path(d)/'p.json'; q.write_text('{');
   with self.assertRaises(Exception): audit(self.r,self.c,q)
   q.write_text('[]');
   with self.assertRaises(Exception): audit(self.r,self.c,q)
 def test_cli_exit_codes_and_output_file(self):
  with tempfile.TemporaryDirectory() as d:
   out=Path(d)/'out.json'; self.assertEqual(0,main(['--format','json','--output',str(out)])); self.assertEqual('ok',json.loads(out.read_text())['status'])
   p=copy.deepcopy(self.plan); p['schema_version']=2; q=Path(d)/'bad.json'; q.write_text(json.dumps(p)); self.assertEqual(2,main(['--write-plan',str(q)]))
 def test_no_code_execution(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d)/'repository.py'; marker=Path(d)/'marker'; r.write_text(f"open({str(marker)!r},'w').write('x')\nclass Repository:\n def create_x(self): self.conn.execute('INSERT x')\n")
   c=Path(d)/'coverage.json'; c.write_text(json.dumps({'write_or_mutating':{'create_x':{'mutation_kind':'insert'}}}))
   p=copy.deepcopy(self.plan); p['methods']={'create_x':copy.deepcopy(self.plan['methods'][self.name()])}; p['methods']['create_x'].update(batch='write_test_harness_and_transaction_foundation',mutation_kind='insert');
   for b in p['batches'].values(): b['methods']=[]
   p['batches']['write_test_harness_and_transaction_foundation']['methods']=['create_x']
   q=Path(d)/'plan.json'; q.write_text(json.dumps(p));
   with self.assertRaises(Exception): audit(r,c,q)
   self.assertFalse(marker.exists())
