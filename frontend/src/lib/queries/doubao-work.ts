// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { isCeRuntime } from "@/lib/runtime-config";

export interface LocalDoubaoStatus {
  reachable: boolean;
  connected: boolean;
}

export function useLocalDoubaoStatus(enabled = true, requireLocalMode = true) {
  const active =
    isCeRuntime() &&
    (!requireLocalMode || import.meta.env.VITE_DOUBAO_WORK_ONLY === "true") &&
    ["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname);
  const query = useQuery({
    queryKey: ["doubao-status"],
    queryFn: ({ signal }) =>
      api.get("api/v1/doubao-work/status", { signal, timeout: 10_000 }).json<LocalDoubaoStatus>(),
    enabled: enabled && active,
    refetchInterval: enabled && active ? 15_000 : false,
    retry: false,
  });
  return {
    ...query,
    active,
    ready: active && !query.isError && Boolean(query.data?.reachable && query.data?.connected),
  };
}
