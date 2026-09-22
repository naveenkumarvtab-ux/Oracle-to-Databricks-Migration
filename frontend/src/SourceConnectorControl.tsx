import { useEffect, useRef, useState } from "react";
import { api } from "./api";

type Props = {
  projectId: string;
  source: { id: string; server_name: string; database_name: string };
};

export default function SourceConnectorControl({ projectId, source }: Props) {
  const [status, setStatus] = useState("Loading");
  const [registration, setRegistration] = useState<{ token: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const path = `/projects/${projectId}/sources/${source.id}/connector`;

  useEffect(() => {
    if (open) dialog.current?.showModal();
  }, [open]);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const result = await api<{ status: string }>(path);
        if (active) setStatus(result.status);
      } catch {
        if (active) setStatus("Unavailable");
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, [path]);

  const mutate = async (method: string) => {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ token: string }>(path, { method });
      setRegistration(method === "POST" ? result : null);
      setStatus(method === "POST" ? "OFFLINE" : "REVOKED");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Connector update failed");
    } finally { setBusy(false); }
  };
  const [port, setPort] = useState("1521");
  const quote = (value: string) => "'" + value.replaceAll("'", "''") + "'";
  const backendUrl = window.location.port === "5173" ? "http://127.0.0.1:8010" : window.location.origin;
  const command = `python scripts/local_connector.py --url ${quote(backendUrl)} --source ${quote(source.id)} --server ${quote(source.server_name)} --database ${quote(source.database_name)} --port ${quote(port)} --source-type 'oracle'`;

  return <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
    <span>{status === "DIRECT" ? "Direct connection" : `Connector: ${status.toLowerCase()}`}</span>
    <button onClick={() => setOpen(true)}>Manage connector</button>
    {open && <dialog ref={dialog} aria-label="Local Oracle connector" onCancel={() => { setOpen(false); setRegistration(null); setError(""); }}
      style={{ border: "1px solid #d6dce5", borderRadius: 8, padding: 0, width: "min(680px, 90vw)", maxHeight: "85vh" }}>
      <div style={{ background: "white", color: "#172b4d", padding: 24, overflowY: "auto", whiteSpace: "normal" }}>
        <h2>Local Oracle connector</h2>
        <p>Run the connector on a machine that can access {source.server_name}. It connects outward to this application over HTTPS. Oracle credentials remain local.</p>
        <p>Status: <strong>{status}</strong></p>
        {error && <p role="alert">{error}</p>}
        {registration ? <>
          <p>Registration token is shown once. Enter it at the connector's password prompt.</p>
          <textarea aria-label="Registration token" readOnly value={registration.token} rows={3} style={{ width: "100%", boxSizing: "border-box" }} />
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 16, margin: "14px 0 8px 0" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.9rem" }}>
              <span><strong>Port:</strong></span>
              <input value={port} onChange={(e) => setPort(e.target.value)} style={{ width: 80, padding: "4px 8px", borderRadius: 4, border: "1px solid #c1c7d0" }} />
            </label>
          </div>
          <p>From the updated repository folder on the Oracle source machine:</p>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>python -m pip install -r scripts/connector-requirements.txt{"\n"}{command}</pre>
          <p>For Oracle Authentication add <code>--username 'system'</code>; the password is prompted securely.</p>
          <p>Keep the connector running. When its status becomes online, close this panel and select Test. See <code>docs/LOCAL_CONNECTOR.md</code> for certificate setup and recovery.</p>
        </> : <p>Registration switches this source to connector mode. Registering again invalidates the previous token and cancels pending connector tasks.</p>}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 16 }}>
          {!registration && <button disabled={busy} onClick={() => void mutate("POST")}>{status === "DIRECT" ? "Register connector" : "Replace registration"}</button>}
          {status !== "DIRECT" && status !== "REVOKED" && <button disabled={busy} onClick={() => {
            if (window.confirm("Revoke this connector and cancel its pending tasks?")) void mutate("DELETE");
          }}>Revoke connector</button>}
          <button disabled={busy} onClick={() => { setOpen(false); setRegistration(null); setError(""); }}>Close</button>
        </div>
      </div>
    </dialog>}
  </div>;
}
