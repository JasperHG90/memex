import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { GifRecorder } from '../utils/recorder.js';
import { waitForServices } from '../utils/wait-for-api.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const API_URL = 'http://localhost:8000/api/v1/';
const DASHBOARD_URL = 'http://localhost:5173';
const OUTPUT_PATH = resolve(__dirname, '../../../assets/memex_dashboard_lineage.gif');

async function main() {
  console.log('Recording: Lineage');
  const urls = await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate to lineage page and dismiss dialogs
    await page.goto(`${urls.dashboardUrl}/lineage`, { waitUntil: 'networkidle' });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1500);

    // Start capturing — show empty state briefly
    recorder.startCapture();
    await page.waitForTimeout(1500);

    // Type search for Python entity
    const searchInput = page.locator('input[placeholder*="Search entities"]');
    await searchInput.click();
    await page.waitForTimeout(500);
    await searchInput.pressSequentially('Python', { delay: 100 });
    await page.waitForTimeout(1500);

    // Select the Python result from dropdown
    const pythonBtn = page.locator('button:has-text("Python")').first();
    if (await pythonBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await pythonBtn.click();
    }

    // Wait for lineage graph to fully render
    await page.waitForSelector('.react-flow__node', { timeout: 15000 });
    await page.waitForTimeout(2000);

    // Click "Fit View" to frame the graph nicely
    const fitBtn = page.getByRole('button', { name: 'Fit View' });
    if (await fitBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await fitBtn.click();
    }
    // *** Linger on the full graph overview ***
    await page.waitForTimeout(4000);

    // Click on a node to open the detail panel
    const nodes = page.locator('.react-flow__node');
    const nodeCount = await nodes.count();
    console.log(`Found ${nodeCount} lineage nodes`);

    if (nodeCount > 1) {
      try {
        await nodes.nth(1).click({ force: true, timeout: 5000 });
      } catch {
        // Skip
      }
    }
    // *** Linger on the node highlight + detail panel ***
    await page.waitForTimeout(4000);

    // Zoom in using the UI button (3 clicks)
    const zoomInBtn = page.getByRole('button', { name: 'Zoom In' });
    for (let i = 0; i < 4; i++) {
      if (await zoomInBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
        await zoomInBtn.click();
      }
      await page.waitForTimeout(400);
    }
    // *** Linger on zoomed view ***
    await page.waitForTimeout(3000);

    // Click a different node while zoomed in
    if (nodeCount > 3) {
      try {
        await nodes.nth(3).click({ force: true, timeout: 5000 });
      } catch {
        // Skip
      }
    }
    // *** Linger on zoomed detail ***
    await page.waitForTimeout(3000);

    // Zoom back out with Fit View
    if (await fitBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
      await fitBtn.click();
    }
    // *** Linger on final overview ***
    await page.waitForTimeout(4000);

    await recorder.stop();
    console.log('Done: Lineage');
  } catch (err) {
    await recorder.stop().catch(() => {});
    throw err;
  }
}

main().catch((err) => {
  console.error('Recording failed:', err);
  process.exit(1);
});
