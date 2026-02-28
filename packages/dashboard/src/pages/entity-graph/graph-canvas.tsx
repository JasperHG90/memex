import React, { useEffect, useMemo, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Edge,
  type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force';
import type { EntityDTO, CooccurrenceRecord } from '@/api/generated';
import { EntityNode, type EntityFlowNode } from './entity-node';
import type { GraphFilters } from './filter-panel';

const nodeTypes = { entity: EntityNode };

interface SimNode extends SimulationNodeDatum {
  id: string;
  name: string;
  mentionCount: number;
  entityType: string;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  count: number;
}

function layoutGraph(entities: EntityDTO[], cooccurrences: CooccurrenceRecord[]) {
  if (entities.length === 0) return { nodes: [] as SimNode[], links: [] as SimLink[] };

  const entityMap = new Map(entities.map((e) => [e.id, e]));

  const nodes: SimNode[] = entities.map((e) => ({
    id: e.id,
    name: e.name,
    mentionCount: e.mention_count ?? 0,
    entityType: e.entity_type ?? 'Concept',
  }));

  const links: SimLink[] = cooccurrences
    .filter((c) => entityMap.has(c.entity_id_1) && entityMap.has(c.entity_id_2))
    .map((c) => ({
      source: c.entity_id_1,
      target: c.entity_id_2,
      count: c.cooccurrence_count,
    }));

  const simulation = forceSimulation(nodes)
    .force(
      'link',
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance(120),
    )
    .force('charge', forceManyBody().strength(-250))
    .force('center', forceCenter(0, 0))
    .force('collide', forceCollide(50));

  // Run synchronously
  simulation.tick(300);
  simulation.stop();

  return { nodes, links };
}

function buildFlowElements(
  entities: EntityDTO[],
  cooccurrences: CooccurrenceRecord[],
  filters: GraphFilters,
) {
  // 1. Filter entities
  const filteredEntities = entities.filter((e) => {
    if ((e.mention_count ?? 0) < filters.minImportance) return false;
    return true;
  });

  const validIds = new Set(filteredEntities.map((e) => e.id));

  // 2. Filter edges
  const filteredCooccurrences = cooccurrences.filter((c) => {
    if (c.cooccurrence_count < filters.minConnectionStrength) return false;
    return validIds.has(c.entity_id_1) && validIds.has(c.entity_id_2);
  });

  // 3. Layout
  const { nodes: simNodes } = layoutGraph(filteredEntities, filteredCooccurrences);

  // 4. Convert to React Flow format
  const flowNodes: EntityFlowNode[] = simNodes.map((n) => ({
    id: n.id,
    type: 'entity' as const,
    position: { x: n.x ?? 0, y: n.y ?? 0 },
    data: {
      label: n.name,
      type: n.entityType ?? 'Concept',
      mentionCount: n.mentionCount,
      entityId: n.id,
    },
  }));

  const maxCount = Math.max(
    1,
    ...filteredCooccurrences.map((c) => c.cooccurrence_count),
  );

  const flowEdges: Edge[] = filteredCooccurrences.map((c) => ({
    id: `${c.entity_id_1}-${c.entity_id_2}`,
    source: c.entity_id_1,
    target: c.entity_id_2,
    animated: false,
    label: String(c.cooccurrence_count),
    labelStyle: { fill: '#A1A1AA', fontSize: 10 },
    labelBgStyle: { fill: '#1A1A1A' },
    labelBgPadding: [4, 2] as [number, number],
    labelBgBorderRadius: 4,
    interactionWidth: 20,
    style: {
      stroke: 'rgba(255, 255, 255, 0.2)',
      strokeWidth: Math.max(1, (c.cooccurrence_count / maxCount) * 4),
    },
  }));

  return { flowNodes, flowEdges };
}

interface GraphCanvasProps {
  entities: EntityDTO[];
  cooccurrences: CooccurrenceRecord[];
  filters: GraphFilters;
  selectedEntityId: string | null;
  onNodeSelect: (entityId: string) => void;
  onNodeDoubleClick: (entityId: string) => void;
  onPaneClick: () => void;
}

export function GraphCanvas({
  entities,
  cooccurrences,
  filters,
  selectedEntityId,
  onNodeSelect,
  onNodeDoubleClick,
  onPaneClick,
}: GraphCanvasProps) {
  const { flowNodes: initialNodes, flowEdges: initialEdges } = useMemo(
    () => buildFlowElements(entities, cooccurrences, filters),
    [entities, cooccurrences, filters],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // Sync when data, filters, or selection change
  useEffect(() => {
    // Build set of connected node IDs for highlighting
    const connectedIds = new Set<string>();
    if (selectedEntityId) {
      connectedIds.add(selectedEntityId);
      for (const e of initialEdges) {
        if (e.source === selectedEntityId) connectedIds.add(e.target);
        if (e.target === selectedEntityId) connectedIds.add(e.source);
      }
    }

    setNodes(initialNodes.map((n) => ({
      ...n,
      selected: n.id === selectedEntityId,
      style: selectedEntityId && !connectedIds.has(n.id)
        ? { opacity: 0.25 }
        : undefined,
    })));

    setEdges(initialEdges.map((e) => {
      if (!selectedEntityId) return e;
      const isConnected = e.source === selectedEntityId || e.target === selectedEntityId;
      return {
        ...e,
        animated: isConnected,
        style: {
          ...e.style,
          stroke: isConnected ? '#3B82F6' : 'rgba(255, 255, 255, 0.08)',
          strokeWidth: isConnected ? Number(e.style?.strokeWidth ?? 1) * 1.5 : e.style?.strokeWidth,
        },
        labelStyle: {
          ...e.labelStyle,
          opacity: isConnected ? 1 : 0.15,
        },
        labelBgStyle: {
          ...e.labelBgStyle,
          opacity: isConnected ? 1 : 0.15,
        },
      };
    }));
  }, [initialNodes, initialEdges, selectedEntityId, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      onNodeSelect(node.id);
    },
    [onNodeSelect],
  );

  const handleNodeDoubleClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      onNodeDoubleClick(node.id);
    },
    [onNodeDoubleClick],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={handleNodeClick}
      onNodeDoubleClick={handleNodeDoubleClick}
      onPaneClick={onPaneClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.1}
      maxZoom={4}
      proOptions={{ hideAttribution: true }}
      className="rounded-lg"
    >
      <Background color="#262626" gap={20} />
      <Controls
        showInteractive={false}
        className="!bg-card !border-border !shadow-lg [&>button]:!bg-card [&>button]:!border-border [&>button]:!text-foreground [&>button:hover]:!bg-hover"
      />
      <MiniMap
        nodeColor="#3B82F6"
        maskColor="rgba(0, 0, 0, 0.7)"
        className="!bg-card !border-border"
      />
    </ReactFlow>
  );
}
