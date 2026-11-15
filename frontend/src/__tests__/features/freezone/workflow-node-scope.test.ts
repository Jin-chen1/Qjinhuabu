import { beforeEach, describe, expect, it } from "vitest";

import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { workflowGenerationTargetsForPreflight } from "@/features/freezone/canvasChatCommands";
import { useCanvasStore } from "@/stores/canvasStore";

describe("workflow node execution scope", () => {
  beforeEach(() => {
    useCanvasStore.setState({ nodes: [], edges: [], selectedNodeId: null });
  });

  function fixture(optIn = false) {
    const store = useCanvasStore.getState();
    const scripts = Array.from({ length: 3 }, () =>
      store.addNode(CANVAS_NODE_TYPES.script, { x: 0, y: 0 }, {}),
    );
    const videos = Array.from({ length: 3 }, (_, index) =>
      store.addNode(CANVAS_NODE_TYPES.video, { x: 0, y: 0 }, {
        prompt: "Product clip",
        ...(index < 2 ? { videoUrl: `/clip-${index}.mp4` } : {}),
      }),
    );
    scripts.forEach((script, index) => store.addEdge(script, videos[index]));
    const group = store.groupNodes([...scripts, ...videos], { label: "Six nodes" })!;
    if (optIn) store.updateNodeData(group, { workflowExecutionScope: "node" });
    const compose = store.addNode(CANVAS_NODE_TYPES.videoCompose, { x: 0, y: 0 }, {});
    videos.forEach((video) => store.addEdge(video, compose));
    const image = store.addNode(CANVAS_NODE_TYPES.imageGen, { x: 0, y: 0 }, {
      prompt: "Outside image",
    });
    store.addEdge(image, scripts[0]);
    return { group, scripts, videos, compose, image };
  }

  it.each(["opt-in", "explicit"] as const)(
    "%s node scope neither appends external compose nor protects the pending video",
    (mode) => {
      const { group, scripts, videos } = fixture(mode === "opt-in");
      const targets = workflowGenerationTargetsForPreflight({
        type: "run_workflow",
        node_ids: [group],
        direction: mode === "opt-in" ? "connected" : "node",
      });
      expect(targets).toEqual([
        ...scripts.map((nodeId) => ({ nodeId, action: "generate_story_script" })),
        { nodeId: videos[2], action: "generate_video" },
      ]);
    },
  );

  it("keeps legacy connected compose expansion and upstream protection", () => {
    const { group, compose } = fixture();
    expect(workflowGenerationTargetsForPreflight({
      type: "run_workflow",
      node_ids: [group],
      direction: "connected",
    })).toEqual([{ nodeId: compose, action: "auto_compose_video" }]);
  });

  it("keeps an explicitly scoped video from traversing external inputs and outputs", () => {
    const { videos } = fixture();
    expect(workflowGenerationTargetsForPreflight({
      type: "run_workflow",
      node_ids: [videos[2]],
      direction: "node",
    })).toEqual([{ nodeId: videos[2], action: "generate_video" }]);
  });
});
