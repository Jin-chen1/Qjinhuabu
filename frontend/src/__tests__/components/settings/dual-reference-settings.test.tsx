// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import ky from "ky";
import { afterEach, describe, expect, it, vi } from "vitest";
import { server } from "@/__mocks__/msw/server";
import { DualReferenceSettings } from "@/components/settings/dual-reference-settings";

vi.mock("@/lib/api", () => ({ api: ky.create({ baseUrl: "http://localhost:3000/" }) }));

const path = "http://localhost:3000/api/v1/freezone/video/dual-reference";
const baseUrl = "https://ark.cn-beijing.volces.com/api/v3";
const testKey = "synthetic-test-secret";
const model25 = "doubao-seedance-2-5-260628";
const model20 = "doubao-seedance-2-0-260128";
const model20Fast = "doubao-seedance-2-0-fast-260128";
const config = {
  configured: false, provider: "ark", baseUrl, model: model25,
  connected: false, supportsReferenceVideo: false, supportsReferenceImage: false, error: "",
  verificationLevel: "none",
  generationVerified: false,
};
const connectedConfig = {
  ...config, configured: true, model: model25, connected: true,
  supportsReferenceVideo: true, supportsReferenceImage: true,
  verificationLevel: "credentials_and_model_catalog",
};
afterEach(() => vi.restoreAllMocks());

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}><DualReferenceSettings /></QueryClientProvider>,
  );
  return { ...view, client };
}

async function enterConfig() {
  await screen.findByText("未配置");
  fireEvent.change(screen.getByLabelText("接口密钥"), { target: { value: testKey } });
  fireEvent.change(screen.getByLabelText("视频模型标识"), { target: { value: model25 } });
}

