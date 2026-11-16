import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useVideoGenerationForm } from "@/features/canvas/nodes/shared/useVideoGenerationForm";
import { useCanvasStore } from "@/stores/canvasStore";
import { type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import { submitFreezoneVideoGen } from "@/api/ops";

vi.mock("@/features/canvas/hooks/useFreezoneVideoModels", () => ({
  useFreezoneVideoModels: () => ({
    models: [{ id: "doubao_work", providerId: "doubao_work", apiModel: "doubao_work",
      label: "Native", supportedModes: ["text_to_video"] }], isLoading: false,
  }),
}));
vi.mock("@/lib/queries/generation-credit-cost", () => ({
  useGenerationCreditCost: () => ({ data: undefined, error: null }),
}));
vi.mock("@/lib/url-params", () => ({
  readUrl: () => ({ project: "guard-test", canvas: "default" }),
}));
vi.mock("@/features/canvas/application/workflowRecipeRuntime", () => ({
  compileWorkflowNodePrompt: vi.fn(async () => "prompt"),
}));
vi.mock("@/api/ops", async (original) => ({
  ...(await original<typeof import("@/api/ops")>()),
  fetchFreezoneVideoCameraTemplates: vi.fn(async () => []),
  submitFreezoneVideoGen: vi.fn(async () => ({
    task_key: "task", task_type: "freezone_video_gen", job_id: "job",
  })),
}));
vi.mock("@/api/tasks", () => ({
  awaitTaskCompletion: vi.fn(async () => ({ result: { url: "/result.mp4" } })),
}));

beforeEach(() => {
  vi.clearAllMocks();
  useCanvasStore.getState().setCanvasData([
    { id: "target", type: "videoNode", position: { x: 0, y: 0 }, data: {
      model: "doubao_work", genMode: "textToVideo", prompt: "merge upstream media",
      durationSec: 5, videoUrl: "/wrong-landscape.mp4",
    } },
    { id: "compose", type: "videoComposeNode", position: { x: 0, y: 0 },
      data: { resultVideoUrl: "/actual-composition.mp4" } },
  ] as CanvasNode[], []);
});
afterEach(cleanup);

it.each(["incoming-compose", "reference-only", "source-marker"])(
  "blocks fresh %s without invoking generation API or changing media", async (kind) => {
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    const oldSubmit = result.current.submit;
    await act(async () => {
      const store = useCanvasStore.getState();
      if (kind === "incoming-compose") store.setCanvasData(store.nodes, [
        { id: "ordinary-edge", source: "compose", target: "target" },
      ]);
      else store.updateNodeData("target", kind === "reference-only"
        ? { referenceOnly: true } : { composeSourceNodeId: "compose" });
      const before = JSON.stringify(useCanvasStore.getState().nodes);
      await oldSubmit();
      expect(JSON.stringify(useCanvasStore.getState().nodes)).toBe(before);
    });
    expect(submitFreezoneVideoGen).not.toHaveBeenCalled();
  },
);

it("allows ordinary generation so the guard is not vacuously blocking everything", async () => {
  const { result } = renderHook(() => useVideoGenerationForm("target"));
  await act(async () => { await result.current.submit(); });
  expect(submitFreezoneVideoGen).toHaveBeenCalledOnce();
});
