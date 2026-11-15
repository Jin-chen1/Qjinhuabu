// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { act, render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReactFlowProvider, type NodeProps } from '@xyflow/react';
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import { CANVAS_NODE_TYPES, type ScriptNodeData } from '@/features/canvas/domain/canvasNodes';
import { ScriptNode } from '@/features/canvas/nodes/ScriptNode';
import { useCanvasStore } from '@/stores/canvasStore';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { dispatchNodeAction } from '@/features/canvas/application/nodeActionResult';
import { submitFreezoneStoryScript, fetchFreezoneStoryScriptResult } from '@/api/ops';
import { awaitTaskCompletion } from '@/api/tasks';

vi.mock('@/api/ops', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/ops')>(),
  submitFreezoneStoryScript: vi.fn(),
  fetchFreezoneStoryScriptResult: vi.fn(),
}));
vi.mock('@/api/tasks', () => ({ awaitTaskCompletion: vi.fn() }));
vi.mock('@/features/canvas/hooks/useNodeGenerationHistory', () => ({
  useNodeGenerationHistory: () => ({ records: [], isLoading: false, refresh: vi.fn() }),
}));

const oldScript = { title: 'Old script', rows: [{ shot_no: 1, visual_description: 'Old shot' }] };
const newScript = { title: 'New script', rows: [{ shot_no: 1, visual_description: 'New shot' }] };

beforeAll(() => vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
}));
afterAll(() => vi.unstubAllGlobals());
afterEach(() => vi.restoreAllMocks());
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  window.history.replaceState({}, '', '/projects/test-project/freezone?canvas=test-canvas');
  useCanvasStore.setState({
    nodes: [{
      id: 'script', type: CANVAS_NODE_TYPES.script, position: { x: 0, y: 0 },
      data: {
        prompt: 'Create a new shot', scriptResult: oldScript, scriptTitle: oldScript.title,
        scriptResultStale: true, workflowResultStale: true,
      },
    }],
    edges: [], selectedNodeId: null,
  });
  vi.mocked(submitFreezoneStoryScript).mockResolvedValue({
    job_id: 'job', task_key: 'task', task_type: 'freezone_story_script',
  } as Awaited<ReturnType<typeof submitFreezoneStoryScript>>);
  vi.mocked(awaitTaskCompletion).mockResolvedValue(undefined as never);
  vi.mocked(fetchFreezoneStoryScriptResult).mockResolvedValue(newScript);
});

function StoredScript() {
  const data = useCanvasStore((state) => state.nodes[0].data) as ScriptNodeData;
  return <ScriptNode {...({ id: 'script', type: CANVAS_NODE_TYPES.script, data } as NodeProps & { data: ScriptNodeData })} />;
}

function setup() {
  const accepted = vi.fn();
  const result = vi.fn();
  const unsubscribeAccepted = canvasEventBus.subscribe('freezone/node-action-accepted', accepted);
  const unsubscribeResult = canvasEventBus.subscribe('freezone/node-action-result', result);
  const view = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ReactFlowProvider><StoredScript /></ReactFlowProvider>
    </QueryClientProvider>,
  );
  return { accepted, result, cleanup: () => {
    view.unmount();
    unsubscribeAccepted();
    unsubscribeResult();
  } };
}

function dispatch(nodeId = 'script', action = 'generate_story_script', requestId: string | null = 'request') {
  act(() => { dispatchNodeAction({ nodeId, action, requestId: requestId ?? undefined, executionMode: 'single' }); });
}

it('accepts once and reports the actual fresh script only after task completion', async () => {
  let complete!: () => void;
  vi.mocked(awaitTaskCompletion).mockImplementation(() => new Promise((resolve) => {
    complete = () => resolve(undefined as never);
  }));
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.accepted).toHaveBeenCalledTimes(1));
    expect(events.result).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
    await act(async () => { complete(); });
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({
      requestId: 'request', nodeId: 'script', action: 'generate_story_script', status: 'success',
      output: expect.objectContaining({ scriptResult: newScript, scriptTitle: newScript.title }),
    })));
    expect(events.result).toHaveBeenCalledTimes(1);
    expect(submitFreezoneStoryScript).toHaveBeenCalledTimes(1);
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(newScript);
    expect(useCanvasStore.getState().nodes[0].data.scriptResultStale).toBe(false);
    expect(useCanvasStore.getState().nodes[0].data.workflowResultStale).toBe(false);
  } finally { events.cleanup(); }
});

