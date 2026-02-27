import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

export function ResultCardSkeleton() {
  return (
    <Card className="bg-card border-border">
      <CardContent className="p-4 space-y-3">
        <Skeleton className="h-4 w-3/4 bg-muted" />
        <Skeleton className="h-3 w-full bg-muted" />
        <Skeleton className="h-3 w-2/3 bg-muted" />
        <div className="flex gap-2">
          <Skeleton className="h-5 w-16 rounded-full bg-muted" />
          <Skeleton className="h-5 w-20 rounded-full bg-muted" />
        </div>
      </CardContent>
    </Card>
  );
}
