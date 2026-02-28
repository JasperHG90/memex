export async function waitForServices(
  apiUrl: string,
  dashboardUrl: string,
  timeoutMs: number = 30_000
): Promise<void> {
  const start = Date.now();

  const poll = async (url: string, label: string): Promise<void> => {
    while (Date.now() - start < timeoutMs) {
      try {
        const res = await fetch(url);
        if (res.status < 500) {
          console.log(`${label} is ready (${url})`);
          return;
        }
      } catch {
        // Service not ready yet
      }
      console.log(`Waiting for ${label}...`);
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error(`Timed out waiting for ${label} at ${url} after ${timeoutMs}ms`);
  };

  await Promise.all([
    poll(apiUrl, 'API'),
    poll(dashboardUrl, 'Dashboard'),
  ]);

  console.log('All services ready');
}
