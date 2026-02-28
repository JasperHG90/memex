import { memo, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { TYPE_COLORS, type EntityFlowNode } from './entity-types';

export { ENTITY_TYPES, TYPE_COLORS, type EntityNodeData, type EntityFlowNode } from './entity-types';

function EntityNodeComponent({ data, selected }: NodeProps<EntityFlowNode>) {
  const [isHovered, setIsHovered] = useState(false);
  const color = TYPE_COLORS[data.type] ?? '#71717A';
  const size = Math.max(32, Math.min(56, 32 + data.mentionCount * 2));

  return (
    <div
      className="flex flex-col items-center group relative"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {/* Tooltip */}
      {isHovered && (
        <div className="absolute -top-16 left-1/2 -translate-x-1/2 z-50 rounded-lg border border-border bg-card px-3 py-2 shadow-lg whitespace-nowrap pointer-events-none">
          <p className="text-xs font-semibold text-foreground">{data.label}</p>
          <p className="text-[10px] text-muted-foreground">{data.type} · {data.mentionCount} mentions</p>
        </div>
      )}
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
