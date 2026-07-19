#!/usr/bin/env python3
"""AST-only consistency gate for the PostgreSQL Repository write plan."""
from __future__ import annotations

import argparse, ast, json, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"batch", "mutation_kind", "risk", "transaction_contract", "current_commit_behavior", "sqlite_postgres_blockers", "side_effects", "dependencies", "returns", "postgres_strategy", "test_strategy", "rollback_strategy", "notes"}
RISK = {"low", "medium", "high", "critical"}

class ConfigError(Exception): pass

def load(path):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ConfigError(str(exc))

def repository_methods(path):
    try: tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc: raise ConfigError(str(exc))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Repository"), None)
    if not cls: raise ConfigError("class Repository not found")
    return {n.name:n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")}

def sql_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
    return ""

def evidence(node, writes):
    result={"commit":False,"rollback":False,"writes":False,"dynamic":False,"calls":set()}
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            attr=n.func.attr
            if attr in {"commit", "rollback"}: result[attr]=True
            if attr == "executescript": result["writes"]=True
            if isinstance(n.func.value, ast.Name) and n.func.value.id == "self" and attr in writes: result["calls"].add(attr)
            if attr in {"execute", "executemany"} and n.args:
                value=sql_text(n.args[0]).upper()
                if not value: result["dynamic"]=True
                if any(token in value for token in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")): result["writes"]=True
    return result

def audit(repository_file=ROOT/"app/repository.py", coverage_manifest=ROOT/"docs/postgres/repository_method_coverage.json", write_plan=ROOT/"docs/postgres/repository_write_surface_plan.json"):
    coverage, plan = load(coverage_manifest), load(write_plan)
    if plan.get("schema_version") != 1: raise ConfigError("unknown schema_version")
    expected=coverage.get("write_or_mutating")
    if not isinstance(expected, dict): raise ConfigError("coverage write_or_mutating must be object")
    batches=plan.get("batches"); methods=plan.get("methods")
    if not isinstance(batches, dict) or not isinstance(methods, dict): raise ConfigError("plan batches and methods must be objects")
    repo=repository_methods(repository_file); writes=set(expected); smoke=set(coverage.get("deferred_read_only",{}))
    listed=[]
    for bid, batch in batches.items():
        if not isinstance(batch,dict): raise ConfigError(f"batch {bid} must be object")
        listed += batch.get("methods", []) if isinstance(batch.get("methods",[]),list) else []
    counts=Counter(listed); plan_names=set(methods)|set(listed)
    missing=sorted(writes-plan_names); duplicates=sorted(k for k,v in counts.items() if v != 1)
    stale=sorted(plan_names-set(repo)); non_write=sorted(plan_names-writes)
    empty=sorted(k for k,v in batches.items() if not isinstance(v.get("methods"),list) or not v["methods"])
    unknown=sorted(k for k,v in methods.items() if not isinstance(v,dict) or v.get("batch") not in batches)
    required=[]; errors=[]
    for name in sorted(writes-set(methods)):
        required.append(name)
    for name, meta in methods.items():
        if not isinstance(meta, dict) or counts.get(name, 0) != 1:
            errors.append(f"batch membership mismatch: {name}")
        elif meta.get("batch") not in batches or name not in batches[meta["batch"]].get("methods", []):
            errors.append(f"batch membership mismatch: {name}")
    commits=[]; rollbacks=[]; calls=[]; dynamic=[]
    for name in sorted(methods):
        meta=methods[name]
        if not isinstance(meta,dict): required.append(name); continue
        absent=sorted(REQUIRED-set(meta));
        if absent or meta.get("risk") not in RISK or not isinstance(meta.get("sqlite_postgres_blockers"),list) or not meta.get("sqlite_postgres_blockers") or not isinstance(meta.get("side_effects"),list) or not meta.get("side_effects") or not isinstance(meta.get("test_strategy"),list) or not meta.get("test_strategy"):
            required.append(name)
        if name in expected and meta.get("mutation_kind") != expected[name].get("mutation_kind"): errors.append(f"mutation_kind mismatch: {name}")
        if name in repo and name in writes:
            e=evidence(repo[name],writes)
            behavior=str(meta.get("current_commit_behavior", "")).lower()
            if e["commit"] and "commit" not in behavior: commits.append(name)
            if e["rollback"] and "rollback" not in behavior: rollbacks.append(name)
            if e["writes"] and not meta.get("sqlite_postgres_blockers"): errors.append(f"write blockers missing: {name}")
            if e["calls"]-set(meta.get("dependencies",[])): calls.append(name)
            if e["dynamic"]: dynamic.append(name)
    # batch prerequisite edges must be acyclic when they name batches.
    edges={b:[x for x in v.get("prerequisites",[]) if x in batches] for b,v in batches.items()}
    cycles=[]; seen=set(); active=set()
    def visit(n):
        if n in active: cycles.append(n); return
        if n in seen:return
        seen.add(n); active.add(n)
        for x in edges[n]: visit(x)
        active.remove(n)
    for b in sorted(edges): visit(b)
    violations=missing+duplicates+stale+non_write+empty+unknown+required+commits+rollbacks+calls+cycles+errors
    summary={"status":"ok" if not violations else "failed", "planned_write_methods_count":len(set(methods)), "expected_write_methods_count":len(writes), "missing_write_methods":missing, "duplicate_planned_methods":duplicates, "stale_planned_methods":stale, "non_write_methods_in_plan":non_write, "empty_batches":empty, "unknown_batches":unknown, "missing_required_fields":sorted(required), "dependency_cycles":sorted(set(cycles)), "unacknowledged_commit_methods":sorted(commits), "unacknowledged_rollback_methods":sorted(rollbacks), "unacknowledged_transitive_write_calls":sorted(calls), "dynamic_sql_methods":sorted(dynamic), "batch_summary":{b:len(v.get("methods",[])) for b,v in sorted(batches.items())}, "recommended_next_batch":"write_test_harness_and_transaction_foundation", "errors":sorted(errors)}
    return summary

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--repository-file",default=ROOT/"app/repository.py"); p.add_argument("--coverage-manifest",default=ROOT/"docs/postgres/repository_method_coverage.json"); p.add_argument("--write-plan",default=ROOT/"docs/postgres/repository_write_surface_plan.json"); p.add_argument("--format",choices=("text","json"),default="text"); p.add_argument("--output")
    a=p.parse_args(argv)
    try: summary=audit(a.repository_file,a.coverage_manifest,a.write_plan); code=0 if summary["status"]=="ok" else 1
    except ConfigError as exc: summary={"status":"error","errors":[str(exc)]}; code=2
    output=json.dumps(summary,indent=2,sort_keys=True)+"\n" if a.format=="json" else f"PostgreSQL Repository write plan: {summary['status']}\n"
    if a.output: Path(a.output).write_text(output,encoding="utf-8")
    else: print(output,end="")
    return code
if __name__ == "__main__": sys.exit(main())
