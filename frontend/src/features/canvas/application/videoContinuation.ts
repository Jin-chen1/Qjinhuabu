// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { isVideoNode, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { classifyVideoReferenceMedia } from '@/features/canvas/domain/videoReferenceLimits';
import { useCanvasStore } from '@/stores/canvasStore';
import { readUrl } from '@/lib/url-params';
import { captureVideoFrameToNode, resolveCaptureSeekSec } from './videoCaptureFrame';

type InputEdge = { source: string; target: string; data?: { keyframeSlot?: 'first' | 'last' } };

/** Empty slots count as inputs too: clicking the CTA twice must not duplicate them. */
export function missingVideoKeyframeSlots(
  nodes: readonly CanvasNode[], edges: readonly InputEdge[], targetId: string,
): Array<'first' | 'last'> {
  const used = new Set<'first' | 'last'>();
  const inputs = edges.filter(edge => edge.target === targetId &&
    classifyVideoReferenceMedia(nodes.find(node => node.id === edge.source)) === 'image');
  for (const edge of inputs) if (edge.data?.keyframeSlot) used.add(edge.data.keyframeSlot);
  for (const edge of inputs) {
    if (edge.data?.keyframeSlot) continue;
    if (!used.has('first')) used.add('first');
    else used.add('last');
  }
  return (['first', 'last'] as const).filter(slot => !used.has(slot));
}

export function isVideoContinuationConnection(
  source: CanvasNode | undefined, target: CanvasNode | undefined,
): boolean {
  return Boolean(isVideoNode(source) && source.data.videoUrl &&
    isVideoNode(target) && !target.data.videoUrl && source.id !== target.id &&
    (!target.data.genMode || ['textToVideo', 'firstFrame', 'firstLastFrame', 'imageToVideo']
      .includes(target.data.genMode)));
}

const pending = new Map<string, Promise<void>>();

/** A new shot consumes the previous tail as an image, never as a video reference. */
export function connectVideoContinuation(sourceId: string, targetId: string): Promise<void> {
  const scope = readUrl();
  const key = `${scope.project}:${scope.canvas}:${sourceId}:${targetId}`;
  const running = pending.get(key);
  if (running) return running;
  const work = (async () => {
    const state = useCanvasStore.getState();
    const source = state.nodes.find(node => node.id === sourceId);
    const target = state.nodes.find(node => node.id === targetId);
    if (!isVideoNode(source) || !isVideoNode(target) ||
      !isVideoContinuationConnection(source, target)) return;
    const existing = state.edges.find(edge => edge.target === targetId &&
      state.nodes.find(node => node.id === edge.source)?.data.continuationSourceVideoId === sourceId);
    if (existing) return;
    const firstInputEdge = state.edges.find(edge => edge.target === targetId &&
      edge.data?.keyframeSlot === 'first');
    const firstInput = state.nodes.find(node => node.id === firstInputEdge?.source);
    const emptyFirstInput = firstInput &&
      classifyVideoReferenceMedia(firstInput) === 'image' &&
      !firstInput.data.imageUrl && !firstInput.data.referenceImageUrl
      ? firstInput : undefined;
    if (!emptyFirstInput && !missingVideoKeyframeSlots(state.nodes, state.edges, targetId).includes('first')) {
      throw new Error('已存在首帧输入，请先断开首帧连线再接续上一段视频。');
    }
    const result = await captureVideoFrameToNode(sourceId, {
      videoUrl: source.data.videoUrl!,
      projectId: scope.project,
      seekSec: resolveCaptureSeekSec('last', {
        fallbackDurationSec: typeof source.data.durationMs === 'number' ? source.data.durationMs / 1000 : null,
      }),
      displayName: '上一段视频尾帧',
      targetImageNodeId: emptyFirstInput?.id,
    });
    if (!result.nodeId) throw new Error(result.error || '上一段视频尾帧提取失败，请重试。');
    const done = useCanvasStore.getState();
    const current = done.nodes.find(node => node.id === targetId);
    if (readUrl().project !== scope.project || readUrl().canvas !== scope.canvas || !current) return;
    if (!emptyFirstInput) {
      const frame = done.nodes.find(node => node.id === result.nodeId);
      if (frame) {
        const width = frame.width ?? 320;
        const height = frame.height ?? 350;
        const x = target.position.x - width - 40;
        let y = target.position.y + (target.height ?? 360) + 60;
        for (let attempt = 0; attempt < 40; attempt++) {
          const occupied = done.nodes.some(node => node.id !== frame.id &&
            x < node.position.x + (node.width ?? 580) + 12 &&
            x + width + 12 > node.position.x &&
            y < node.position.y + (node.height ?? 360) + 12 &&
            y + height + 12 > node.position.y);
          if (!occupied) break;
          y += height + 40;
        }
        done.updateNodePosition(frame.id, { x, y });
      }
    }
    done.updateNodeData(result.nodeId, { continuationSourceVideoId: sourceId });
    done.addEdgeWithData(result.nodeId, targetId, { keyframeSlot: 'first' });
    done.updateNodeData(targetId, {
      genMode: current.data.genMode === 'firstLastFrame' ? 'firstLastFrame' : 'firstFrame',
      aspectRatio: source.data.aspectRatio || target.data.aspectRatio,
    });
  })();
  pending.set(key, work);
  void work.finally(() => pending.delete(key)).catch(() => undefined);
  return work;
}
