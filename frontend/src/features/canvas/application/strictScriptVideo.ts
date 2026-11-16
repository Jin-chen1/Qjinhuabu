import { CANVAS_NODE_TYPES } from '../domain/canvasNodes';

const SCRIPT_TEXT_FIELDS = [
  'shot_no', 'duration', 'visual_description', 'character_1',
  'character_description_1', 'character_2', 'character_description_2',
  'shot', 'character_action', 'emotion', 'scene_tags', 'lighting_mood',
  'sound', 'shot_prompt', 'video_motion_prompt', 'dialogue',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * null means opt-out. Call with freshly read, directly connected upstream nodes.
 * Producers must mark invalidated results with scriptResultStale until replaced.
 * Image references are deliberately excluded from the textual script.
 */
export function readStrictScriptVideoPrompt(
  data: Record<string, unknown>,
  upstreamNodes: readonly { id: string; type?: string; data: Record<string, unknown> }[],
): string | null {
  const sourceId = data.strictScriptSourceNodeId;
  if (sourceId == null) return null;
  if (typeof sourceId !== 'string' || !sourceId.trim()) {
    throw new Error('请为视频指定脚本来源节点。');
  }
  const source = upstreamNodes.find((node) => node.id === sourceId);
  if (!source || source.type !== CANVAS_NODE_TYPES.script) {
    throw new Error('请先连接对应的脚本生成器。');
  }
  const script = source.data;
  if (script.isGenerating || script.syncStatus === 'syncing') {
    throw new Error('脚本仍在生成，请等待完成。');
  }
  if (script.generationError || script.syncStatus === 'error') {
    throw new Error('脚本生成失败，请先重新生成脚本。');
  }
  if (script.scriptResultStale === true || script.workflowResultStale === true || script.syncStatus === 'stale') {
    throw new Error('脚本已失效，请先重新生成脚本。');
  }
  const result = script.scriptResult;
  if (!isRecord(result) || !Array.isArray(result.rows) || !result.rows.length) {
    throw new Error('脚本尚未生成有效分镜。');
  }
  return result.rows.map((row: unknown) => {
    if (!isRecord(row)) throw new Error('脚本包含无效分镜。');
    const content = SCRIPT_TEXT_FIELDS.flatMap((field) => {
      const value = row[field];
      return (typeof value === 'string' && value.trim()) || typeof value === 'number'
        ? [`${field}: ${value}`] : [];
    });
    if (!content.length) throw new Error('脚本包含空分镜。');
    return content.join('\n');
  }).join('\n\n');
}

/** Strict video duration comes from all script rows, never the video form default. */
export function readStrictScriptVideoDuration(
  data: Record<string, unknown>,
  upstreamNodes: readonly { id: string; type?: string; data: Record<string, unknown> }[],
): number | null {
  if (readStrictScriptVideoPrompt(data, upstreamNodes) === null) return null;
  const source = upstreamNodes.find((node) => node.id === data.strictScriptSourceNodeId)!;
  const rows = (source.data.scriptResult as { rows: Record<string, unknown>[] }).rows;
  const total = rows.reduce((sum, row) => {
    const duration = row.duration;
    if (typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0) {
      throw new Error('脚本分镜时长无效，请先补齐脚本时长。');
    }
    return sum + duration;
  }, 0);
  if (!Number.isFinite(total)) throw new Error('脚本总时长无效。');
  return total;
}
