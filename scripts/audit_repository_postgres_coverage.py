#!/usr/bin/env python3
"""AST-only audit for PostgreSQL Repository read-surface coverage."""
from __future__ import annotations

import argparse, ast, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_CATEGORIES = ("deferred_read_only", "write_or_mutating", "infrastructure_or_mixed")
MANIFEST_TOP_LEVEL_KEYS = ("schema_version", *MANIFEST_CATEGORIES)
EXCLUDED_RUNTIME_SQL_DIRS = frozenset({"__pycache__", "backups", "data", "logs"})
WRITE_OPS = {"insert", "update", "delete", "replace", "create", "alter", "drop", "truncate"}
DDL_OPS = {"create", "alter", "drop", "truncate"}
SQL_APIS = {"execute", "executemany", "executescript"}

class ConfigError(Exception): pass

def parse_file(path):
    try: return ast.parse(Path(path).read_text(), filename=str(path))
    except (OSError, SyntaxError) as e: raise ConfigError(f"cannot parse {path}: {e}")

def literal(node):
    try: return ast.literal_eval(node)
    except Exception as e: raise ConfigError(f"cannot statically read SMOKE_METHODS: {e}")

def find_repo_methods(tree):
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Repository"), None)
    if cls is None: raise ConfigError("class Repository not found")
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")}

def find_smoke_methods(tree):
    values=[]; found=False
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id=="SMOKE_METHODS" for t in n.targets):
            found=True; val=literal(n.value)
            if not isinstance(val,(tuple,list)): raise ConfigError("SMOKE_METHODS must be tuple/list literal")
            values=list(val)
    if not found: raise ConfigError("SMOKE_METHODS not found")
    if not all(isinstance(x,str) for x in values): raise ConfigError("SMOKE_METHODS must contain strings")
    return values

def read_manifest(path):
    try: data=json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e: raise ConfigError(f"cannot read manifest: {e}")
    if not isinstance(data, dict): raise ConfigError("manifest top level must be object")
    unknown=sorted(set(data)-set(MANIFEST_TOP_LEVEL_KEYS))
    missing=sorted(set(MANIFEST_TOP_LEVEL_KEYS)-set(data))
    if unknown: raise ConfigError(f"manifest has unknown top-level keys: {', '.join(unknown)}")
    if missing: raise ConfigError(f"manifest is missing top-level keys: {', '.join(missing)}")
    if data["schema_version"] != 1: raise ConfigError("unknown manifest schema_version")
    for cat in MANIFEST_CATEGORIES:
        if not isinstance(data.get(cat), dict): raise ConfigError(f"manifest {cat} must be object")
    for name, meta in data["deferred_read_only"].items():
        validate_metadata(name, meta, "deferred_read_only", ("reason", "blockers", "recommended_batch"))
        if not isinstance(meta["blockers"], list) or not all(isinstance(item, str) and item for item in meta["blockers"]):
            raise ConfigError(f"manifest deferred_read_only.{name}.blockers must be list of non-empty strings")
    for name, meta in data["write_or_mutating"].items():
        validate_metadata(name, meta, "write_or_mutating", ("reason", "mutation_kind"))
    for name, meta in data["infrastructure_or_mixed"].items():
        validate_metadata(name, meta, "infrastructure_or_mixed", ("reason",))
    return data

def validate_metadata(name, meta, category, required_keys):
    if not isinstance(meta, dict):
        raise ConfigError(f"manifest {category}.{name} must be object")
    unknown=sorted(set(meta)-set(required_keys))
    missing=sorted(set(required_keys)-set(meta))
    if unknown:
        raise ConfigError(f"manifest {category}.{name} has unknown metadata keys: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"manifest {category}.{name} is missing metadata keys: {', '.join(missing)}")
    for key in required_keys:
        if key != "blockers" and (not isinstance(meta[key], str) or not meta[key].strip()):
            raise ConfigError(f"manifest {category}.{name}.{key} must be non-empty string")

def static_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
    if isinstance(node, ast.JoinedStr):
        s=""
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value,str): s+=v.value
            else: break
        return s or None
    return None

