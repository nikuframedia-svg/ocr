const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({executablePath: config.chromium, headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, extraHTTPHeaders: {'X-OCR-Probe': '1'}});
    const errors = [], dialogs = [], writes = [], lookups = [];
    page.on('pageerror', error => errors.push(error.stack));
    page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
    page.on('request', request => {
      if (request.method() === 'POST') writes.push(request);
      if (request.url().includes('of-lookup')) lookups.push(request);
    });
    await page.goto(config.url);
    await page.waitForFunction(() => Boolean(window.Alpine));
    assert.equal(await page.locator('button[title="Editar linha"]').count(), 0);
    assert.equal(await page.locator('.row-direct-fields, .of-wizard-overlay').count(), 0);
    // Desktop and phone use the same real table cells. Neither opens a modal
    // or needs an OF from the plan, even for literal text and blank values.
    async function edit(field, value, key = 'Enter') {
      const cell = page.locator('#cell-rows-4-' + field);
      await cell.locator('div[x-show="!editing"]').click();
      const input = cell.locator('input[name="new_value"]');
      await input.waitFor({state:'visible'});
      await input.fill(value);
      const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/edit'));
      if (key === 'blur') await page.locator('h1').click();
      else await input.press(key);
      const saved = await response;
      assert.equal(saved.status(), 200);
      await page.waitForFunction(({id, value}) => {
        const el = document.getElementById(id);
        return el?.querySelector('input[name="new_value"]')?.value === value
          && window.Alpine.$data(el).editing === false;
      }, {id: 'cell-rows-4-' + field, value});
      assert.equal(await page.locator('.sheet-bg:visible').count(), 0);
    }
    await edit('of', '266303');
    await edit('modelo', '  MODELO LIVRE + BASE  ', 'blur');
    // Escape cancels locally and must not trigger blur-to-save.
    const model = page.locator('#cell-rows-4-modelo');
    await model.locator('div[x-show="!editing"]').click();
    await model.locator('input[name="new_value"]').fill('CANCELAR');
    const beforeCancel = writes.length;
    await model.locator('input[name="new_value"]').press('Escape');
    await page.locator('h1').click();
    await page.waitForTimeout(100);
    assert.equal(writes.length, beforeCancel);
    await page.setViewportSize({width:390,height:1000});
    await edit('of', '  OF livre  ', 'blur');
    await edit('of', '266303');
    await edit('qtd', '');
    await edit('qtd', 'por contar');
    assert.equal(lookups.length, 0);
    await page.reload({waitUntil:'networkidle'});
    assert.equal(await page.locator('#cell-rows-4-of input[name="new_value"]').inputValue(), '266303');
    assert.equal(await page.locator('#cell-rows-4-modelo input[name="new_value"]').inputValue(), '  MODELO LIVRE + BASE  ');
    assert.equal(await page.locator('#cell-rows-4-qtd input[name="new_value"]').inputValue(), 'por contar');
    for (const width of [390, 1440]) {
      await page.setViewportSize({width,height:1000});
      await page.screenshot({path:`${config.output}/original-inline-edit-${width}.png`,fullPage:true});
    }
    // Validar flushes the last edit and commits once, without business checks.
    const of = page.locator('#cell-rows-4-of');
    await of.locator('div[x-show="!editing"]').click();
    await of.locator('input[name="new_value"]').fill('266303');
    await page.locator('#validate-form button[type="submit"]').click();
    await page.waitForURL(url => !url.pathname.startsWith('/sheet/'));
    assert.equal(writes.filter(request => request.url().endsWith('/validate')).length, 1);
    assert.deepEqual(errors, []);
    assert.deepEqual(dialogs, []);
    console.log('Original: desktop/mobile inline editing, Enter, blur, Escape, literal text, blanks, persistence, no lookup/dialog, single validation passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
