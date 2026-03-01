import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { GifRecorder } from '../utils/recorder.js';
import { waitForServices } from '../utils/wait-for-api.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const API_URL = 'http://localhost:8000/api/v1/';
const DASHBOARD_URL = 'http://localhost:5173';
const OUTPUT_PATH = resolve(__dirname, '../../../assets/memex_dashboard_entity_graph.gif');

async function main() {
  console.log('Recording: Entity Graph');
  const urls = await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate directly to entity graph page
    await page.goto(`${urls.dashboardUrl}/entity`, { waitUntil: 'networkidle' });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(500);

    // Wait for the graph to render
    await page.waitForSelector('.react-flow__node', { timeout: 15000 });
    await page.waitForTimeout(2000);

    // Close the filter panel if visible
    const filterCollapse = page.locator('text=/Graph Filters/i');
    if (await filterCollapse.isVisible({ timeout: 2000 }).catch(() => false)) {
      await filterCollapse.click();
      await page.waitForTimeout(500);
    }

    // Start capturing — graph is visible
    recorder.startCapture();

    // *** Linger on the full graph overview ***
    await page.waitForTimeout(4000);

    // Click on a node to show selection/detail
    const nodes = page.locator('.react-flow__node');
    const nodeCount = await nodes.count();
    console.log(`Found ${nodeCount} graph nodes`);

    if (nodeCount > 0) {
      try {
        await nodes.first().click({ force: true, timeout: 5000 });
      } catch {
        // Skip
      }
    }
    // *** Linger on node selection ***
    await page.waitForTimeout(4000);

    // Search for Python entity
    const searchInput = page.locator('input[placeholder*="Search entities"]');
    await searchInput.click();
    await page.waitForTimeout(500);
    await searchInput.pressSequentially('Python', { delay: 100 });
    await page.waitForTimeout(1500);

    // Select the first search result
    const searchResult = page.locator('.entity-search-root [role="option"], .entity-search-root [class*="item"]').first();
    if (await searchResult.isVisible({ timeout: 2000 }).catch(() => false)) {
      await searchResult.click();
    } else {
      await page.keyboard.press('Enter');
    }

    // Wait for graph to re-center
    await page.waitForTimeout(3000);

    // Zoom in using the UI button
    const zoomInBtn = page.getByRole('button', { name: 'Zoom In' });
    for (let i = 0; i < 3; i++) {
      if (await zoomInBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
        await zoomInBtn.click();
      }
      await page.waitForTimeout(400);
    }
    // *** Linger on zoomed view ***
    await page.waitForTimeout(3000);

    // Click on a nearby node
    if (nodeCount > 2) {
      try {
        await nodes.nth(2).click({ force: true, timeout: 5000 });
      } catch {
        // Skip
      }
    }
    // *** Linger on node detail ***
    await page.waitForTimeout(4000);

    // Fit view to show full graph
    const fitBtn = page.getByRole('button', { name: 'Fit View' });
    if (await fitBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
      await fitBtn.click();
    }
    // *** Linger on final overview ***
    await page.waitForTimeout(4000);

    await recorder.stop();
    console.log('Done: Entity Graph');
  } catch (err) {
    await recorder.stop().catch(() => {});
    throw err;
  }
}

main().catch((err) => {
  console.error('Recording failed:', err);
  process.exit(1);
});
