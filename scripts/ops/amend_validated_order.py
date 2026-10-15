"""Audited maintenance of one validated OF and its still-pending factory CSV.

Inspection is read-only by default. This never creates or redistributes an
already imported CSV and does not reopen a sheet or change validation time.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def pending_csv_update(content: bytes, *, image: str, index: int,
                       old_order: str, new_order: str) -> bytes:
    text = content.decode('utf-8-sig')
    lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=';')
    table = False
    columns = None
    production_index = 0
    table_count = 0
    replacement = None
    while True:
        start = reader.line_num
        try:
            cells = next(reader)
        except StopIteration:
            break
        end = reader.line_num
        if cells and cells[0].startswith('###'):
            table = cells[0].startswith('### TABELA DE ')
            if table:
                table_count += 1
                if table_count > 1:
                    raise ValueError('CSV has multiple production tables')
            columns = None
            continue
        if not table or not cells:
            continue
        if columns is None:
            columns = cells
            if columns.count('OF') != 1 or columns.count('FICHEIRO') != 1:
                raise ValueError('CSV table has no unambiguous OF/image columns')
            continue
        if production_index == index:
            if replacement is not None:
                raise ValueError('CSV has multiple matching production tables')
            if cells[columns.index('FICHEIRO')] != image:
                raise ValueError('CSV belongs to a different source image')
            column = columns.index('OF')
            if cells[column] != old_order:
                raise ValueError('CSV order changed or does not match the sheet')
            changed = list(cells)
            changed[column] = new_order
            ending = '\r\n' if lines[end - 1].endswith('\r\n') else '\n'
            stream = io.StringIO(newline='')
            csv.writer(stream, delimiter=';', lineterminator=ending).writerow(changed)
            replacement = (start, end, stream.getvalue())
        production_index += 1
    if replacement is None:
        raise ValueError('CSV production row not found')
    start, end, changed = replacement
    amended = ''.join(lines[:start]) + changed + ''.join(lines[end:])
    return (b'\xef\xbb\xbf' if content.startswith(b'\xef\xbb\xbf') else b'') + amended.encode('utf-8')


def inspect(database: Path, pending_csv: Path, imported: Path, *, sheet_id: int,
            index: int, revision: int, old_order: str, new_order: str) -> dict:
    if not old_order or not new_order or old_order == new_order or index < 0:
        raise ValueError('A distinct old/new order and valid row are required')
    if not imported.is_dir() or imported.resolve() != (pending_csv.parent / 'imported').resolve():
        raise ValueError('The actual adjacent imported directory is required')
    if not pending_csv.is_file() or (imported / pending_csv.name).exists():
        raise ValueError('CSV is missing or already imported; reconcile the external destination first')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        sheet = conn.execute('SELECT * FROM sheets WHERE id=?', (sheet_id,)).fetchone()
        if sheet is None or sheet['status'] != 'validated' or sheet['revision'] != revision:
            raise ValueError('Sheet status/revision differs from the reviewed correction')
        data = json.loads(sheet['sheet_data'])
        rows = data.get('rows') or []
        if index >= len(rows) or rows[index].get('of') != old_order:
            raise ValueError('Sheet row/order differs from the reviewed correction')
        if sheet['factory_csv_name'] != pending_csv.name:
            raise ValueError('CSV is not the recorded factory file for this sheet')
        production = conn.execute('SELECT * FROM production_rows WHERE sheet_id=? AND row_index=?',
                                  (sheet_id, index)).fetchall()
        if len(production) != 1 or production[0]['of'] != old_order:
            raise ValueError('Production row is missing, ambiguous, or inconsistent')
        content = pending_csv.read_bytes()
        updated = pending_csv_update(content, image=Path(sheet['image_path']).name,
                                     index=index, old_order=old_order, new_order=new_order)
        return {'sheet': dict(sheet), 'production': dict(production[0]),
                'csv_before': content, 'csv_after': updated}


def amend(database: Path, pending_csv: Path, imported: Path, backup: Path, *,
          sheet_id: int, index: int, revision: int, old_order: str, new_order: str,
          actor: str, reason: str, apply: bool = False) -> dict:
    state = inspect(database, pending_csv, imported, sheet_id=sheet_id, index=index,
                    revision=revision, old_order=old_order, new_order=new_order)
    result = {'sheet_id': sheet_id, 'row': index + 1, 'old_order': old_order,
              'new_order': new_order, 'revision_before': revision,
              'csv_sha256_before': digest(state['csv_before']),
              'csv_sha256_after': digest(state['csv_after']), 'applied': False}
    if not apply:
        return result
    if not actor.strip() or not reason.strip():
        raise ValueError('Actor and reason are required for the audit')
    backup.mkdir(parents=True, exist_ok=False)
    (backup / 'sheet-before.json').write_text(json.dumps(state['sheet'], indent=2) + '\n')
    (backup / 'production-before.json').write_text(json.dumps(state['production'], indent=2) + '\n')
    (backup / 'factory-before.csv').write_bytes(state['csv_before'])
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'app-before.db') as target:
            source.backup(target)
    temporary = pending_csv.with_name(pending_csv.name + '.amendment-tmp')
    csv_replaced = False
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = dict(conn.execute('SELECT * FROM sheets WHERE id=?', (sheet_id,)).fetchone())
        production = dict(conn.execute('SELECT * FROM production_rows WHERE id=?',
                                       (state['production']['id'],)).fetchone())
        if current != state['sheet'] or production != state['production']:
            raise ValueError('Sheet/production changed after inspection')
        if (imported / pending_csv.name).exists() or pending_csv.read_bytes() != state['csv_before']:
            raise ValueError('CSV changed after inspection')
        data = json.loads(current['sheet_data'])
        data['rows'][index]['of'] = new_order
        conn.execute('UPDATE sheets SET sheet_data=?,revision=revision+1 WHERE id=?',
                     (json.dumps(data, ensure_ascii=False), sheet_id))
        human = set(filter(None, (production.get('human_fields') or '').split(','))) | {'of'}
        conn.execute('UPDATE production_rows SET of=?,human_fields=? WHERE id=?',
                     (new_order, ','.join(sorted(human)), production['id']))
        audit = conn.execute('INSERT INTO edits (sheet_id,field_path,old_value,new_value,source,actor,reason,sheet_revision) '
                             "VALUES (?,?,?,?,'human',?,?,?)",
                             (sheet_id, f'rows[{index}].of', old_order, new_order, actor, reason, revision + 1))
        temporary.write_bytes(state['csv_after'])
        if (imported / pending_csv.name).exists() or pending_csv.read_bytes() != state['csv_before']:
            raise ValueError('CSV changed before replacement')
        temporary.replace(pending_csv)
        csv_replaced = True
        conn.commit()
        result.update(applied=True, revision_after=revision + 1, audit_id=audit.lastrowid)
    except BaseException:
        conn.rollback()
        if csv_replaced and pending_csv.read_bytes() == state['csv_after']:
            temporary.write_bytes(state['csv_before'])
            temporary.replace(pending_csv)
        raise
    finally:
        conn.close()
        temporary.unlink(missing_ok=True)
    (backup / 'amendment.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--csv', required=True, type=Path)
    parser.add_argument('--imported', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--sheet', required=True, type=int)
    parser.add_argument('--row', required=True, type=int, help='One-based production row')
    parser.add_argument('--revision', required=True, type=int)
    parser.add_argument('--old-order', required=True)
    parser.add_argument('--new-order', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(amend(args.database, args.csv, args.imported, args.backup,
                           sheet_id=args.sheet, index=args.row - 1, revision=args.revision,
                           old_order=args.old_order, new_order=args.new_order,
                           actor=args.actor, reason=args.reason, apply=args.apply)))


if __name__ == '__main__':
    main()
