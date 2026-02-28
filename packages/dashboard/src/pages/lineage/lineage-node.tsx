import { memo } from 'react';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { FileText, Brain, Eye, Lightbulb, Tag, File, type LucideIcon } from 'lucide-react';

export interface LineageNodeData extends Record<string, unknown> {
  label: string;
  entityType: string;
  content?: string;
  highlighted?: boolean;
  dimmed?: boolean;
  raw?: Record<string, unknown>;
}

const NODE_STYLES: Record<string, { bg: string; border: string; icon: LucideIcon }> = {
  note: { bg: 'rgba(59, 130, 246, 0.18)', border: '#3B82F6', icon: FileText },
  memory_unit: { bg: 'rgba(34, 197, 94, 0.18)', border: '#22C55E', icon: Brain },
  observation: { bg: 'rgba(245, 158, 11, 0.18)', border: '#F59E0B', icon: Eye },
  mental_model: { bg: 'rgba(168, 85, 247, 0.18)', border: '#A855F7', icon: Lightbulb },
  entity: { bg: 'rgba(6, 182, 212, 0.18)', border: '#06B6D4', icon: Tag },
  asset: { bg: 'rgba(139, 92, 246, 0.18)', border: '#8B5CF6', icon: File },
};

const DEFAULT_STYLE = { bg: 'rgba(161, 161, 170, 0.12)', border: '#71717A', icon: File };

type LineageNodeType = Node<LineageNodeData>;

function LineageNodeComponent({ data }: NodeProps<LineageNodeType>) {
  const style = NODE_STYLES[data.entityType] ?? DEFAULT_STYLE;
  const Icon = style.icon;
  const truncatedContent = data.content
    ? data.content.length > 60
      ? data.content.slice(0, 60) + '...'
      : data.content
    : null;

  return (
    <div
      className="rounded-lg border px-3 py-2 shadow-md transition-all duration-150 hover:scale-105 hover:shadow-lg cursor-pointer"
      style={{
        backgroundColor: style.bg,
        borderColor: data.highlighted ? 'var(--foreground)' : style.border,
        borderWidth: data.highlighted ? 2 : 1,
        opacity: data.dimmed ? 0.2 : 1,
        minWidth: 180,
        maxWidth: 220,
      }}
    >
      <Handle type="target" position={Position.Left} className="!bg-muted-foreground !w-2 !h-2" />
      <div className="flex items-center gap-2 mb-1">
        <Icon size={14} style={{ color: style.border }} />
        <span
          className="text-xs font-semibold uppercase tracking-wide"
          style={{ color: style.border }}
        >
          {data.entityType.replace('_', ' ')}
        </span>
      </div>
      <div className="text-sm font-medium text-foreground truncate">{data.label}</div>
      {truncatedContent && (
        <div className="text-xs text-muted-foreground mt-1 line-clamp-2">{truncatedContent}</div>
      )}
      <Handle type="source" position={Position.Right} className="!bg-muted-foreground !w-2 !h-2" />
    </div>
  );
}

export const LineageNode = memo(LineageNodeComponent);
