import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeTypes,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  MarkerType,
  Panel,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import dagre from 'dagre';
import { LineageNode, type LineageNodeData } from './lineage-node';
import type { LineageResponse } from '@/api/hooks/use-lineage';

/** Resolve a CSS custom property to its computed value. */
function resolveCssColor(varName: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return value || fallback;
}

const nodeTypes: NodeTypes = {
  lineage: LineageNode,
};

const NODE_WIDTH = 200;
const NODE_HEIGHT = 70;

function getLayoutedElements(
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', nodesep: 50, ranksep: 100 });

  nodes.forEach((node) => g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => g.setEdge(edge.source, edge.target));

  dagre.layout(g);

  return {
    nodes: nodes.map((node) => {
      const pos = g.node(node.id);
      return {
        ...node,
        position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      };
    }),
    edges,
  };
}

function findConnected(
  nodeId: string,
  edges: Edge[],
  direction: 'upstream' | 'downstream',
): Set<string> {
  const visited = new Set<string>();
  const queue = [nodeId];
  while (queue.length > 0) {
    const current = queue.shift()!;
    if (visited.has(current)) continue;
    visited.add(current);
    for (const edge of edges) {
      if (direction === 'upstream' && edge.target === current) queue.push(edge.source);
      if (direction === 'downstream' && edge.source === current) queue.push(edge.target);
    }
  }
  return visited;
}

interface NodeEntry {
  id: string;
  label: string;
  entityType: string;
  content?: string;
  raw: Record<string, unknown>;
  vaultId?: string;
}

function getLabel(entity: Record<string, unknown>): string {
  const title = entity.title ?? entity.name ?? entity.canonical_name;
  if (typeof title === 'string' && title) return title;

  const meta = entity.doc_metadata;
  if (meta && typeof meta === 'object') {
    const m = meta as Record<string, unknown>;
    const metaLabel = m.title ?? m.name ?? m.source;
    if (typeof metaLabel === 'string' && metaLabel) return metaLabel;
  }

  const text = entity.statement ?? entity.text;
  if (typeof text === 'string' && text) {
    return text.length > 30 ? text.slice(0, 30) + '...' : text;
  }

  const id = entity.id ?? entity.uuid;
  if (typeof id === 'string') return id.slice(0, 8);

  return 'Unknown';
}

function getContent(entity: Record<string, unknown>): string | undefined {
  const text = entity.statement ?? entity.text ?? entity.content ?? entity.original_text;
  if (typeof text === 'string' && text) return text;
  return undefined;
}

