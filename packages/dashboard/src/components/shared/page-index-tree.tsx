import { useCallback } from 'react';
import { cn } from '@/lib/utils';
import { ScrollArea } from '@/components/ui/scroll-area';

/**
 * Raw page index node from the API.
 * The tree is recursive: each node may have `children`.
 */
export interface PageIndexNode {
  title: string;
  level: number;
  summary?: Record<string, string> | null;
  children?: PageIndexNode[];
  node_id?: string;
}

interface PageIndexTreeProps {
  nodes: PageIndexNode[] | null | undefined;
  onNodeClick?: (node: PageIndexNode) => void;
  selectedNodeId?: string | null;
  maxHeight?: string;
  className?: string;
}

/**
 * Recursive tree component that renders a note's hierarchical page index
 * with box-drawing characters (vertical bars, branches, and L-connectors).
 */
export function PageIndexTree({
  nodes,
  onNodeClick,
  selectedNodeId,
  maxHeight = '300px',
  className,
}: PageIndexTreeProps) {
  if (!nodes || nodes.length === 0) {
    return (
      <p className="text-xs text-muted-foreground py-4">
        No page index available.
      </p>
    );
  }

  return (
    <ScrollArea className={cn('w-full', className)} style={{ maxHeight }}>
      <div className="w-full">
        {nodes.map((node, i) => (
          <TreeNode
            key={node.node_id ?? `${node.title}-${i}`}
            node={node}
            depth={0}
            isLast={i === nodes.length - 1}
            ancestorIsLast={[]}
            onNodeClick={onNodeClick}
            selectedNodeId={selectedNodeId}
          />
        ))}
      </div>
    </ScrollArea>
  );
}

// --- Internal recursive node ---

interface TreeNodeProps {
  node: PageIndexNode;
  depth: number;
  isLast: boolean;
  ancestorIsLast: boolean[];
  onNodeClick?: (node: PageIndexNode) => void;
  selectedNodeId?: string | null;
}

function TreeNode({
  node,
  depth,
  isLast,
  ancestorIsLast,
  onNodeClick,
  selectedNodeId,
}: TreeNodeProps) {
  const handleClick = useCallback(() => {
    onNodeClick?.(node);
  }, [onNodeClick, node]);

  const isSelected = selectedNodeId != null && node.node_id === selectedNodeId;
  const isRoot = depth === 0;

  // Build the box-drawing prefix
  const prefix = buildPrefix(depth, isLast, ancestorIsLast);

  // Format 5W summary fields
  const summary = formatSummary(node.summary);

  const validChildren = (node.children ?? []).filter((c) => c.title);

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={handleClick}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            handleClick();
          }
        }}
        className={cn(
          'flex flex-col w-full py-1 border-b border-white/[0.04] cursor-pointer',
          'hover:bg-white/[0.02] transition-colors',
          isSelected && 'bg-primary/10',
        )}
      >
        {/* Title row: prefix + title */}
        <div className="flex items-baseline w-full">
          {prefix && (
            <span
              className="shrink-0 font-mono text-xs leading-relaxed"
              style={{ color: 'rgba(120, 120, 180, 0.55)', whiteSpace: 'pre' }}
            >
              {prefix}
            </span>
          )}
          <span
            className={cn(
              'flex-1 leading-relaxed',
              isRoot ? 'text-[13px] font-semibold text-foreground' : 'text-xs text-muted-foreground',
              isSelected && 'text-primary',
            )}
          >
            {node.title}
          </span>
        </div>

        {/* Summary row (if present) */}
        {summary && (
          <span
            className="text-[11px] text-muted-foreground/80 leading-snug"
            style={{ paddingLeft: depth * 28 }}
          >
            {summary}
          </span>
        )}
      </div>

      {/* Recurse into children */}
      {validChildren.map((child, i) => (
        <TreeNode
          key={child.node_id ?? `${child.title}-${i}`}
          node={child}
          depth={depth + 1}
          isLast={i === validChildren.length - 1}
          ancestorIsLast={[...ancestorIsLast, isLast]}
          onNodeClick={onNodeClick}
          selectedNodeId={selectedNodeId}
        />
      ))}
    </>
  );
}

// --- Helpers ---

/**
 * Build box-drawing tree connector prefix.
 *
 * Each ancestor contributes either "│   " (if not last among siblings)
 * or "    " (if last). The current node contributes "├── " or "└── ".
 */
function buildPrefix(
  depth: number,
  isLast: boolean,
  ancestorIsLast: boolean[],
): string {
  if (depth === 0) return '';

  const parts: string[] = [];
  for (const wasLast of ancestorIsLast) {
    parts.push(wasLast ? '    ' : '│   ');
  }
  parts.push(isLast ? '└── ' : '├── ');
  return parts.join('');
}

/**
 * Format the 5W summary dict into a single readable string.
 */
function formatSummary(
  summary: Record<string, string> | null | undefined,
): string {
  if (!summary || typeof summary !== 'object') return '';

  const keys = ['who', 'what', 'how', 'when', 'where'] as const;
  const parts = keys
    .map((k) => summary[k])
    .filter((v): v is string => Boolean(v));

  return parts.join(' | ');
}
