import { useMemo, useRef, useEffect, useState } from 'react';
import {
  Brain,
  FileText,
  Network,
  Cpu,
  HardDrive,
  Clock,
} from 'lucide-react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import { MetricCard } from '@/components/shared/metric-card';
import { MetricCardSkeleton } from '@/components/shared/metric-card-skeleton';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent } from '@/components/ui/card';
import { useSystemStats, useMetrics } from '@/api/hooks/use-stats';

const MAX_DATA_POINTS = 60;

interface TimeSeriesPoint {
  time: string;
  timestamp: number;
  cpu: number;
  memory: number;
  requests: number;
}

function parsePrometheusMetrics(text: string): Record<string, number> {
  const metrics: Record<string, number> = {};
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || !line.trim()) continue;
    const [key, value] = line.split(' ');
    if (key && value) metrics[key] = parseFloat(value);
  }
  return metrics;
}

function parseHttpRequestsTotal(text: string): number {
  let total = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || !line.trim()) continue;
    if (line.includes('http_requests_total')) {
      const parts = line.split(' ');
      if (parts.length >= 2) {
        total += parseFloat(parts[parts.length - 1]);
      }
    }
  }
  return total;
}

function formatTime(timestamp: number): string {
  const d = new Date(timestamp);
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
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

const chartTooltipStyle = {
  backgroundColor: 'var(--card)',
  border: '1px solid var(--border)',
  borderRadius: '8px',
  color: 'var(--foreground)',
};

const chartTooltipLabelStyle = {
  color: 'var(--muted-foreground)',
};

export default function SystemStatusPage() {
  const { data: stats, isLoading: statsLoading } = useSystemStats();
  const { data: metricsText, isLoading: metricsLoading } = useMetrics();

  const [history, setHistory] = useState<TimeSeriesPoint[]>([]);
  const lastMetricsTextRef = useRef<string | undefined>(undefined);

  const prometheus = useMemo(() => {
    if (!metricsText) return {};
    return parsePrometheusMetrics(metricsText);
  }, [metricsText]);

  // Append a new data point whenever metricsText changes.
  useEffect(() => {
    if (!metricsText || metricsText === lastMetricsTextRef.current) return;
    lastMetricsTextRef.current = metricsText;

    const now = Date.now();
    const cpu = prometheus['process_cpu_seconds_total'] ?? 0;
    const memBytes = prometheus['process_resident_memory_bytes'] ?? 0;
    const memMb = memBytes / 1024 / 1024;
    const requests = parseHttpRequestsTotal(metricsText);

    const point: TimeSeriesPoint = {
      time: formatTime(now),
      timestamp: now,
      cpu,
      memory: memMb,
      requests,
    };

    // eslint-disable-next-line react-hooks/set-state-in-effect -- accumulating time-series from polled external data
    setHistory((prev) => {
      const next = [...prev, point];
      if (next.length > MAX_DATA_POINTS) {
        next.splice(0, next.length - MAX_DATA_POINTS);
      }
      return next;
    });
  }, [metricsText, prometheus]);

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

      <div className="space-y-2">
        <p className="text-sm font-medium text-foreground">Resource History</p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {/* CPU Usage Chart */}
          <Card>
            <CardContent>
              <p className="mb-3 text-sm font-medium text-foreground">CPU Usage (seconds)</p>
              {history.length < 2 ? (
                <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">
                  Collecting data...
                </div>
              ) : (
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={history}>
                    <defs>
                      <linearGradient id="cpuGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#3B82F6" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="#3B82F6" stopOpacity={0.05} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="var(--border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                      tickFormatter={(v: number) => v.toFixed(2)}
                    />
                    <Tooltip
                      contentStyle={chartTooltipStyle}
                      labelStyle={chartTooltipLabelStyle}
                      formatter={(value: number | undefined) => [`${(value ?? 0).toFixed(2)}s`, 'CPU']}
                    />
                    <Area
                      type="monotone"
                      dataKey="cpu"
                      stroke="#3B82F6"
                      strokeWidth={2}
                      fill="url(#cpuGradient)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          {/* Memory Usage Chart */}
          <Card>
            <CardContent>
              <p className="mb-3 text-sm font-medium text-foreground">Memory Usage (MB)</p>
              {history.length < 2 ? (
                <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">
                  Collecting data...
                </div>
              ) : (
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={history}>
                    <defs>
                      <linearGradient id="memoryGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#22C55E" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="#22C55E" stopOpacity={0.05} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="var(--border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                      tickFormatter={(v: number) => `${Math.round(v)}`}
                    />
                    <Tooltip
                      contentStyle={chartTooltipStyle}
                      labelStyle={chartTooltipLabelStyle}
                      formatter={(value: number | undefined) => [`${Math.round(value ?? 0)} MB`, 'Memory']}
                    />
                    <Area
                      type="monotone"
                      dataKey="memory"
                      stroke="#22C55E"
                      strokeWidth={2}
                      fill="url(#memoryGradient)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          {/* HTTP Requests Chart */}
          <Card>
            <CardContent>
              <p className="mb-3 text-sm font-medium text-foreground">HTTP Requests (total)</p>
              {history.length < 2 ? (
                <div className="flex h-[200px] items-center justify-center text-sm text-muted-foreground">
                  Collecting data...
                </div>
              ) : (
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={history}>
                    <defs>
                      <linearGradient id="requestsGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#F59E0B" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="#F59E0B" stopOpacity={0.05} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="var(--border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
                      axisLine={{ stroke: 'var(--border)' }}
                      tickLine={false}
                      tickFormatter={(v: number) => `${Math.round(v)}`}
                    />
                    <Tooltip
                      contentStyle={chartTooltipStyle}
                      labelStyle={chartTooltipLabelStyle}
                      formatter={(value: number | undefined) => [`${Math.round(value ?? 0)}`, 'Requests']}
                    />
                    <Area
                      type="monotone"
                      dataKey="requests"
                      stroke="#F59E0B"
                      strokeWidth={2}
                      fill="url(#requestsGradient)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
