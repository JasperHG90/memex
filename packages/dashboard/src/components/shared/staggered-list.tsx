import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

interface StaggeredListProps {
  children: ReactNode[];
  className?: string;
}

export function StaggeredList({ children, className }: StaggeredListProps) {
  return (
    <div className={cn(className)}>
      {children.map((child, i) => (
        <div
          key={i}
          className="animate-in fade-in slide-in-from-bottom-1 fill-mode-both"
          style={{ animationDelay: `${i * 50}ms` }}
        >
          {child}
        </div>
      ))}
    </div>
  );
}
