import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { execSync } from 'child_process';
import { mkdtempSync, rmSync, existsSync, mkdirSync, readdirSync } from 'fs';
import { join, dirname } from 'path';
import { tmpdir } from 'os';

interface RecorderOptions {
  width?: number;
  height?: number;
  fps?: number;
}

/**
 * Records browser interactions as a GIF by taking periodic screenshots
 * and stitching them with ffmpeg. More reliable than video recording
 * in headless/containerized environments.
 */
export class GifRecorder {
  private outputPath: string;
  private width: number;
  private height: number;
  private fps: number;
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  private page: Page | null = null;
  private tmpDir: string | null = null;
  private frameCount: number = 0;
  private capturing: boolean = false;
  private capturePromise: Promise<void> | null = null;

  constructor(outputPath: string, options?: RecorderOptions) {
    this.outputPath = outputPath;
    this.width = options?.width ?? 1280;
    this.height = options?.height ?? 720;
    this.fps = options?.fps ?? 8;
  }

  async start(): Promise<Page> {
    this.tmpDir = mkdtempSync(join(tmpdir(), 'memex-recording-'));
    this.frameCount = 0;

    this.browser = await chromium.launch({
      headless: true,
      chromiumSandbox: false,
    });
    this.context = await this.browser.newContext({
      viewport: { width: this.width, height: this.height },
      colorScheme: 'dark',
    });

    // Dismiss onboarding modal and any first-visit dialogs
    await this.context.addInitScript(() => {
      localStorage.setItem('memex_onboarding_completed', 'true');
    });

    this.page = await this.context.newPage();

    return this.page;
  }

  /** Begin capturing screenshots. Call after the page content is ready. */
  startCapture(): void {
    if (!this.page || !this.tmpDir) {
      throw new Error('Recorder not started');
    }

    this.capturing = true;
    const intervalMs = Math.round(1000 / this.fps);

    // Sequential capture loop: wait for each screenshot before scheduling the next.
    // This prevents overlapping screenshot calls that cause silent failures.
    this.capturePromise = (async () => {
      while (this.capturing && this.page && this.tmpDir) {
        const start = Date.now();
        try {
          const framePath = join(this.tmpDir, `frame-${String(this.frameCount).padStart(5, '0')}.png`);
          await this.page.screenshot({ path: framePath });
          this.frameCount++;
        } catch {
          // Page might be navigating, skip frame
        }
        // Sleep for the remainder of the interval (or skip if screenshot took too long)
        const elapsed = Date.now() - start;
        const sleepMs = Math.max(0, intervalMs - elapsed);
        if (sleepMs > 0 && this.capturing) {
          await new Promise((r) => setTimeout(r, sleepMs));
        }
      }
    })();
  }

  async stop(): Promise<void> {
    if (!this.page || !this.browser || !this.tmpDir) {
      throw new Error('Recorder not started');
    }

    // Stop the capture loop and wait for it to finish
    this.capturing = false;
    if (this.capturePromise) {
      await this.capturePromise;
      this.capturePromise = null;
    }

    // Take one final frame
    try {
      const framePath = join(this.tmpDir, `frame-${String(this.frameCount).padStart(5, '0')}.png`);
      await this.page.screenshot({ path: framePath });
      this.frameCount++;
    } catch {
      // ignore
    }

    await this.page.close();
    await this.browser.close();

    // Count actual files on disk (frameCount may over-count if screenshots failed)
    const actualFrames = readdirSync(this.tmpDir).filter((f) => f.startsWith('frame-') && f.endsWith('.png')).length;

    if (actualFrames === 0) {
      rmSync(this.tmpDir, { recursive: true, force: true });
      throw new Error('No frames captured');
    }

    const outputDir = dirname(this.outputPath);
    if (!existsSync(outputDir)) {
      mkdirSync(outputDir, { recursive: true });
    }

    const palettePath = join(this.tmpDir, 'palette.png');
    const inputPattern = join(this.tmpDir, 'frame-%05d.png');

    try {
      // Pass 1: Generate palette from all frames
      execSync(
        `ffmpeg -y -framerate ${this.fps} -i "${inputPattern}" -vf "palettegen=stats_mode=full" "${palettePath}"`,
        { stdio: 'pipe' }
      );

      // Pass 2: Generate GIF using palette
      execSync(
        `ffmpeg -y -framerate ${this.fps} -i "${inputPattern}" -i "${palettePath}" -lavfi "[0:v][1:v] paletteuse=dither=bayer:bayer_scale=3" -loop 0 "${this.outputPath}"`,
        { stdio: 'pipe' }
      );

      const stats = execSync(`ls -lh "${this.outputPath}"`).toString().trim();
      console.log(`GIF saved: ${this.outputPath} (${actualFrames} frames, ${(actualFrames / this.fps).toFixed(1)}s)`);
      console.log(`  ${stats}`);
    } finally {
      rmSync(this.tmpDir, { recursive: true, force: true });
      this.tmpDir = null;
      this.browser = null;
      this.context = null;
      this.page = null;
    }
  }
}
