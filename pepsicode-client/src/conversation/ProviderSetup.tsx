import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSessionStore, type ModelCapabilities, type ProviderDraft } from "../stores/session";
import styles from "./ModelSettings.module.css";

const steps = ["厂商名称", "API 地址", "鉴权方式", "API Key", "模型列表地址", "选择模型", "确认配置"];

export default function ProviderSetup({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { probeProvider, saveProvider, cancelProviderSetup, providers } = useSessionStore();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<ProviderDraft>({ provider: "custom", name: "", baseUrl: "", authType: "bearer", apiKey: "", modelsUrl: "" });
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
    setDraft((previous) => ({ ...previous, [field]: value }));
    setError("");
  }

  function cancel() {
    active.current = false;
    setDraft((previous) => ({ ...previous, apiKey: "" }));
    void cancelProviderSetup().catch(() => {});
    onCancel();
  }

  async function next(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (step === 0 && !draft.name.trim()) { setError("请输入厂商名称"); return; }
    if (step === 1) {
      try {
        const url = new URL(draft.baseUrl);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
        if (/\/(messages|chat\/completions|responses)\/?$/.test(url.pathname)) throw new Error();
      } catch { setError("请填写不含凭据和调用路径的 Anthropic 兼容 Base URL"); return; }
    }
    if (step === 3 && (!draft.apiKey.trim() || /\s/.test(draft.apiKey.trim()))) { setError("请填写不包含空格或换行的 API Key"); return; }
    if (step === 4 || step === 6) {
      setBusy(true);
      try {
        if (step === 4) {
          const result = await probeProvider(draft);
          if (!active.current) return;
          setModels(result.models); setToken(result.token); setVerified(result.verified);
          setNotice(result.catalogError); setModel(""); setStep(5);
        } else {
          await saveProvider(token, model);
          if (!active.current) return;
          setDraft((previous) => ({ ...previous, apiKey: "" }));
          onDone();
        }
      } catch (e) {
        if (active.current) setError(e instanceof Error ? e.message : String(e));
      } finally { if (active.current) setBusy(false); }
      return;
    }
    setStep(step + 1);
  }

  return <form onSubmit={(event) => void next(event)}>
    <h2 id="model-settings-title">添加自定义厂商 · {step + 1}/{steps.length}</h2>
    <p>接口格式：Anthropic Messages 兼容。完成前不会保存配置。</p>
    {step === 0 && <label className={styles.field}>{steps[step]}
      <input key={step} autoFocus required maxLength={60} value={draft.name} onChange={(event) => change("name", event.target.value)} />
      <span>起一个便于在服务商菜单中识别的名字。</span>
    </label>}
    {step === 1 && <label className={styles.field}>{steps[step]}
      <input key={step} autoFocus type="url" required value={draft.baseUrl} onChange={(event) => change("baseUrl", event.target.value)} />
      <span>填写服务商提供的 Anthropic 兼容 Base URL，例如 https://gateway.example/anthropic，省略 /messages 调用路径。</span>
    </label>}
    {step === 2 && <label className={styles.field}>{steps[step]}
      <select autoFocus value={draft.authType} onChange={(event) => change("authType", event.target.value)}>
        <option value="bearer">Bearer Token（Authorization）</option>
        <option value="api_key">API Key（x-api-key）</option>
      </select>
      <span>请按服务商文档选择鉴权方式。</span>
    </label>}
    {step === 3 && <label className={styles.field}>{steps[step]}
      <input key={step} autoFocus type="password" required autoComplete="new-password" value={draft.apiKey} onChange={(event) => change("apiKey", event.target.value)} />
      <span>输入已隐藏，保存在本机配置文件中，不进入聊天记录。</span>
    </label>}
    {step === 4 && <>
      <label className={styles.field}>{steps[step]}（可选）
        <input key={step} autoFocus value={draft.modelsUrl || ""} disabled={draft.modelsUrl === false} onChange={(event) => change("modelsUrl", event.target.value)} />
        <span>留空自动查找，也可填写同源的目录路径。读取只发送 GET 请求。</span>
      </label>
      <label className={styles.checkbox}><input type="checkbox" checked={draft.modelsUrl === false} onChange={(event) => change("modelsUrl", event.target.checked ? false : "")} />服务商不提供模型列表，手动填写模型 ID</label>
    </>}
    {step === 5 && <>
      {notice && <p role="status">{notice}</p>}
      <label className={styles.field}>{models.length ? "选择接口返回的模型" : "模型 ID"}
        {models.length > 0 ? <select autoFocus required value={model} onChange={(event) => setModel(event.target.value)}>
          <option value="" disabled>请选择模型</option>
          {models.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}
        </select> : <input key={step} autoFocus required disabled={verified} value={model} onChange={(event) => setModel(event.target.value)} />}
        {!models.length && <span>{verified ? "接口返回了空列表，请返回检查地址或账号权限。" : "未验证可用性，请从服务商控制台或文档复制完整模型 ID。"}</span>}
      </label>
    </>}
    {step === 6 && <>
      <dl className={styles.summary}><dt>厂商</dt><dd>{draft.name}</dd><dt>API 地址</dt><dd>{draft.baseUrl}</dd><dt>鉴权</dt><dd>{draft.authType} · Key 已填写</dd><dt>模型</dt><dd>{model}</dd></dl>
      {!verified && <p>模型可用性尚未验证，首次对话将使用你填写的 ID。</p>}
      {providers.some((item) => item.id === "custom") && <p>保存后将更新已有的自定义厂商配置。</p>}
      <p>确认后写入本机 ~/.pepsi-code/settings.json，立即启用并设为新会话默认。环境变量仍优先于已保存的默认设置。</p>
    </>}
    {error && <p className={styles.error} role="alert">{error}</p>}
    <div className={styles.actions}>
      <button type="button" onClick={cancel} disabled={busy && step === 6}>取消</button>
      {step > 0 && <button type="button" disabled={busy} onClick={() => { setStep(step - 1); setError(""); }}>上一步</button>}
      <button type="submit" disabled={busy || (step === 5 && !model)}>{busy ? (step === 6 ? "保存中…" : "读取模型中…") : step === 6 ? "保存并使用" : step === 4 ? "读取模型列表" : "下一步"}</button>
    </div>
  </form>;
}