def sql_op(sql):
    if sql is None: return "dynamic_or_unknown"
    s=re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*", "", sql, flags=re.S)
    m=re.match(r"\s*([A-Za-z]+)", s)
    if not m: return "dynamic_or_unknown"
    kw=m.group(1).lower()
    if kw in {"select","insert","update","delete","replace","pragma","create","alter","drop","truncate"}: return "ddl" if kw in DDL_OPS else kw
    return "dynamic_or_unknown"

def attr_chain(node):
    parts=[]
    while isinstance(node, ast.Attribute): parts.append(node.attr); node=node.value
    if isinstance(node, ast.Name): parts.append(node.id)
    return list(reversed(parts))

def analyze_method(fn, write_methods):
    evidence=[]; dynamic=[]
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            chain=attr_chain(n.func)
            if chain and chain[-1] in SQL_APIS:
                op=sql_op(static_string(n.args[0]) if n.args else None)
                if op in WRITE_OPS or op=="ddl": evidence.append({"line":n.lineno,"kind":"sql","operation":op,"api":chain[-1]})
                elif op=="dynamic_or_unknown": dynamic.append({"line":n.lineno,"api":chain[-1]})
            if chain == ["self","conn","commit"] or chain == ["self","conn","rollback"] or chain == ["self","transaction"]:
                evidence.append({"line":n.lineno,"kind":"call","operation":".".join(chain)})
            if len(chain)==2 and chain[0]=="self" and chain[1] in write_methods:
                evidence.append({"line":n.lineno,"kind":"transitive_write_call","operation":chain[1]})
    return evidence, dynamic

def enclosing_name(tree, line):
    best=(0,"<module>")
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and hasattr(n,'lineno'):
            end=getattr(n,'end_lineno',n.lineno)
            if n.lineno <= line <= end and n.lineno >= best[0]: best=(n.lineno,n.name)
    return best[1]

def runtime_census(app_dir, repo_file):
    calls=[]
    app_dir=Path(app_dir).resolve()
    project_root=app_dir.parent
    for path in sorted(app_dir.rglob("*.py")):
        if any(part in EXCLUDED_RUNTIME_SQL_DIRS for part in path.relative_to(app_dir).parts):
            continue
        if path.resolve()==Path(repo_file).resolve(): continue
        tree=parse_file(path)
        text=path.read_text().splitlines()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                chain=attr_chain(n.func)
                if chain and chain[-1] in SQL_APIS:
                    op=sql_op(static_string(n.args[0]) if n.args else None)
                    ctx=(text[n.lineno-1].strip() if n.lineno-1 < len(text) else "")[:140]
                    calls.append({"file":str(path.relative_to(project_root)),"function":enclosing_name(tree,n.lineno),"line":n.lineno,"api":chain[-1],"operation":op,"context":re.sub(r"\s+"," ",ctx)})
    files=sorted({c['file'] for c in calls})
    calls.sort(key=lambda c: (c['file'], c['line'], c['api'], c['context']))
    return {"calls":calls,"runtime_select_calls":sum(c['operation']=='select' for c in calls),"runtime_write_calls":sum(c['operation'] in {'insert','update','delete','replace'} for c in calls),"runtime_schema_calls":sum(c['operation']=='ddl' or c['operation']=='pragma' for c in calls),"runtime_dynamic_unknown_calls":sum(c['operation']=='dynamic_or_unknown' for c in calls),"files_with_direct_sql":files}

