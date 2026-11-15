import { describe, expect, it } from 'vitest';
import { invalidateSkillWorkflowResults, skillWorkflowTextGate, skillWorkflowUpstreamIssue } from './skillWorkflowGate';
import { deterministicNodeOutputIssue } from './workflowQualityGate';
import { workflowGroupDescendants, workflowGroupState } from '../canvas/domain/workflowGroupState';

const data = { workflowCatalog: { skillId: 'viral-video-oneclick', gateRequired: true } };

function upstreamIssue(upstreamData: Record<string, unknown>): string | null {
  const nodes = [
    { id: 'image-review', type: 'textAnnotationNode', position: { x: 0, y: 0 },
      data: upstreamData },
    { id: 'video', type: 'videoNode', position: { x: 0, y: 0 }, data },
  ];
  return skillWorkflowUpstreamIssue('video', nodes as never, [
    { id: 'dependency', source: 'image-review', target: 'video' },
  ] as never);
}

describe('skill workflow gates', () => {
  it('requires a current explicit stage result, not a passing word in historical evidence', () => {
    expect(skillWorkflowTextGate(data, '历史证据：通过\n当前缺少台词')?.status).toBe('paused');
    expect(skillWorkflowTextGate(data, '运行结论：暂停\n缺少真实逐字稿')?.status).toBe('paused');
    expect(skillWorkflowTextGate(data, '运行结论：通过\n证据齐全')?.status).toBe('passed');
    expect(skillWorkflowTextGate(data, '运行结论：待核验\n有候选，无实际台词回比')?.status).toBe('awaiting_evidence');
    expect(skillWorkflowTextGate({}, '普通文本')).toBeNull();
  });

  it('blocks downstream even in a new run where an upstream output already exists', () => {
    const upstream = {
      id: 'intake', type: 'textAnnotationNode', position: { x: 0, y: 0 },
      data: { ...data, content: '运行结论：暂停\n当前通道不支持参考视频。',
              workflowTextGenerated: true,
              workflowGate: { status: 'paused', reason: '当前通道不支持参考视频。' } },
    };
    const target = { id: 'render', type: 'videoNode', position: { x: 0, y: 0 }, data };
    const edges = [{ id: 'dependency', source: 'intake', target: 'render' }];
    expect(skillWorkflowUpstreamIssue('render', [upstream, target] as never, edges as never))
      .toContain('当前通道不支持参考视频');
    upstream.data.workflowGate.status = 'passed';
    expect(skillWorkflowUpstreamIssue('render', [upstream, target] as never, edges as never)).toBeNull();
  });

  it.each([
    { name: 'null gate', patch: { workflowGate: null }, reason: '缺少有效通过结论' },
    { name: 'missing gate', patch: { workflowGate: undefined }, reason: '缺少有效通过结论' },
    { name: 'ungenerated output', patch: { workflowTextGenerated: false }, reason: '尚未生成' },
    { name: 'missing generated marker', patch: { workflowTextGenerated: undefined }, reason: '尚未生成' },
    { name: 'truthy generated marker', patch: { workflowTextGenerated: 'true' }, reason: '尚未生成' },
    { name: 'stale passed result', patch: { workflowResultStale: true }, reason: '已失效' },
    { name: 'unknown gate status', patch: { workflowGate: { status: 'pending' } }, reason: '缺少有效通过结论' },
    { name: 'awaiting evidence', patch: { workflowGate: {
      status: 'awaiting_evidence', reason: '尚未核验商品一致性',
    } }, reason: '上游阶段待核验：尚未核验商品一致性' },
  ])('blocks $name despite saved output and task receipt', ({ patch, reason }) => {
    expect(upstreamIssue({
      ...data,
      content: '运行结论：通过\n历史核验记录',
      workflowTextGenerated: true,
      workflowGate: { status: 'passed', reason: '历史证据齐全' },
      generationTaskJobId: 'saved-receipt',
      imageUrl: '/candidate.png',
      imageNaturalWidth: 2048,
      imageNaturalHeight: 2048,
      ...patch,
    })).toContain(reason);
  });

  it.each([undefined, false])('accepts a current passed result with stale marker %s', (stale) => {
    expect(upstreamIssue({
      ...data,
      workflowTextGenerated: true,
      workflowResultStale: stale,
      workflowGate: { status: 'passed', reason: '当前证据核验通过' },
    })).toBeNull();
  });

  it('enforces gateRequired even when the catalog has no skillId', () => {
    expect(upstreamIssue({
      workflowCatalog: { gateRequired: true },
      workflowTextGenerated: true,
      workflowGate: null,
    })).toContain('缺少有效通过结论');
  });

  it.each([
    {},
    { workflowCatalog: { skillId: 'ordinary-skill' } },
    { workflowCatalog: { skillId: 'ordinary-skill', gateRequired: false } },
  ])('preserves ungated upstream behavior for %j', (catalogData) => {
    expect(upstreamIssue({
      ...catalogData, content: '普通上游输出', workflowGate: null,
      workflowTextGenerated: false, workflowResultStale: true,
    })).toBeNull();
    expect(upstreamIssue({
      ...catalogData, workflowGate: { status: 'paused', reason: '原有暂停原因' },
    })).toBe('上游阶段暂停：原有暂停原因');
    expect(upstreamIssue({
      ...catalogData, workflowGate: { status: 'awaiting_evidence', reason: '原有待核验原因' },
    })).toBe('上游阶段暂停：原有待核验原因');
  });

  it('checks image review through an intermediate stage and terminates on cycles', () => {
    const nodes = [
      { id: 'image-review', type: 'textAnnotationNode', position: { x: 0, y: 0 },
        data: { ...data, workflowTextGenerated: true, workflowGate: null } },
      { id: 'plan', type: 'textAnnotationNode', position: { x: 0, y: 0 },
        data: { ...data, workflowTextGenerated: true, workflowGate: { status: 'passed' } } },
      { id: 'video', type: 'videoNode', position: { x: 0, y: 0 }, data },
    ];
    const edges = [
      { id: 'review-plan', source: 'image-review', target: 'plan' },
      { id: 'plan-video', source: 'plan', target: 'video' },
      { id: 'cycle', source: 'video', target: 'image-review' },
    ];
    expect(skillWorkflowUpstreamIssue('video', nodes as never, edges as never))
      .toContain('缺少有效通过结论');
  });

  it('does not bypass a stage gate for a saved task receipt', () => {
    expect(deterministicNodeOutputIssue('generate_text', {
      ...data, workflowGate: { status: 'paused', reason: '商品图未核实' },
      generationTaskJobId: 'old-receipt',
    })).toContain('商品图未核实');
  });

  it('invalidates all dependent stages after a product input changes', () => {
    const nodes = [
      { id: 'product', type: 'uploadImageNode', position: { x: 0, y: 0 },
        data: { skillWorkflowInput: true, imageUrl: 'old.png' } },
      { id: 'intake', type: 'textAnnotationNode', position: { x: 0, y: 0 },
        data: { ...data, workflowTextGenerated: true, workflowGate: { status: 'passed' } } },
      { id: 'video', type: 'videoNode', position: { x: 0, y: 0 },
        data: { ...data, videoUrl: 'previous.mp4' } },
    ] as never;
    const changed = invalidateSkillWorkflowResults(nodes, nodes, [
      { id: 'a', source: 'product', target: 'intake' },
      { id: 'b', source: 'intake', target: 'video' },
    ] as never, 'product', { imageUrl: 'new.png' });
    expect(changed[1].data.workflowTextGenerated).toBe(false);
    expect(changed[1].data.workflowGate).toBeNull();
    expect(changed[2].data.workflowResultStale).toBe(true);
    expect(changed[2].data.videoUrl).toBe('previous.mp4');
  });

  it('exposes the run button for nested stage groups and excludes input nodes', () => {
    const nodes = [
      { id: 'outer', type: 'groupNode', data: {}, position: { x: 0, y: 0 } },
      { id: 'inner', type: 'groupNode', parentId: 'outer', data: {}, position: { x: 0, y: 0 } },
      { id: 'input', type: 'textAnnotationNode', parentId: 'inner',
        data: { workflowCatalog: { stepId: 'input' }, workflowCatalogRole: 'user_input' },
        position: { x: 0, y: 0 } },
      { id: 'intake', type: 'textAnnotationNode', parentId: 'inner',
        data: { workflowCatalog: { recipeId: 'viral-video-intake', skillId: 'viral-video-oneclick' } },
        position: { x: 0, y: 0 } },
    ] as never;
    const members = workflowGroupDescendants(nodes, 'outer');
    expect(workflowGroupState(members).totalCount).toBe(1);
    expect(workflowGroupState(members).canContinue).toBe(true);
  });
});
