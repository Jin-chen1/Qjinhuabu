import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface MediaLibraryStatus {
  enabled: boolean;
  url: string | null;
  saved: number;
  pending: number;
  unknown: number;
  state: "disabled" | "initializing" | "ready" | "attention";
}

export function useMediaLibrary() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["local-media-library"],
    queryFn: ({ signal }) =>
      api.get("api/v1/doubao-work/media-library", { signal, timeout: 10_000 })
        .json<MediaLibraryStatus>(),
    refetchInterval: 5_000,
    retry: false,
  });
  const sync = useMutation({
    mutationFn: () => api.post("api/v1/doubao-work/media-library/sync", { retry: 0 })
      .json<MediaLibraryStatus>(),
    onSuccess: (data) => {
      client.setQueryData(["local-media-library"], data);
    },
  });
  return { ...query, sync };
}
