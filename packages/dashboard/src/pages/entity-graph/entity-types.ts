import type { Node } from '@xyflow/react';

export const ENTITY_TYPES = ['Person', 'Organization', 'Location', 'Concept', 'Technology', 'File', 'Misc'] as const;

export const TYPE_COLORS: Record<string, string> = {
  Person: '#3B82F6',
  Organization: '#A855F7',
  Location: '#22C55E',
  Concept: '#F59E0B',
  Technology: '#06B6D4',
  File: '#EF4444',
  Misc: '#71717A',
};

export interface EntityNodeData {
  label: string;
  type: string;
  mentionCount: number;
  entityId: string;
  [key: string]: unknown;
}

export type EntityFlowNode = Node<EntityNodeData, 'entity'>;
