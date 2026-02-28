import { Card, CardContent } from '@/components/ui/card';
import type { LucideIcon } from 'lucide-react';
import { useAnimatedNumber } from '@/hooks/use-animated-number';

interface MetricCardProps {
  icon: LucideIcon;
  label: string;
  value: string | number;
  description?: string;
}

export function MetricCard({ icon: Icon, label, value, description }: MetricCardProps) {
  const numericValue = typeof value === 'number' ? value : null;
  const animatedValue = useAnimatedNumber(numericValue ?? 0);

  return (
    <Card className="bg-card border-border hover:border-primary/30 transition-colors duration-150">
      <CardContent className="p-6">
        <div className="flex items-center gap-3">
          <Icon className="h-5 w-5 text-muted-foreground" />
          <span className="text-sm text-muted-foreground">{label}</span>
        </div>
        <div className="mt-2 text-2xl font-bold text-foreground">
          {numericValue != null ? animatedValue.toLocaleString() : value}
        </div>
        {description && <p className="mt-1 text-xs text-muted-foreground">{description}</p>}
      </CardContent>
    </Card>
  );
}
