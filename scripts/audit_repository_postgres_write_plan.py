#!/usr/bin/env python3
"""AST-only consistency gate for the PostgreSQL Repository write plan."""
from __future__ import annotations
import argparse, ast, json, sys
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TOP={"schema_version","baseline","batches","methods","recommended_next_batch","foundation_status","rollback_smoke_covered_methods"}
BASELINE={"repository_public_methods_count":112,"smoke_covered_read_count":61,"deferred_read_only_count":0,"write_or_mutating_count":50,"infrastructure_or_mixed_count":1,"read_surface_coverage_percent":100.0,"repository_smoke_checks_count":611}
BATCH={"title","stage_hint","rationale","risk","scope","prerequisites","methods","out_of_scope","acceptance"}
METHOD={"batch","mutation_kind","risk","transaction_contract","current_commit_behavior","sqlite_postgres_blockers","side_effects","dependencies","returns","postgres_strategy","test_strategy","rollback_strategy","notes"}
RISK={"low","medium","high","critical"}; KINDS={"insert","update","delete","upsert","multi_write","transaction_boundary","write_with_history","write_with_external_side_effect","mixed_read_write"}
CONTRACTS={"single_statement_autocommit","explicit_commit","optional_commit_parameter","caller_owned_transaction","multi_statement_atomic","nested_repository_transaction","rollback_on_exception","unknown_needs_audit"}
COMMITS={"commits internally","commits only if commit=True","never commits","rollbacks internally","caller expected to commit","mixed"}
class ConfigError(Exception): pass
def load(path):
 try: value=json.loads(Path(path).read_text(encoding='utf-8'))
 except (OSError,json.JSONDecodeError) as e: raise ConfigError(str(e))
 if not isinstance(value,dict): raise ConfigError('top-level JSON must be an object')
 return value
