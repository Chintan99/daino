import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { qk, useSettings } from "../../api/hooks";
import type {
  CatalogModel,
  ProviderCheck,
  ProviderForm,
  ProviderHealth,
} from "../../api/types";
import { useUIStore } from "../../store/uiStore";
import { ModelCombo } from "./ModelCombo";

/**
 * Provider setup, inside the agent column.
 *
 * This is where "which model am I talking to" is answered, so this is where
 * connecting one belongs — rather than in a menu three levels deep or, worse,
 * only in the CLI. Keys are never rendered back: the field is blank when editing
 * and blank means "keep the stored one".
 */

/** Local runtimes serve a handful of pulled models; hosted catalogs are huge. */
const SEARCHABLE: ProviderForm["type"][] = ["openrouter", "openai-compatible"];

const TYPES: { id: ProviderForm["type"]; label: string; base: string; hint: string }[] = [
  {
    id: "openrouter",
    label: "OpenRouter",
    base: "https://openrouter.ai/api/v1",
    hint: "Hosted models. The key is validated and the model must exist before saving.",
  },
  {
    id: "ollama",
    label: "Ollama",
    base: "http://127.0.0.1:11434/v1",
    hint: "Local models. Nothing leaves this machine.",
  },
  {
    id: "vllm",
    label: "vLLM",
    base: "http://127.0.0.1:8000/v1",
    hint: "A self-hosted vLLM server.",
  },
  {
    id: "openai-compatible",
    label: "OpenAI-compatible",
    base: "",
    hint: "Any gateway speaking the OpenAI chat-completions API.",
  },
];

const BLANK: ProviderForm = {
  name: "",
  type: "openrouter",
  base_url: TYPES[0].base,
  model: "",
  api_key: "",
  scope: "project",
  make_default: true,
};

