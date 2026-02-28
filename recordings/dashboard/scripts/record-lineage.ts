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
  await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate to lineage page
    await page.goto(`${DASHBOARD_URL}/lineage`, { waitUntil: 'networkidle' });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1500);

    // NOW start capturing — show the empty state briefly
    recorder.startCapture();
    await page.waitForTimeout(1500);

    // Search for Python entity
    const searchInput = page.locator('input[placeholder*="Search entities"]');
    await searchInput.click();
    await page.waitForTimeout(500);
    await searchInput.pressSequentially('Python', { delay: 80 });
    await page.waitForTimeout(2000);

    // Select the first matching result
    const pythonResult = page.locator('text=/Python/').first();
    if (await pythonResult.isVisible({ timeout: 3000 }).catch(() => false)) {
      await pythonResult.click();
    }

    // Wait for the lineage graph to render
    await page.waitForSelector('.react-flow__node', { timeout: 15000 });
    await page.waitForTimeout(3000);

    // Slowly scroll/pan to show the full graph
    await page.mouse.move(640, 400);
    for (let i = 0; i < 3; i++) {
      await page.mouse.wheel(0, 150);
      await page.waitForTimeout(1000);
    }

    // Hover over a node to show details
    const nodes = page.locator('.react-flow__node');
    const nodeCount = await nodes.count();
    console.log(`Found ${nodeCount} lineage nodes`);

    if (nodeCount > 2) {
      try {
        await nodes.nth(2).hover({ force: true, timeout: 5000 });
        await page.waitForTimeout(2000);
      } catch {
        // Skip
      }
    }

    // Scroll back
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await page.waitForTimeout(2000);

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
