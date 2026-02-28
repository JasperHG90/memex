import { memo } from 'react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';

const TYPE_COLORS: Record<string, string> = {
  Person: '#3B82F6',
  Organization: '#A855F7',
  Location: '#22C55E',
  Concept: '#F59E0B',
  Event: '#EF4444',
  Technology: '#06B6D4',
};

export interface EntityNodeData {
  label: string;
  type: string;
  mentionCount: number;
  entityId: string;
  [key: string]: unknown;
}

export type EntityFlowNode = Node<EntityNodeData, 'entity'>;

function EntityNodeComponent({ data, selected }: NodeProps<EntityFlowNode>) {
  const color = TYPE_COLORS[data.type] ?? '#71717A';
  const size = Math.max(32, Math.min(56, 32 + data.mentionCount * 2));

  return (
    <div className="flex flex-col items-center group">
      <Handle type="target" position={Position.Top} className="!invisible" />
      <div
        className={`rounded-full flex items-center justify-center text-xs font-bold border-2 transition-transform duration-150 ${
          selected
            ? 'scale-110 ring-2 ring-primary ring-offset-2 ring-offset-background'
            : 'group-hover:scale-110'
        }`}
        style={{
          width: size,
          height: size,
          backgroundColor: color + '33',
          borderColor: color,
        }}
      >
        <span style={{ color }}>{data.label?.[0]?.toUpperCase() ?? '?'}</span>
      </div>
      <span className="text-xs text-foreground mt-1 max-w-20 truncate text-center leading-tight">
        {data.label}
      </span>
      <Handle type="source" position={Position.Bottom} className="!invisible" />
    </div>
  );
}

export const EntityNode = memo(EntityNodeComponent);
