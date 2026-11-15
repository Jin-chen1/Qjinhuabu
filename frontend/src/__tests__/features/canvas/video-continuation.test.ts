import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { captureVideoFrameToNode } from '@/features/canvas/application/videoCaptureFrame';
import {
  connectVideoContinuation,
  isVideoContinuationConnection,
  missingVideoKeyframeSlots,
} from '@/features/canvas/application/videoContinuation';

vi.mock('@/features/canvas/application/videoCaptureFrame', () => ({
  resolveCaptureSeekSec: () => 4.95,
  captureVideoFrameToNode: vi.fn(async (_sourceId, opts) => {
    const state = useCanvasStore.getState();
    const nodeId = opts.targetImageNodeId
      ? (state.convertNodeType(opts.targetImageNodeId, CANVAS_NODE_TYPES.exportImage,
          { imageUrl: '/static/tail.png' }) ? opts.targetImageNodeId : null)
      : state.addDerivedExportNode('source', '/static/tail.png', '9:16');
    state.addEdge('source', nodeId!);
    return { nodeId, error: null };
  }),
}));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'project', canvas: 'canvas' }) }));

const video = (id: string, data = {}): CanvasNode => ({
  id, type: CANVAS_NODE_TYPES.video, position: { x: id === 'source' ? 0 : 900, y: 0 },
  data,
} as CanvasNode);

describe('video continuation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useCanvasStore.getState().setCanvasData([
      video('source', { videoUrl: '/static/source.mp4', durationMs: 5000 }),
      video('target'),
    ], []);
  });

  it('connects the previous tail frame into the existing target, with source provenance', async () => {
    await connectVideoContinuation('source', 'target');
    const { nodes, edges } = useCanvasStore.getState();
    const frame = nodes.find(n => n.type === CANVAS_NODE_TYPES.exportImage)!;
    expect(nodes.filter(n => n.type === CANVAS_NODE_TYPES.video)).toHaveLength(2);
    expect(edges).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'source', target: frame.id }),
      expect.objectContaining({ source: frame.id, target: 'target', data: expect.objectContaining({ keyframeSlot: 'first' }) }),
    ]));
    expect(nodes.find(n => n.id === 'target')?.data.genMode).toBe('firstFrame');
    expect(missingVideoKeyframeSlots(nodes, edges, 'target')).toEqual(['last']);
  });

  it('repeated linking reuses the first-frame input', async () => {
    await connectVideoContinuation('source', 'target');
    await connectVideoContinuation('source', 'target');
    expect(captureVideoFrameToNode).toHaveBeenCalledTimes(1);
  });

  it('preserves first-last-frame mode and only asks for the missing tail input', async () => {
    useCanvasStore.getState().updateNodeData('target', { genMode: 'firstLastFrame' });
    await connectVideoContinuation('source', 'target');
    expect(useCanvasStore.getState().nodes.find(n => n.id === 'target')?.data.genMode).toBe('firstLastFrame');
  });

  it('leaves video editing and populated targets on the video-reference path', () => {
    const source = video('source', { videoUrl: '/static/source.mp4' });
    expect(isVideoContinuationConnection(source, video('target', { genMode: 'videoEdit' }))).toBe(false);
    expect(isVideoContinuationConnection(source, video('target', { videoUrl: '/static/result.mp4' }))).toBe(false);
  });

  it('does not silently create blank inputs when capture fails', async () => {
    vi.mocked(captureVideoFrameToNode).mockResolvedValueOnce({ nodeId: null, error: 'capture failed' });
    await expect(connectVideoContinuation('source', 'target')).rejects.toThrow('capture failed');
    expect(useCanvasStore.getState().nodes).toHaveLength(2);
    expect(useCanvasStore.getState().edges).toHaveLength(0);
  });

  it('fills the existing empty first-frame slot without creating another group or input', async () => {
    const state = useCanvasStore.getState();
    const firstId = state.addNode(CANVAS_NODE_TYPES.upload, { x: 400, y: 0 }, {});
    const lastId = state.addNode(CANVAS_NODE_TYPES.upload, { x: 400, y: 400 }, {});
    state.addEdgeWithData(firstId, 'target', { keyframeSlot: 'first' });
    state.addEdgeWithData(lastId, 'target', { keyframeSlot: 'last' });
    state.updateNodeData('target', { genMode: 'firstLastFrame' });
    await connectVideoContinuation('source', 'target');
    const done = useCanvasStore.getState();
    expect(done.nodes).toHaveLength(4);
    expect(done.nodes.find(node => node.id === firstId)?.data.imageUrl).toBe('/static/tail.png');
    expect(done.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'source', target: firstId }),
      expect.objectContaining({ source: firstId, target: 'target' }),
    ]));
    expect(missingVideoKeyframeSlots(done.nodes, done.edges, 'target')).toEqual([]);
  });
});
