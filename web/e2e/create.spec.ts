import { expect, test } from '@playwright/test';

const preview = (page: import('@playwright/test').Page) => page.getByLabel('Generated editing request');

test('the brief builds the request live and survives a reload', async ({ page }) => {
  await page.goto('/en/create');
  await page.getByLabel('What would you like to create?').fill('Explain a feature clearly');
  await page.getByLabel('Video folder on your computer').fill('/tmp/Raw videos café');
  await page.getByRole('radio', { name: /4:5/ }).click();
  await page.getByRole('radio', { name: /Claude Code/ }).click();
  await page.getByLabel('Pace').selectOption('dynamic');
  const text = preview(page);
  await expect(text).toContainText('/editonny');
  await expect(text).toContainText('Edit the videos in: /tmp/Raw videos café');
  await expect(text).toContainText('Goal: Explain a feature clearly.');
  await expect(text).toContainText('Format: 4:5 (portrait feed).');
  await expect(text).toContainText('Pace: dynamic, with precise cuts that preserve complete words.');
  await page.reload();
  await expect(page.getByLabel('Video folder on your computer')).toHaveValue('/tmp/Raw videos café');
  await expect(page.getByRole('radio', { name: /Claude Code/ })).toHaveAttribute('aria-checked', 'true');
  await expect(preview(page)).toContainText('/tmp/Raw videos café');
});

test('start over restores the defaults', async ({ page }) => {
  await page.goto('/en/create');
  await page.getByLabel('Video folder on your computer').fill('/Videos/Ana');
  await page.getByRole('button', { name: 'Start over' }).click();
  await expect(page.getByLabel('Video folder on your computer')).toHaveValue('');
  await expect(preview(page)).toContainText('[enter the video folder]');
  await expect(page.getByRole('status')).toContainText('Brief cleared.');
});

test('an example link applies the example and cleans the URL', async ({ page }) => {
  await page.goto('/en/create?example=lesson');
  await expect(page).toHaveURL(/\/en\/create$/);
  await expect(page.getByLabel('What would you like to create?')).toHaveValue(/Edit the lesson with a short opening/);
  await expect(page.getByRole('status')).toContainText('A lesson with a clear opening');
});

test('a scene link adds the scene to the request', async ({ page }) => {
  await page.goto('/pt-br/criar?scene=revenue-counter');
  await expect(page).toHaveURL(/\/pt-br\/criar$/);
  await expect(page.getByText('Cena: Dinheiro subindo')).toBeVisible();
  await expect(page.getByLabel('Pedido de edição gerado')).toContainText('Referência do catálogo: Dinheiro subindo (revenue-counter).');
  await page.getByRole('button', { name: 'Remover a cena Dinheiro subindo' }).click();
  await expect(page.getByLabel('Pedido de edição gerado')).not.toContainText('revenue-counter');
});

test('unknown or hostile ids change nothing and are removed from the URL', async ({ page }) => {
  await page.goto('/en/create?scene=nope&example=../x');
  await expect(page).toHaveURL(/\/en\/create$/);
  await expect(preview(page)).toContainText('[enter the video folder]');
  await expect(preview(page)).not.toContainText('Catalog reference');
});

test('turning visuals off drops the selected scene', async ({ page }) => {
  await page.goto('/en/create?scene=revenue-counter');
  await expect(preview(page)).toContainText('revenue-counter');
  await page.getByLabel('Visual scenes and effects').selectOption('none');
  await expect(preview(page)).not.toContainText('revenue-counter');
});

test('copy puts the request on the clipboard', async ({ page, context, browserName }) => {
  test.skip(browserName !== 'chromium', 'clipboard permissions are Chromium-only');
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/es/crear');
  await page.getByRole('button', { name: 'Copiar pedido' }).first().click();
  await expect(page.getByRole('status')).toContainText('Pedido copiado.');
  // The Windows clipboard stores CRLF; the copied request itself uses LF.
  const clipboard = (await page.evaluate(() => navigator.clipboard.readText())).replaceAll('\r\n', '\n');
  expect(clipboard).toBe(await page.getByLabel('Pedido de edición generado').textContent());
  expect(clipboard.startsWith('$editonny\n\nEdita los videos en:')).toBe(true);
});

test('copy without clipboard access selects the text and explains how to copy', async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) } }); });
  await page.goto('/en/create');
  await page.getByRole('button', { name: 'Copy request' }).first().click();
  // Next.js also renders a route announcer with role=alert.
  await expect(page.getByRole('alert').filter({ hasText: 'The text is selected' })).toBeVisible();
  expect(await page.evaluate(() => getSelection()?.toString().startsWith('$editonny'))).toBe(true);
});

test('phones get a sticky copy bar until the request card is on screen', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'phone layout');
  await page.goto('/en/create');
  // CSS locator: the bar leaves the accessibility tree while hidden.
  const bar = page.locator('[data-visible] button');
  await expect(bar).toBeInViewport();
  await preview(page).scrollIntoViewIfNeeded();
  await expect(bar).toBeHidden();
});

test('the same example can be applied again after editing the brief', async ({ page }) => {
  // Client-side navigation keeps the Create page mounted, which is where a stale guard would hide the second request.
  await page.goto('/en/create?example=lesson');
  const goal = page.getByLabel('What would you like to create?');
  await expect(goal).toHaveValue(/Edit the lesson with a short opening/);
  for (const edit of ['Something else', 'Changed again']) {
    await goal.fill(edit);
    await page.getByRole('contentinfo').getByRole('link', { name: 'Requests', exact: true }).click();
    await page.getByRole('button', { name: /A lesson with a clear opening/ }).click();
    await page.getByRole('dialog').getByRole('link', { name: 'Customize request' }).click();
    await expect(page).toHaveURL(/\/en\/create$/);
    await expect(goal).toHaveValue(/Edit the lesson with a short opening/);
  }
});

test('each choice group is a single Tab stop on its selected option', async ({ page, isMobile }) => {
  test.skip(isMobile, 'keyboard flow');
  await page.goto('/en/create');
  await page.getByLabel('Video folder on your computer').focus();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('radio', { name: /Original/ })).toBeFocused();
  await expect(page.getByRole('radio', { name: /Original/ })).toHaveAttribute('aria-checked', 'true');
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Pace')).toBeFocused();
});
