"""Execute the real browser functions to catch checkbox/payload divergence."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _run(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the browser regression tests")
    result = subprocess.run(
        [node, "-e", script, str(_ROOT)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_capture_serializes_the_checkbox_state_for_every_original_row():
    _run(r"""
const assert = require('node:assert/strict'), fs = require('node:fs');
const root = process.argv[1];
const source = fs.readFileSync(root + '/backend/app/web/templates/capture.html', 'utf8');
const start = source.indexOf('function captureApp()');
eval(source.slice(start, source.indexOf('</script>', start)));
console.log = () => {};
const rawValues = [null, '', 'None', '-', 'x', ' X ', 'SIM'];
const raw = {sheets:[{sheet_id:7546, row_fields_extra:['fecho'], colunas_produzidas:'28',
  rows:rawValues.map((fecho,row_index) => ({
    row_index,fecho,qtd:String(row_index+1),modelo:'REPEATED'}))},
  {sheet_id:7545,row_fields_extra:['sucata'],rows:[{row_index:0,qtd:'2',sucata:''}]}]};
(async () => {
  for (const marked of [[], [1], [0,2,5]]) {
    const app = captureApp(); app.uploadedSheetIds = [7546,7545];
    let sent;
    global.fetch = async (_url, options) => {
      if (!options) return {ok:true,status:200,json:async()=>raw};
      sent = JSON.parse(options.body);
      return {ok:true,json:async()=>({ok:true})};
    };
    global.alert = message => { throw new Error(message); };
    await app.loadQtds();
    assert.deepEqual(app.qtdSheets[0].rows.map(r=>r.fecho_checked),[false,false,false,false,true,true,false]);
    // Includes unmarking the OCR's X, not just adding a new mark.
    app.qtdSheets[0].rows.forEach((r,i)=>{r.fecho_checked=marked.includes(i);});
    await app.submitQtds();
    const edits = sent.edits.filter(e=>e.field_path.endsWith('.fecho'));
    assert.deepEqual(edits.map(e=>e.value),rawValues.map((_,i)=>marked.includes(i)?'X':''));
    assert.deepEqual(edits.map(e=>e.field_path),rawValues.map((_,i)=>`rows[${i}].fecho`));
    assert.equal(sent.edits.filter(e=>e.field_path.endsWith('.qtd')).length,8);
    assert(raw.sheets[0].rows.every((r,i)=>r.fecho===rawValues[i]));
    assert.equal(app.stage,'done');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_validation_waits_for_edit_commit_and_keeps_failed_drafts():
    _run(r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const handlers={}, revision={value:'4'}, metrics=[];
global.document={addEventListener:(name,handler)=>{handlers[name]=handler;},querySelector:()=>revision};
global.window={ocrLatency:{start:()=>result=>metrics.push(result)}};
eval(fs.readFileSync(process.argv[1]+'/backend/app/web/static/sheet_write_guard.js','utf8'));
(async()=>{
  const form={getAttribute:()=>'/sheet/7955/edit',querySelector:()=>({value:'rows[4].of'})};
  const elt={closest:()=>form}, xhr={getResponseHeader:()=> '5'};
  const config={elt,parameters:{}};
  handlers['htmx:configRequest']({detail:config});
  assert.equal(config.parameters.expected_revision,'4');
  handlers['htmx:beforeRequest']({detail:{elt,xhr}});
  let resolved=false;
  const flush=window.sheetWriteGuard.flush().then(result=>{resolved=true;return result;});
  await Promise.resolve();assert.equal(resolved,false);
  handlers['htmx:afterRequest']({detail:{xhr,successful:false}});
  assert.equal(await flush,false);assert.equal(revision.value,'4');
  handlers['htmx:beforeRequest']({detail:{elt,xhr}});
  handlers['htmx:afterRequest']({detail:{xhr,successful:true}});
  assert.equal(await window.sheetWriteGuard.flush(),true);assert.equal(revision.value,'5');
  assert.deepEqual(metrics,['error','ok']);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
