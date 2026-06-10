import { test, expect } from '@playwright/test';

const BASE = process.env.TM_TEST_BASE || 'http://127.0.0.1:8711';
const KEY = process.env.TM_TEST_API_KEY;
const CONTAINER = process.env.TM_TEST_CONTAINER;

if (!KEY) {
  throw new Error('TM_TEST_API_KEY environment variable is required');
}

test.describe('Dashboard E2E Smoke Test', () => {
  
  test('login, overview, memory, containers, jobs, and settings check', async ({ page }) => {
    // Set 60 seconds timeout for this test
    test.setTimeout(60000);

    // 1. Visit root page, wait for redirect to /login
    console.log(`Navigating to ${BASE}/admin/ui/`);
    await page.goto(`${BASE}/admin/ui/`);
    await page.waitForURL('**/login');
    console.log('Redirected to login page.');

    // 2. Perform login
    await page.fill('input[type="password"]', KEY);
    await page.click('button[type="submit"]');
    
    // 3. Should redirect to overview
    await page.waitForURL('**/overview');
    console.log('Successfully logged in. On Overview page.');

    // 4. Verify Overview cards are rendered (4 KPI cards in the first grid)
    const cards = page.locator('.grid').first().locator('.panel');
    await expect(cards).toHaveCount(4);
    
    // Verify profiles list has items
    const profileItems = page.locator('section:has-text("Profiles") ul li, section:has-text("配置") ul li');
    await expect(profileItems.first()).toBeVisible({ timeout: 15000 });
    const profileCount = await profileItems.count();
    console.log(`Found ${profileCount} model profiles.`);
    expect(profileCount).toBeGreaterThan(0);

    // 5. Navigate to Memory Page using sidebar navigation (SPA)
    console.log('Navigating to Memory page via sidebar...');
    await page.click('a[href$="/memory"]');
    // Memory auto-appends ?container=<largest> on mount (replace navigation,
    // predates a11cf73), so a bare `**/memory` glob races — accept the query.
    await page.waitForURL(/\/memory(\?|$)/);
    
    // Select the correct container if passed
    if (CONTAINER) {
      console.log(`Selecting container: ${CONTAINER}`);
      const select = page.locator('select.w-full');
      await expect(select).toBeVisible();
      // Select option matching CONTAINER
      await select.selectOption({ value: CONTAINER });
      await page.waitForTimeout(1000); // Wait for transition
    }

    // Verify at least one real memory card is displayed (ignoring skeletons without fade-in)
    const memoryCards = page.locator('.grid > .panel.fade-in');
    await expect(memoryCards.first()).toBeVisible({ timeout: 15000 });
    
    // Verify details of the memory card contains the smoke test query text
    const targetText = 'kuiper-belt-flag-2026-pizza';
    await expect(memoryCards.first()).toContainText(targetText);
    console.log(`Successfully verified memory card contains "${targetText}" in default list.`);

    // Perform an E2E search assertion
    console.log('Running search check...');
    const searchInput = page.locator('input.flex-1');
    await searchInput.fill(targetText);
    await searchInput.press('Enter');
    
    // Verify that the search returns matching results (wait for new cards to render)
    await expect(memoryCards.first()).toBeVisible({ timeout: 15000 });
    await expect(memoryCards.first()).toContainText(targetText);
    console.log('E2E search assertion passed.');

    // 6. Navigate to Containers list page via sidebar (SPA)
    console.log('Navigating to Containers page via sidebar...');
    await page.click('a[href$="/containers"]');
    await page.waitForURL('**/containers');
    
    const containerTableRows = page.locator('table tbody tr');
    await expect(containerTableRows.first()).toBeVisible({ timeout: 10000 });
    
    if (CONTAINER) {
      const containerRow = page.locator(`table tbody tr:has-text("${CONTAINER}")`);
      await expect(containerRow).toBeVisible();
      console.log(`Verified container ${CONTAINER} is visible in the container list.`);
    }

    // 7. Navigate to Jobs page via sidebar (SPA)
    console.log('Navigating to Jobs page via sidebar...');
    await page.click('a[href$="/jobs"]');
    await page.waitForURL('**/jobs');
    
    const jobRows = page.locator('table tbody tr');
    await expect(jobRows.first()).toBeVisible({ timeout: 10000 });
    console.log('Verified jobs table contains items.');

    // 8. Navigate to Settings page via sidebar (SPA)
    console.log('Navigating to Settings page via sidebar...');
    await page.click('a[href$="/settings"]');
    await page.waitForURL('**/settings');
    
    const versionLabel = page.locator('text=/v\\d+\\./');
    await expect(versionLabel.first()).toBeVisible({ timeout: 10000 });
    const versionText = await versionLabel.first().innerText();
    console.log(`Verified dashboard version: ${versionText}`);
  });
});
