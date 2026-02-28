import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { GifRecorder } from '../utils/recorder.js';
import { waitForServices } from '../utils/wait-for-api.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const API_URL = 'http://localhost:8000/api/v1/';
const DASHBOARD_URL = 'http://localhost:5173';
const OUTPUT_PATH = resolve(__dirname, '../../../assets/memex_dashboard.gif');

async function main() {
  console.log('Recording: Dashboard Overview');
  await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate and wait for data to load before starting capture
    await page.goto(DASHBOARD_URL, { waitUntil: 'networkidle' });
    await page.waitForSelector('text=/Notes/i', { timeout: 15000 });
    await page.waitForSelector('text=/Memories/i', { timeout: 5000 });
    // Dismiss command palette if open
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1000);

    // NOW start capturing — content is ready
    recorder.startCapture();

    // Show the overview for a moment
    await page.waitForTimeout(3000);

    // Slowly scroll down to show chart and recent memories
    await page.mouse.move(640, 400);
    for (let i = 0; i < 4; i++) {
      await page.mouse.wheel(0, 200);
      await page.waitForTimeout(1000);
    }

    // Pause at bottom
    await page.waitForTimeout(2000);

    // Scroll back up smoothly
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await page.waitForTimeout(2000);

    await recorder.stop();
    console.log('Done: Dashboard Overview');
  } catch (err) {
    await recorder.stop().catch(() => {});
    throw err;
  }
}

main().catch((err) => {
  console.error('Recording failed:', err);
  process.exit(1);
});
