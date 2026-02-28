import { useState } from 'react';
import { RefreshCw, Loader2, Zap } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from 'sonner';
import { formatLabel } from '@/components/shared/format-label';
import { useReflectionQueue, useTriggerReflection } from '@/api/hooks/use-reflections';
import { useEntities } from '@/api/hooks/use-entities';

export default function Reflection() {
  const { data: queue, isLoading, refetch } = useReflectionQueue();
  const { data: allEntities } = useEntities({ limit: 200, sort: '-mentions' });
  const triggerReflection = useTriggerReflection();
  const [triggering, setTriggering] = useState<string | null>(null);

  const entityNameMap = new Map(
    allEntities?.map((e) => [e.id, e]) ?? []
  );

  async function handleTrigger(entityId: string) {
    setTriggering(entityId);
    try {
      await triggerReflection.mutateAsync(entityId);
      toast.success('Reflection completed');
      void refetch();
    } catch (err) {
      toast.error(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setTriggering(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <PageHeader
          title="Reflection Queue"
          description="Entities pending reflection for mental model synthesis"
        />
        <Button variant="outline" size="sm" onClick={() => refetch()}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Card key={i} className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-4">
                  <Skeleton className="h-10 w-10 rounded-full" />
                  <div className="flex-1">
                    <Skeleton className="h-4 w-48 mb-1" />
                    <Skeleton className="h-3 w-32" />
                  </div>
                  <Skeleton className="h-8 w-20" />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : !queue || queue.length === 0 ? (
        <Card className="bg-card border-border">
          <CardContent className="flex flex-col items-center justify-center py-12">
            <RefreshCw className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold text-foreground">Queue Empty</h3>
            <p className="text-sm text-muted-foreground mt-1">
              No entities are currently pending reflection.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {queue.map((item) => {
            const entity = entityNameMap.get(item.entity_id);
            const isTriggering = triggering === item.entity_id;
            return (
              <Card key={item.entity_id} className="bg-card border-border hover:border-primary/30 transition-colors">
                <CardContent className="p-4">
                  <div className="flex items-center gap-4">
                    <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
                      <Zap className="h-5 w-5 text-primary" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-semibold text-foreground truncate">
                        {entity?.name ?? item.entity_id.slice(0, 12) + '...'}
                      </p>
                      <div className="flex items-center gap-2 mt-0.5">
                        {entity?.entity_type && (
                          <Badge variant="secondary" className="text-[10px]">
                            {formatLabel(entity.entity_type)}
                          </Badge>
                        )}
                        <span className="text-xs text-muted-foreground">
                          Priority: {item.priority_score.toFixed(2)}
                        </span>
                        {entity?.mention_count != null && (
                          <span className="text-xs text-muted-foreground">
                            · {entity.mention_count} mentions
                          </span>
                        )}
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={isTriggering}
                      onClick={() => handleTrigger(item.entity_id)}
                    >
                      {isTriggering ? (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                      )}
                      {isTriggering ? 'Reflecting...' : 'Trigger'}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
