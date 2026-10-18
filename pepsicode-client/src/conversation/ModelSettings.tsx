import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSessionStore, type ModelCapabilities } from "../stores/session";
import styles from "./ModelSettings.module.css";
import ProviderSetup from "./ProviderSetup";

export const effortLabels: Record<string, string> = {
  default: "默认", off: "关闭思考", low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高",
};

export default function ModelSettings({ onClose }: { onClose: () => void }) {
  const { model, provider, providers, reasoningEffort, models, catalogError, loadProviderModels, isRunning, isMock, modelUpdating, updateModel } = useSessionStore();
  const [adding, setAdding] = useState(false);
  const [draftProvider, setDraftProvider] = useState(provider);
  const [choices, setChoices] = useState(models);
  const [catalogNotice, setCatalogNotice] = useState(catalogError);
  const [refreshing, setRefreshing] = useState(false);
  const [draftModel, setDraftModel] = useState(model);
  const [effort, setEffort] = useState(reasoningEffort);
  const [persist, setPersist] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const selectedModel = draftModel.trim();
  const capabilities: ModelCapabilities | undefined = choices.find((item) => item.id === selectedModel);
  const busy = isRunning || modelUpdating || refreshing;

  async function refresh() {
    setRefreshing(true);
    setError("");
    try {
      const result = await loadProviderModels(draftProvider, true);
      setChoices(result.models);
      setCatalogNotice(result.catalogError);
    }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setRefreshing(false); }
  }

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  useEffect(() => {
    if (capabilities && !capabilities.reasoningLevels.includes(effort)) setEffort("default");
  }, [capabilities, effort]);

  useEffect(() => {
    let cancelled = false;
    setRefreshing(true);
    setError("");
    loadProviderModels(draftProvider).then((result) => {
      if (!cancelled) { setChoices(result.models); setCatalogNotice(result.catalogError); }
    }).catch((e) => {
      if (!cancelled) setError(e instanceof Error ? e.message : String(e));
    }).finally(() => { if (!cancelled) setRefreshing(false); });
    return () => { cancelled = true; };
  }, [draftProvider, loadProviderModels]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !capabilities || capabilities.id !== selectedModel) return;
    setError("");
    try {
      await updateModel(selectedModel, effort, persist, draftProvider);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <dialog ref={dialogRef} className={styles.dialog} onCancel={(event) => { if (modelUpdating) event.preventDefault(); else onClose(); }} aria-labelledby="model-settings-title">
      {adding ? <ProviderSetup onDone={onClose} onCancel={() => setAdding(false)} /> : <form onSubmit={submit}>
        <h2 id="model-settings-title">模型与思考深度</h2>
        <p>从下一轮对话生效，保留当前聊天记录。</p>
        <button type="button" onClick={() => setAdding(true)} disabled={isRunning || modelUpdating}>+ 添加自定义厂商</button>
        {providers.length > 0 && <label className={styles.field}>
          服务商
          <select value={draftProvider} disabled={busy} onChange={(event) => {
            if (event.target.value === "__add__") { setAdding(true); return; }
            setDraftProvider(event.target.value); setDraftModel(""); setEffort("default"); setChoices([]); setCatalogNotice("");
          }}>
            {providers.map((item) => <option key={item.id} value={item.id} disabled={!item.configured}>{item.name}{item.configured ? "" : "（未配置 Key）"}</option>)}
            <option value="__add__">+ 添加自定义厂商…</option>
          </select>
        </label>}
        <label className={styles.field}>
          模型
          <select autoFocus value={draftModel} disabled={busy} required
            onChange={(event) => { setDraftModel(event.target.value); setEffort("default"); }}>
            {!choices.some((item) => item.id === draftModel) && <option value={draftModel} disabled>{draftModel ? `${draftModel}（未在接口列表中）` : "请选择模型"}</option>}
            {choices.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}{item.name && item.name !== item.id ? ` (${item.id})` : ""}</option>)}
          </select>
        </label>
        <p>名称来自当前 API，选择后使用对应的模型 ID 发起请求。</p>
        <button type="button" onClick={() => void refresh()} disabled={busy}>{refreshing ? "读取中…" : "刷新模型列表"}</button>
        {catalogNotice && <p className={styles.error} role="status">{catalogNotice}</p>}
        <label className={styles.field}>
          思考深度
          <select value={effort} onChange={(event) => setEffort(event.target.value)} disabled={busy || !capabilities || capabilities.reasoningLevels.length <= 1}>
            {(capabilities?.reasoningLevels || ["default"]).map((level) => (
              <option key={level} value={level}>{effortLabels[level] || level}</option>
            ))}
          </select>
        </label>
        {capabilities?.reasoningMode === "none" && <p>暂未声明该模型的思考能力，使用服务端默认设置。</p>}
        <label className={styles.checkbox}>
          <input type="checkbox" checked={persist} onChange={(event) => setPersist(event.target.checked)} disabled={busy} />
          同时保存为新会话默认设置
        </label>
        <p>切换服务商会使用该服务商独立的 API 地址与 Key。环境变量仍优先于保存的默认设置。</p>
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.actions}>
          <button type="button" onClick={onClose} disabled={modelUpdating}>取消</button>
          <button type="submit" disabled={busy || isMock || !capabilities || capabilities.id !== selectedModel}>
            {modelUpdating ? "应用中…" : "应用"}
          </button>
        </div>
      </form>}
    </dialog>
  );
}
