import { CANVAS_NODE_TYPES, type CanvasNode, type CanvasEdge } from './canvasNodes';

export const PREPARE_REPLICATION_REFERENCE = 'prepare_replication_reference';

export function replicationTargets(node: CanvasNode | undefined | null) {
  if (node?.type !== CANVAS_NODE_TYPES.video) return null;
  const value = node.data.replicationPreparationTargets;
  if (!value || typeof value !== 'object') return null;
  const targets = value as Record<string, unknown>;
  return typeof targets.firstFrameNodeId === 'string' && targets.firstFrameNodeId.trim()
    && typeof targets.audioNodeId === 'string' && targets.audioNodeId.trim()
    && targets.firstFrameNodeId !== targets.audioNodeId
    && targets.firstFrameNodeId !== node.id && targets.audioNodeId !== node.id
    ? { firstFrameNodeId: targets.firstFrameNodeId, audioNodeId: targets.audioNodeId }
    : null;
}

/** Execution-only dependencies; these do not become media inputs. */
export function replicationWorkflowEdges(nodes: CanvasNode[], edges: CanvasEdge[]): CanvasEdge[] {
  return [...edges, ...nodes.flatMap(node => {
    const targets = replicationTargets(node);
    return targets ? [targets.firstFrameNodeId, targets.audioNodeId].map(target => ({
      id: `replication:${node.id}:${target}`, source: node.id, target,
    })) : [];
  })];
}

export function hasPreparedReplicationReference(node: CanvasNode | undefined | null, nodes: CanvasNode[]): boolean {
  const targets = replicationTargets(node);
  if (!node || !targets) return false;
  const prepared = node.data.replicationPrepared as Record<string, unknown> | undefined;
  const frame = nodes.find(n => n.id === targets.firstFrameNodeId);
  const audio = nodes.find(n => n.id === targets.audioNodeId);
  return Boolean(prepared && prepared.videoUrl === node.data.videoUrl
    && typeof prepared.source_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(prepared.source_sha256)
    && [prepared.duration_ms, prepared.width, prepared.height].every(v => typeof v === 'number' && Number.isFinite(v) && v > 0)
    && prepared.firstFrameNodeId === targets.firstFrameNodeId
    && prepared.audioNodeId === targets.audioNodeId
    && frame?.type === CANVAS_NODE_TYPES.upload && audio?.type === CANVAS_NODE_TYPES.audio
    && prepared.first_frame_url && prepared.audio_url
    && frame.data.imageUrl === prepared.first_frame_url && audio.data.audioUrl === prepared.audio_url
    && frame.data.workflowResultStale !== true && audio.data.workflowResultStale !== true);
}

export function invalidateReplicationResults(
  previous: CanvasNode[], next: CanvasNode[], edges: CanvasEdge[], nodeId: string,
  patch: Record<string, unknown>,
): CanvasNode[] {
  const graph = replicationWorkflowEdges(previous, edges);
  const descendants = (seeds: string[]) => {
    const ids = new Set(seeds);
    const queue = [...seeds];
    while (queue.length) {
      const sourceId = queue.shift();
      for (const edge of graph.filter(e => e.source === sourceId)) {
        if (!ids.has(edge.target)) { ids.add(edge.target); queue.push(edge.target); }
      }
    }
    return ids;
  };
  const scope = descendants(previous.filter(n => replicationTargets(n)).map(n => n.id));
  if (!scope.has(nodeId)) return next;
  const before = previous.find(n => n.id === nodeId);
  const source = Boolean(replicationTargets(before));
  const keys = source ? ['videoUrl', 'replicationPreparationTargets', 'workflowCatalog']
    : ['prompt', 'content', 'text', 'workflowCatalog', 'imageUrl', 'audioUrl', 'videoUrl'];
  if (!keys.some(key => key in patch && !Object.is(before?.data[key], patch[key]))) return next;
  const affected = descendants([nodeId]);
  const inputChanged = source || ['prompt', 'text', 'workflowCatalog'].some(key => key in patch);
  if (!inputChanged) affected.delete(nodeId);
  return next.map(node => affected.has(node.id) ? {
    ...node, data: {
      ...node.data, workflowResultStale: true,
      workflowInvalidationReason: node.id === nodeId ? 'node_input_changed' : 'upstream_input_changed',
    },
  } : node);
}
