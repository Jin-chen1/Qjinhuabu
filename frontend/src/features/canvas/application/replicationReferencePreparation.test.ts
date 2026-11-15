import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/api/client';
import { CANVAS_NODE_TYPES } from '../domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import {
  hasGeneratedResult, workflowGenerationTargetsForPreflight,
  applyCanvasChatCommandsAsync, extractCanvasChatCommandEnvelopes, CANVAS_CHAT_COMMANDS_SCHEMA_VERSION,
} from '@/features/freezone/canvasChatCommands';
import { buildCanvasNodeActionCatalog } from '@/features/freezone/canvasNodeActionCatalog';
import { prepareReplicationReference, subscribeReplicationPreparation } from './replicationReferencePreparation';
import { canvasEventBus } from './canvasServices';
import { dispatchNodeAction, subscribeNodeAction, publishNodeActionAccepted, publishNodeActionSuccess } from './nodeActionResult';

vi.mock('@/api/client', async (original) => ({
  ...await original<typeof import('@/api/client')>(),
  apiClient: vi.fn(),
}));

const response = {
  first_frame_url: '/frame.png', audio_url: '/audio.wav',
  duration_ms: 5057, width: 720, height: 1280, source_sha256: 'a'.repeat(64),
};
const json = vi.fn();

function fixture() {
  const store = useCanvasStore.getState();
  const frame = store.addNode(CANVAS_NODE_TYPES.upload, { x: 0, y: 0 }, {});
  const audio = store.addNode(CANVAS_NODE_TYPES.audio, { x: 0, y: 0 }, {});
  const source = store.addNode(CANVAS_NODE_TYPES.video, { x: 0, y: 0 }, {
    videoUrl: '/source.mp4', workflowCatalogRole: 'user_input',
    replicationPreparationTargets: { firstFrameNodeId: frame, audioNodeId: audio },
  });
  return { store, frame, audio, source };
}