it.each([
  ['empty', { title: 'Empty', rows: [] }],
])('fails a %s result without replacing the previous script', async (_name, script) => {
  vi.mocked(fetchFreezoneStoryScriptResult).mockResolvedValue(script);
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({
      status: 'error', error: expect.any(String),
    })));
    expect(events.accepted).toHaveBeenCalledTimes(1);
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
    expect(useCanvasStore.getState().nodes[0].data.scriptTitle).toBe(oldScript.title);
    expect(useCanvasStore.getState().nodes[0].data.scriptResultStale).toBe(true);
    expect(useCanvasStore.getState().nodes[0].data.workflowResultStale).toBe(true);
  } finally { events.cleanup(); }
});

it('accepts identical valid content from a completed new task and clears stale flags', async () => {
  vi.mocked(fetchFreezoneStoryScriptResult).mockResolvedValue(structuredClone(oldScript));
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({
      status: 'success', output: expect.objectContaining({ scriptResult: oldScript }),
    })));
    expect(submitFreezoneStoryScript).toHaveBeenCalledTimes(1);
    expect(awaitTaskCompletion).toHaveBeenCalledTimes(1);
    expect(useCanvasStore.getState().nodes[0].data.scriptResultStale).toBe(false);
    expect(useCanvasStore.getState().nodes[0].data.workflowResultStale).toBe(false);
  } finally { events.cleanup(); }
});

it('reports a backend failure and preserves the previous script', async () => {
  vi.mocked(submitFreezoneStoryScript).mockRejectedValue(new Error('Generation failed'));
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({ status: 'error' })));
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
  } finally { events.cleanup(); }
});

it('fails missing input rather than reporting the existing script as success', async () => {
  useCanvasStore.getState().updateNodeData('script', { prompt: '' });
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({ status: 'error' })));
    expect(submitFreezoneStoryScript).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
  } finally { events.cleanup(); }
});

it('ignores actions for other nodes or actions and unsubscribes on unmount', async () => {
  const events = setup();
  dispatch('other', 'generate_story_script', null);
  dispatch('script', 'generate_video', null);
  await act(async () => {});
  expect(events.accepted).not.toHaveBeenCalled();
  expect(submitFreezoneStoryScript).not.toHaveBeenCalled();
  events.cleanup();
  dispatch('script', 'generate_story_script', null);
  expect(submitFreezoneStoryScript).not.toHaveBeenCalled();
});

it('reports missing project without submitting or discarding the old script', async () => {
  window.history.replaceState({}, '', '/');
  const events = setup();
  try {
    dispatch();
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({ status: 'error' })));
    expect(submitFreezoneStoryScript).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
  } finally { events.cleanup(); }
});

it('rejects an overlapping request without submitting twice or claiming the running result', async () => {
  let complete!: () => void;
  vi.mocked(awaitTaskCompletion).mockImplementation(() => new Promise((resolve) => {
    complete = () => resolve(undefined as never);
  }));
  const events = setup();
  try {
    dispatch();
    dispatch('script', 'generate_story_script', 'overlap');
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({
      requestId: 'overlap', status: 'error',
    })));
    expect(submitFreezoneStoryScript).toHaveBeenCalledTimes(1);
    expect(useCanvasStore.getState().nodes[0].data.scriptResult).toEqual(oldScript);
    await act(async () => { complete(); });
    await waitFor(() => expect(events.result).toHaveBeenCalledWith(expect.objectContaining({
      requestId: 'request', status: 'success',
    })));
    expect(events.accepted).toHaveBeenCalledTimes(2);
    expect(events.result).toHaveBeenCalledTimes(2);
  } finally { events.cleanup(); }
});
