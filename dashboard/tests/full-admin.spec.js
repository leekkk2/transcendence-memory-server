import { test, expect } from '@playwright/test';
const base = process.env.TM_ADMIN_TEST_BASE || 'http://127.0.0.1:18712';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('tm-admin-lang', 'en'));
  await page.goto(base + '/admin/ui/login');
  await page.locator('input[type=password]').fill('tm-local-browser-fixture');
  await page.locator('button[type=submit]').click();
  await expect(page).toHaveURL(/overview/);
});

test('desktop full status, queued text/PDF/image, document details and graph', async ({ page }) => {
  await expect(page.getByTestId('full-status')).toContainText('Full ready');
  await page.getByRole('link', { name: 'Documents', exact: true }).first().click();
  await expect(page.getByTestId('document-container')).toHaveValue('admin-ui-test');
  await page.getByTestId('ingest-text').fill('Aurora is maintained by Atlas Lab. Browser text submission.');
  await page.getByTestId('ingest-submit').click();
  await expect(page.getByTestId('ingest-receipt')).toContainText('is queued');
  const first = await page.getByTestId('ingest-receipt').innerText();
  await page.getByRole('button', { name: 'PDF / image / file', exact: true }).click();
  for (const [name, mimeType, buffer] of [
    ['sample.pdf', 'application/pdf', Buffer.from('%PDF-1.4 browser fixture')],
    ['sample.png', 'image/png', Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xoAAAAASUVORK5CYII=', 'base64')],
  ]) {
    await page.getByTestId('ingest-file').setInputFiles({ name, mimeType, buffer });
    await page.getByTestId('ingest-submit').click();
    await expect(page.getByTestId('ingest-receipt')).toContainText('is queued');
    await expect(page.getByTestId('ingest-file')).toHaveValue('');
  }
  expect(await page.getByTestId('ingest-receipt').innerText()).not.toEqual(first);
  await page.getByRole('row').filter({ hasText: 'manual.pdf' }).getByRole('button', { name: 'View details' }).click();
  await expect(page.getByTestId('document-text')).toContainText('Aurora telescope');
  await page.getByRole('combobox', { name: 'Processing status' }).selectOption('failed');
  await expect(page.getByRole('row').filter({ hasText: 'broken.pdf' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'manual.pdf' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Jobs', exact: true }).first().click();
  const pending = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Cancel job', exact: true }) }).first();
  await pending.getByRole('button', { name: 'Cancel job', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm cancellation', exact: true }).click();
  await expect(page.getByRole('cell', { name: 'cancelled', exact: true }).first()).toBeVisible();
  await page.goto(base + '/admin/ui/documents?container=admin-ui-test');
  await page.locator('main').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: '.local/screens/documents-desktop.png', fullPage: true });
  const jobsResponse = await page.request.get(base + '/jobs');
  const jobs = await jobsResponse.json();
  await page.route('**/jobs**', async route => route.fulfill({ json: { ...jobs, jobs: [...jobs.jobs, { id: 999999, op: 'ingest-document-text', container: 'admin-ui-test', status: 'done', last_error: 'successful output must not be an error' }] } }));
  await page.goto(base + '/admin/ui/documents?container=admin-ui-test');
  await expect(page.getByText('successful output must not be an error')).toHaveCount(0);
  await page.getByRole('link', { name: 'Knowledge graph', exact: true }).first().click();
  await expect(page.getByTestId('graph-canvas').locator('canvas').first()).toBeVisible();
  await page.getByRole('button', { name: 'Aurora telescope' }).click();
  await expect(page.getByText('An optical telescope')).toBeVisible();
  // Deterministic response exercises rendering and JSON/CSRF transport. Production acceptance uses real queries.
  let payload;
  await page.route('**/query', async route => {
    payload = route.request().postDataJSON();
    expect(route.request().headers()['x-requested-with']).toBe('XMLHttpRequest');
    await route.fulfill({ json: { status: 'ok', answer: 'Atlas Lab maintains Aurora. <script>must stay text</script>', citations: ['manual.pdf'], mode: 'hybrid', container: 'admin-ui-test' } });
  });
  await page.getByTestId('graph-question').fill('Who maintains Aurora?');
  await page.getByTestId('graph-submit').click();
  await expect(page.getByTestId('query-result')).toContainText('Atlas Lab maintains Aurora');
  expect(payload.container).toBe('admin-ui-test');
  expect(payload.mode).toBe('hybrid');
  await expect(page.getByTestId('query-result').locator('script')).toHaveCount(0);
  await page.screenshot({ path: '.local/screens/graph-desktop.png', fullPage: true });
});

test('mobile navigation, Chinese labels, unavailable state, upload error without retry', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem('tm-admin-lang', 'zh-CN'));
  await page.goto(base + '/admin/ui/documents?container=admin-ui-test');
  await expect(page.getByRole('heading', { name: '文档入库', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByTestId('ingest-text').fill('A browser transport error example.');
  let calls = 0;
  await page.route('**/documents/text', async route => {
    calls++;
    await route.fulfill({ status: 503, json: { detail: 'Parser temporarily unavailable' } });
  });
  await page.getByTestId('ingest-submit').click();
  await expect(page.getByRole('alert')).toContainText('响应未确认');
  expect(calls).toBe(1);
  await page.screenshot({ path: '.local/screens/documents-mobile.png', fullPage: true });
  await page.route('**/health', async route => {
    const response = await route.fetch();
    const json = await response.json();
    json.runtime_ready.documents_file = false;
    json.build_flavor = 'lite';
    await route.fulfill({ json });
  });
  await page.goto(base + '/admin/ui/overview');
  await expect(page.getByTestId('full-status')).toContainText('请检查状态');
  await page.goto(base + '/admin/ui/documents?container=admin-ui-test');
  await page.getByRole('button', { name: 'PDF / 图片 / 文件', exact: true }).click();
  await page.getByTestId('ingest-file').setInputFiles({ name: 'test.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF') });
  await expect(page.getByTestId('ingest-submit')).toBeDisabled();
});
