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


def test_lookup_filters_pages_labels_and_applies_original_entry():
    _run(r"""
const assert = require('node:assert/strict'), fs = require('node:fs');
const root=process.argv[1];
const source=fs.readFileSync(root+'/backend/app/web/templates/sheet.html','utf8');
const start=source.indexOf('function ofWizard(');
eval(source.slice(start,source.indexOf('</script>',start)));
(async()=>{
  const wizard=ofWizard(7);wizard.active=true;wizard.row_index=2;
  wizard.ofInput='';wizard.modelInput='FST';wizard.lengthInput='1076';
  let lookedUp, applied, reloads=0;
  global.window={location:{reload:()=>reloads++}};
  const result={found:true,offset:50,total:51,limit:50,has_more:false,
    revision:3,plan_sha256:'snapshot',setor:'ACABAMENTO MTG2',entries:[
      {of:'266068',orig_idx:54,remaining:0,done:true,fechado:false,status_known:true,pending_valid:true}]};
  global.fetch=async(url,options)=>{
    if(!options){lookedUp=url;return{ok:true,json:async()=>result};}
    applied=JSON.parse(options.body);
    return{ok:true,text:async()=>JSON.stringify({ok:true,n_applied:1,revision:4,final_row:{}})};
  };
  await wizard.lookup(50);
  const url=new URL(lookedUp,'http://localhost');
  assert.equal(url.searchParams.get('q'),'');assert.equal(url.searchParams.get('modelo'),'FST');
  assert.equal(url.searchParams.get('comp_mm'),'1076');assert.equal(url.searchParams.get('offset'),'50');
  assert.equal(wizard.pendingLabel(result.entries[0]),'Por fazer em ACABAMENTO MTG2: 0 · Aberta');
  assert(wizard.pendingLabel({pending_valid:false,status_known:false}).includes('Indisponível'));
  await wizard.apply();assert.equal(applied.entry_idx,54);assert.equal(applied.row_index,2);
  assert.equal(applied.expected_revision,3);assert.equal(applied.plan_sha256,'snapshot');
  assert.equal(reloads,1);
  wizard.selectedEntry=0;wizard.clearResults();assert(!wizard.canApply);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_older_search_response_cannot_replace_a_newer_filter():
    _run(r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const source=fs.readFileSync(process.argv[1]+'/backend/app/web/templates/sheet.html','utf8');
const start=source.indexOf('function ofWizard(');
eval(source.slice(start,source.indexOf('</script>',start)));
(async()=>{
  const wizard=ofWizard(7);wizard.ofInput='OLD';let resolveOld;
  global.fetch=()=>new Promise(resolve=>{resolveOld=resolve;});
  const old=wizard.lookup();wizard.ofInput='NEW';wizard.clearResults();
  global.fetch=async()=>({ok:true,json:async()=>({found:true,q:'NEW',entries:[]})});
  await wizard.lookup();resolveOld({ok:true,json:async()=>({found:true,q:'OLD',entries:[]})});
  await old;assert.equal(wizard.lookupResult.q,'NEW');assert.equal(wizard.loading,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
