// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CANVAS_NODE_TYPES, type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import type { ModelOption } from "@/features/canvas/ui/ProviderModelPicker";
import { useVideoGenerationForm } from "@/features/canvas/nodes/shared/useVideoGenerationForm";
import { useGenerationCreditCost } from "@/lib/queries/generation-credit-cost";
import { BillingRuleNotConfiguredError } from "@/lib/api-errors";
import { useCanvasStore } from "@/stores/canvasStore";
import { submitFreezoneVideoOmniGen } from "@/api/ops";

const catalog = vi.hoisted(() => ({
  models: [] as ModelOption[],
  isLoading: false,
  isFallback: false,
  error: null,
}));
vi.mock("@/features/canvas/hooks/useFreezoneVideoModels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/canvas/hooks/useFreezoneVideoModels")>()),
  useFreezoneVideoModels: () => catalog,
}));
vi.mock("@/lib/queries/generation-credit-cost", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/queries/generation-credit-cost")>()),
  useGenerationCreditCost: vi.fn(),
}));
vi.mock("@/lib/url-params", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/url-params")>()),
  readUrl: () => ({ project: "dual-reference-test", canvas: "default" }),
}));
vi.mock("@/api/ops", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/ops")>()),
  fetchFreezoneVideoCameraTemplates: vi.fn(async () => []),
  submitFreezoneVideoOmniGen: vi.fn(async () => ({
    task_key: "dual-test-task", task_type: "freezone_video_omni_gen", job_id: "dual-test-job",
  })),
}));
vi.mock("@/api/tasks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/tasks")>()),
  awaitTaskCompletion: vi.fn(async () => ({ result: { url: "/static/test-result.mp4" } })),
}));

const native: ModelOption = {
  id: "doubao_work", providerId: "doubao_work", apiModel: "doubao_work", label: "豆包工作",
  supportedModes: ["text_to_video", "image_to_video", "first_frame", "first_last_frame"],
  referenceVideoMax: 0, referenceImageMax: 2, referenceAudioMax: 0,
};
const ark: ModelOption = {
  id: "ark_dual_reference", providerId: "ark",
  apiModel: "ark_dual_reference", label: "火山方舟",
  supportedModes: ["all_reference"],
  referenceVideoMax: 1, referenceImageMax: 1, referenceAudioMax: 0,
};

function seed() {
  useCanvasStore.getState().setCanvasData([
    {
      id: "target", type: CANVAS_NODE_TYPES.video, position: { x: 0, y: 0 },
      data: {
        model: "ark_dual_reference", genMode: "allReference",
        prompt: "将参考片中的商品替换为参考图片里的商品", durationSec: 5,
      },
    },
    {
      id: "product", type: CANVAS_NODE_TYPES.imageEdit, position: { x: 0, y: 0 },
      data: { imageUrl: "/static/test-product.png" },
    },
    {
      id: "reference", type: CANVAS_NODE_TYPES.video, position: { x: 0, y: 0 },
      data: { videoUrl: "/static/test-reference.mp4", durationMs: 5000 },
    },
  ] as CanvasNode[], [
    { id: "product-edge", source: "product", target: "target" },
    { id: "reference-edge", source: "reference", target: "target" },
  ]);
}

function targetData() {
  return useCanvasStore.getState().nodes.find((node) => node.id === "target")?.data;
}

describe("双参考视频表单", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    catalog.models = [native];
    catalog.isLoading = false;
    catalog.isFallback = false;
    vi.mocked(useGenerationCreditCost).mockReturnValue({
      data: undefined, error: null,
    } as ReturnType<typeof useGenerationCreditCost>);
    seed();
  });
  afterEach(cleanup);

  it("目录尚无方舟时暂停提交并保留双参考预设，目录刷新后恢复", async () => {
    const { result, rerender } = renderHook(() => useVideoGenerationForm("target"));
    expect(result.current.selectedVideoModelId).toBe("");
    expect(result.current.submitDisabled).toBe(true);
    expect(targetData()).toMatchObject({ model: "ark_dual_reference", genMode: "allReference" });
    await act(async () => { await result.current.submit(); });
    expect(submitFreezoneVideoOmniGen).not.toHaveBeenCalled();

    catalog.models = [native, ark];
    rerender();
    await waitFor(() => expect(result.current.submitDisabled).toBe(false));
    expect(result.current.selectedVideoModelId).toBe("ark_dual_reference");
    expect(targetData()).toMatchObject({ model: "ark_dual_reference", genMode: "allReference" });
  });

  it("方舟自带密钥不询问应用积分，也不被旧积分错误拦住提交", async () => {
    catalog.models = [native, ark];
    vi.mocked(useGenerationCreditCost).mockReturnValue({
      data: undefined, error: new BillingRuleNotConfiguredError("测试无积分规则", 409),
    } as unknown as ReturnType<typeof useGenerationCreditCost>);
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    expect(useGenerationCreditCost).toHaveBeenLastCalledWith("feature", null, expect.any(Object));
    expect(result.current.submitDisabled).toBe(false);
    expect(result.current.formProps.totalCreditCostDisplay).toBe("使用火山方舟账户计费，不消耗应用积分");
    expect(result.current.formProps.creditPromotion).toBeNull();
    await act(async () => { await result.current.submit(); });
    expect(submitFreezoneVideoOmniGen).toHaveBeenCalledWith(
      "dual-reference-test", expect.objectContaining({ model: "ark_dual_reference" }),
    );
  });

  it("原生豆包仍不支持视频参考且不会被伪装成双参考服务", () => {
    useCanvasStore.getState().updateNodeData("target", { model: "doubao_work" });
    const { result } = renderHook(() => useVideoGenerationForm("target"));
    expect(result.current.submitDisabled).toBe(true);
    expect(result.current.formProps.totalCreditCostDisplay).toBe("使用豆包工作账号额度");
  });
});
