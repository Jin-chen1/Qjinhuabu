// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import type { Options } from "ky";
import { api } from "@/lib/api";

export const DUAL_REFERENCE_QUERY_KEY = ["dual-reference-config"] as const;
export const ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3";
export const ARK_DEFAULT_MODEL = "doubao-seedance-2-5-260628";
export const ARK_VIDEO_MODELS = [
  ARK_DEFAULT_MODEL,
  "doubao-seedance-2-0-260128",
  "doubao-seedance-2-0-fast-260128",
] as const;
export const ARK_CONSOLE_URL = "https://console.volcengine.com/ark/";
const configPath = "api/v1/freezone/video/dual-reference/config";

const statusSchema = z.object({
  configured: z.boolean(),
  provider: z.string(),
  baseUrl: z.string(),
  model: z.string(),
  connected: z.boolean(),
  supportsReferenceVideo: z.boolean().default(false),
  supportsReferenceImage: z.boolean().default(false),
  error: z.string().nullish(),
  verificationLevel: z.string().default("none"),
  generationVerified: z.boolean().default(false),
});

export type DualReferenceConfig = z.infer<typeof statusSchema>;
export interface DualReferenceConfigInput {
  apiKey?: string;
  baseUrl: string;
  model: string;
}

export function isSecureDualReferenceEndpoint(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "ark.cn-beijing.volces.com" &&
      (!url.port || url.port === "443") && url.pathname.replace(/\/$/, "") === "/api/v3" &&
      !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

export function isSupportedDualReferenceModel(model: string): boolean {
  return ARK_VIDEO_MODELS.some((confirmed) => confirmed === model);
}

export function disconnectDualReference(config: DualReferenceConfig): DualReferenceConfig {
  return {
    ...config, connected: false, supportsReferenceVideo: false,
    supportsReferenceImage: false, verificationLevel: "none",
    generationVerified: false,
  };
}

class DualReferenceError extends Error {}

function responseError(status: number): Error {
  if (status === 404 || status === 405) {
    return new DualReferenceError("双参考服务接口尚未接通，请由后端完成配置与验证接口。");
  }
  if (status === 401 || status === 403) {
    return new DualReferenceError("授权验证未通过，请检查接口密钥和所选模型的权限。");
  }
  if (status === 429) return new DualReferenceError("服务请求过于频繁，请稍后再验证。");
  if (status === 400 || status === 422) {
    return new DualReferenceError("服务未接受配置，请检查服务端点、接口密钥及模型标识。");
  }
  return new DualReferenceError("双参考服务请求失败，请检查后端服务后重试。");
}

async function requestConfig(path: string, options: Options = {}): Promise<DualReferenceConfig> {
  try {
    const response = await api(path, {
      ...options, retry: 0, timeout: 30_000, throwHttpErrors: false, cache: "no-store",
    });
    if (!response.ok) throw responseError(response.status);
    const body: unknown = await response.json();
    const envelope = z.object({ ok: z.literal(true), data: z.unknown() }).safeParse(body);
    const parsed = statusSchema.safeParse(envelope.success ? envelope.data.data : body);
    if (!parsed.success) throw new DualReferenceError("双参考服务返回了无效配置，请检查后端接口。");
    // Only allowlisted status fields reach the query cache; never retain key previews or raw errors.
    const status = parsed.data;
    const native = /doubao|native/i.test(status.provider);
    const connected = status.configured && status.connected && !status.error && !native &&
      isSupportedDualReferenceModel(status.model) && isSecureDualReferenceEndpoint(status.baseUrl) &&
      status.verificationLevel === "credentials_and_model_catalog";
    const safe = {
      ...status,
      connected: Boolean(connected),
      generationVerified: false,
      error: status.error ? "授权未通过，请检查密钥、模型权限和服务端点。" : "",
    };
    return connected ? safe : disconnectDualReference(safe);
  } catch (error) {
    // Do not propagate request/response objects containing credentials into React Query or UI.
    if (error instanceof DualReferenceError) {
      throw new Error(error.message);
    }
    throw new Error("双参考服务请求未完成，请检查网络或后端服务后重新读取配置。");
  }
}

export function useDualReferenceConfig(enabled = true) {
  return useQuery({
    queryKey: DUAL_REFERENCE_QUERY_KEY,
    queryFn: ({ signal }) => requestConfig(configPath, { signal }),
    enabled,
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
}

// Imperative writes deliberately avoid mutation-cache variables, which would retain the API key.
export async function saveDualReferenceConfig(
  input: DualReferenceConfigInput,
): Promise<DualReferenceConfig> {
  const result = await requestConfig(configPath, { method: "put", json: input });
  if (!result.configured || result.error) {
    throw new Error("服务未接受配置，请检查服务端点、接口密钥及模型标识。");
  }
  return disconnectDualReference(result);
}

export function verifyDualReferenceConfig(): Promise<DualReferenceConfig> {
  return requestConfig("api/v1/freezone/video/dual-reference/verify", { method: "post" });
}
