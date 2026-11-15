// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, useCanGoBack, useRouter } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { HTTPError } from "ky";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { isCeRuntime } from "@/lib/runtime-config";
import { useLocalDoubaoStatus } from "@/lib/queries/doubao-work";

type Mode = "text" | "image" | "video";
type Job = {
  id: string; mode: Mode; prompt: string; status: string; created: number;
  text: string; error: string | null;
  files: { name: string; kind: string; width: number; height: number; duration?: number }[];
};
const labels: Record<string, string> = {
  queued: "等待提交", session_pending: "创建会话", upload_pending: "上传参考图片",
  event_pending: "提交请求", submitted: "正在生成", delivery_pending: "下载并校验文件",
  completed: "已完成", failed: "未完成", outcome_unknown: "受理结果待核验",
};
const terminal = new Set(["completed", "failed", "outcome_unknown"]);
const root = "api/v1/doubao-work";

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取图片"));
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
}

function JobResult({ job: initial }: { job: Job }) {
  const query = useQuery({
    queryKey: ["doubao-job", initial.id],
    queryFn: () => api.get(`${root}/jobs/${initial.id}`, { timeout: 240_000 }).json<Job>(),
    initialData: initial,
    refetchInterval: (query) => terminal.has(query.state.data?.status ?? "") ? false : 5000,
    retry: false,
  });
  const job = query.data;
  return (
    <article className="space-y-4 border-b border-border py-6" aria-label={`任务 ${job.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">
          {new Date(job.created * 1000).toLocaleString()} · {job.id.slice(0, 8)}
        </span>
        <span role="status" className="text-sm">{labels[job.status] ?? job.status}</span>
      </div>
      <p className="whitespace-pre-wrap break-words">{job.prompt}</p>
      {job.text && <div className="rounded-lg bg-muted/40 p-4 text-sm leading-7">
        <p className="mb-2 text-xs text-muted-foreground">模型回复 · 尺寸、时长和水印以实际文件为准</p>
        <div className="whitespace-pre-wrap break-words">{job.text}</div>
      </div>}
      {job.files.map((file) => {
        const url = `/${root}/jobs/${job.id}/files/${file.name}`;
        return (
          <figure key={file.name} className="space-y-2">
            {file.kind === "image"
              ? <img src={url} alt="生成结果" className="max-h-[480px] max-w-full rounded-lg object-contain" />
              : <video src={url} controls preload="metadata" className="max-h-[480px] max-w-full rounded-lg" />}
            <figcaption className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
              <span>{file.width} × {file.height}{file.duration ? ` · ${file.duration} 秒` : ""}</span>
              <a href={url} download={file.name} className="text-primary underline underline-offset-4">下载原文件</a>
            </figcaption>
          </figure>
        );
      })}
      {job.error && (
        <p role="alert" className="text-sm text-destructive">
          {job.status === "outcome_unknown"
            ? "请求可能已受理，请到豆包工作核验会话；本任务不会自动重复提交。"
            : job.error.startsWith("NO_")
              ? "回复未包含所需媒体产物，请查看回复内容。"
              : `查询或交付暂未完成（${job.error}）。请确认本地 API 已启动且授权有效。`}
        </p>
      )}
      {(query.error || job.status === "delivery_pending" || job.error === "QUERY_OR_DELIVERY_FAILED") &&
        <Button variant="outline" onClick={() => void query.refetch()}>继续查询与下载</Button>}
    </article>
  );
}

function Workbench() {
  const [mode, setMode] = useState<Mode>("text");
  const [prompt, setPrompt] = useState("");
  const [refs, setRefs] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const status = useLocalDoubaoStatus(true, false);
  const history = useQuery({
    queryKey: ["doubao-jobs"],
    queryFn: () => api.get(`${root}/jobs`).json<Job[]>(),
    refetchInterval: 10000,
    retry: false,
  });
  const [pendingId, setPendingId] = useState(() => sessionStorage.getItem("doubao-pending-id"));
  const submitLock = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitLock.current || pendingId || !prompt.trim()) return;
    submitLock.current = true;
    setBusy(true);
    setError("");
    let id: string | null = null;
    try {
      if (refs.length > 9 || refs.some((file) => file.size > 20 * 1024 * 1024)
        || refs.reduce((sum, file) => sum + file.size, 0) > 40 * 1024 * 1024) {
        throw new Error("最多9张参考图，每张20MB，总计40MB");
      }
      const attachments = await Promise.all(refs.map(async (file) => ({
        name: file.name, contentBase64: await fileBase64(file),
      })));
      id = crypto.randomUUID();
      sessionStorage.setItem("doubao-pending-id", id);
      setPendingId(id);
      await api.post(`${root}/jobs`, {
        json: { id, mode, prompt, refs: attachments }, retry: 0, timeout: 120_000,
      }).json<Job>();
      sessionStorage.removeItem("doubao-pending-id");
      setPendingId(null);
      setPrompt("");
      setRefs([]);
      if (fileInput.current) fileInput.current.value = "";
      await history.refetch();
    } catch (cause) {
      const rejected = cause instanceof HTTPError && [400, 403, 413, 422].includes(cause.response.status);
      if (rejected) {
        sessionStorage.removeItem("doubao-pending-id");
        setPendingId(null);
      }
      setError(rejected ? "请求被拒绝，请检查参考图片格式、大小及任务描述。"
        : id ? "连接中断。请先核验任务状态，避免重复生成。" : String(cause));
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }
  async function checkPending() {
    if (!pendingId) return;
    try {
      await api.get(`${root}/jobs/${pendingId}`, { retry: 0, timeout: 240_000 }).json<Job>();
      sessionStorage.removeItem("doubao-pending-id");
      setPendingId(null);
      setError("");
      await history.refetch();
    } catch {
      setError("任务尚未查询到。请检查本地服务，并在豆包工作核验是否已受理。");
    }
  }
  if (!isCeRuntime()) return <p>豆包工作仅在本机社区版中提供。</p>;
  return (
    <div className="mx-auto max-w-5xl space-y-8 pb-16">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h1 className="text-2xl font-semibold">豆包工作</h1>
          <span role="status" className="text-sm text-muted-foreground">
            {status.ready ? "本地 API 已连接" : status.isLoading ? "检查连接…" : "本地 API 未连接"}
          </span>
        </div>
        <p className="text-sm leading-6 text-muted-foreground">
          文本对话、图片识别、图片生成与视频生成。结果保留平台水印，生成会使用当前账号额度。
        </p>
      </div>
      {!status.ready && !status.isLoading &&
        <p role="alert" className="rounded-lg border border-border p-4 text-sm">请启动“启动API.command”，并通过“授权豆包工作.command”完成授权。</p>}
      <form onSubmit={(event) => void submit(event)} className="space-y-5 rounded-xl border border-border p-5 sm:p-7">
        <div className="flex flex-wrap gap-2" aria-label="生成类型">
          {(["text", "image", "video"] as const).map((value) =>
            <Button key={value} type="button" variant={mode === value ? "default" : "outline"}
              disabled={busy || Boolean(pendingId)} aria-pressed={mode === value} onClick={() => setMode(value)}>
              {value === "text" ? "文本 / 图片识别" : value === "image" ? "图片生成" : "视频生成"}
            </Button>)}
        </div>
        <div className="space-y-2">
          <Label htmlFor="doubao-prompt">任务描述</Label>
          <Textarea id="doubao-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)}
            disabled={busy || Boolean(pendingId)} maxLength={20000} rows={5} required
            placeholder={mode === "text" ? "输入问题，或上传图片后描述需要识别的内容"
              : mode === "image" ? "描述商品、背景、构图和图片用途" : "描述画面、动作和镜头，可上传参考图片"} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="doubao-refs">参考图片（可选）</Label>
          <input ref={fileInput} id="doubao-refs" type="file" multiple
            accept="image/png,image/jpeg,image/webp" disabled={busy || Boolean(pendingId)}
            onChange={(event) => setRefs(Array.from(event.target.files ?? []))}
            className="block w-full text-sm file:mr-4 file:rounded-md file:border file:border-border file:bg-muted file:px-3 file:py-2 file:text-foreground" />
          <p className="text-xs text-muted-foreground">PNG / JPEG / WebP · 最多9张 · 每张20MB · 总计40MB</p>
          {refs.length > 0 && <p className="break-words text-sm">{refs.map((file) => file.name).join("、")}</p>}
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {pendingId && <div className="space-y-2 text-sm">
          <p className="break-all">待核验任务：{pendingId}</p>
          <Button type="button" variant="outline" disabled={busy} onClick={() => void checkPending()}>核验提交状态</Button>
        </div>}
        <Button type="submit" disabled={busy || Boolean(pendingId) || !status.ready || !prompt.trim()}>
          {busy ? "正在提交…" : "开始任务"}
        </Button>
      </form>
      <section aria-label="任务历史">
        <h2 className="text-lg font-medium">任务历史</h2>
        {history.error && <p role="alert" className="py-4 text-sm text-destructive">无法读取任务历史，请检查 Q 金 服务。</p>}
        {history.data?.length === 0 && <p className="py-6 text-sm text-muted-foreground">提交第一个任务后，结果会显示在这里。刷新页面可继续查询。</p>}
        {history.data?.map((job) => <JobResult key={job.id} job={job} />)}
      </section>
    </div>
  );
}

function WorkbenchPage() {
  const router = useRouter();
  const canGoBack = useCanGoBack();

  return (
    <div
      className="-m-6 min-h-[calc(100%+3rem)] p-6"
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        if (canGoBack) {
          router.history.back();
        } else {
          void router.navigate({ to: "/" });
        }
      }}
    >
      <Workbench />
    </div>
  );
}

export const Route = createFileRoute("/_app/doubao-work")({ component: WorkbenchPage });
