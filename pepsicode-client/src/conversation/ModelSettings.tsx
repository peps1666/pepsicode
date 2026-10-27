import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSessionStore, type ModelCapabilities } from "../stores/session";
import styles from "./ModelSettings.module.css";
import ProviderSetup from "./ProviderSetup";
import Select from "../components/Select";
import Button from "../components/Button";
import Icon from "../components/Icon";

export const effortLabels: Record<string, string> = {
  default: "默认", off: "关闭思考", low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高",
};

export default function ModelSettings({ onClose }: { onClose: () => void }) {
  const { model, provider, providers, reasoningEffort, models, catalogError, loadProviderModels, isRunning, isMock, modelUpdating, updateModel } = useSessionStore();
  const [setupProvider, setSetupProvider] = useState<string | null>(null);
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
  const unconfigured = isMock || !providers.find((item) => item.id === draftProvider)?.configured;

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
    if (unconfigured) { setRefreshing(false); return; }
    setRefreshing(true);
    setError("");
    loadProviderModels(draftProvider).then((result) => {
      if (!cancelled) { setChoices(result.models); setCatalogNotice(result.catalogError); }
    }).catch((e) => {
      if (!cancelled) setError(e instanceof Error ? e.message : String(e));
    }).finally(() => { if (!cancelled) setRefreshing(false); });
    return () => { cancelled = true; };
  }, [draftProvider, loadProviderModels, unconfigured]);

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
      {setupProvider !== null ? <ProviderSetup initialProvider={setupProvider} onDone={onClose} onCancel={() => setSetupProvider(null)} /> : <form className={styles.form} onSubmit={submit}>
        <header className={styles.heading}>
          <span className={styles.headingIcon}><Icon name="settings" size={21} /></span>
          <div><h2 id="model-settings-title">模型设置</h2><p>选择服务商、模型与思考深度</p></div>
          <Button variant="ghost" className={styles.close} aria-label="关闭模型设置" onClick={onClose} disabled={modelUpdating}><Icon name="close" size={17} /></Button>
        </header>
        <div className={styles.section}>
          <div className={styles.field}>
            <div className={styles.fieldHeading}><span>服务商</span><Button variant="ghost" className={styles.inlineButton} onClick={() => setSetupProvider(draftProvider || "deepseek")} disabled={isRunning || modelUpdating}>配置 API Key</Button></div>
            <Select label="服务商" value={draftProvider} disabled={isRunning || modelUpdating} placeholder="选择服务商"
              options={[...providers.map(item => ({ value: item.id, label: item.name, description: item.configured ? "已配置" : "填写 API Key 后使用" })), { value: "__add__", label: "添加自定义厂商", separator: true }]}
              onChange={(value) => {
                if (value === "__add__") { setSetupProvider("custom"); return; }
                const selected = providers.find(item => item.id === value);
                if (selected && !selected.configured) { setSetupProvider(selected.id); return; }
                setDraftProvider(value); setDraftModel(""); setEffort("default"); setChoices([]); setCatalogNotice("");
              }} />
            {unconfigured && <p className={styles.hint}>首次使用？选择服务商即可填写 API Key。</p>}
          </div>
          <div className={styles.field}>
            <div className={styles.fieldHeading}><span>模型</span><Button variant="ghost" className={styles.inlineButton} onClick={() => void refresh()} disabled={busy || unconfigured}><Icon name="refresh" size={13} />{refreshing ? "读取中" : "刷新列表"}</Button></div>
            <Select label="模型" value={draftModel} disabled={busy || unconfigured} autoFocus={!unconfigured}
              placeholder={unconfigured ? "请先配置服务商" : refreshing ? "正在读取模型…" : "选择模型"}
              options={unconfigured ? [] : choices.map(item => ({ value: item.id, label: item.name || item.id, description: item.name && item.name !== item.id ? item.id : undefined }))}
              onChange={value => { setDraftModel(value); setEffort("default"); }} />
            {catalogNotice && <p className={styles.notice} role="status">{catalogNotice}</p>}
          </div>
          <div className={styles.field}>
            <span className={styles.fieldTitle}>思考深度</span>
            <Select label="思考深度" value={effort} options={(capabilities?.reasoningLevels || ["default"]).map(level => ({ value: level, label: effortLabels[level] || level }))}
              onChange={setEffort} disabled={busy || !capabilities || capabilities.reasoningLevels.length <= 1} />
            <p className={styles.hint}>{capabilities?.reasoningMode === "none" ? "当前模型使用服务端默认思考设置。" : "新设置从下一轮对话生效，当前聊天记录会保留。"}</p>
          </div>
        </div>
        <label className={styles.preference}>
          <input type="checkbox" checked={persist} onChange={event => setPersist(event.target.checked)} disabled={busy} />
          <span><strong>设为新会话默认</strong><small>下次新建对话时使用此配置</small></span>
        </label>
        {error && <p className={styles.error} role="alert">{error}</p>}
        <footer className={styles.actions}>
          <Button variant="ghost" className={styles.footerNote} onClick={() => { void window.pepsiAPI.showAbout(); }}>关于 Pepsicode</Button>
          <Button onClick={onClose} disabled={modelUpdating}>取消</Button>
          <Button variant="primary" type="submit" disabled={busy || isMock || !capabilities || capabilities.id !== selectedModel}>{modelUpdating ? "应用中…" : "应用设置"}</Button>
        </footer>
      </form>}
    </dialog>
  );
}
