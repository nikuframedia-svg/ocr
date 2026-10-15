const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({executablePath: config.chromium, headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}, extraHTTPHeaders: {'X-OCR-Probe': '1'}});
  const errors = [], writes = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/validate')) writes.push(request.url()); });
  await page.goto(config.url);
  await page.waitForFunction(() => Boolean(window.Alpine));
  const button = page.locator('button[title="Editar linha"]').nth(4);
  await button.click();
  const field = page.locator('.row-direct-fields input[x-model*="of"]');
  await field.waitFor({state: 'visible'});
  await field.fill('266303');
  await page.getByRole('button', {name: 'Guardar alterações', exact: true}).click();
  await page.waitForLoadState('networkidle');
  await page.locator('button[title="Editar linha"]').nth(4).click();
  assert.equal(await field.inputValue(), '266303');
  await page.getByRole('button', {name: 'Cancelar', exact: true}).click();
  for (const width of [390, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.screenshot({path: `${config.output}/original-free-edit-${width}.png`, fullPage: true});
  }
  await page.locator('#validate-form button[type="submit"]').click();
  await page.waitForURL(url => !url.pathname.startsWith('/sheet/'));
  await page.waitForTimeout(200);
  assert.equal(writes.length, 1);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Original browser: exact OF, optional lookup, desktop/mobile, single validation passed.');
})().catch(error => { console.error(error); process.exit(1); });
