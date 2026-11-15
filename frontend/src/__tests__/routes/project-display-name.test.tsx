import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import ky from "ky";
import type { ComponentType, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const navigation = vi.hoisted(() => ({
  navigate: vi.fn(),
  preloadRoute: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
}));
vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: unknown) => ({ options }),
  useNavigate: () => navigation.navigate,
  useRouter: () => ({ preloadRoute: navigation.preloadRoute }),
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) =>
    select({ location: { pathname: "/projects/project-id/freezone" } }),
  Link: ({ children }: { children?: ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/queries/product-surfaces", () => ({
  useProductSurfaces: () => ({ data: {}, isPending: false }),
  surfaceAccess: () => ({ available: true }),
}));
vi.mock("@/components/projects/share-project-dialog", () => ({
  ShareProjectDialog: () => null,
}));

import { server } from "@/__mocks__/msw/server";
import { ProjectSwitcher } from "@/components/layout/project-header-navigation";
import { useAllProjectSummaries, useCreateProject } from "@/lib/queries/projects";
import { Route } from "@/routes/_app/index";
import { useAppStore } from "@/stores/app-store";

const NAME = "viral_replica_keyframes";
const DISPLAY_NAME = "爆款复刻·关键帧版";
const Dashboard = Route.options.component as ComponentType;

function wrapper(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function mockSummary(displayName?: string | null) {
  server.use(http.get("http://localhost:3000/api/v1/projects/summaries", () =>
    HttpResponse.json({ ok: true, data: [{
      id: "project-id", name: NAME, display_name: displayName,
      status: "active", effective_role: "owner",
    }] }),
  ));
}

beforeEach(() => {
  navigation.navigate.mockClear();
  localStorage.clear();
  useAppStore.setState({ dashboardTab: "active", dashboardView: "card" });
});
afterEach(() => server.resetHandlers());

describe("项目中文显示名", () => {
  it("摘要保留显示名和原始名称", async () => {
    mockSummary(DISPLAY_NAME);
    const { result } = renderHook(useAllProjectSummaries, { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data?.[0]).toMatchObject({
      id: "project-id", name: NAME, displayName: DISPLAY_NAME,
    }));
  });

  it.each(["card", "list"] as const)("首页 %s 显示中文但导航仍使用项目标识", async (view) => {
    mockSummary(DISPLAY_NAME);
    useAppStore.setState({ dashboardView: view });
    render(<Dashboard />, { wrapper: wrapper() });
    const heading = await screen.findByRole("heading", { name: DISPLAY_NAME });
    expect(heading).toHaveAttribute("title", DISPLAY_NAME);
    fireEvent.click(heading);
    expect(navigation.navigate).toHaveBeenCalledWith({
      to: "/projects/$project/freezone", params: { project: "project-id" },
    });
  });

  it.each([undefined, null, ""])("旧项目无显示名时保留原名称 (%s)", async (displayName) => {
    mockSummary(displayName);
    render(<Dashboard />, { wrapper: wrapper() });
    expect(await screen.findByRole("heading", { name: NAME })).toHaveAttribute("title", NAME);
  });

  it("项目标题和切换菜单优先中文，切换仍使用原标识", async () => {
    mockSummary(DISPLAY_NAME);
    render(<ProjectSwitcher current="project-id" />, { wrapper: wrapper() });
    const trigger = await screen.findByRole("button", { name: DISPLAY_NAME });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem", { name: new RegExp(DISPLAY_NAME) }));
    expect(navigation.navigate).toHaveBeenCalledWith({
      to: "/projects/$project/freezone", params: { project: "project-id" },
    });
  });

  it("旧项目标题回退到原名称", async () => {
    mockSummary();
    render(<ProjectSwitcher current="project-id" />, { wrapper: wrapper() });
    expect(await screen.findByRole("button", { name: NAME })).toBeInTheDocument();
  });

  it("创建请求支持显示名，同时兼容旧字符串调用", async () => {
    const requests: unknown[] = [];
    server.use(http.post("http://localhost:3000/api/v1/projects", async ({ request }) => {
      const body = await request.json();
      requests.push(body);
      return HttpResponse.json({ ok: true, data: { id: "project-id", name: NAME } });
    }));
    const { result } = renderHook(useCreateProject, { wrapper: wrapper() });
    await result.current.mutateAsync({ name: NAME, display_name: DISPLAY_NAME });
    expect(requests[requests.length - 1]).toEqual({ name: NAME, display_name: DISPLAY_NAME });
    await result.current.mutateAsync(NAME);
    expect(requests[requests.length - 1]).toEqual({ name: NAME });
  });
});
