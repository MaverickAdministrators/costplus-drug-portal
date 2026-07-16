// End-to-end checks for the "Check Your Drug" lookup and savings receipt.
// 24 assertions per portal file:
//   1. searching a real drug prints the receipt with a real price line
//   2. strength chips exist, switch selection, and update the prefilled amount
//   3. typing into the try-it calculator updates the reward live
//   4. the $50 cap note appears when 10% of savings exceeds $50, hides otherwise
//   5. unknown drugs fall back to the formula-only no-match copy
//   6. over file:// (data fetch blocked) the receipt still prints formula-only, no page errors
//
// Usage:
//   npm test                       -> both portal files
//   npm test -- alkeme.html        -> one file
//
// Requires Chromium in Playwright's cache: npx playwright install chromium
import { chromium } from 'playwright-core';
import { serve } from './serve.mjs';

const pages = process.argv.slice(2).length ? process.argv.slice(2) : ['alkeme.html', 'acme-corporation.html'];

const { server, port } = await serve();
const browser = await chromium.launch();
let totalPass = 0, totalFail = 0;

for (const PAGE of pages) {
  let pass = 0, fail = 0;
  const ok = (cond, msg) => { if (cond) { pass++; console.log(`  ok  ${msg}`); } else { fail++; console.log(`  FAIL ${msg}`); } };

  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));

  await page.goto(`http://127.0.0.1:${port}/${PAGE}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400); // give the data fetch time to settle

  console.log(`\n[http] ${PAGE} — real-drug search`);
  await page.fill('#drugSearch', 'Atorvastatin');
  await page.press('#drugSearch', 'Enter');
  await page.waitForTimeout(750);
  ok(await page.locator('#receiptWrap.printed').count() === 1, 'receipt prints');
  // .r-drug is CSS-uppercased, so compare case-insensitively
  ok((await page.locator('#receipt .r-drug').innerText()).toLowerCase() === 'atorvastatin', 'drug name shown');
  const priceLine = await page.locator('#receipt .r-real strong').count();
  ok(priceLine === 1, 'real price line rendered from data');
  const price1 = priceLine ? await page.locator('#receipt .r-real strong').innerText() : '';
  ok(/^\$\d+\.\d{2}$/.test(price1), `price is money-formatted (${price1})`);

  console.log(`\n[http] strength chips`);
  const chips = page.locator('#receipt .r-strengths .chip');
  const chipCount = await chips.count();
  ok(chipCount > 1, `multiple strengths (${chipCount})`);
  const payBefore = await page.inputValue('#tryPay');
  await chips.nth(1).click();
  await page.waitForTimeout(150);
  ok(await chips.nth(1).getAttribute('aria-pressed') === 'true', 'second chip selected (aria-pressed)');
  ok((await page.locator('#receipt .r-strengths .chip.sel').count()) === 1, 'exactly one chip has .sel');
  const payAfter = await page.inputValue('#tryPay');
  ok(payBefore !== payAfter, `switching strength updates prefill (${payBefore} -> ${payAfter})`);

  console.log(`\n[http] live calculator`);
  await page.fill('#tryPay', '10');
  await page.fill('#tryRetail', '100');
  await page.waitForTimeout(100);
  ok(await page.locator('[data-role="pay"]').innerText() === '$10.00', 'pay line = $10.00');
  ok((await page.locator('[data-role="back"]').innerText()).includes('$10.00'), 'reimburse line mirrors pay');
  ok(await page.locator('[data-role="reward"]').innerText() === '+$9.00', 'reward = +$9.00 (10% of $90 saved)');
  ok(await page.locator('[data-role="paid"]').innerText() === '+$19.00', 'paid-to-you = payment + reward');
  ok(!(await page.locator('[data-role="cap"]').evaluate(el => el.classList.contains('show'))), 'cap note hidden under $50');

  await page.fill('#tryRetail', '800');
  await page.waitForTimeout(100);
  ok(await page.locator('[data-role="reward"]').innerText() === '+$50.00', 'reward capped at +$50.00');
  ok(await page.locator('[data-role="cap"]').evaluate(el => el.classList.contains('show')), 'cap note appears past $50');

  await page.fill('#tryRetail', '');
  await page.waitForTimeout(100);
  ok((await page.locator('[data-role="reward"]').innerText()).includes('10% of savings'), 'reward resets to formula text');

  console.log(`\n[http] page chips + no-match`);
  await page.locator('.chips .chip[data-drug="Metformin"]').click();
  await page.waitForTimeout(750);
  ok((await page.locator('#receipt .r-drug').innerText()).toLowerCase() === 'metformin', 'page chip triggers search');
  await page.fill('#drugSearch', 'Zzzqx');
  await page.press('#drugSearch', 'Enter');
  await page.waitForTimeout(750);
  ok((await page.locator('#receipt .r-real').innerText()).includes("couldn"), 'no-match copy shows');
  ok((await page.locator('#receipt .r-tag').count()) === 1, 'formula still shown on no-match');

  console.log(`\n[file://] fallback (fetch blocked)`);
  const page2 = await ctx.newPage();
  page2.on('pageerror', e => errors.push('file:// ' + String(e)));
  await page2.goto(new URL(`../${PAGE}`, import.meta.url).href);
  await page2.waitForTimeout(600);
  await page2.fill('#drugSearch', 'Atorvastatin');
  await page2.press('#drugSearch', 'Enter');
  await page2.waitForTimeout(750);
  ok(await page2.locator('#receiptWrap.printed').count() === 1, 'receipt still prints');
  ok((await page2.locator('#receipt .r-real').count()) === 0, 'no price line (formula-only fallback)');
  ok((await page2.locator('#receipt .r-tag').count()) === 1, 'formula block present');
  await page2.fill('#tryPay', '20');
  await page2.fill('#tryRetail', '120');
  ok(await page2.locator('[data-role="reward"]').innerText() === '+$10.00', 'calculator works offline');

  ok(errors.length === 0, `no page errors${errors.length ? ': ' + errors.join('; ') : ''}`);

  await ctx.close();
  console.log(`\n${PAGE}: ${pass} passed, ${fail} failed`);
  totalPass += pass; totalFail += fail;
}

await browser.close();
server.close();
if (pages.length > 1) console.log(`\ntotal: ${totalPass} passed, ${totalFail} failed`);
process.exit(totalFail ? 1 : 0);
