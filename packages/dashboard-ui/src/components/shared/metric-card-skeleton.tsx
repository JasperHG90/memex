import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

export function MetricCardSkeleton() {
  return (
    <Card className="bg-card border-border">
      <CardContent className="p-6 space-y-3">
        <div className="flex items-center gap-3">
          <Skeleton className="h-5 w-5 rounded bg-muted" />
          <Skeleton className="h-4 w-24 bg-muted" />
        </div>
        <Skeleton className="h-8 w-16 bg-muted" />
      </CardContent>
    </Card>
  );
}
