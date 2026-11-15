import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  image: vi.fn(),
  video: vi.fn(),
}));

vi.mock("@/api/ops", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/ops")>()),
  fetchFreezoneImageModels: api.image,
  fetchFreezoneVideoModels: api.video,
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.resetAllMocks();
  vi.resetModules();
});

describe.each(["image", "video"] as const)("%s catalog recovery", (domain) => {
  async function loadHook(local: boolean) {
    vi.stubEnv("VITE_DOUBAO_WORK_ONLY", local ? "true" : "");
    vi.spyOn(console, "warn").mockImplementation(() => {});
    if (domain === "image") {
      const module = await import("@/features/canvas/hooks/useFreezoneImageModels");
      return module.useFreezoneImageModels;
    }
    const module = await import("@/features/canvas/hooks/useFreezoneVideoModels");
    return module.useFreezoneVideoModels;
  }

  it("keeps only Doubao Work when the local API is unavailable on first load", async () => {
    api[domain].mockRejectedValue(new Error("connection refused"));
    const hook = await loadHook(true);
    const { result } = renderHook(() => hook(`offline-${domain}`));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.models.map((model) => model.id)).toEqual(["doubao_work"]);
    expect(result.current.models[0].label).toBe("豆包工作（本机授权）");
    expect(result.current.isFallback).toBe(true);
    expect(result.current.error?.message).toBe("connection refused");
  });

  it("keeps the last successful catalog when a focus refresh fails", async () => {
    const models = [{
      id: "configured-model", providerId: "openai", apiModel: "configured-model",
      label: "Configured model",
    }];
    api[domain].mockResolvedValueOnce(models).mockRejectedValue(new Error("offline"));
    const hook = await loadHook(false);
    const { result } = renderHook(() => hook(`refresh-${domain}`));
    await waitFor(() => expect(result.current.isFallback).toBe(false));
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(result.current.error?.message).toBe("offline"));
    expect(result.current.models).toEqual(models);
  });

  it("keeps a successful empty catalog empty after a failed refresh", async () => {
    api[domain].mockResolvedValueOnce([]).mockRejectedValue(new Error("offline"));
    const hook = await loadHook(true);
    const { result } = renderHook(() => hook(`empty-${domain}`));
    await waitFor(() => expect(result.current.isFallback).toBe(false));
    expect(result.current.models).toEqual([]);
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(result.current.error?.message).toBe("offline"));
    expect(result.current.models).toEqual([]);
  });
});
