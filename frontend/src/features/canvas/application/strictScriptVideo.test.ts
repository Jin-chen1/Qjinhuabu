import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { readStrictScriptVideoPrompt } from './strictScriptVideo';

const source = (data: Record<string, unknown> = {}) => ({
  id: 'script',
  type: 'scriptNode',
  data: {
    scriptResult: { rows: [{
      visual_description: '  Visual\nexact  ',
      shot_prompt: 'Shot',
      video_motion_prompt: 'Move slowly',
      dialogue: '"Do not change me."',
      reference: 'https://image.example/first.png',
    }] },
    ...data,
  },
});

describe('strict script video prompt', () => {
  it('wires the rendered node form to the strict-aware handler, bypassing recipe and camera inputs', () => {
    const nodeSource = readFileSync('src/features/canvas/nodes/VideoNode.tsx', 'utf8');
    expect(nodeSource).toContain('onSubmit={handleSubmit}');
    expect(nodeSource).toContain('const strictPrompt = readStrictScriptVideoPrompt(');
    expect(nodeSource).toContain('upstreamNodesInEdgeOrder(freshState.nodes, freshState.edges, id)');
    expect(nodeSource).toContain('strictPrompt ?? await compileWorkflowNodePrompt');
    expect(nodeSource).toContain('strictPrompt !== null ? null : cameraMovementId');
    expect(nodeSource).toContain('strictPrompt !== null ? null : sceneOptimize ?? null');
    expect(nodeSource).not.toContain('useVideoGenerationForm(');
  });
  it('leaves legacy nodes opt-out', () => {
    expect(readStrictScriptVideoPrompt({}, [])).toBeNull();
  });

  it('preserves visual, shot, motion and dialogue without embedding image URLs', () => {
    const prompt = readStrictScriptVideoPrompt(
      { strictScriptSourceNodeId: 'script', prompt: 'OLD INPUT' }, [source()],
    );
    expect(prompt).toBe('visual_description:   Visual\nexact  \nshot_prompt: Shot\nvideo_motion_prompt: Move slowly\ndialogue: "Do not change me."');
  });

  it('reads updated rows on each call, never a cached prompt', () => {
    const node = source();
    node.data.scriptResult.rows[0].dialogue = 'Fresh dialogue';
    expect(readStrictScriptVideoPrompt({ strictScriptSourceNodeId: 'script' }, [node]))
      .toContain('dialogue: Fresh dialogue');
  });

  it.each([
    { isGenerating: true },
    { generationError: 'failed' },
    { scriptResultStale: true },
    { syncStatus: 'stale' },
    { syncStatus: 'syncing' },
    { syncStatus: 'error' },
    { scriptResult: null },
    { scriptResult: { rows: [] } },
    { scriptResult: { rows: [{ reference: 'image', dialogue: '   ' }] } },
    { scriptResult: { rows: [{ visual_description: 'valid' }, null] } },
  ])('fails closed for unusable source %j', (data) => {
    expect(() => readStrictScriptVideoPrompt(
      { strictScriptSourceNodeId: 'script' }, [source(data)],
    )).toThrow();
  });

  it('rejects missing, disconnected, wrong-type and malformed source selection', () => {
    for (const nodes of [[], [{ ...source(), type: 'textAnnotation' }]]) {
      expect(() => readStrictScriptVideoPrompt({ strictScriptSourceNodeId: 'script' }, nodes)).toThrow();
    }
    expect(() => readStrictScriptVideoPrompt({ strictScriptSourceNodeId: '' }, [source()])).toThrow();
  });

  it('retains row order and rejects any empty row rather than silently dropping it', () => {
    expect(readStrictScriptVideoPrompt({ strictScriptSourceNodeId: 'script' }, [
      source({ scriptResult: { rows: [{ dialogue: 'First' }, { dialogue: 'Second' }] } }),
    ])).toBe('dialogue: First\n\ndialogue: Second');
    expect(() => readStrictScriptVideoPrompt({ strictScriptSourceNodeId: 'script' }, [
      source({ scriptResult: { rows: [{ dialogue: 'First' }, {}] } }),
    ])).toThrow();
  });
});
