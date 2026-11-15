// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const runtime = vi.hoisted(() => ({ ce: true }));
vi.mock("@/lib/runtime-config", () => ({ isCeRuntime: () => runtime.ce }));
vi.mock("@/lib/api", () => ({ api: ky.create({ baseUrl: "http://localhost:3000/" }) }));
import { useLocalDoubaoStatus } from "@/lib/queries/doubao-work";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  runtime.ce = true;
  vi.stubEnv("VITE_DOUBAO_WORK_ONLY", "true");
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("native Doubao connection status", () => {
  it("clears ready when a later refresh fails, even with cached connected status", async () => {
    let fail = false;
    server.use(http.get("http://localhost:3000/api/v1/doubao-work/status", () =>
      fail ? new HttpResponse(null, { status: 503 }) :
        HttpResponse.json({ reachable: true, connected: true })));
    const { result } = renderHook(() => useLocalDoubaoStatus(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.ready).toBe(true));
    fail = true;
    await act(async () => { await result.current.refetch(); });
    expect(result.current.data?.connected).toBe(true);
    await waitFor(() => expect(result.current.ready).toBe(false));
  });

  it.each(["ee", "disabled", "remote", "closed"])("does not request local status in %s mode", async (mode) => {
    if (mode === "ee") runtime.ce = false;
    if (mode === "disabled") vi.stubEnv("VITE_DOUBAO_WORK_ONLY", "false");
    if (mode === "remote") vi.stubGlobal("location", new URL("https://example.com"));
    const requested = vi.fn();
    server.use(http.get("http://localhost:3000/api/v1/doubao-work/status", () => {
      requested();
      return HttpResponse.json({ reachable: true, connected: true });
    }));
    const { result } = renderHook(() => useLocalDoubaoStatus(mode !== "closed"), { wrapper: wrapper() });
    expect(result.current.ready).toBe(false);
    expect(result.current.fetchStatus).toBe("idle");
    expect(requested).not.toHaveBeenCalled();
  });
});
