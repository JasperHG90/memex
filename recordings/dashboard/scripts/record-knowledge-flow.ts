import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { GifRecorder } from '../utils/recorder.js';
import { waitForServices } from '../utils/wait-for-api.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const API_URL = 'http://localhost:8000/api/v1/';
const DASHBOARD_URL = 'http://localhost:5173';
const OUTPUT_PATH = resolve(__dirname, '../../../assets/memex_dashboard_knowledge_flow.gif');

async function main() {
  console.log('Recording: Knowledge Flow');
  await waitForServices(API_URL, DASHBOARD_URL);

  const recorder = new GifRecorder(OUTPUT_PATH, { fps: 6 });
  const page = await recorder.start();

  try {
    // Navigate to knowledge flow page and wait for content
    await page.goto(`${DASHBOARD_URL}/knowledge-flow`, { waitUntil: 'networkidle' });
    await page.waitForSelector('text=/Knowledge Flow/i', { timeout: 10000 });
    // Dismiss any dialogs
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1500);

    // NOW start capturing — content is ready
    recorder.startCapture();

    // Show the pipeline overview
    await page.waitForTimeout(3000);

    // Slowly scroll down to show the activity columns
    await page.mouse.move(640, 400);
    for (let i = 0; i < 4; i++) {
      await page.mouse.wheel(0, 200);
      await page.waitForTimeout(1200);
    }

    // Pause at bottom
    await page.waitForTimeout(2000);

    // Scroll back up
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await page.waitForTimeout(2000);

    await recorder.stop();
    console.log('Done: Knowledge Flow');
  } catch (err) {
    await recorder.stop().catch(() => {});
    throw err;
  }
}

main().catch((err) => {
  console.error('Recording failed:', err);
  process.exit(1);
});
