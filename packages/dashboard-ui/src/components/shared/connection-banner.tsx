import { useCallback, useEffect, useState } from 'react';
import { WifiOff, X } from 'lucide-react';

interface ConnectionBannerProps {
  isError: boolean;
}

export function ConnectionBanner({ isError }: ConnectionBannerProps) {
  const [dismissed, setDismissed] = useState(false);
  const [showRestored, setShowRestored] = useState(false);
  const [prevIsError, setPrevIsError] = useState(isError);

  // React-recommended pattern: derive state from props during render
  // See: https://react.dev/reference/react/useState#storing-information-from-previous-renders
  if (isError !== prevIsError) {
    setPrevIsError(isError);
    if (isError) {
      setDismissed(false);
      setShowRestored(false);
    } else {
      setShowRestored(true);
    }
  }

  // Auto-hide "restored" message after delay
  useEffect(() => {
    if (!showRestored) return;
    const timer = setTimeout(() => setShowRestored(false), 1500);
    return () => clearTimeout(timer);
  }, [showRestored]);

  const visible = isError || showRestored;
  const handleDismiss = useCallback(() => setDismissed(true), []);

  if (!visible || dismissed) return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="fixed top-0 inset-x-0 z-50 flex items-center justify-between gap-3 bg-destructive/90 px-4 py-2 text-sm text-white backdrop-blur-sm animate-in slide-in-from-top duration-300"
    >
      <div className="flex items-center gap-2">
        <WifiOff className="h-4 w-4" aria-hidden="true" />
        <span>
          {isError
            ? 'Unable to connect to Memex server. Retrying...'
            : 'Connection restored.'}
        </span>
      </div>
      <button
        onClick={handleDismiss}
        className="rounded p-1 hover:bg-white/20 transition-colors"
        aria-label="Dismiss connection banner"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