export function ProviderPanel() {
  const qc = useQueryClient();
  const { data: settings } = useSettings();
  const setAgentView = useUIStore((s) => s.setAgentView);

  const [form, setForm] = useState<ProviderForm | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [busy, setBusy] = useState<"" | "test" | "save" | "catalog">("");
  const [result, setResult] = useState<ProviderHealth | null>(null);
  const [checks, setChecks] = useState<ProviderCheck[]>([]);
  const [error, setError] = useState<string>("");

  const providers = settings?.providers ?? [];
  const typeInfo = useMemo(
    () => TYPES.find((t) => t.id === form?.type) ?? TYPES[0],
    [form?.type],
  );

  // Which providers every agent role currently points at.
  const routedProviders = useMemo(() => {
    if (!settings) return new Set<string>();
    const byProfile = new Map(settings.models.map((m) => [m.name, m.provider]));
    return new Set(
      Object.values(settings.routing)
        .map((profile) => byProfile.get(profile))
        .filter((name): name is string => !!name),
    );
  }, [settings]);

  const startNew = () => {
    setEditing(null);
    setForm({ ...BLANK });
    setCatalog([]);
    setResult(null);
    setChecks([]);
    setError("");
  };

  const startEdit = (name: string) => {
    const provider = providers.find((p) => p.name === name);
    if (!provider) return;
    setEditing(name);
    setForm({
      name: provider.name,
      type: provider.type as ProviderForm["type"],
      base_url: provider.base_url,
      model: provider.model,
      api_key: "", // never rendered back; blank keeps the stored reference
      scope: provider.scope,
      make_default: false,
    });
    setCatalog([]);
    setResult(null);
    setChecks([]);
    setError("");
  };

  const patch = (values: Partial<ProviderForm>) =>
    setForm((current) => (current ? { ...current, ...values } : current));

  const pickType = (type: ProviderForm["type"]) => {
    const info = TYPES.find((t) => t.id === type);
    setCatalog([]);
    patch({
      type,
      // Only overwrite a base URL the user has not customised.
      base_url:
        !form?.base_url || TYPES.some((t) => t.base === form?.base_url)
          ? (info?.base ?? "")
          : form.base_url,
    });
  };

  const run = async (
    kind: "test" | "save" | "catalog",
    action: () => Promise<void>,
  ) => {
    if (!form) return;
    setBusy(kind);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const test = () =>
    run("test", async () => {
      const answer = await api.testProvider(form as ProviderForm);
      setResult(answer.provider);
      setChecks(answer.checks ?? []);
    });

  const loadCatalog = () =>
    run("catalog", async () => {
      const answer = await api.providerCatalog(form as ProviderForm);
      setCatalog(answer.models);
      if (!answer.models.length) setError("This provider does not publish a model list.");
    });

  /**
   * Fetch the catalog as soon as there is somewhere to fetch it from.
   *
   * The list is the point of the field — making the user press a button first
   * means the common case (pick an installed model) still starts with typing.
   * Debounced, because the base URL is edited a character at a time.
   */
  useEffect(() => {
    if (!form?.base_url || !form.type) return;
    const type = form.type;
    const baseUrl = form.base_url;
    const name = form.name;
    const key = form.api_key;
    const timer = window.setTimeout(() => {
      setBusy("catalog");
      api
        .providerCatalog({ ...BLANK, name, type, base_url: baseUrl, api_key: key })
        .then((answer) => setCatalog(answer.models))
        .catch(() => setCatalog([]))
        .finally(() => setBusy((current) => (current === "catalog" ? "" : current)));
    }, 500);
    return () => window.clearTimeout(timer);
    // The key is deliberately not a dependency: a catalog reload on every
    // keystroke of a secret is both wasteful and surprising.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form?.type, form?.base_url, form?.name]);

  const save = () =>
    run("save", async () => {
      const answer = await api.saveProvider(form as ProviderForm);
      qc.setQueryData(qk.settings, answer.settings);
      qc.invalidateQueries({ queryKey: qk.projectInfo });
      setResult(answer.provider);
      setChecks([]);
      setEditing(answer.provider.name);
      patch({ api_key: "" });
    });

  // Nothing configured yet? Open the form rather than an empty list.
  useEffect(() => {
    if (settings && providers.length === 0 && !form) startNew();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, providers.length]);

  return (
    <div className="provider-panel">
      <div className="panel-header">
        <button
          className="btn icon"
          title="Back to the conversation"
          onClick={() => setAgentView("chat")}
        >
          ‹
        </button>
        Providers
        <span className="spacer" />
        <button className="btn sm" onClick={startNew}>
          + New
        </button>
      </div>

      <div className="panel-body">
        <div className="provider-list">
          {providers.length === 0 && (
            <div className="empty">No provider configured yet.</div>
          )}
          {providers.map((provider) => (
            <button
              key={provider.name}
              className={`provider-row ${editing === provider.name ? "active" : ""}`}
              onClick={() => startEdit(provider.name)}
            >
              <span className="grow">
                <span className="name">
                  {provider.name}
                  {routedProviders.has(provider.name) && (
                    <span className="badge ok" title="Agent roles route here">
                      in use
                    </span>
                  )}
                </span>
                <span className="hint mono">
                  {provider.type} · {provider.model || "no model"}
                </span>
              </span>
              <span className="muted">edit</span>
            </button>
          ))}
        </div>

        {form && (
          <div className="provider-form">
            <div className="section-title">
              {editing ? `Edit ${editing}` : "New provider"}
            </div>

            <label className="field">
              <span>Name</span>
              <input
                className="input"
                value={form.name}
                disabled={!!editing}
                placeholder="openrouter, local-ollama…"
                onChange={(e) => patch({ name: e.target.value })}
              />
            </label>

            <label className="field">
              <span>Type</span>
              <select
                className="input"
                value={form.type}
                onChange={(e) => pickType(e.target.value as ProviderForm["type"])}
              >
                {TYPES.map((type) => (
                  <option key={type.id} value={type.id}>
                    {type.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="muted field-hint">{typeInfo.hint}</div>

            <label className="field">
              <span>Base URL</span>
              <input
                className="input"
                value={form.base_url}
                placeholder="https://host/v1"
                onChange={(e) => patch({ base_url: e.target.value })}
              />
            </label>

            <label className="field">
              <span>Model</span>
              <ModelCombo
                value={form.model}
                options={catalog}
                searchable={SEARCHABLE.includes(form.type)}
                loading={busy === "catalog"}
                onChange={(model) => patch({ model })}
                onReload={loadCatalog}
              />
            </label>

            <label className="field">
              <span>API key</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={form.api_key}
                placeholder={
                  editing ? "unchanged" : "key, or env://VAR — stored outside the repo"
                }
                onChange={(e) => patch({ api_key: e.target.value })}
              />
            </label>

            <label className="field">
              <span>Scope</span>
              <select
                className="input"
                value={form.scope}
                onChange={(e) => patch({ scope: e.target.value as ProviderForm["scope"] })}
              >
                <option value="project">This project</option>
                <option value="global">Every project</option>
              </select>
            </label>
            {editing && form.scope === "project" && (
              <div className="muted field-hint">
                Saved into this repository's <span className="mono">.daino/config.yaml</span>.
              </div>
            )}
            {editing && form.scope === "global" && (
              <div className="muted field-hint">
                Shared by every project on this machine.
              </div>
            )}

            <label className="row check">
              <input
                type="checkbox"
                checked={form.make_default}
                onChange={(e) => patch({ make_default: e.target.checked })}
              />
              <span>Use for every agent role</span>
            </label>

            {result && (
              <div className={`test-result ${result.connected ? "ok" : "bad"}`}>
                <strong>
                  {result.connected ? "✓ Ready to use" : "✗ Not usable yet"}
                </strong>
                <span className="mono">{result.detail}</span>
              </div>
            )}
            {checks.length > 0 && (
              <div className="check-list">
                {checks.map((check) => (
                  <div key={check.name} className={`check ${check.status}`}>
                    <span className="mark">
                      {check.status === "pass" ? "✓" : check.status === "fail" ? "✗" : "–"}
                    </span>
                    <span className="grow">
                      <span className="name">{check.name}</span>
                      <span className="detail">{check.detail}</span>
                    </span>
                  </div>
                ))}
              </div>
            )}
            {error && <div className="test-result bad">{error}</div>}

            <div className="provider-actions">
              <button
                className="btn"
                disabled={busy !== "" || !form.base_url}
                title="Checks the endpoint, the key, the model, and one real one-token request"
                onClick={test}
              >
                {busy === "test" ? "Testing…" : "Test connection"}
              </button>
              <span className="grow" />
              <button className="btn subtle" onClick={() => setForm(null)}>
                Cancel
              </button>
              <button
                className="btn primary"
                disabled={busy !== "" || !form.name.trim() || !form.base_url}
                onClick={save}
              >
                {busy === "save" ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
