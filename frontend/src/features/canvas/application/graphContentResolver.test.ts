// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from 'vitest';

import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '../domain/canvasNodes';
import { collectUpstreamReferenceUrls, DefaultGraphContentResolver, extractUpstreamContent } from './graphContentResolver';

describe('extractUpstreamContent workflow metadata', () => {
  it('passes generated script shot prompts to a connected image instead of the input brief', () => {
    const script = {
      id: 'script', type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 },
      data: {
        prompt: '请分析参考视频',
        scriptResult: {
          rows: [
            { shot_prompt: '黑框眼镜棚拍', visual_description: '眼镜展示' },
            { visual_description: '镜腿细节特写' },
          ],
        },
      },
    } as CanvasNode;
    const image = {
      id: 'image', type: CANVAS_NODE_TYPES.imageGen,
      position: { x: 500, y: 0 }, data: {},
    } as CanvasNode;
    const edges = [{ id: 'script-image', source: 'script', target: 'image' }] as CanvasEdge[];
    const inputs = new DefaultGraphContentResolver().collectInputContents('image', [script, image], edges);
    expect(inputs[0].text).toBe('黑框眼镜棚拍\n\n镜腿细节特写');
  });
  it('keeps the input text usable before a script result exists', () => {
    const script = {
      id: 'script', type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 }, data: { prompt: '黑框眼镜棚拍', scriptResult: null },
    } as CanvasNode;
    expect(extractUpstreamContent(script).text).toBe('黑框眼镜棚拍');
  });
  it('reads an uploaded image from its source URL', () => {
    const source = {
      id: 'source', type: CANVAS_NODE_TYPES.upload,
      position: { x: 0, y: 0 }, data: { source_url: '/static/project/upload.png' },
    } as CanvasNode;
    expect(extractUpstreamContent(source).imageUrl).toBe('/static/project/upload.png');
  });
  it('passes only the connected existing image to a Recipe target', () => {
    const source = (id: string, imageUrl: string) => ({
      id, type: CANVAS_NODE_TYPES.upload, position: {x: 0, y: 0}, data: {imageUrl},
    }) as CanvasNode;
    const target = {
      id: 'blue-cup', type: CANVAS_NODE_TYPES.imageGen,
      position: {x: 0, y: 0}, data: {},
    } as CanvasNode;
    const nodes = [source('red-cup-source', '/static/red.png'),
      source('distractor-source', '/static/distractor.png'), target];
    const edges = [{id: 'reference', source: 'red-cup-source', target: 'blue-cup',
      data: {link_type: 'media_input_for'}}] as CanvasEdge[];
    const inputs = new DefaultGraphContentResolver().collectInputContents('blue-cup', nodes, edges);
    expect(collectUpstreamReferenceUrls(inputs)).toEqual(['/static/red.png']);
  });
  it('projects workflow step ids for deterministic input selection', () => {
    const node = {
      id: 'brief-node',
      type: CANVAS_NODE_TYPES.textAnnotation,
      position: { x: 0, y: 0 },
      data: {
        content: '广告脚本',
        workflowCatalog: { stepId: 'ad-brief' },
      },
    } as CanvasNode;

    expect(extractUpstreamContent(node)).toMatchObject({
      nodeId: 'brief-node',
      workflowStepId: 'ad-brief',
      text: '广告脚本',
    });
  });
});