describe("双参考服务授权", () => {
  it("defaults to Ark with a write-only password field and official console link", async () => {
    server.use(http.get(`${path}/config`, () => HttpResponse.json(config)));
    renderSettings();
    await screen.findByText("未配置");
    expect(screen.getByText("火山方舟")).toBeInTheDocument();
    expect(screen.getByLabelText("服务端点")).toHaveValue(baseUrl);
    expect(screen.getByLabelText("服务端点")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("视频模型标识")).toHaveValue(model25);
    for (const model of [model25, model20, model20Fast]) {
      expect(screen.getByText(model, { selector: "li" })).toBeInTheDocument();
    }
    expect(screen.queryByText(/可留空保存预设/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("接口密钥")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("接口密钥")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByLabelText("接口密钥")).toHaveValue("");
    expect(screen.getByRole("link", { name: "打开火山方舟官方控制台" }))
      .toHaveAttribute("href", "https://console.volcengine.com/ark/");
    expect(screen.getByRole("button", { name: "验证连接与权限" })).toBeDisabled();
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
  });

  it("saves without claiming a connection, then uses only the explicit verify endpoint", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    const requests: string[] = [];
    let saved = false;
    server.use(
      http.get(`${path}/config`, () => HttpResponse.json(saved ? connectedConfig : config)),
      http.put(`${path}/config`, async ({ request }) => {
        requests.push("保存");
        expect(await request.json()).toEqual({ apiKey: testKey, baseUrl, model: model25 });
        saved = true;
        return HttpResponse.json({ ok: true, data: { ...connectedConfig, apiKey: testKey } });
      }),
      http.post(`${path}/verify`, async ({ request }) => {
        requests.push("验证");
        expect(await request.text()).toBe("");
        return HttpResponse.json({ ok: true, data: connectedConfig });
      }),
    );
    const { client } = renderSettings();
    await enterConfig();
    fireEvent.click(screen.getByRole("button", { name: "保存授权配置" }));
    await screen.findByText("已保存，未验证");
    expect(screen.getByLabelText("接口密钥")).toHaveValue("");
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
    expect(requests).toEqual(["保存"]);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state))).not.toContain(testKey);
    expect(client.getMutationCache().getAll()).toHaveLength(0);
    expect(dispatch.mock.calls.filter(([event]) => event.type === "media-model-catalog-updated")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "验证连接与权限" }));
    await screen.findByText("当前模型支持双参考");
    expect(requests).toEqual(["保存", "验证"]);
    expect(screen.getByText("参考视频：支持")).toBeInTheDocument();
    expect(screen.getByText("参考图片：支持")).toBeInTheDocument();
    expect(screen.getByText(/不是视频生成实测/)).toBeInTheDocument();
    expect(dispatch.mock.calls.filter(([event]) => event.type === "media-model-catalog-updated")).toHaveLength(2);
  });

  it("does not read a key back from the server and invalidates support on unsaved edits", async () => {
    server.use(http.get(`${path}/config`, () => HttpResponse.json({ ...connectedConfig, apiKey: testKey })));
    const { client } = renderSettings();
    await screen.findByText("当前模型支持双参考");
    expect(screen.getByLabelText("接口密钥")).toHaveValue("");
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state))).not.toContain(testKey);
    fireEvent.change(screen.getByLabelText("视频模型标识"), { target: { value: model20 } });
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证连接与权限" })).toBeDisabled();
    expect(screen.getByText("配置已修改，请先保存再验证")).toBeInTheDocument();
  });

  it("clears old connection capabilities after verification fails without echoing upstream errors", async () => {
    server.use(
      http.get(`${path}/config`, () => HttpResponse.json(connectedConfig)),
      http.post(`${path}/verify`, () => HttpResponse.json({ error: `denied ${testKey}` }, { status: 403 })),
    );
    const { client } = renderSettings();
    await screen.findByText("当前模型支持双参考");
    fireEvent.click(screen.getByRole("button", { name: "验证连接与权限" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("权限");
    expect(document.body.textContent).not.toContain(testKey);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state))).not.toContain(testKey);
  });

  it.each([
    { ...connectedConfig, provider: "doubao_work" },
    { ...connectedConfig, connected: false },
    { ...connectedConfig, configured: false },
    { ...connectedConfig, error: "权限不足" },
  ])("does not promote native or unverified status to dual-reference support: %j", async (status) => {
    server.use(http.get(`${path}/config`, () => HttpResponse.json(status)));
    renderSettings();
    await waitFor(() => expect(screen.queryByText("正在读取配置")).not.toBeInTheDocument());
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
    expect(screen.queryByText("参考视频：支持")).not.toBeInTheDocument();
  });

  it("reports image-only verified capability without claiming dual-reference support", async () => {
    server.use(http.get(`${path}/config`, () =>
      HttpResponse.json({ ...connectedConfig, supportsReferenceVideo: false })));
    renderSettings();
    await screen.findByText("已连接，当前模型不支持双参考");
    expect(screen.getByText("参考视频：不支持")).toBeInTheDocument();
    expect(screen.getByText("参考图片：支持")).toBeInTheDocument();
  });

  it("fails closed when the configuration endpoint is missing and offers a safe retry", async () => {
    server.use(http.get(`${path}/config`, () => new HttpResponse(null, { status: 404 })));
    renderSettings();
    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("接口尚未接通");
    expect(screen.getByRole("button", { name: "重新读取配置" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "验证连接与权限" })).toBeDisabled();
    expect(screen.queryByText("当前模型支持双参考")).not.toBeInTheDocument();
  });

  it.each(["", "ep-other", "doubao-seedance-2-5-261015"])("rejects unconfirmed model %j before sending the key", async (model) => {
    const save = vi.fn();
    server.use(
      http.get(`${path}/config`, () => HttpResponse.json(config)),
      http.put(`${path}/config`, () => { save(); return HttpResponse.json(connectedConfig); }),
    );
    renderSettings();
    await enterConfig();
    fireEvent.change(screen.getByLabelText("视频模型标识"), { target: { value: model } });
    fireEvent.click(screen.getByRole("button", { name: "保存授权配置" }));
    expect(screen.getByRole("alert")).toHaveTextContent("三个视频模型标识");
    expect(save).not.toHaveBeenCalled();
  });

  it.each([model20, model20Fast])("saves confirmed video model %s", async (model) => {
    server.use(
      http.get(`${path}/config`, () => HttpResponse.json({ ok: true, data: config })),
      http.put(`${path}/config`, async ({ request }) => {
        expect(await request.json()).toEqual({ apiKey: testKey, baseUrl, model });
        return HttpResponse.json({ ok: true, data: { ...config, configured: true, model } });
      }),
    );
    renderSettings();
    await screen.findByText("未配置");
    fireEvent.change(screen.getByLabelText("接口密钥"), { target: { value: testKey } });
    fireEvent.change(screen.getByLabelText("视频模型标识"), { target: { value: model } });
    fireEvent.click(screen.getByRole("button", { name: "保存授权配置" }));
    await screen.findByText("已保存，未验证");
    expect(screen.getByRole("button", { name: "验证连接与权限" })).toBeEnabled();
  });
});
