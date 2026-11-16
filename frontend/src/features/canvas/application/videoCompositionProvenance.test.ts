import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { compositionResultVideoData, isCompositionResultVideo, isVideoGenerationBlocked, videoPlaybackUrl } from "./videoCompositionProvenance";

describe("legacy composition provenance", () => {
  const graph = {
    nodes: [
      { id: "new-compose", type: "videoComposeNode", data: {} },
      { id: "video4", type: "videoNode", data: { prompt: "merge media", videoUrl: "/original.mp4" } },
      { id: "ordinary", type: "videoNode", data: {} },
    ],
    edges: [{ source: "new-compose", target: "video4" }],
  };
  it("blocks an unmarked result regardless of its prompt or existing media", () => {
    const before = JSON.stringify(graph);
    expect(isCompositionResultVideo("video4", graph)).toBe(true);
    expect(JSON.stringify(graph)).toBe(before);
  });
  it("carries markers to an independent copy even after disconnecting", () => {
    const data = compositionResultVideoData("video4", graph);
    expect(data).toEqual({ referenceOnly: true, composeSourceNodeId: "new-compose" });
    expect(isCompositionResultVideo("copy", {
      nodes: [{ id: "copy", type: "videoNode", data }], edges: [],
    })).toBe(true);
  });
  it("does not block ordinary generation or videos referencing a result video", () => {
    expect(isCompositionResultVideo("ordinary", {
      ...graph, edges: [...graph.edges, { source: "video4", target: "ordinary" }],
    })).toBe(false);
  });
  it.each([{ referenceOnly: true }, { composeSourceNodeId: "deleted-compose" }])(
    "retains explicit protection without a source node: %j", (data) => {
      expect(Boolean(compositionResultVideoData("target", {
        nodes: [{ id: "target", type: "videoNode", data }], edges: [],
      }).referenceOnly)).toBe(true);
    },
  );
  it("keeps an ordinary reference-only original in the normal playback branch", () => {
    const referenceGraph = {
      nodes: [{ id: "new-reference", type: "videoNode",
        data: { referenceOnly: true, videoUrl: "/original.mp4" } }], edges: [],
    };
    expect(isCompositionResultVideo("new-reference", referenceGraph)).toBe(false);
    expect(isVideoGenerationBlocked("new-reference", referenceGraph)).toBe(true);
    expect(videoPlaybackUrl("new-reference", referenceGraph)).toBe("/original.mp4");
  });
  it("plays upstream composition instead of the wrong landscape without mutating URLs", () => {
    const composed = {
      ...graph, nodes: graph.nodes.map((node) => node.id === "new-compose"
        ? { ...node, data: { resultVideoUrl: "/composed.mp4" } } : node),
    };
    const before = JSON.stringify(composed);
    expect(videoPlaybackUrl("video4", composed)).toBe("/composed.mp4");
    expect(JSON.stringify(composed)).toBe(before);
  });
});

describe("composition generation entry guards", () => {
  it("renders composition results separately without replacing stored media", () => {
    const source = readFileSync("src/features/canvas/nodes/VideoNode.tsx", "utf8");
    expect(source.includes("compositionPlaybackUrl")).toBe(true);
    expect(source.includes('if (isCompositionResult) return null;')).toBe(true);
  });
  it.each(["VideoNode.tsx", "shared/useVideoGenerationForm.ts"])(
    "checks current graph before any submit side effects in %s",
    (file) => {
      const source = readFileSync(`src/features/canvas/nodes/${file}`, "utf8");
      const submit = source.slice(source.indexOf("const handleSubmit = useCallback"));
      expect(submit).toContain("isVideoGenerationBlocked(id, useCanvasStore.getState(), data)");
      expect(submit.indexOf("isVideoGenerationBlocked(")).toBeLessThan(
        submit.indexOf("updateNodeData("),
      );
    },
  );
  it("preserves composition provenance when applying an album video", () => {
    const source = readFileSync("src/features/canvas/nodes/VideoNode.tsx", "utf8");
    const apply = source.slice(source.indexOf("const handleApplyAlbumVideoToCanvas"), source.indexOf("const handleDownloadAlbumVideo"));
    expect(apply).toContain("compositionResultVideoData(");
  });
  it("allows video plus audio to open the timeline", () => {
    const source = readFileSync("src/features/canvas/nodes/VideoComposeNode.tsx", "utf8");
    expect(source).toContain("const canOpen = canAutoCompose;");
  });
});
