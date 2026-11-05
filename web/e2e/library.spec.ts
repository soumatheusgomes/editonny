import { expect, test } from '@playwright/test';

test('catalog search and category filter narrow the scenes', async ({ page }) => {
  await page.goto('/en/catalog');
  await expect(page.getByText('145 scenes')).toBeVisible();
  await page.getByLabel('Search scenes').fill('comparison');
  await expect(page).toHaveURL(/q=comparison/);
  const count = Number((await page.getByText(/^\d+ scenes?$/).textContent())!.split(' ')[0]);
  expect(count).toBeGreaterThan(0);
  expect(count).toBeLessThan(145);
  await page.getByLabel('Search scenes').fill('');
  await page.getByLabel('Category').selectOption('Transitions');
  await expect(page.getByText('10 scenes')).toBeVisible();
  await page.getByLabel('Search scenes').fill('zzzz-nothing');
  await expect(page.getByText('No scenes found. Try another word or choose all categories.')).toBeVisible();
});

test('a scene opens from its card and from a deep link, and Escape closes it', async ({ page }) => {
  await page.goto('/pt-br/catalogo?scene=revenue-counter');
  const dialog = page.getByRole('dialog', { name: 'Dinheiro subindo' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Quando a fala cita faturamento');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(page).not.toHaveURL(/scene=/);
  await page.getByRole('button', { name: /Três passos empilhados/ }).click();
  await expect(page.getByRole('dialog', { name: 'Três passos empilhados' })).toBeVisible();
  await expect(page).toHaveURL(/scene=three-steps/);
});

test('an unknown scene or category in the URL is ignored', async ({ page }) => {
  await page.goto('/en/catalog?scene=nope&category=Invalid');
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByText('145 scenes')).toBeVisible();
});

test('"Use in my request" carries the scene to the create page', async ({ page }) => {
  await page.goto('/es/catalogo?scene=revenue-counter');
  await page.getByRole('dialog').getByRole('link', { name: 'Usar en mi pedido' }).click();
  await expect(page).toHaveURL(/\/es\/crear$/);
  await expect(page.getByText('Escena: Dinero subiendo')).toBeVisible();
  await expect(page.getByLabel('Pedido de edición generado')).toContainText('(revenue-counter)');
});

test('examples search, open, and customize the brief', async ({ page }) => {
  await page.goto('/en/examples');
  await expect(page.getByText('54 requests')).toBeVisible();
  await page.getByLabel('Search examples').fill('lesson');
  await page.getByRole('button', { name: /A lesson with a clear opening/ }).click();
  const dialog = page.getByRole('dialog', { name: 'A lesson with a clear opening' });
  await expect(dialog).toContainText('Identify sections that must remain complete.');
  await expect(dialog.getByLabel('Request preview')).toContainText('Goal: Edit the lesson with a short opening');
  await dialog.getByRole('link', { name: 'Customize request' }).click();
  await expect(page).toHaveURL(/\/en\/create$/);
  await expect(page.getByLabel('What would you like to create?')).toHaveValue(/Edit the lesson with a short opening/);
});

test('copying from a scene dialog uses the saved agent', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/en/create');
  await page.getByRole('radio', { name: /Claude Code/ }).click();
  await page.goto('/en/catalog?scene=revenue-counter');
  await page.getByRole('dialog').getByRole('button', { name: 'Copy request' }).click();
  await expect(page.getByRole('status')).toContainText('Request copied.');
  // The Windows clipboard stores CRLF; the copied request itself uses LF.
  const clipboard = (await page.evaluate(() => navigator.clipboard.readText())).replaceAll('\r\n', '\n');
  expect(clipboard.startsWith('/editonny\n')).toBe(true);
  expect(clipboard).toContain('Catalog reference: Revenue counter (revenue-counter).');
});

test('without clipboard access the scene dialog selects the request itself', async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) } }); });
  await page.goto('/en/catalog?scene=revenue-counter');
  await page.getByRole('dialog').getByRole('button', { name: 'Copy request' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The text is selected' })).toBeVisible();
  const selected = await page.evaluate(() => getSelection()?.toString() ?? '');
  expect(selected.startsWith('$editonny')).toBe(true);
  expect(selected).toContain('(revenue-counter)');
});

