#!/usr/bin/env python3
"""Validate design artifacts only. Does not launch OpenCode or certify M0."""
import copy
import importlib.metadata
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from jsonschema import Draft202012Validator, FormatChecker

HERE = Path(__file__).resolve().parent
registry = json.loads((HERE / 'mcp-tools.json').read_text(encoding='utf-8'))
examples = json.loads((HERE / 'examples.json').read_text(encoding='utf-8'))
states = json.loads((HERE / 'states.json').read_text(encoding='utf-8'))
index = {t['name']: t for t in registry['tools']}
assert len(index) == 8 and len(examples['examples']) == 8
checks = 0
for t in registry['tools']:
    for key in ['inputSchema', 'outputSchema']:
        Draft202012Validator.check_schema(t[key]); checks += 1
for ex in examples['examples']:
    t = index[ex['tool']]
    for key, sk in [('input', 'inputSchema'), ('output', 'outputSchema'), ('error_output', 'outputSchema')]:
        Draft202012Validator(t[sk], format_checker=FormatChecker()).validate(ex[key]); checks += 1

def reject(name, value, output=False):
    global checks
    key = 'outputSchema' if output else 'inputSchema'
    assert not Draft202012Validator(index[name][key], format_checker=FormatChecker()).is_valid(value), (name, value)
    checks += 1

byname = {e['tool']: e for e in examples['examples']}
for name in index:
    v = copy.deepcopy(byname[name]['input']); v['unrecognized_field'] = True; reject(name, v)
    v = copy.deepcopy(byname[name]['output']); v['error'] = byname[name]['error_output']['error']; reject(name, v, True)
v = copy.deepcopy(byname['worker_start']['input']); v['request_id'] = 'not-an-id'; reject('worker_start', v)
v = copy.deepcopy(byname['worker_wait']['input']); v['wait_ms'] = 25001; reject('worker_wait', v)
v = copy.deepcopy(byname['worker_wait']['input']); v['wait_ms'] = -1; reject('worker_wait', v)
v = copy.deepcopy(byname['worker_result']['input']); v['section'] = 'content'; reject('worker_result', v)
v = copy.deepcopy(byname['worker_followup']['input']); v['mode'] = 'select_model'; reject('worker_followup', v)
v = copy.deepcopy(byname['worker_followup']['input']); v['model'] = 'opencode/other'; reject('worker_followup', v)
v = copy.deepcopy(byname['worker_status']['input']); v['limit'] = 2; reject('worker_status', v)
v = copy.deepcopy(byname['worker_start']['input']); v['workspace']['source_ref'] = '--help'; reject('worker_start', v)
v = copy.deepcopy(byname['worker_start']['input']); v['workspace']['include_existing_changes'] = True; reject('worker_start', v)
v = copy.deepcopy(byname['worker_start']['input']); v['workspace'].update(include_existing_changes=True, context_files=['x.py'], use_committed_only=True); reject('worker_start', v)
v = copy.deepcopy(byname['worker_start']['output']); v['data']['job']['active_attempt_id'] = None; reject('worker_start', v, True)
for old, targets in states['transitions'].items():
    assert old in states['states'] and all(s in states['states'] for s in targets)
    checks += 1
assert states['transitions']['cancelled'] == []
assert 'starting' not in states['transitions']['needs_attention']
checks += 2
sql = (HERE / 'schema.sql').read_text(encoding='utf-8')
for found in re.findall(r"state TEXT NOT NULL CHECK\(state IN \(([^)]+)\)\)", sql):
    assert set(re.findall(r"'([^']+)'", found)) == set(states['states']); checks += 1
with tempfile.TemporaryDirectory(prefix='hina-spec-ddl-') as td:
    db = sqlite3.connect(str(Path(td) / 'spec.sqlite'))
    try:
        db.executescript(sql)
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert len(tables) == 10
        checks += 3
        db.execute("INSERT INTO jobs(job_id,principal_id,goal,requested_model,profile,repository_path,state,created_at,updated_at) VALUES('j','local:1','fixture','opencode/test','develop','/fixture','accepted','2026-10-18T00:00:00Z','2026-10-18T00:00:00Z')")
        db.execute("INSERT INTO attempts(attempt_id,job_id,ordinal,executor_id,model,billing_class,state,contract_revision,binary_sha256) VALUES('a','j',1,'w0','opencode/test','free','running','0.3',?)", ('0'*64,))
        db.execute("UPDATE jobs SET active_attempt_id='a' WHERE job_id='j'")
        db.commit()
        def db_reject(stmt, params=()):
            global checks
            db.execute('SAVEPOINT negative')
            try:
                db.execute(stmt, params)
            except sqlite3.IntegrityError:
                checks += 1
            else:
                raise AssertionError('Expected constraint failure: ' + stmt)
            finally:
                db.execute('ROLLBACK TO negative'); db.execute('RELEASE negative')
        db_reject("INSERT INTO attempts(attempt_id,job_id,ordinal,executor_id,model,billing_class,state,contract_revision,binary_sha256) VALUES('a2','j',2,'w1','opencode/test','free','running','0.3','x')")
        db_reject("UPDATE jobs SET state='imaginary' WHERE job_id='j'")
        db.execute("INSERT INTO requests VALUES('local:1','r','worker_start','h','{}','j','a','accepted',NULL,'now','now')")
        db_reject("INSERT INTO requests VALUES('local:1','r','worker_start','h','{}','j','a','accepted',NULL,'now','now')")
        db_reject("INSERT INTO requests VALUES('local:1','r2','worker_start','h','not-json','j','a','accepted',NULL,'now','now')")
        db_reject("INSERT INTO artifacts(artifact_id,job_id,attempt_id,result_revision,section,visibility,relative_path,media_type,byte_length,sha256,redacted,complete,applyable,created_at) VALUES('art','j','a',1,'changes','export','x','text/plain',1,?,1,1,1,'now')", ('0'*64,))
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []; checks += 1
    finally:
        db.close()
print(json.dumps({'validation': 'PASS', 'checks': checks, 'tools': len(index), 'sql_tables': 10,
                  'jsonschema_version': importlib.metadata.version('jsonschema'), 'sqlite_version': sqlite3.sqlite_version,
                  'scope': 'Offline design schema/examples/DDL invariants only', 'm0_live_certified': False}, indent=2))
