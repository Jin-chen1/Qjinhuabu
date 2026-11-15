import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';

type RecordData = Record<string, unknown>;
export type SkillWorkflowGate = {
  status: 'passed' | 'paused' | 'awaiting_evidence';
  reason: string;
};

function record(value: unknown): RecordData {
  return value && typeof value === 'object' ? value as RecordData : {};
}

export function skillWorkflowTextGate(data: unknown, content: string): SkillWorkflowGate | null {
  if (record(record(data).workflowCatalog).gateRequired !== true) return null;
  const lines = content.trim().split(/\r?\n/);
  const conclusion = lines[0]?.replace(/[*#]/g, '').trim();
  const match = /^运行结论[：:]\s*(通过|暂停|待核验)\s*$/.exec(conclusion ?? '');
  if (!match) return { status: 'paused', reason: '配方未返回首行运行结论，请核验当前阶段证据。' };
  return {
    status: match[1] === '通过' ? 'passed' : match[1] === '暂停' ? 'paused' : 'awaiting_evidence',
    reason: lines.slice(1).join('\n').trim() || conclusion,
  };
}

export function skillWorkflowUpstreamIssue(
  nodeId: string, nodes: CanvasNode[], edges: CanvasEdge[],
): string | null {
  const visited = new Set([nodeId]);
  const pending = [nodeId];
  while (pending.length) {
    const target = pending.pop();
    for (const edge of edges.filter((item) => item.target === target)) {
      if (visited.has(edge.source)) continue;
      visited.add(edge.source);
      pending.push(edge.source);
      const data = record(nodes.find((node) => node.id === edge.source)?.data);
      const gate = record(data.workflowGate);
      const gateRequired = record(data.workflowCatalog).gateRequired === true;
      if (gate.status === 'paused' || gate.status === 'awaiting_evidence') {
        if (gateRequired && gate.status === 'awaiting_evidence') {
          return `上游阶段待核验：${String(gate.reason || '请核验阶段证据')}`;
        }
        return `上游阶段暂停：${String(gate.reason || '请核验阶段证据')}`;
      }
      if (gateRequired) {
        if (data.workflowResultStale === true) {
          return '上游阶段结果已失效，请重新生成并核验当前阶段证据。';
        }
        if (data.workflowTextGenerated !== true) {
          return '上游阶段尚未生成当前结果，请先生成并核验当前阶段证据。';
        }
        if (gate.status !== 'passed') {
          return '上游阶段缺少有效通过结论，请核验当前阶段证据。';
        }
      }
    }
  }
  return null;
}

/** Only configured skill inputs/outputs invalidate dependent stage evidence. */
export function invalidateSkillWorkflowResults(
  before: CanvasNode[], after: CanvasNode[], edges: CanvasEdge[],
  nodeId: string, patch: RecordData,
): CanvasNode[] {
  const source = before.find((node) => node.id === nodeId);
  const data = record(source?.data);
  if (!record(data.workflowCatalog).skillId && data.skillWorkflowInput !== true) return after;
  const inputKeys = ['content', 'prompt', 'imageUrl', 'videoUrl', 'audioUrl', 'workflowCatalog'];
  if (!inputKeys.some((key) => key in patch && !Object.is(data[key], patch[key]))) return after;
  const dirty = new Set<string>();
  const pending = [nodeId];
  // A user's instruction edit invalidates the edited stage as well.
  if ('prompt' in patch || 'workflowCatalog' in patch ||
      ('content' in patch && !('workflowTextGenerated' in patch))) dirty.add(nodeId);
  while (pending.length) {
    const target = pending.pop();
    for (const edge of edges.filter((item) => item.source === target)) {
      if (dirty.has(edge.target)) continue;
      dirty.add(edge.target);
      pending.push(edge.target);
    }
  }
  return after.map((node) => dirty.has(node.id) && record(record(node.data).workflowCatalog).skillId
    ? { ...node, data: { ...node.data, workflowResultStale: true, workflowTextGenerated: false,
        workflowGate: null, workflowGeneratedAt: null } } as CanvasNode
    : node);
}