def audit(repository_file=ROOT/'app/repository.py', smoke_script=ROOT/'scripts/postgres_repository_smoke.py', manifest=ROOT/'docs/postgres/repository_method_coverage.json'):
    repository_file=Path(repository_file); smoke_script=Path(smoke_script); manifest=Path(manifest)
    methods=find_repo_methods(parse_file(repository_file)); smoke=find_smoke_methods(parse_file(smoke_script)); data=read_manifest(manifest)
    method_names=set(methods); smoke_counts=Counter(smoke); smoke_set=set(smoke)
    cats={cat:set(data[cat]) for cat in MANIFEST_CATEGORIES}
    manifest_all=[]
    for cat in MANIFEST_CATEGORIES: manifest_all += list(data[cat])
    manifest_counts=Counter(manifest_all)
    write_methods=cats['write_or_mutating']
    obvious={name: analyze_method(fn, write_methods) for name,fn in methods.items()}
    duplicate_classifications=sorted([m for m,c in manifest_counts.items() if c>1] + [m for m in smoke_set if m in manifest_counts])
    unclassified=sorted(method_names - smoke_set - set(manifest_all))
    stale=sorted(set(manifest_all)-method_names)
    unknown_smoke=sorted(smoke_set-method_names)
    duplicate_smoke=sorted([m for m,c in smoke_counts.items() if c>1])
    smoke_write=sorted([m for m in smoke_set & method_names if obvious[m][0] or m in write_methods])
    deferred_write=sorted([m for m in cats['deferred_read_only'] & method_names if obvious[m][0]])
    deferred_groups=defaultdict(list)
    for m, meta in data['deferred_read_only'].items(): deferred_groups[meta.get('recommended_batch','unknown')].append(m)
    direct=runtime_census(repository_file.parent, repository_file)
    errors=[]
    for label, items in [("unclassified",unclassified),("duplicate_classifications",duplicate_classifications),("stale_manifest_entries",stale),("unknown_smoke_methods",unknown_smoke),("duplicate_smoke_methods",duplicate_smoke),("smoke_write_suspects",smoke_write),("deferred_write_suspects",deferred_write)]:
        if items: errors.append(f"{label}: {', '.join(items)}")
    covered=len(smoke_set); deferred=len(cats['deferred_read_only']); read_total=covered+deferred
    summary={"status":"ok" if not errors else "failed","repository_public_methods_count":len(methods),"smoke_covered_read_count":covered,"deferred_read_only_count":deferred,"write_or_mutating_count":len(cats['write_or_mutating']),"infrastructure_or_mixed_count":len(cats['infrastructure_or_mixed']),"classified_methods_count":len((smoke_set|set(manifest_all)) & method_names),"unclassified":unclassified,"duplicates":duplicate_classifications,"duplicate_classifications":duplicate_classifications,"stale_manifest_entries":stale,"smoke_methods_count":len(smoke),"duplicate_smoke_methods":duplicate_smoke,"unknown_smoke_methods":unknown_smoke,"smoke_write_suspects":smoke_write,"deferred_write_suspects":deferred_write,"dynamic_sql_methods":sorted(m for m,(e,d) in obvious.items() if d),"deferred_groups":{k:sorted(v) for k,v in sorted(deferred_groups.items())},"direct_runtime_sql_summary":direct,"read_surface_total":read_total,"read_surface_covered":covered,"read_surface_deferred":deferred,"read_surface_coverage_percent":round((covered/read_total*100),2) if read_total else 100.0,"errors":errors}
    return summary

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--repository-file',default=ROOT/'app/repository.py'); p.add_argument('--smoke-script',default=ROOT/'scripts/postgres_repository_smoke.py'); p.add_argument('--manifest',default=ROOT/'docs/postgres/repository_method_coverage.json'); p.add_argument('--format',choices=['text','json'],default='text'); p.add_argument('--output')
    a=p.parse_args(argv)
    try: summary=audit(a.repository_file,a.smoke_script,a.manifest); code=0 if summary['status']=='ok' else 1
    except ConfigError as e: summary={"status":"error","errors":[str(e)]}; code=2
    out=json.dumps(summary,indent=2,sort_keys=True) if a.format=='json' else render_text(summary)
    if a.output: Path(a.output).write_text(out+'\n')
    else: print(out)
    return code

def render_text(s):
    lines=[f"status: {s.get('status')}"]
    for k in ["repository_public_methods_count","smoke_covered_read_count","deferred_read_only_count","write_or_mutating_count","infrastructure_or_mixed_count","classified_methods_count","read_surface_coverage_percent"]: lines.append(f"{k}: {s.get(k)}")
    if s.get('errors'): lines.append('errors:'); lines += [f"- {e}" for e in s['errors']]
    return '\n'.join(lines)
if __name__=='__main__': sys.exit(main())
