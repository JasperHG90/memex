import { useEffect, useRef, useState } from 'react';

const HEALTH_CHECK_INTERVAL = 15_000;
const HEALTH_CHECK_URL = '/api/v1/stats/counts';

export function useConnectionStatus() {
  const [isConnected, setIsConnected] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval>>(null);

  useEffect(() => {
    async function checkConnection() {
      try {
        const response = await fetch(HEALTH_CHECK_URL, { method: 'GET' });
        setIsConnected(response.ok);
      } catch {
        setIsConnected(false);
      }
    }

    intervalRef.current = setInterval(checkConnection, HEALTH_CHECK_INTERVAL);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  return { isConnected };
}
