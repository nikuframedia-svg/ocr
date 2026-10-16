"""An export must agree with the saved sheet after row indices change."""
from copy import deepcopy
import io

import openpyxl
import pytest

from app.web import db, export


@pytest.fixture
def saved_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'app.db')
    db.init_db()
    class Watcher:
        def get_refs(self):
            return {}
    monkeypatch.setattr('app.cross_check.get_watcher', lambda: Watcher())
    data = {'template_name': 'acabamento',
            'header': {'data': '15-10-2026', 'setor_maquina': 'ACABAMENTO', 'operador': 'TEST'},
            'footer': {}, 'rows': [
                {'of': order, 'modelo': model, 'qtd': qty}
                for order, model, qty in [('266614', 'REF-A', '5'), ('REP', 'REMOVE', '0'),
                                          ('266374', 'REF-B', '4'), ('266375', 'REF-C', '6')]]}
    sid = db.insert_sheet('synthetic.jpg')
    db.update_extraction(sid, deepcopy(data), {}, deepcopy(data))
    # This is the real mobile-quantity flow, before a reviewer deletes a row.
    for i, row in enumerate(data['rows']):
        for field in ('qtd', 'of', 'modelo'):
            db.apply_edit(sid, f'rows[{i}].{field}', row[field])
    return sid, data


def cpis_rows():
    book = openpyxl.load_workbook(io.BytesIO(export.build_cpis_workbook(None, None)), data_only=True)
    labels = {key: i for i, (key, _) in enumerate(export.CPIS_COLUMNS)}
    return [{key: values[index] for key, index in labels.items()}
            for values in list(book.active.values)[1:]]


@pytest.mark.parametrize('removed', [0, 1, 2])
def test_export_uses_validated_quantities_and_identity_after_deletion(saved_sheet, removed):
    sid, original = saved_sheet
    db.delete_row(sid, removed)
    db.validate_sheet(sid, 'TEST')
    expected = [r for i, r in enumerate(original['rows']) if i != removed]
    rows = cpis_rows()
    assert [(r['of'], r['modelo'], r['qtd']) for r in rows] == [
        (r['of'], r['modelo'], int(r['qtd'])) for r in expected]
    # The standard Excel download uses the same committed values.
    book = openpyxl.load_workbook(io.BytesIO(export.export_excel(None, None)), data_only=True)
    found = {}
    for sheet in book.worksheets[1:]:
        for values in sheet.values:
            if len(values) > 5 and values[4] in {r['modelo'] for r in expected}:
                found[values[4]] = values[5]
    assert found == {r['modelo']: int(r['qtd']) for r in expected}


@pytest.mark.parametrize('literal,expected', [('', None), ('por contar', 'por contar'), (' 0,00 ', 0)])
def test_latest_literal_text_and_blank_survive_index_shift(saved_sheet, literal, expected):
    sid, _ = saved_sheet
    db.apply_edit(sid, 'rows[2].qtd', literal)
    db.delete_row(sid, 1)
    db.validate_sheet(sid, 'TEST')
    rows = cpis_rows()
    assert rows[1]['modelo'] == 'REF-B'
    assert rows[1]['qtd'] == expected
    assert rows[2]['qtd'] == 6


def test_export_never_replays_audit_values_over_its_selected_snapshot(saved_sheet, monkeypatch):
    sid, _ = saved_sheet
    db.delete_row(sid, 1)
    original = export.attach_human_values
    def concurrent_edit(rows):
        # The query has already selected quantity 4; another browser now writes 99.
        db.apply_edit(sid, 'rows[1].qtd', '99')
        return original(rows)
    monkeypatch.setattr(export, 'attach_human_values', concurrent_edit)
    selected = export._query_cpis_rows(None, None, None)
    assert selected[1]['qtd'] == 4
    assert export._build_cpis_row(selected[1])['qtd'] == 4
    assert db.get_sheet(sid)['sheet_data']['rows'][1]['qtd'] == '99'
