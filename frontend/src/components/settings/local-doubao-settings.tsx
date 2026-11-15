// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import { Loader2, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { useLocalDoubaoStatus } from "@/lib/queries/doubao-work";
import { useMediaLibrary } from "@/lib/queries/media-library";

export function LocalDoubaoSettings({
  connection,
}: {
  connection: ReturnType<typeof useLocalDoubaoStatus>;
}) {
  const { t } = useTranslation();
  const key = "settings.modelConfig.localDoubao";
  const state = connection.isLoading
    ? "checking"
    : connection.ready
      ? "connected"
      : connection.isError || !connection.data?.reachable
        ? "offline"
        : "unauthorized";
  const hint = state === "connected" ? "readyHint" : state === "unauthorized" ? "authorizeHint" : "startHint";

  return (
    <section className="space-y-4 px-5 py-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-medium">{t(`${key}.title`)}</h3>
        <span role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
          {connection.isLoading ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : (
            <span
              className={`size-2 rounded-full ${connection.ready ? "bg-success" : "bg-warning"}`}
              aria-hidden
            />
          )}
          {t(`${key}.${state}`)}
        </span>
      </div>
      {!connection.isLoading && <p className="text-sm leading-relaxed text-muted-foreground">{t(`${key}.${hint}`)}</p>}
      <p className="text-xs leading-relaxed text-muted-foreground">{t(`${key}.limitations`)}</p>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={connection.isFetching}
        onClick={() => void connection.refetch()}
      >
        <RotateCw className={`size-3.5 ${connection.isFetching ? "animate-spin" : ""}`} aria-hidden />
        {t(`${key}.refresh`)}
      </Button>
    </section>
  );
}

export function LocalDoubaoStorageSettings() {
  const { t } = useTranslation();
  const key = "settings.mediaStorage.localDoubao";
  const library = useMediaLibrary();
  const state = library.isError || library.sync.isError ? "offline"
    : library.isLoading ? "checking" : library.data?.state ?? "initializing";

  return (
    <section className="space-y-4 px-5 py-5">
      <h3 className="text-sm font-medium">{t(`${key}.title`)}</h3>
      <div className="space-y-3">
        <p className="text-sm font-medium">{t(`${key}.libraryTitle`)}</p>
        <p role="status" className="text-sm leading-relaxed text-muted-foreground">
          {t(`${key}.${state}`)}
        </p>
        {library.data && !library.isError && (
          <p className="text-sm text-muted-foreground">
            {t(`${key}.counts`, { saved: library.data.saved, pending: library.data.pending })}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-4">
          {library.data?.url && (
            <a href={library.data.url} target="_blank" rel="noreferrer"
              className="text-sm text-foreground underline underline-offset-4">
              {t(`${key}.openLibrary`)}
            </a>
          )}
          <Button type="button" size="sm" variant="outline"
            disabled={library.sync.isPending || library.data?.enabled === false}
            onClick={() => library.sync.mutate()}>
            <RotateCw className={`size-3.5 ${library.sync.isPending ? "animate-spin" : ""}`} aria-hidden />
            {t(`${key}.sync`)}
          </Button>
        </div>
      </div>
      <p className="text-sm leading-relaxed text-muted-foreground">
        {t(`${key}.references`)}
      </p>
      <p className="text-sm leading-relaxed text-muted-foreground">
        {t(`${key}.outputs`)}
      </p>
      <p className="text-xs leading-relaxed text-muted-foreground">
        {t(`${key}.backup`)}
      </p>
    </section>
  );
}
