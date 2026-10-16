"""The authorised maintenance changes exactly one OF, never reimports CSVs."""
from copy import deepcopy
import csv
import importlib.util
import io
import json
from pathlib import Path
import sqlite3

import pytest
from app.web import db, main

spec = importlib.util.spec_from_file_location('amend_validated_order', Path(__file__).parents[2] / 'scripts/ops/amend_validated_order.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    database = tmp_path / 'app.db'
    monkeypatch.setattr(db, '_DB_PATH', database)
    db.init_db()
    sid = db.insert_sheet('images/identified-source.jpg')
    row = {'of':'265958','modelo':'ESCADAS TL465-101','qtd':'2','nesting':'515.1-05'}
    data = {'template_name':'gemini_hd36', 'header':{'data':'14-10-2026','setor_maquina':'HD36'},
            'rows':[deepcopy(row) for _ in range(5)], 'footer':{}}
    db.update_extraction(sheet_id=sid,raw_extraction=deepcopy(data),dq_audit={'cells':{}},sheet_data=data)
    db.validate_sheet(sid,'TEST')
    db.set_factory_csv_name(sid,'pending.csv')
    pending = tmp_path / 'csv' / 'pending.csv'
    imported = pending.parent / 'imported'
    imported.mkdir(parents=True)
    pending.write_bytes(main._to_3block_csv('identified-source.jpg', data).encode('utf-8'))
    revision = db.get_sheet(sid)['revision']
    return database, pending, imported, sid, revision


def call(state, tmp_path, **kwargs):
    database,pending,imported,sid,revision = state
    return ops.amend(database,pending,imported,tmp_path/'backup',sheet_id=sid,index=4,
                     revision=revision,old_order='265958',new_order='266303',
                     actor='user-authorized-maintenance',reason='User requested the correction',**kwargs)


def test_default_is_read_only_and_creates_no_artifacts(state,tmp_path):
    database,pending,_,sid,_ = state
    before = db.get_sheet(sid)
    content = pending.read_bytes()
    assert call(state,tmp_path)['applied'] is False
    assert db.get_sheet(sid) == before and pending.read_bytes() == content
    assert not (tmp_path/'backup').exists()


def test_one_cell_audit_production_ids_validation_and_other_csv_values(state,tmp_path):
    database,pending,_,sid,revision = state
    before = db.get_sheet(sid)
    content = pending.read_bytes()
    with db.conn() as c:
        production = [dict(row) for row in c.execute('SELECT * FROM production_rows ORDER BY id')]
    result = call(state,tmp_path,apply=True)
    after = db.get_sheet(sid)
    expected = deepcopy(before)
    expected['sheet_data']['rows'][4]['of'] = '266303'
    expected['revision'] += 1
    assert after == expected
    with db.conn() as c:
        current = [dict(row) for row in c.execute('SELECT * FROM production_rows ORDER BY id')]
        audit = dict(c.execute('SELECT * FROM edits WHERE id=?',(result['audit_id'],)).fetchone())
    expected_rows = deepcopy(production)
    expected_rows[4]['of'] = '266303'
    expected_rows[4]['human_fields'] = 'of'
    assert current == expected_rows
    assert audit['field_path']=='rows[4].of' and audit['old_value']=='265958' and audit['new_value']=='266303'
    assert audit['source']=='human' and audit['sheet_revision']==revision+1
    assert audit['actor']=='user-authorized-maintenance'
    original_rows=list(csv.reader(io.StringIO(content.decode('utf-8-sig')),delimiter=';'))
    amended_rows=list(csv.reader(io.StringIO(pending.read_text(encoding='utf-8-sig')),delimiter=';'))
    differences=[(i,j) for i,row in enumerate(original_rows) for j,value in enumerate(row) if amended_rows[i][j]!=value]
    assert len(differences)==1
    i,j=differences[0]
    assert original_rows[i][j]=='265958' and amended_rows[i][j]=='266303'
    assert (tmp_path/'backup'/'factory-before.csv').read_bytes()==content
    assert (tmp_path/'backup'/'app-before.db').is_file()
    with pytest.raises(ValueError,match='status/revision'):
        call(state,tmp_path,apply=True)


@pytest.mark.parametrize('change',['revision','status','row','production','csv','imported','missing'])
def test_conflicts_and_imported_files_abort_without_writing(state,tmp_path,change):
    database,pending,imported,sid,revision=state
    with db.conn() as c:
        if change=='revision': c.execute('UPDATE sheets SET revision=revision+1 WHERE id=?',(sid,))
        if change=='status': c.execute("UPDATE sheets SET status='extracted' WHERE id=?",(sid,))
        if change=='row':
            data=db.get_sheet(sid)['sheet_data']; data['rows'][4]['of']='other'
            c.execute('UPDATE sheets SET sheet_data=? WHERE id=?',(json.dumps(data),sid))
        if change=='production': c.execute("UPDATE production_rows SET of='other' WHERE sheet_id=? AND row_index=4",(sid,))
    if change=='csv': pending.write_bytes(pending.read_bytes().replace(b'identified-source.jpg',b'wrong-source.jpg'))
    if change=='imported': (imported/pending.name).write_text('Already consumed')
    if change=='missing': pending.unlink()
    before=db.get_sheet(sid)
    with pytest.raises(ValueError): call(state,tmp_path,apply=True)
    assert db.get_sheet(sid)==before and not (tmp_path/'backup').exists()


def test_commit_failure_rolls_back_database_and_atomic_csv_replacement(state,tmp_path,monkeypatch):
    database,pending,_,sid,_=state
    before=db.get_sheet(sid); content=pending.read_bytes()
    real_connect=sqlite3.connect
    class FailCommit:
        def __init__(self,conn): object.__setattr__(self,'conn',conn)
        def __getattr__(self,name): return getattr(self.conn,name)
        def __setattr__(self,name,value): setattr(self.conn,name,value)
        def commit(self): raise RuntimeError('Simulated commit failure')
    def connect(path,*args,**kwargs):
        connection=real_connect(path,*args,**kwargs)
        return FailCommit(connection) if path==database and not kwargs.get('uri') else connection
    monkeypatch.setattr(ops.sqlite3,'connect',connect)
    with pytest.raises(RuntimeError,match='commit failure'): call(state,tmp_path,apply=True)
    monkeypatch.setattr(ops.sqlite3,'connect',real_connect)
    assert db.get_sheet(sid)==before and pending.read_bytes()==content
    with db.conn() as c: assert c.execute('SELECT COUNT(*) FROM edits').fetchone()[0]==0


@pytest.mark.parametrize('race',['sheet','production','csv','imported'])
def test_changes_between_inspection_and_transaction_are_preserved(state,tmp_path,monkeypatch,race):
    database,pending,imported,sid,_=state
    original=ops.inspect
    content=pending.read_bytes()
    def raced(*args,**kwargs):
        snapshot=original(*args,**kwargs)
        if race=='sheet':
            with db.conn() as c: c.execute('UPDATE sheets SET revision=revision+1 WHERE id=?',(sid,))
        if race=='production':
            with db.conn() as c: c.execute("UPDATE production_rows SET of='a-new-human-order' WHERE sheet_id=? AND row_index=4",(sid,))
        if race=='csv': pending.write_bytes(content+b'\r\n')
        if race=='imported': (imported/pending.name).write_bytes(content)
        return snapshot
    monkeypatch.setattr(ops,'inspect',raced)
    with pytest.raises(ValueError,match='changed'): call(state,tmp_path,apply=True)
    assert db.get_sheet(sid)['sheet_data']['rows'][4]['of']=='265958'
    assert pending.read_bytes()==(content+b'\r\n' if race=='csv' else content)
    with db.conn() as c: assert c.execute('SELECT COUNT(*) FROM edits').fetchone()[0]==0


def test_csv_multiline_quotes_bom_and_non_target_lines_are_preserved():
    text='\ufeff### CABEÇALHO\r\nFICHEIRO;DATA\r\nimage.jpg;14-10-2026\r\n\r\n### TABELA DE NESTING\r\nFICHEIRO;OF;MODELO\r\nimage.jpg;265958;"texto; com\r\naspas ""livres"""\r\n\r\n### RODAPÉ\r\nFICHEIRO;HORAS\r\nimage.jpg;8\r\n'
    amended=ops.pending_csv_update(text.encode(),image='image.jpg',index=0,old_order='265958',new_order='266303')
    assert amended==text.replace('image.jpg;265958;','image.jpg;266303;').encode()
