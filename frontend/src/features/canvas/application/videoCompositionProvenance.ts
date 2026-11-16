// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
type ProvenanceData = {
  [key: string]: unknown;
  referenceOnly?: unknown;
  composeSourceNodeId?: unknown;
};

type ProvenanceGraph = {
  nodes: readonly { id: string; type?: string; data: ProvenanceData }[];
  edges: readonly { source: string; target: string }[];
};

export function compositionResultVideoData(
  id: string,
  graph: ProvenanceGraph,
  fallback: ProvenanceData = {},
): { referenceOnly?: true; composeSourceNodeId?: string } {
  const data = graph.nodes.find((node) => node.id === id)?.data ?? fallback;
  const sourceId = typeof data.composeSourceNodeId === "string"
    && data.composeSourceNodeId.trim() ? data.composeSourceNodeId : undefined;
  // Legacy canvases created result nodes with only an ordinary compose edge.
  const incomingSource = graph.edges.find((edge) =>
    edge.target === id && graph.nodes.some((node) =>
      node.id === edge.source && node.type === "videoComposeNode",
    ),
  )?.source;
  const composeSourceNodeId = sourceId ?? incomingSource;
  return data.referenceOnly || composeSourceNodeId
    ? { referenceOnly: true, ...(composeSourceNodeId ? { composeSourceNodeId } : {}) }
    : {};
}

export function isCompositionResultVideo(
  id: string,
  graph: ProvenanceGraph,
  fallback: ProvenanceData = {},
): boolean {
  return Boolean(compositionResultVideoData(id, graph, fallback).composeSourceNodeId);
}

export function isVideoGenerationBlocked(
  id: string,
  graph: ProvenanceGraph,
  fallback: ProvenanceData = {},
): boolean {
  return compositionResultVideoData(id, graph, fallback).referenceOnly === true;
}

export function videoPlaybackUrl(
  id: string,
  graph: ProvenanceGraph,
  fallback: ProvenanceData = {},
): string | null {
  const data = graph.nodes.find((node) => node.id === id)?.data ?? fallback;
  const sourceId = compositionResultVideoData(id, graph, data).composeSourceNodeId;
  const source = graph.nodes.find((node) => node.id === sourceId);
  const url = sourceId
    ? source?.data.resultVideoUrl
      ?? (data.composeSourceNodeId && data.referenceOnly ? data.videoUrl : null)
    : data.videoUrl;
  return typeof url === "string" && url ? url : null;
}