def repo_methods(path):
 try: tree=ast.parse(Path(path).read_text(encoding='utf-8'),filename=str(path))
 except (OSError,SyntaxError) as e: raise ConfigError(str(e))
 cls=next((x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Repository'),None)
 if not cls: raise ConfigError('class Repository not found')
 return {x.name:x for x in cls.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and not x.name.startswith('_')}
def text(node):
 if isinstance(node,ast.Constant) and isinstance(node.value,str): return node.value,False
 if isinstance(node,ast.JoinedStr): return ''.join(x.value for x in node.values if isinstance(x,ast.Constant) and isinstance(x.value,str)),any(isinstance(x,ast.FormattedValue) for x in node.values)
 return '',True
def strings(value, nonempty=True): return isinstance(value,list) and (bool(value) or not nonempty) and all(isinstance(x,str) and x.strip() for x in value) and len(value)==len(set(value))
def exact(value, keys, label):
 if not isinstance(value,dict): raise ConfigError(f'{label} must be an object')
 if set(value)!=keys: raise ConfigError(f'{label} keys must be exactly {sorted(keys)}')
def evidence(node,writes):
 out={'commit':False,'rollback':False,'writes':False,'dynamic':False,'calls':set()}
 for x in ast.walk(node):
  if not isinstance(x,ast.Call) or not isinstance(x.func,ast.Attribute): continue
  a=x.func.attr; out['commit']|=a=='commit'; out['rollback']|=a=='rollback'; out['writes']|=a=='executescript'
  if isinstance(x.func.value,ast.Name) and x.func.value.id=='self' and a in writes: out['calls'].add(a)
  if a in {'execute','executemany'} and x.args:
   q,dynamic=text(x.args[0]); q=q.upper(); out['dynamic']|=dynamic
   out['writes']|=any(k in q for k in ('INSERT','UPDATE','DELETE','REPLACE','CREATE','ALTER','DROP'))
 return out
def audit(repository_file=ROOT/'app/repository.py',coverage_manifest=ROOT/'docs/postgres/repository_method_coverage.json',write_plan=ROOT/'docs/postgres/repository_write_surface_plan.json'):
 coverage,plan=load(coverage_manifest),load(write_plan); exact(plan,TOP,'plan')
 if type(plan['schema_version']) is not int or plan['schema_version']!=1: raise ConfigError('unknown schema_version')
 exact(plan['baseline'],set(BASELINE),'baseline')
 if plan['baseline']!=BASELINE: raise ConfigError('baseline does not match expected constants')
 if plan['foundation_status']!="foundation_added": raise ConfigError('Stage 51 foundation_status must be foundation_added')
 expected=coverage.get('write_or_mutating');
 if not isinstance(expected,dict) or len(expected)!=50: raise ConfigError('coverage write_or_mutating is invalid')
 if plan['baseline']['write_or_mutating_count']!=len(expected): raise ConfigError('baseline does not match coverage manifest')
 batches,methods=plan['batches'],plan['methods']
 if not isinstance(batches,dict) or not isinstance(methods,dict): raise ConfigError('batches and methods must be objects')
 errors=[]; listed=[]
 smoked=plan['rollback_smoke_covered_methods']
 if not strings(smoked): errors.append('rollback_smoke_covered_methods must be a non-empty unique list of strings')
 for bid,b in batches.items():
  try: exact(b,BATCH,f'batch {bid}')
  except ConfigError as e: errors.append(str(e)); continue
  if not all(isinstance(b[x],str) and b[x].strip() for x in ('title','stage_hint','rationale','scope')) or b['risk'] not in RISK or not strings(b['prerequisites'],False) or not strings(b['methods']) or not strings(b['out_of_scope']) or not strings(b['acceptance']): errors.append(f'invalid batch metadata: {bid}')
  listed+=b['methods']
 next_batch=plan['recommended_next_batch']
 if not isinstance(next_batch,str) or next_batch not in batches: errors.append('invalid recommended_next_batch')
 else:
  b=batches[next_batch]
  if b.get('stage_hint')!='Stage 51' or 'rollback-only' not in str(b.get('rationale','')).lower() or 'harness' not in str(b.get('rationale','')).lower(): errors.append('recommended next batch is not a Stage 51 rollback-only harness')
  if any(bid!=next_batch and next_batch not in batch.get('prerequisites',[]) for bid,batch in batches.items()): errors.append('recommended next batch is not prerequisite for every other batch')
 repo=repo_methods(repository_file); writes=set(expected); counts=Counter(listed); names=set(methods)|set(listed)
 if isinstance(smoked,list):
  for name in smoked:
   if name not in writes: errors.append(f'rollback-smoked method is not write_or_mutating: {name}')
   if name not in repo: errors.append(f'rollback-smoked method is stale: {name}')
  expected_smoked={'set_hlr_limit_override','set_app_setting_value','delete_app_setting_value','upsert_hlr_daily_usage','create_user','update_user','update_user_password','set_user_permissions','create_country','create_currency','create_provider','create_prefix','get_or_create_country','get_or_create_currency','get_or_create_provider','get_or_create_prefix','ensure_project_exists','ensure_phone_number_type_exists','ensure_phone_assignment_type_exists','create_server','create_change_reason','update_dictionary_snapshots'}
  if set(smoked) != expected_smoked: errors.append('rollback_smoke_covered_methods must contain exactly the Stage 51-59 methods')
  if '_change_log' in smoked: errors.append('_change_log is private and must not be rollback-smoked')
  if 'update_dictionary_snapshots' not in smoked: errors.append('update_dictionary_snapshots must be rollback-smoked')
  if 'set_hlr_limit_override' not in smoked or methods.get('set_hlr_limit_override',{}).get('batch')!='write_test_harness_and_transaction_foundation': errors.append('set_hlr_limit_override must remain a foundation rollback probe')
  for name in ('set_app_setting_value','delete_app_setting_value','upsert_hlr_daily_usage','create_user','update_user','update_user_password','set_user_permissions'):
   if name not in smoked or methods.get(name,{}).get('batch')!='app_settings_and_admin_low_risk': errors.append(f'{name} must be an app-settings/admin rollback probe')
  for name in ('create_country','create_currency','create_provider','create_prefix','get_or_create_country','get_or_create_currency','get_or_create_provider','get_or_create_prefix','ensure_project_exists','ensure_phone_number_type_exists','ensure_phone_assignment_type_exists','create_server','create_change_reason','update_dictionary_snapshots'):
   if name not in smoked or methods.get(name,{}).get('batch')!='dictionary_and_snapshot_writes': errors.append(f'{name} must be a dictionary rollback probe')
  dictionary_methods={name for name, meta in methods.items() if isinstance(meta, dict) and meta.get('batch') == 'dictionary_and_snapshot_writes'}
  if dictionary_methods-set(smoked): errors.append('dictionary_and_snapshot_writes methods must all be rollback-smoked')
 missing=sorted(writes-names); dup=sorted(k for k,v in counts.items() if v!=1); stale=sorted(names-set(repo)); nonwrite=sorted(names-writes); empty=sorted(k for k,b in batches.items() if not isinstance(b,dict) or not b.get('methods')); unknown=[]; required=[]; commits=[]; rollbacks=[]; calls=[]; dynamic=[]
 for name,meta in methods.items():
  if not isinstance(meta,dict): required.append(name); continue
  if set(meta)!=METHOD: required.append(name); continue
  if meta['batch'] not in batches: unknown.append(name)
  if meta['mutation_kind'] not in KINDS or meta['mutation_kind']!=expected.get(name,{}).get('mutation_kind') or meta['risk'] not in RISK or meta['transaction_contract'] not in CONTRACTS or meta['current_commit_behavior'] not in COMMITS or not strings(meta['sqlite_postgres_blockers']) or not strings(meta['side_effects']) or not strings(meta['dependencies'],False) or not isinstance(meta['returns'],str) or not meta['returns'].strip() or not isinstance(meta['postgres_strategy'],str) or not meta['postgres_strategy'].strip() or not strings(meta['test_strategy']) or not isinstance(meta['rollback_strategy'],str) or not meta['rollback_strategy'].strip() or not isinstance(meta['notes'],str): required.append(name)
  if counts.get(name)!=1 or (meta['batch'] in batches and name not in batches[meta['batch']].get('methods',[])): errors.append(f'batch membership mismatch: {name}')
  if name in repo and name in writes:
   e=evidence(repo[name],writes); behavior=meta['current_commit_behavior']
   if e['commit'] and behavior not in {'commits internally','commits only if commit=True','mixed'}: commits.append(name)
   if e['rollback'] and behavior not in {'rollbacks internally','mixed'}: rollbacks.append(name)
   if e['calls']-set(meta['dependencies']): calls.append(name)
   if e['dynamic']: dynamic.append(name)
 for name in writes-set(methods): required.append(name)
 edges={k:[x for x in v.get('prerequisites',[]) if x in batches] for k,v in batches.items() if isinstance(v,dict)}; cycles=[]; seen=set(); active=set()
 def visit(x):
  if x in active: cycles.append(x); return
  if x not in seen:
   seen.add(x); active.add(x); [visit(y) for y in edges[x]]; active.remove(x)
 [visit(x) for x in sorted(edges)]
 bad=missing+dup+stale+nonwrite+empty+unknown+required+commits+rollbacks+calls+cycles+errors
 return {'status':'ok' if not bad else 'failed','planned_write_methods_count':len(methods),'expected_write_methods_count':len(writes),'rollback_smoke_covered_methods_count':len(smoked) if isinstance(smoked,list) else 0,'missing_write_methods':missing,'duplicate_planned_methods':dup,'stale_planned_methods':stale,'non_write_methods_in_plan':nonwrite,'empty_batches':empty,'unknown_batches':sorted(unknown),'missing_required_fields':sorted(required),'dependency_cycles':sorted(set(cycles)),'unacknowledged_commit_methods':sorted(commits),'unacknowledged_rollback_methods':sorted(rollbacks),'unacknowledged_transitive_write_calls':sorted(calls),'dynamic_sql_methods':sorted(dynamic),'batch_summary':{k:len(v.get('methods',[])) for k,v in sorted(batches.items())},'recommended_next_batch':next_batch,'errors':sorted(errors)}
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument('--repository-file',default=ROOT/'app/repository.py'); p.add_argument('--coverage-manifest',default=ROOT/'docs/postgres/repository_method_coverage.json'); p.add_argument('--write-plan',default=ROOT/'docs/postgres/repository_write_surface_plan.json'); p.add_argument('--format',choices=('text','json'),default='text'); p.add_argument('--output'); a=p.parse_args(argv)
 try: summary=audit(a.repository_file,a.coverage_manifest,a.write_plan); code=0 if summary['status']=='ok' else 1
 except ConfigError as e: summary={'status':'error','errors':[str(e)]}; code=2
 rendered=json.dumps(summary,indent=2,sort_keys=True)+'\n' if a.format=='json' else f"PostgreSQL Repository write plan: {summary['status']}\n"
 if a.output: Path(a.output).write_text(rendered,encoding='utf-8')
 else: print(rendered,end='')
 return code
if __name__=='__main__': sys.exit(main())
