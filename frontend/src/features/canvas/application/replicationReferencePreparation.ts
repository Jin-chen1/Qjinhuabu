import { apiClient } from '@/api/client';
import { useCanvasStore } from '@/stores/canvasStore';
import { CANVAS_NODE_TYPES } from '../domain/canvasNodes';
import { PREPARE_REPLICATION_REFERENCE, replicationTargets } from '../domain/replicationPreparation';
import {
  subscribeNodeAction, publishNodeActionAccepted, publishNodeActionSuccess, publishNodeActionError,
} from './nodeActionResult';

const inFlight = new Set<string>();

export function subscribeReplicationPreparation(
  nodeId: string, context: () => { project?: string | null; canvas?: string | null },
): () => void {
  return subscribeNodeAction(payload => {
    if (payload.nodeId !== nodeId || payload.action !== PREPARE_REPLICATION_REFERENCE) return;
    const { requestId, action } = payload;
    publishNodeActionAccepted(requestId, nodeId, action);
    const url = context();
    void prepareReplicationReference(url.project ?? '', url.canvas ?? 'default', nodeId)
      .then(output => publishNodeActionSuccess(requestId, nodeId, action, output))
      .catch(error => publishNodeActionError(requestId, nodeId, action, error));
  });
}

export async function prepareReplicationReference(projectId: string, canvasId: string, nodeId: string) {
  const key = `${projectId}:${canvasId}:${nodeId}`;
  if (inFlight.has(key)) throw new Error('参考素材准备正在进行');
  const state = useCanvasStore.getState();
  const source = state.nodes.find(n => n.id === nodeId);
  const targets = replicationTargets(source);
  const videoUrl = source?.data.videoUrl;
  const frame = state.nodes.find(n => n.id === targets?.firstFrameNodeId);
  const audio = state.nodes.find(n => n.id === targets?.audioNodeId);
  if (!projectId || !canvasId || !targets || typeof videoUrl !== 'string' || !videoUrl.trim()
    || frame?.type !== CANVAS_NODE_TYPES.upload || audio?.type !== CANVAS_NODE_TYPES.audio) {
    throw new Error('缺少原视频或预建首帧/音频节点');
  }
  const frameData = frame.data;
  const audioData = audio.data;
  inFlight.add(key);
  state.updateNodeData(nodeId, { replicationPrepared: undefined, workflowResultStale: true });
  try {
    const result = await apiClient(`/projects/${encodeURIComponent(projectId)}/freezone/reference/prepare`, {
      method: 'POST', retry: 0, timeout: 120_000,
      json: { video_url: videoUrl, canvas_id: canvasId, node_id: nodeId },
    }).json<{
      first_frame_url: string; audio_url: string | null; duration_ms: number;
      width: number; height: number; source_sha256: string;
    }>();
    if (!result?.audio_url?.trim()) throw new Error('原视频缺少音轨');
    if (!result.first_frame_url?.trim()
      || ![result.duration_ms, result.width, result.height].every(v => Number.isFinite(v) && v > 0)
      || !/^[a-f0-9]{64}$/i.test(result.source_sha256)) {
      throw new Error('参考素材准备响应不完整');
    }
    const fresh = useCanvasStore.getState();
    const current = fresh.nodes.find(n => n.id === nodeId);
    const currentTargets = replicationTargets(current);
    const currentFrame = fresh.nodes.find(n => n.id === frame.id);
    const currentAudio = fresh.nodes.find(n => n.id === audio.id);
    if (current?.data.videoUrl !== videoUrl
      || currentTargets?.firstFrameNodeId !== frame.id || currentTargets.audioNodeId !== audio.id
      || currentFrame?.type !== frame.type || currentAudio?.type !== audio.type
      || currentFrame.data !== frameData || currentAudio.data !== audioData) {
      throw new Error('准备期间原视频或目标节点已改变');
    }
    // Validate the entire response and fresh graph before the first synchronous write.
    fresh.updateNodeData(frame.id, {
      imageUrl: result.first_frame_url, previewImageUrl: result.first_frame_url,
      videoUrl: null, widthPx: result.width, heightPx: result.height,
      workflowResultStale: false,
    });
    fresh.updateNodeData(audio.id, {
      audioUrl: result.audio_url, durationMs: result.duration_ms, workflowResultStale: false,
    });
    fresh.updateNodeData(nodeId, {
      replicationPrepared: { ...result, ...targets, videoUrl },
      durationMs: result.duration_ms, widthPx: result.width, heightPx: result.height,
      workflowResultStale: false,
    });
    return result;
  } finally {
    inFlight.delete(key);
  }
}