function lineageToGraph(
  lineage: LineageResponse,
  edgeColor: string,
): { nodes: Node[]; edges: Edge[] } {
  const nodesMap = new Map<string, NodeEntry>();
  const edgeSet = new Set<string>();
  const rawEdges: Array<{ source: string; target: string }> = [];

  const layerOrder: Record<string, number> = {
    asset: 0,
    note: 1,
    memory_unit: 2,
    observation: 3,
    mental_model: 4,
    entity: 5,
  };

  function processNode(node: LineageResponse): string {
    const entityType = node.entity_type ?? 'unknown';
    const entity = node.entity ?? {};
    const id = String(entity.id ?? entity.uuid ?? `anon-${Math.random()}`);

    if (!nodesMap.has(id)) {
      const vaultId = typeof entity.vault_id === 'string' ? entity.vault_id : undefined;
      nodesMap.set(id, {
        id,
        label: getLabel(entity),
        entityType,
        content: getContent(entity),
        raw: entity,
        vaultId,
      });
    }

    // Handle assets for notes
    if (entityType === 'note') {
      const assets = entity.assets;
      if (Array.isArray(assets)) {
        for (const assetPath of assets) {
          if (typeof assetPath !== 'string') continue;
          const assetId = `asset:${assetPath}`;
          const assetName = assetPath.split('/').pop() ?? assetPath;
          if (!nodesMap.has(assetId)) {
            nodesMap.set(assetId, {
              id: assetId,
              label: assetName,
              entityType: 'asset',
              raw: { path: assetPath },
            });
          }
          const edgeKey = `${assetId}->${id}`;
          if (!edgeSet.has(edgeKey)) {
            edgeSet.add(edgeKey);
            rawEdges.push({ source: assetId, target: id });
          }
        }
      }
    }

    return id;
  }

  function traverse(node: LineageResponse): void {
    const sourceId = processNode(node);
    const sourceLayer = layerOrder[node.entity_type] ?? 5;

    for (const child of node.derived_from ?? []) {
      const targetId = processNode(child);
      const targetLayer = layerOrder[child.entity_type] ?? 5;

      // Edge direction: from lower layer to higher layer (left to right)
      const [from, to] =
        targetLayer < sourceLayer ? [targetId, sourceId] : [sourceId, targetId];

      const edgeKey = `${from}->${to}`;
      if (!edgeSet.has(edgeKey)) {
        edgeSet.add(edgeKey);
        rawEdges.push({ source: from, target: to });
      }

      traverse(child);
    }
  }

  traverse(lineage);

  const nodes: Node[] = Array.from(nodesMap.values()).map((entry) => ({
    id: entry.id,
    type: 'lineage',
    position: { x: 0, y: 0 },
    data: {
      label: entry.label,
      entityType: entry.entityType,
      content: entry.content,
      highlighted: false,
      dimmed: false,
      raw: entry.raw,
      vaultId: entry.vaultId,
    } satisfies LineageNodeData,
  }));

  const edges: Edge[] = rawEdges.map((e) => ({
    id: `${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
    type: 'default',
    animated: false,
    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: edgeColor },
    style: { stroke: edgeColor, strokeWidth: 1.5 },
  }));

  return getLayoutedElements(nodes, edges);
}

const LEGEND_ITEMS = [
  { label: 'Note', color: '#3B82F6' },
  { label: 'Memory Unit', color: '#22C55E' },
  { label: 'Observation', color: '#F59E0B' },
  { label: 'Mental Model', color: '#A855F7' },
  { label: 'Entity', color: '#06B6D4' },
  { label: 'Asset', color: '#8B5CF6' },
];

interface LineageGraphProps {
  lineage: LineageResponse;
  onNodeClick?: (nodeId: string, entityType: string, data: Record<string, unknown>) => void;
}

export function LineageGraph({ lineage, onNodeClick }: LineageGraphProps) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // Resolve CSS variables to actual colors for SVG markers (which don't support var()).
  // Re-resolve when the .light class toggles on <html>.
  const [themeKey, setThemeKey] = useState(0);
  useEffect(() => {
    const observer = new MutationObserver(() => setThemeKey((k) => k + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- themeKey triggers re-resolve on theme change
  const mutedFg = useMemo(() => resolveCssColor('--muted-foreground', '#A1A1AA'), [themeKey]);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- themeKey triggers re-resolve on theme change
  const fg = useMemo(() => resolveCssColor('--foreground', '#EDEDED'), [themeKey]);

  const { layoutedNodes, layoutedEdges } = useMemo(() => {
    const result = lineageToGraph(lineage, mutedFg);
    return { layoutedNodes: result.nodes, layoutedEdges: result.edges };
  }, [lineage, mutedFg]);

  const [nodes, setNodes, onNodesChange] = useNodesState(layoutedNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(layoutedEdges);
  const [prevLineage, setPrevLineage] = useState(lineage);

  // React-recommended pattern: derive state from props during render
  if (prevLineage !== lineage) {
    setPrevLineage(lineage);
    setSelectedNodeId(null);
  }

  // Re-layout when lineage data changes
  useEffect(() => {
    setNodes(layoutedNodes);
    setEdges(layoutedEdges);
  }, [layoutedNodes, layoutedEdges, setNodes, setEdges]);

  // Apply highlighting when selection changes
  // Use layoutedEdges (stable from useMemo) for graph traversal to avoid
  // circular deps: edges -> setEdges -> edges -> ...
  useEffect(() => {
    if (!selectedNodeId) {
      setNodes((nds) =>
        nds.map((n) => ({
          ...n,
          data: { ...n.data, highlighted: false, dimmed: false },
        })),
      );
      setEdges((eds) =>
        eds.map((e) => ({
          ...e,
          animated: false,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: mutedFg },
          style: { stroke: mutedFg, strokeWidth: 1.5 },
        })),
      );
      return;
    }

    const upstream = findConnected(selectedNodeId, layoutedEdges, 'upstream');
    const downstream = findConnected(selectedNodeId, layoutedEdges, 'downstream');
    const connected = new Set([...upstream, ...downstream]);

    setNodes((nds) =>
      nds.map((n) => ({
        ...n,
        data: {
          ...n.data,
          highlighted: connected.has(n.id),
          dimmed: !connected.has(n.id),
        },
      })),
    );

    setEdges((eds) =>
      eds.map((e) => {
        const isHighlighted = connected.has(e.source) && connected.has(e.target);
        return {
          ...e,
          animated: isHighlighted,
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color: isHighlighted ? fg : mutedFg,
          },
          style: {
            stroke: isHighlighted ? fg : mutedFg,
            strokeWidth: isHighlighted ? 2.5 : 1.5,
            opacity: isHighlighted ? 0.8 : 0.15,
          },
        };
      }),
    );
  }, [selectedNodeId, layoutedEdges, setNodes, setEdges, mutedFg, fg]);

  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      setSelectedNodeId((prev) => (prev === node.id ? null : node.id));
      if (onNodeClick) {
        const nodeData = node.data as LineageNodeData;
        onNodeClick(node.id, nodeData.entityType, nodeData.raw ?? {});
      }
    },
    [onNodeClick],
  );

  return (
    <div className="w-full h-full rounded-lg border border-border bg-card overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.1}
        maxZoom={2}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--border)" />
        <Controls
          className="!bg-card !border-border !shadow-lg [&>button]:!bg-card [&>button]:!border-border [&>button]:!text-foreground [&>button:hover]:!bg-hover"
        />
        <Panel position="bottom-right">
          <div className="flex gap-3 bg-card/80 backdrop-blur-sm border border-border rounded-lg px-3 py-2">
            {LEGEND_ITEMS.map((item) => (
              <div key={item.label} className="flex items-center gap-1.5">
                <div
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: item.color }}
                />
                <span className="text-xs text-muted-foreground">{item.label}</span>
              </div>
            ))}
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}
