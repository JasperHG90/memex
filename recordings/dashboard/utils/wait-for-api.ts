/**
 * Poll a service URL until it responds. Automatically tries IPv6 loopback
 * as a fallback (Vite often binds to [::1] only in containers).
 * Returns the URL that actually responded so callers can use it for navigation.
 */
async function poll(
  url: string,
  label: string,
  start: number,
  timeoutMs: number,
): Promise<string> {
  const ipv6Url = url.replace('localhost', '[::1]');
  const urls = url === ipv6Url ? [url] : [url, ipv6Url];

  while (Date.now() - start < timeoutMs) {
    for (const candidate of urls) {
      try {
        const res = await fetch(candidate);
        if (res.status < 500) {
          console.log(`${label} is ready (${candidate})`);
          return candidate;
        }
      } catch {
        // Service not ready yet
      }
    }
    console.log(`Waiting for ${label}...`);
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`Timed out waiting for ${label} at ${url} after ${timeoutMs}ms`);
}

/**
 * Wait for API and Dashboard to be ready.
 * Returns the resolved URLs (which may differ from input if IPv6 fallback was used).
 */
export async function waitForServices(
  apiUrl: string,
  dashboardUrl: string,
  timeoutMs: number = 30_000,
): Promise<{ apiUrl: string; dashboardUrl: string }> {
  const start = Date.now();

  const [resolvedApi, resolvedDashboard] = await Promise.all([
    poll(apiUrl, 'API', start, timeoutMs),
    poll(dashboardUrl, 'Dashboard', start, timeoutMs),
  ]);

  console.log('All services ready');
  return { apiUrl: resolvedApi, dashboardUrl: resolvedDashboard };
}