test('leaving a search through same-page navigation clears it, and Back restores it', async ({ page }) => {
  await page.goto('/en/catalog?q=comparison');
  await expect(page.getByLabel('Search scenes')).toHaveValue('comparison');
  await page.getByRole('contentinfo').getByRole('link', { name: 'Motion scenes', exact: true }).click();
  await expect(page).toHaveURL(/\/en\/catalog$/);
  await expect(page.getByLabel('Search scenes')).toHaveValue('');
  await expect(page.getByText('145 scenes')).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel('Search scenes')).toHaveValue('comparison');
});

test('typing a search keeps every keystroke while the URL catches up', async ({ page }) => {
  await page.goto('/en/catalog');
  await page.getByLabel('Search scenes').pressSequentially('transition glow', { delay: 20 });
  await expect(page).toHaveURL(/q=transition\+glow|q=transition%20glow/);
  await expect(page.getByLabel('Search scenes')).toHaveValue('transition glow');
});

test('a tall dialog scrolls so its actions stay reachable on short screens', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 844, height: 390 }, hasTouch: true });
  const page = await context.newPage();
  await page.goto('/pt-br/catalogo?scene=revenue-counter');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  // Scroll the way a visitor does (wheel over the dialog); programmatic scrolling also moves overflow: hidden boxes.
  const box = (await dialog.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 2000);
  await expect(dialog.getByRole('link', { name: 'Usar no meu pedido' })).toBeInViewport();
  await context.close();
});

test('navigating away mid-search drops the pending search', async ({ page }) => {
  await page.goto('/en/catalog?q=comparison');
  await page.getByLabel('Search scenes').fill('new search');
  await page.getByRole('contentinfo').getByRole('link', { name: 'Motion scenes', exact: true }).click();
  await page.waitForTimeout(600);
  await expect(page).toHaveURL(/\/en\/catalog$/);
  await expect(page.getByLabel('Search scenes')).toHaveValue('');
});

test('cards work before the interactive grid loads', async ({ page }) => {
  // Without JavaScript only the server-rendered grid exists; its cards must still open the item.
  await page.context().route('**/_next/static/chunks/**', route => route.abort());
  await page.goto('/en/examples');
  await expect(page.getByRole('link', { name: /A lesson with a clear opening/ })).toHaveAttribute('href', '/en/examples?example=lesson');
  await page.goto('/en/catalog');
  await expect(page.getByRole('link', { name: /Revenue counter/ })).toHaveAttribute('href', '/en/catalog?scene=revenue-counter');
});

test('copy feedback inside a dialog is announced from the dialog', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/en/examples?example=lesson');
  await page.getByRole('dialog').getByRole('button', { name: 'Copy request' }).click();
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('Request copied.');
});

test('scene previews play on keyboard focus and chat motifs are visible at rest', async ({ page, isMobile }) => {
  test.skip(isMobile, 'keyboard focus');
  await page.goto('/en/catalog');
  // Wait for the interactive grid (buttons) to replace the server-rendered one (links).
  const card = page.getByRole('button', { name: /Revenue counter/ });
  await expect(card).toBeVisible();
  const chat = page.locator('.ds-motif--chat').first();
  await chat.scrollIntoViewIfNeeded();
  expect(await chat.locator('span').first().evaluate(node => getComputedStyle(node).opacity)).toBe('1');
  await card.focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  expect(await card.locator('.ds-motif span').first().evaluate(node => getComputedStyle(node).animationPlayState)).toBe('running');
});

test('the server-rendered catalog speaks the page language', async ({ page }) => {
  await page.context().route('**/_next/static/chunks/**', route => route.abort());
  await page.goto('/pt-br/catalogo');
  await expect(page.getByText('▶ PRÉVIA').first()).toBeAttached();
  await expect(page.getByText('HOVER TO PLAY')).toHaveCount(0);
});
