import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { FileText, Brain, Eye, Lightbulb, Tag, File, type LucideIcon } from 'lucide-react';

export interface LineageNodeData extends Record<string, unknown> {
  label: string;
  entityType: string;
  content?: string;
  highlighted?: boolean;
  dimmed?: boolean;
}

const NODE_STYLES: Record<string, { bg: string; border: string; icon: LucideIcon }> = {
  note: { bg: 'rgba(59, 130, 246, 0.12)', border: '#3B82F6', icon: FileText },
  memory_unit: { bg: 'rgba(34, 197, 94, 0.12)', border: '#22C55E', icon: Brain },
  observation: { bg: 'rgba(245, 158, 11, 0.12)', border: '#F59E0B', icon: Eye },
  mental_model: { bg: 'rgba(168, 85, 247, 0.12)', border: '#A855F7', icon: Lightbulb },
  entity: { bg: 'rgba(6, 182, 212, 0.12)', border: '#06B6D4', icon: Tag },
  asset: { bg: 'rgba(139, 92, 246, 0.12)', border: '#8B5CF6', icon: File },
};

const DEFAULT_STYLE = { bg: 'rgba(161, 161, 170, 0.12)', border: '#71717A', icon: File };

function LineageNodeComponent({ data }: NodeProps) {
  const nodeData = data as unknown as LineageNodeData;
  const style = NODE_STYLES[nodeData.entityType] ?? DEFAULT_STYLE;
  const Icon = style.icon;
  const truncatedContent = nodeData.content
    ? nodeData.content.length > 60
      ? nodeData.content.slice(0, 60) + '...'
      : nodeData.content
    : null;

  return (
    <div
      className="rounded-lg border px-3 py-2 shadow-md transition-all duration-150 hover:scale-105 hover:shadow-lg"
      style={{
        backgroundColor: style.bg,
        borderColor: nodeData.highlighted ? '#ffffff' : style.border,
        borderWidth: nodeData.highlighted ? 2 : 1,
        opacity: nodeData.dimmed ? 0.2 : 1,
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
          {nodeData.entityType.replace('_', ' ')}
        </span>
      </div>
      <div className="text-sm font-medium text-foreground truncate">{nodeData.label}</div>
      {truncatedContent && (
        <div className="text-xs text-muted-foreground mt-1 line-clamp-2">{truncatedContent}</div>
      )}
      <Handle type="source" position={Position.Right} className="!bg-muted-foreground !w-2 !h-2" />
    </div>
  );
}

export const LineageNode = memo(LineageNodeComponent);
