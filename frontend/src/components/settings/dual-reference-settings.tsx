// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ARK_BASE_URL, ARK_CONSOLE_URL, ARK_DEFAULT_MODEL, ARK_VIDEO_MODELS, DUAL_REFERENCE_QUERY_KEY,
  disconnectDualReference, isSecureDualReferenceEndpoint,
  isSupportedDualReferenceModel,
  saveDualReferenceConfig, useDualReferenceConfig, verifyDualReferenceConfig,
  type DualReferenceConfig,
} from "@/lib/queries/dual-reference";

export function DualReferenceSettings({ open = true }: { open?: boolean }) {
  const id = useId();
  const queryClient = useQueryClient();
  const query = useDualReferenceConfig(open);
  const [apiKey, setApiKey] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [pending, setPending] = useState<"save" | "verify" | null>(null);
  const [error, setError] = useState("");
  const busyRef = useRef(false);
  const config = query.data;
  const savedBaseUrl = config?.baseUrl || ARK_BASE_URL;
  const savedModel = config?.model ?? ARK_DEFAULT_MODEL;
  const baseUrl = ARK_BASE_URL;
  const model = draft ?? savedModel;
  const dirty = Boolean(apiKey.trim()) || baseUrl !== savedBaseUrl || model !== savedModel;
  const busy = Boolean(pending) || query.isFetching;
  const connected = !busy && !dirty && !error && !query.isError && Boolean(config?.connected);
  const dualSupported = connected && config?.supportsReferenceVideo && config?.supportsReferenceImage;
  const alert = error || (query.isError ? query.error.message : "") || config?.error;
  const status = pending === "save" ? "正在保存配置"
    : pending === "verify" ? "正在验证连接与权限"
    : query.isFetching ? "正在读取配置"
    : dirty ? "配置已修改，请先保存再验证"
    : connected ? dualSupported ? "当前模型支持双参考" : "已连接，当前模型不支持双参考"
    : alert ? "未连接"
    : config?.configured ? "已保存，未验证" : "未配置";

  async function run(action: "save" | "verify") {
    if (busyRef.current || busy) return;
    if (action === "save" && !isSecureDualReferenceEndpoint(baseUrl.trim())) {
      setError("仅支持火山方舟北京区官方 HTTPS 接口。");
      return;
    }
    if (!isSupportedDualReferenceModel(model)) {
      setError("请选择下方列出的三个视频模型标识之一；不支持空模型或推理接入点。");
      return;
    }
    if (action === "save" && !apiKey.trim() && !config?.configured) {
      setError("首次授权配置需要填写接口密钥。");
      return;
    }
    if (action === "verify" && (!config?.configured || dirty || !model.trim())) return;
    busyRef.current = true;
    setPending(action);
    setError("");
    await queryClient.cancelQueries({ queryKey: DUAL_REFERENCE_QUERY_KEY });
    if (config) queryClient.setQueryData(DUAL_REFERENCE_QUERY_KEY, disconnectDualReference(config));
    try {
      let result: DualReferenceConfig;
      if (action === "save") {
        const input = {
          baseUrl: baseUrl.trim(), model: model.trim(),
          ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
        };
        setApiKey("");
        result = await saveDualReferenceConfig(input);
        setDraft(null);
      } else {
        result = await verifyDualReferenceConfig();
      }
      queryClient.setQueryData(DUAL_REFERENCE_QUERY_KEY, result);
      window.dispatchEvent(new Event("media-model-catalog-updated"));
      if (action === "verify" && !result.connected) {
        setError(result.error || "连接与权限验证未通过，请核对密钥、模型标识及授权范围。");
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "双参考服务请求失败，请稍后重试。");
    } finally {
      setApiKey("");
      busyRef.current = false;
      setPending(null);
    }
  }

  return (
    <section className="space-y-5 px-5 py-5" aria-labelledby={`${id}-title`}>
      <div className="space-y-2">
        <h3 id={`${id}-title`} className="text-sm font-medium">双参考服务授权配置</h3>
        <p className="text-sm leading-relaxed text-muted-foreground">
          单独授权可同时使用参考视频和参考图片的服务，不改变当前豆包工作模式或其他网关设置。
        </p>
      </div>
      <div className="space-y-2">
        <p className="text-sm">服务提供方：<span>火山方舟</span>（默认）</p>
        <a href={ARK_CONSOLE_URL} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-sm text-foreground underline underline-offset-4">
          打开火山方舟官方控制台
          <ExternalLink className="size-3.5" aria-hidden />
        </a>
        <p className="text-sm leading-relaxed text-muted-foreground">
          请在官方控制台登录、创建接口密钥并开通所选视频模型权限。没有密钥时不能完成授权。
        </p>
      </div>
      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void run("save"); }}>
        <div className="space-y-2">
          <Label htmlFor={`${id}-key`}>接口密钥</Label>
          <Input id={`${id}-key`} type="password" autoComplete="new-password" spellCheck={false}
            autoCapitalize="none" value={apiKey} disabled={busy} maxLength={4096}
            aria-describedby={`${id}-key-hint`}
            placeholder={config?.configured ? "已提交密钥；留空保留，输入新密钥可替换" : "请输入官方控制台创建的接口密钥"}
            onChange={(event) => { setApiKey(event.target.value); setError(""); }} />
          <p id={`${id}-key-hint`} className="text-sm leading-relaxed text-muted-foreground">
            密钥仅提交至本应用后端，不从服务端回显，不写入浏览器持久存储；提交后清空输入框。
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-endpoint`}>服务端点</Label>
          <Input id={`${id}-endpoint`} type="url" value={baseUrl} readOnly
            spellCheck={false} aria-describedby={`${id}-endpoint-hint`} />
          <p id={`${id}-endpoint-hint`} className="text-sm leading-relaxed text-muted-foreground">
            当前仅支持火山方舟北京区官方 HTTPS 地址及 /api/v3 路径，地址不可修改；不支持第三方端点。
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-model`}>视频模型标识</Label>
          <Input id={`${id}-model`} value={model} disabled={busy} spellCheck={false} maxLength={200}
            placeholder="填写下方三个精确视频模型标识之一"
            list={`${id}-models`}
            aria-describedby={`${id}-model-hint`}
            onChange={(event) => { setDraft(event.target.value); setError(""); }} />
          <datalist id={`${id}-models`}>
            {ARK_VIDEO_MODELS.map((confirmed) => <option key={confirmed} value={confirmed} />)}
          </datalist>
          <p id={`${id}-model-hint`} className="text-sm leading-relaxed text-muted-foreground">
            默认使用豆包视频模型 2.5。当前仅接受以下三个精确型号，必须在官方控制台开通相应权限；
            不支持留空、推理接入点或其他型号。
          </p>
          <ul className="space-y-1 break-all text-sm text-muted-foreground">
            {ARK_VIDEO_MODELS.map((confirmed) => <li key={confirmed}>{confirmed}</li>)}
          </ul>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" disabled={busy}>保存授权配置</Button>
          <Button type="button" size="sm" variant="outline"
            disabled={busy || dirty || !config?.configured || !isSupportedDualReferenceModel(model) || query.isError}
            onClick={() => void run("verify")}>验证连接与权限</Button>
          <Button type="button" size="sm" variant="ghost" disabled={busy || dirty}
            onClick={() => { setError(""); void query.refetch(); }}>重新读取配置</Button>
        </div>
      </form>
      <div className="space-y-2">
        <p role="status" className="flex items-center gap-2 text-sm">
          {busy && <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />}
          {status}
        </p>
        {alert && <p role="alert" className="text-sm leading-relaxed text-foreground">{alert}</p>}
        {connected && (
          <div className="space-y-1 text-sm">
            <p>参考视频：{config?.supportsReferenceVideo ? "支持" : "不支持"}</p>
            <p>参考图片：{config?.supportsReferenceImage ? "支持" : "不支持"}</p>
            <p>验证级别：密钥与模型目录；生成实测：未执行</p>
          </div>
        )}
        <p className="text-sm leading-relaxed text-muted-foreground">
          验证仅检查密钥、所选模型访问权限及官方支持目录，不创建付费视频任务；这是权限与目录核验，不是视频生成实测。
        </p>
      </div>
      <p className="text-sm leading-relaxed text-muted-foreground">
        提交时仅为本次参考视频生成临时访问链接，约 1 小时到期；图片通过官方接口直接传送；不会转发整个工作台。
      </p>
      <p className="text-sm leading-relaxed text-muted-foreground">
        原生豆包工作通道不支持视频参考，双参考能力保持为 0。单图、首帧或首尾帧图片输入不等于双参考；
        保存授权配置不代表已连接，只有验证通过后才展示所选模型支持的参考能力。
      </p>
    </section>
  );
}
