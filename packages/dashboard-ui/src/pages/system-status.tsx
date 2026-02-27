import { useMemo } from 'react';
import {
  Brain,
  FileText,
  Network,
  Cpu,
  HardDrive,
  Clock,
} from 'lucide-react';
import { MetricCard } from '@/components/shared/metric-card';
import { MetricCardSkeleton } from '@/components/shared/metric-card-skeleton';
import { PageHeader } from '@/components/layout/page-header';
import { useSystemStats, useMetrics } from '@/api/hooks/use-stats';

function parsePrometheusMetrics(text: string): Record<string, number> {
  const metrics: Record<string, number> = {};
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || !line.trim()) continue;
    const [key, value] = line.split(' ');
    if (key && value) metrics[key] = parseFloat(value);
  }
  return metrics;
}

function formatUptime(startTimeSeconds: number): string {
  const uptimeSeconds = Date.now() / 1000 - startTimeSeconds;
  if (uptimeSeconds <= 0) return 'Unknown';

  const days = Math.floor(uptimeSeconds / 86400);
  const hours = Math.floor((uptimeSeconds % 86400) / 3600);
  const minutes = Math.floor((uptimeSeconds % 3600) / 60);

  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  parts.push(`${minutes}m`);
  return parts.join(' ');
}

export default function SystemStatusPage() {
  const { data: stats, isLoading: statsLoading } = useSystemStats();
  const { data: metricsText, isLoading: metricsLoading } = useMetrics();

  const prometheus = useMemo(() => {
    if (!metricsText) return {};
    return parsePrometheusMetrics(metricsText);
  }, [metricsText]);

  const cpuDisplay = useMemo(() => {
    const val = prometheus['process_cpu_seconds_total'];
    if (val === undefined) return '--';
    return `${val.toFixed(2)}s`;
  }, [prometheus]);

  const memoryDisplay = useMemo(() => {
    const val = prometheus['process_resident_memory_bytes'];
    if (val === undefined) return '--';
    const mb = val / 1024 / 1024;
    return `${mb.toFixed(2)} MB`;
  }, [prometheus]);

  const uptimeDisplay = useMemo(() => {
    const val = prometheus['process_start_time_seconds'];
    if (val === undefined) return '--';
    return formatUptime(val);
  }, [prometheus]);

  const isLoading = statsLoading || metricsLoading;

  return (
    <div className="w-full space-y-6">
      <PageHeader title="System Status" description="Auto-refreshes every 5 seconds" />

      <div className="space-y-2">
        <p className="text-sm font-medium text-foreground">Key Performance Indicators</p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {isLoading ? (
            <>
              <MetricCardSkeleton />
              <MetricCardSkeleton />
              <MetricCardSkeleton />
            </>
          ) : (
            <>
              <MetricCard
                icon={Brain}
                label="Memory Units"
                value={stats?.memories ?? 0}
              />
              <MetricCard
                icon={FileText}
                label="Notes"
                value="--"
                description="Not available in current API"
              />
              <MetricCard
                icon={Network}
                label="Entities"
                value={stats?.entities ?? 0}
              />
            </>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <p className="text-sm font-medium text-foreground">Resource Usage</p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {metricsLoading ? (
            <>
              <MetricCardSkeleton />
              <MetricCardSkeleton />
              <MetricCardSkeleton />
            </>
          ) : (
            <>
              <MetricCard
                icon={Cpu}
                label="CPU Usage"
                value={cpuDisplay}
                description="Total CPU seconds consumed"
              />
              <MetricCard
                icon={HardDrive}
                label="Memory Usage"
                value={memoryDisplay}
                description="Resident memory"
              />
              <MetricCard
                icon={Clock}
                label="Uptime"
                value={uptimeDisplay}
                description="Auto-refreshes every 5s"
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
