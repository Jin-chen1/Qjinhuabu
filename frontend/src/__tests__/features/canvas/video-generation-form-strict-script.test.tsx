import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CANVAS_NODE_TYPES, type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import { useVideoGenerationForm } from "@/features/canvas/nodes/shared/useVideoGenerationForm";
import { readStrictScriptVideoPrompt } from "@/features/canvas/application/strictScriptVideo";
import { compileWorkflowNodePrompt } from "@/features/canvas/application/workflowRecipeRuntime";
import { useCanvasStore } from "@/stores/canvasStore";
import { submitFreezoneVideoGen } from "@/api/ops";

vi.mock("@/features/canvas/hooks/useFreezoneVideoModels", () => ({
  useFreezoneVideoModels: () => ({
    models: [{ id: "doubao_work", providerId: "doubao_work", apiModel: "doubao_work",
      label: "Native", supportedModes: ["text_to_video"], sceneOptimizeOptions: ["realistic"] }],
    isLoading: false,
  }),
}));
vi.mock("@/lib/queries/generation-credit-cost", () => ({
  useGenerationCreditCost: () => ({ data: undefined, error: null }),
}));
vi.mock("@/lib/url-params", () => ({
  readUrl: () => ({ project: "strict-test", canvas: "default" }),
}));
vi.mock("@/features/canvas/application/workflowRecipeRuntime", () => ({
  compileWorkflowNodePrompt: vi.fn(async () => "compiled non-strict prompt"),
}));
vi.mock("@/features/canvas/application/errorDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/canvas/application/errorDialog")>()),
  showErrorDialog: vi.fn(),
}));
vi.mock("@/api/ops", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/ops")>()),
  fetchFreezoneVideoCameraTemplates: vi.fn(async () => []),
  submitFreezoneVideoGen: vi.fn(async () => ({
    task_key: "strict-task", task_type: "freezone_video_gen", job_id: "strict-job",
  })),
}));
vi.mock("@/api/tasks", () => ({
  awaitTaskCompletion: vi.fn(async () => ({ result: { url: "/static/result.mp4" } })),
}));

function seed(prompt = "unwanted handwritten prompt", strict = true) {
  useCanvasStore.getState().setCanvasData([
    { id: "target", type: CANVAS_NODE_TYPES.video, position: { x: 0, y: 0 },
      data: { model: "doubao_work", genMode: "textToVideo", prompt,
        durationSec: 5, cameraMovement: "push_in", sceneOptimize: "realistic",
        ...(strict ? { strictScriptSourceNodeId: "script" } : {}) } },
    { id: "script", type: CANVAS_NODE_TYPES.script, position: { x: 0, y: 0 },
      data: { scriptResult: { rows: [{ shot_no: 1, duration: 6, visual_description: "original script" }] } } },
  ] as CanvasNode[], [{ id: "script-edge", source: "script", target: "target" }]);
}

describe("shared video generation strict script", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
    seed();
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("reads fresh target and script data without compiling or injecting camera and scene options", async () => {
    seed("unwanted handwritten prompt", false);
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    const submit = result.current.submit;
    await act(async () => {
      useCanvasStore.getState().updateNodeData("target", { strictScriptSourceNodeId: "script" });
      useCanvasStore.getState().updateNodeData("script", {
        scriptResult: { rows: [{ shot_no: 2, duration: 7, visual_description: "fresh script" }] },
      });
      await submit();
    });
    const nodes = useCanvasStore.getState().nodes;
    const expected = readStrictScriptVideoPrompt(
      nodes.find((node) => node.id === "target")!.data, nodes.filter((node) => node.id === "script"),
    );
    expect(submitFreezoneVideoGen).toHaveBeenCalledWith("strict-test", expect.objectContaining({
      prompt: expected, cameraTemplateId: null, sceneOptimize: null, durationSeconds: 7,
    }));
    expect(compileWorkflowNodePrompt).not.toHaveBeenCalled();
  });

  it("allows strict submission without handwritten prompt", async () => {
    seed("");
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    expect(result.current.submitDisabled).toBe(false);
    await act(async () => { await result.current.submit(); });
    expect(submitFreezoneVideoGen).toHaveBeenCalledOnce();
  });

  it.each([0, 16])("rejects invalid or unsupported strict duration %s", async (duration) => {
    useCanvasStore.getState().updateNodeData("script", {
      scriptResult: { rows: [{ shot_no: 1, duration, visual_description: "script" }] },
    });
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    await act(async () => { await result.current.submit(); });
    expect(submitFreezoneVideoGen).not.toHaveBeenCalled();
    expect(compileWorkflowNodePrompt).not.toHaveBeenCalled();
    if (duration === 16) {
      expect(useCanvasStore.getState().nodes.find((node) => node.id === "target")?.data.generationError)
        .toBe("脚本总时长超出当前模型支持范围，请调整脚本或模型。");
    }
  });

  it.each(["disconnected", "stale", "generating", "empty"])("blocks fresh %s script without fallback", async (state) => {
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    const submit = result.current.submit;
    await act(async () => {
      const store = useCanvasStore.getState();
      if (state === "disconnected") store.setCanvasData(store.nodes, []);
      else store.updateNodeData("script", state === "stale" ? { scriptResultStale: true }
        : state === "generating" ? { isGenerating: true } : { scriptResult: { rows: [] } });
      await submit();
    });
    expect(submitFreezoneVideoGen).not.toHaveBeenCalled();
    expect(compileWorkflowNodePrompt).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes.find((node) => node.id === "target")?.data)
      .toMatchObject({ isGenerating: false, generationError: expect.any(String) });
  });

  it("preserves compilation and scene options for non-strict generation", async () => {
    seed("ordinary prompt", false);
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    await act(async () => { await result.current.submit(); });
    expect(compileWorkflowNodePrompt).toHaveBeenCalledOnce();
    expect(submitFreezoneVideoGen).toHaveBeenCalledWith("strict-test", expect.objectContaining({
      prompt: "compiled non-strict prompt", cameraTemplateId: "push_in", sceneOptimize: "realistic",
    }));
  });
});
