import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSessionStore, type ModelCapabilities, type ProviderDraft } from "../stores/session";
import styles from "./ModelSettings.module.css";
import Select from "../components/Select";
import Button from "../components/Button";
import Icon from "../components/Icon";

export default function ProviderSetup({ initialProvider = "deepseek", onDone, onCancel }: { initialProvider?: string; onDone: () => void; onCancel: () => void }) {
  const { probeProvider, saveProvider, cancelProviderSetup, providers } = useSessionStore();
  function newDraft(id: string): ProviderDraft {
    const preset = providers.find(item => item.id === id);
    return { provider: id, name: id === "custom" ? "" : preset?.name || id, baseUrl: preset?.baseUrl || "", authType: "bearer", apiKey: "", modelsUrl: "" };
  }
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<ProviderDraft>(() => newDraft(initialProvider));
  const [models, setModels] = useState<ModelCapabilities[]>([]);
  const [model, setModel] = useState("");
  const [token, setToken] = useState("");
  const [verified, setVerified] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; void cancelProviderSetup().catch(() => {}); };
  }, [cancelProviderSetup]);

  function change(field: keyof ProviderDraft, value: string | false) {
    setDraft(previous => ({ ...previous, [field]: value }));
    setError("");
  }

  async function next(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (step === 1) {
      if (!model.trim() || /\s/.test(model.trim())) { setError("请填写完整模型 ID，不能包含空格"); return; }
      setStep(2); return;
    }
    if (step === 0) {
      if (!draft.name.trim()) { setError("请输入厂商名称"); return; }
      try {
        const url = new URL(draft.baseUrl);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || /\/(messages|chat\/completions|responses)\/?$/.test(url.pathname)) throw new Error();
      } catch { setError("请填写不含凭据和调用路径的 Anthropic 兼容 Base URL"); return; }
      if (!draft.apiKey.trim() || /\s/.test(draft.apiKey.trim())) { setError("请填写不包含空格或换行的 API Key"); return; }
    }
    setBusy(true);
    try {
      if (step === 0) {
        const result = await probeProvider({ ...draft, apiKey: draft.apiKey.trim() });
        if (!active.current) return;
        setModels(result.models); setToken(result.token); setVerified(result.verified);
        setNotice(result.catalogError); setModel(""); setStep(1);
      } else {
        await saveProvider(token, model.trim());
        if (!active.current) return;
        setDraft(previous => ({ ...previous, apiKey: "" }));
        onDone();
      }
    } catch (e) {
      if (active.current) setError(e instanceof Error ? e.message : String(e));
    } finally { if (active.current) setBusy(false); }
  }

  function cancel() { active.current = false; setDraft(previous => ({ ...previous, apiKey: "" })); onCancel(); }

  return <form className={styles.form} onSubmit={next}>
    <header className={styles.heading}>
      <span className={styles.headingIcon}><Icon name="settings" size={21} /></span>
      <div><h2 id="model-settings-title">配置服务商</h2><p>{["连接你的模型服务", "选择本次使用的模型", "检查并保存连接配置"][step]}</p></div>
      <Button variant="ghost" className={styles.close} aria-label="返回模型设置" onClick={cancel} disabled={busy && step === 2}><Icon name="close" size={17} /></Button>
    </header>
    <ol className={styles.steps} aria-label="配置进度">{["连接服务", "选择模型", "确认配置"].map((title, index) => <li key={title} aria-current={index === step ? "step" : undefined} data-complete={index < step || undefined}><span>{index < step ? <Icon name="check" size={12} /> : index + 1}</span>{title}</li>)}</ol>
    <div className={styles.section}>
    {step === 0 && <>
      <div className={styles.field}><span className={styles.fieldTitle}>服务商</span>
        <Select label="配置服务商" value={draft.provider} disabled={busy} onChange={value => { setDraft(newDraft(value)); setError(""); }}
          options={[...providers.filter(item => item.id !== "custom").map(item => ({ value: item.id, label: item.name })), { value: "custom", label: "自定义厂商", separator: true }]} />
      </div>
      {draft.provider === "custom" && <label className={styles.field}>厂商名称
        <input aria-label="厂商名称" required maxLength={60} value={draft.name} disabled={busy} onChange={event => change("name", event.target.value)} />
      </label>}
      <label className={styles.field}>API Key
        <input aria-label="API Key" autoFocus type="password" required autoComplete="new-password" value={draft.apiKey} disabled={busy} onChange={event => change("apiKey", event.target.value)} />
        <span>仅保存在本机配置中，不进入聊天记录。修改已有服务商时，请重新填写 Key。</span>
      </label>
      <details key={draft.provider} open={draft.provider === "custom" ? true : undefined} className={styles.advanced}>
        <summary>API 地址与高级设置</summary>
        <label className={styles.field}>API 地址
          <input aria-label="API 地址" type="url" required value={draft.baseUrl} disabled={busy} onChange={event => change("baseUrl", event.target.value)} />
          <span>使用 Anthropic Messages 兼容接口的 Base URL，省略 /messages。</span>
        </label>
        <label className={styles.field}>鉴权方式
        <Select label="鉴权方式" value={draft.authType} disabled={busy} onChange={value => change("authType", value)} options={[{ value: "bearer", label: "Bearer Token", description: "Authorization 请求头" }, { value: "api_key", label: "API Key", description: "x-api-key 请求头" }]} />
        </label>
        <label className={styles.field}>模型列表地址（可选）
          <input aria-label="模型列表地址" value={draft.modelsUrl || ""} disabled={busy || draft.modelsUrl === false} onChange={event => change("modelsUrl", event.target.value)} />
          <span>留空自动查找，也可填写同源路径。</span>
        </label>
        <label className={styles.checkbox}><input type="checkbox" disabled={busy} checked={draft.modelsUrl === false} onChange={event => change("modelsUrl", event.target.checked ? false : "")} />手动填写模型 ID，跳过模型列表读取</label>
      </details>
    </>}
    {step === 1 && <>
      {notice && <p role="status">{notice}</p>}
      <label className={styles.field}>{models.length ? "选择模型" : "模型 ID"}
        {models.length > 0 ? <Select label="选择模型" autoFocus value={model} onChange={setModel} options={models.map(item => ({ value: item.id, label: item.name || item.id, description: item.name && item.name !== item.id ? item.id : undefined }))} /> : <input aria-label="选择模型" autoFocus required disabled={verified} value={model} onChange={event => setModel(event.target.value)} />}
        {!models.length && <span>{verified ? "接口返回空列表，请返回检查地址或账号权限。" : "从服务商控制台或文档复制完整模型 ID，首次对话时验证可用性。"}</span>}
      </label>
    </>}
    {step === 2 && <>
      <dl className={styles.summary}><dt>服务商</dt><dd>{draft.name}</dd><dt>API 地址</dt><dd>{draft.baseUrl}</dd><dt>鉴权</dt><dd>{draft.authType} · Key 已填写</dd><dt>模型</dt><dd>{model}</dd></dl>
      {!verified && <p>模型可用性尚未验证，首次对话将使用你填写的 ID。</p>}
      {providers.some(item => item.id === draft.provider && item.configured) && <p>保存后将更新此服务商已有的配置。</p>}
      <p>配置保存在本机 ~/.pepsi-code/settings.json。环境变量优先于保存的默认设置。</p>
    </>}
    </div>
    {error && <p className={styles.error} role="alert">{error}</p>}
    <footer className={styles.actions}>
      <Button variant="ghost" onClick={cancel} disabled={busy && step === 2}>取消</Button>
      <span className={styles.actionSpacer} />
      {step > 0 && <Button disabled={busy} onClick={() => { setStep(step - 1); setError(""); }}>上一步</Button>}
      <Button variant="primary" type="submit" disabled={busy || (step === 1 && !model)}>{busy ? (step === 2 ? "保存中…" : "读取模型中…") : step === 2 ? "保存并使用" : "下一步"}</Button>
    </footer>
  </form>;
}
