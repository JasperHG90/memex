import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { GifRecorder } from '../utils/recorder.js';
import { waitForServices } from '../utils/wait-for-api.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const API_URL = 'http://localhost:8000/api/v1/';
const DASHBOARD_URL = 'http://localhost:5173';
const OUTPUT_PATH = resolve(__dirname, '../../../assets/memex_dashboard_memory_search.gif');

async function main() {
  console.log('Recording: Memory Search');
  await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate to search page and wait for content
    await page.goto(`${DASHBOARD_URL}/search`, { waitUntil: 'networkidle' });
    await page.waitForSelector('text=/Memory Search/i', { timeout: 10000 });
    // Dismiss any dialogs
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1000);

    // NOW start capturing — page is ready
    recorder.startCapture();

    // Show the empty search page briefly
    await page.waitForTimeout(1500);

    // Click the search input
    const searchInput = page.locator('input[placeholder*="Search memories"]');
    await searchInput.click();
    await page.waitForTimeout(500);

    // Type a search query with natural typing speed
    await searchInput.pressSequentially('How does Python handle memory management?', {
      delay: 60,
    });
    await page.waitForTimeout(1000);

    // Click the Search button
    const searchButton = page.locator('button:has-text("Search")');
    await searchButton.click();

    // Wait for results to appear
    await page.waitForTimeout(5000);

    // Scroll down through results
    await page.mouse.move(640, 400);
    for (let i = 0; i < 3; i++) {
      await page.mouse.wheel(0, 200);
      await page.waitForTimeout(1000);
    }

    // Scroll back up
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await page.waitForTimeout(2000);

    await recorder.stop();
    console.log('Done: Memory Search');
  } catch (err) {
    await recorder.stop().catch(() => {});
    throw err;
  }
}

main().catch((err) => {
  console.error('Recording failed:', err);
  process.exit(1);
});