describe('replication reference preparation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useCanvasStore.setState({ nodes: [], edges: [] });
    json.mockReset();
    vi.mocked(apiClient).mockReturnValue({ json } as unknown as ReturnType<typeof apiClient>);
    json.mockResolvedValue(response);
  });

  it('does not skip source video; opts into the catalog and default workflow action', () => {
    const { source } = fixture();
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
    expect(buildCanvasNodeActionCatalog(useCanvasStore.getState().nodes.find(n => n.id === source)!).actions
      .some(a => a.action === 'prepare_replication_reference')).toBe(true);
    expect(workflowGenerationTargetsForPreflight({
      type: 'run_workflow', node_ids: [source], direction: 'node',
    })).toEqual([{ nodeId: source, action: 'prepare_replication_reference' }]);
  });

  it('waits for complete response and fills existing nodes, binding cache to input URL', async () => {
    const { source, frame, audio, store } = fixture();
    await prepareReplicationReference('project', 'canvas', source);
    expect(apiClient).toHaveBeenCalledWith('/projects/project/freezone/reference/prepare', expect.objectContaining({
      method: 'POST', json: { video_url: '/source.mp4', canvas_id: 'canvas', node_id: source },
    }));
    const nodes = useCanvasStore.getState().nodes;
    expect(nodes).toHaveLength(3);
    expect(nodes.find(n => n.id === frame)?.data.imageUrl).toBe('/frame.png');
    expect(nodes.find(n => n.id === audio)?.data.audioUrl).toBe('/audio.wav');
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(true);
    store.updateNodeData(source, { videoUrl: '/changed.mp4' });
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
  });

  it.each([
    { ...response, audio_url: null },
    { ...response, width: 0 },
    { ...response, source_sha256: '' },
  ])('rejects incomplete media without partial writes: %j', async (result) => {
    const { source, frame, audio } = fixture();
    json.mockResolvedValue(result);
    await expect(prepareReplicationReference('project', 'canvas', source)).rejects.toThrow();
    expect(useCanvasStore.getState().nodes.find(n => n.id === frame)?.data.imageUrl).toBeFalsy();
    expect(useCanvasStore.getState().nodes.find(n => n.id === audio)?.data.audioUrl).toBeFalsy();
  });

  it.each(['source', 'target', 'binding'] as const)('rejects changed %s after awaiting response', async (change) => {
    const { source, frame, store } = fixture();
    json.mockImplementation(async () => {
      if (change === 'source') store.updateNodeData(source, { videoUrl: '/other.mp4' });
      if (change === 'target') store.updateNodeData(frame, { imageUrl: '/manual.png' });
      if (change === 'binding') store.updateNodeData(source, {
        replicationPreparationTargets: { firstFrameNodeId: 'other', audioNodeId: 'other' },
      });
      return response;
    });
    await expect(prepareReplicationReference('project', 'canvas', source)).rejects.toThrow();
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
  });

  it('preserves legacy video generation and completed URL behavior', () => {
    const id = useCanvasStore.getState().addNode(CANVAS_NODE_TYPES.video, { x: 0, y: 0 }, { videoUrl: '/legacy.mp4' });
    expect(hasGeneratedResult(id, 'generate_video')).toBe(true);
    expect(buildCanvasNodeActionCatalog(useCanvasStore.getState().nodes[0]).actions
      .some(a => a.action === 'prepare_replication_reference')).toBe(false);
  });

  it('finds preparation upstream of a recipe consuming the prebuilt frame, without generating audio', () => {
    const { source, frame, store } = fixture();
    const recipe = store.addNode(CANVAS_NODE_TYPES.textAnnotation, { x: 0, y: 0 }, {
      workflowCatalog: { recipeId: 'analyze' },
    });
    store.addEdge(frame, recipe);
    expect(workflowGenerationTargetsForPreflight({
      type: 'run_workflow', node_ids: [recipe], direction: 'connected',
    })).toEqual(expect.arrayContaining([
      { nodeId: source, action: 'prepare_replication_reference' },
      { nodeId: recipe, action: 'generate_text' },
    ]));
    expect(workflowGenerationTargetsForPreflight({
      type: 'run_workflow', scope: 'canvas',
    }).some(a => a.action === 'generate_audio')).toBe(false);
  });

  it('invalidates downstream recipes on changed source URL', async () => {
    const { source, frame, store } = fixture();
    await prepareReplicationReference('project', 'canvas', source);
    const recipe = store.addNode(CANVAS_NODE_TYPES.textAnnotation, { x: 0, y: 0 }, {
      content: 'old analysis', workflowTextGenerated: true,
      workflowCatalog: { recipeId: 'analyze' },
    });
    store.addEdge(frame, recipe);
    store.updateNodeData(source, { videoUrl: '/new.mp4' });
    expect(useCanvasStore.getState().nodes.find(n => n.id === recipe)?.data.workflowResultStale).toBe(true);
  });

  it.each([false, true])('publishes accepted immediately and only publishes final success/error after response (missing audio=%s)', async (missingAudio) => {
    const { source } = fixture();
    let resolve!: (value: typeof response) => void;
    json.mockReturnValue(new Promise(r => { resolve = r; }));
    const events: string[] = [];
    const offAccepted = canvasEventBus.subscribe('freezone/node-action-accepted', () => events.push('accepted'));
    const offResult = canvasEventBus.subscribe('freezone/node-action-result', event => events.push(event.status));
    const offHandler = subscribeReplicationPreparation(source, () => ({ project: 'project', canvas: 'canvas' }));
    try {
      dispatchNodeAction({ nodeId: source, action: 'prepare_replication_reference', requestId: 'prepare-test' });
      expect(events).toEqual(['accepted']);
      resolve({ ...response, ...(missingAudio ? { audio_url: '' } : {}) });
      await vi.waitFor(() => expect(events).toEqual(['accepted', missingAudio ? 'error' : 'success']));
    } finally {
      offHandler(); offAccepted(); offResult();
    }
  });

  it('executes preparation before downstream recipe even when recipe is first in the node list', async () => {
    const store = useCanvasStore.getState();
    const recipe = store.addNode(CANVAS_NODE_TYPES.textAnnotation, { x: 0, y: 0 }, {
      workflowCatalog: { recipeId: 'analyze' },
    });
    const { source, frame } = fixture();
    store.addEdge(frame, recipe);
    const events: string[] = [];
    const offPrep = subscribeReplicationPreparation(source, () => ({ project: 'project', canvas: 'mock' }));
    const offRecipe = subscribeNodeAction(payload => {
      if (payload.nodeId !== recipe) return;
      expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(true);
      events.push('recipe');
      publishNodeActionAccepted(payload.requestId, recipe, payload.action);
      store.updateNodeData(recipe, { content: 'analysis', workflowTextGenerated: true });
      publishNodeActionSuccess(payload.requestId, recipe, payload.action, { content: 'analysis' });
    });
    json.mockImplementation(async () => { events.push('prepare'); return response; });
    try {
      const result = await applyCanvasChatCommandsAsync(extractCanvasChatCommandEnvelopes([{
        schema_version: CANVAS_CHAT_COMMANDS_SCHEMA_VERSION, commands: [{
          type: 'run_workflow', node_ids: [recipe, source], direction: 'node',
        }],
      }]), { canvasId: 'mock', actionTimeoutMs: 2000, actionAcceptTimeoutMs: 100 });
      expect(result.errors).toEqual([]);
      expect(events).toEqual(['prepare', 'recipe']);
    } finally { offPrep(); offRecipe(); }
  });

  it('invalidates a completed recipe when its catalog changes, preserving legacy nodes', () => {
    const { source, frame, store } = fixture();
    const recipe = store.addNode(CANVAS_NODE_TYPES.textAnnotation, { x: 0, y: 0 }, {
      workflowCatalog: { recipeId: 'old' }, workflowTextGenerated: true, content: 'analysis',
    });
    const legacy = store.addNode(CANVAS_NODE_TYPES.video, { x: 0, y: 0 }, { videoUrl: '/legacy.mp4' });
    store.addEdge(frame, recipe);
    store.updateNodeData(recipe, { workflowCatalog: { recipeId: 'new' } });
    expect(hasGeneratedResult(recipe, 'generate_text')).toBe(false);
    expect(hasGeneratedResult(legacy, 'generate_video')).toBe(true);
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
  });

  it('does not resurrect prior prepared state after a missing-audio error', async () => {
    const { source } = fixture();
    await prepareReplicationReference('project', 'canvas', source);
    json.mockResolvedValue({ ...response, audio_url: null });
    await expect(prepareReplicationReference('project', 'canvas', source)).rejects.toThrow('音轨');
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
  });

  it('blocks downstream execution when original audio is missing', async () => {
    const { source, frame, store } = fixture();
    const recipe = store.addNode(CANVAS_NODE_TYPES.textAnnotation, { x: 0, y: 0 }, {
      workflowCatalog: { recipeId: 'analyze' },
    });
    store.addEdge(frame, recipe);
    json.mockResolvedValue({ ...response, audio_url: null });
    const offPrep = subscribeReplicationPreparation(source, () => ({ project: 'project', canvas: 'no-audio' }));
    const recipeAction = vi.fn();
    const offRecipe = subscribeNodeAction(payload => { if (payload.nodeId === recipe) recipeAction(); });
    try {
      const result = await applyCanvasChatCommandsAsync(extractCanvasChatCommandEnvelopes([{
        schema_version: CANVAS_CHAT_COMMANDS_SCHEMA_VERSION, commands: [{
          type: 'run_workflow', node_ids: [recipe, source], direction: 'node',
        }],
      }]), { canvasId: 'no-audio', actionTimeoutMs: 2000, actionAcceptTimeoutMs: 100 });
      expect(result.errors.join(' ')).toContain('音轨');
      expect(recipeAction).not.toHaveBeenCalled();
    } finally { offPrep(); offRecipe(); }
  });

  it('refuses deleted or wrong-type targets before making a request', async () => {
    const { source, frame } = fixture();
    useCanvasStore.setState(state => ({
      nodes: state.nodes.filter(n => n.id !== frame),
    }));
    await expect(prepareReplicationReference('project', 'canvas', source)).rejects.toThrow('预建');
    expect(apiClient).not.toHaveBeenCalled();
  });

  it('runs only the explicitly scoped opt-in source even when it belongs to a user-input group', () => {
    const { source, store } = fixture();
    const other = store.addNode(CANVAS_NODE_TYPES.imageGen, { x: 0, y: 0 }, { prompt: 'other' });
    store.groupNodes([source, other], { label: 'group' });
    expect(workflowGenerationTargetsForPreflight({
      type: 'run_workflow', node_ids: [source], direction: 'node',
    })).toEqual([{ nodeId: source, action: 'prepare_replication_reference' }]);
  });

  it('rejects an incomplete persisted prepared record instead of skipping', async () => {
    const { source, store } = fixture();
    await prepareReplicationReference('project', 'canvas', source);
    const prepared = useCanvasStore.getState().nodes.find(n => n.id === source)!.data.replicationPrepared;
    store.updateNodeData(source, { replicationPrepared: { ...prepared as object, source_sha256: '' } });
    expect(hasGeneratedResult(source, 'prepare_replication_reference')).toBe(false);
  });
});
