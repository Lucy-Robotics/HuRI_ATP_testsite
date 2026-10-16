import { useCallback, useEffect, useState } from "react";
import { BACKEND_URL } from "../config.js";
import { useAvailableModules } from "../hooks/useAvailableModules.js";
import { presetIsAvailable } from "../modules.js";
import { useHuriStatus } from "../hooks/useHuriStatus.js";

const POLL_MS = 2000;

const STATUS_LABELS = {
  stopped: "Stopped",
  starting: "Starting…",
  running: "Running",
  stopping: "Stopping…",
  crashed: "Crashed",
};

/** Build the URL for a fresh client tab pre-configured with a given preset —
 * read on load by App.jsx's `?preset=` handling. Opening several of these is
 * how you cover HuRI/ATP.xlsx F4 (multi-client) from this one panel. */
function clientUrlForPreset(presetName) {
  const url = new URL(window.location.href);
  url.search = "";
  if (presetName && presetName !== "custom") {
    url.searchParams.set("preset", presetName);
  }
  return url.toString();
}

/**
 * Admin control panel: launch/stop a local HuRI (Ray Serve) instance from one
 * of huri-modules' configs, watch its logs, then jump to the Ray
 * dashboard or open client tab(s) against it — covers HuRI/ATP.xlsx F1
 * end-to-end and gives F2-F11 a one-click way to get both HuRI and a client
 * up. See backend/huri_launcher.py for the process supervision this drives.
 */
export default function LauncherPanel({ onClose }) {
  const [configs, setConfigs] = useState([]);
  const [installProblem, setInstallProblem] = useState(null);
  const [selectedConfig, setSelectedConfig] = useState("");
  const [presets, setPresets] = useState({});
  const [selectedPreset, setSelectedPreset] = useState("custom");
  const availableModules = useAvailableModules();
  const { status, refresh: refreshStatus } = useHuriStatus();
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`${BACKEND_URL}/launcher/huri/configs`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        setConfigs(data.configs || []);
        setInstallProblem(data.install_problem || null);
        setSelectedConfig((prev) => prev || data.configs?.[0] || "");
      })
      .catch(() => setConfigs([]));
    fetch(`${BACKEND_URL}/presets`, { credentials: "include" })
      .then((r) => r.json())
      .then(setPresets)
      .catch(() => setPresets({}));
  }, []);

  // A preset picked before /huri-modules resolved could turn out to need a
  // module this instance doesn't have; drop back to the always-available
  // default rather than let "Open client tab" launch it anyway.
  useEffect(() => {
    if (selectedPreset === "custom") return;
    const preset = presets[selectedPreset];
    if (preset && !presetIsAvailable(preset, availableModules)) {
      setSelectedPreset("custom");
    }
  }, [presets, availableModules, selectedPreset]);

  const refreshLogs = useCallback(() => {
    fetch(`${BACKEND_URL}/launcher/huri/logs?tail=200`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => setLogs(data.lines || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshLogs();
    const id = setInterval(refreshLogs, POLL_MS);
    return () => clearInterval(id);
  }, [refreshLogs]);

  const isBusyStatus = status?.status === "starting" || status?.status === "stopping";
  const isRunning = status?.status === "running" || isBusyStatus;
  // HuRI answering on its port without this backend owning the process — a
  // `serve run` in a terminal, or an instance that outlived a backend restart
  // (see huri_launcher.py's _external_is_up). Clients work against it exactly
  // as they would against one started here; only the Stop button can't.
  const isExternal = Boolean(status?.external);

  const start = async () => {
    if (!selectedConfig) return;
    setBusy(true);
    setError("");
    try {
      const resp = await fetch(`${BACKEND_URL}/launcher/huri/start`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: selectedConfig }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Failed to start HuRI");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
      refreshStatus();
      refreshLogs();
    }
  };

  const stop = async () => {
    setBusy(true);
    setError("");
    try {
      const resp = await fetch(`${BACKEND_URL}/launcher/huri/stop`, {
        method: "POST",
        credentials: "include",
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Failed to stop HuRI");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
      refreshStatus();
      refreshLogs();
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card launcher-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">HuRI Control Panel</div>

        <div className="modal-section-title">1. Launch HuRI (HuRI/ATP.xlsx F1)</div>
        <div className="launcher-row">
          <select
            className="modal-select"
            value={selectedConfig}
            onChange={(e) => setSelectedConfig(e.target.value)}
            disabled={isRunning || busy}
          >
            {configs.length === 0 && <option value="">No configs found</option>}
            {configs.map((c) => (
              <option key={c} value={c}>
                huri-modules/config/{c}
              </option>
            ))}
          </select>
          {!isRunning ? (
            <button
              type="button"
              className="modal-apply"
              onClick={start}
              disabled={busy || !selectedConfig}
            >
              Start
            </button>
          ) : (
            !isExternal && (
              <button
                type="button"
                className="modal-apply"
                onClick={stop}
                disabled={busy || isBusyStatus}
              >
                Stop
              </button>
            )
          )}
        </div>

        {installProblem && (
          <div className="modal-hint" style={{ color: "var(--danger)" }}>
            {installProblem}
          </div>
        )}
        {status && (
          <div className="launcher-status-row">
            <span className={`status-pill launcher-status-${status.status}`}>
              {STATUS_LABELS[status.status] ?? status.status}
              {isExternal ? " (external)" : ""}
            </span>
            {status.config && <span className="modal-hint">config: {status.config}</span>}
            {status.pid && <span className="modal-hint">pid: {status.pid}</span>}
            <a
              href={status.dashboard_url}
              target="_blank"
              rel="noreferrer"
              className="status-pill"
              style={{ textDecoration: "none" }}
            >
              Open Ray dashboard ↗
            </a>
          </div>
        )}
        {isExternal && (
          <div className="modal-hint">
            HuRI is up but wasn't started from this panel, so it can't be stopped here
            — stop it wherever you started it. Clients connect to it all the same.
          </div>
        )}
        {status?.last_error && (
          <div className="modal-hint" style={{ color: "var(--danger)" }}>
            {status.last_error}
          </div>
        )}
        {error && (
          <div className="modal-hint" style={{ color: "var(--danger)" }}>
            {error}
          </div>
        )}

        <pre className="launcher-log">
          {logs.length ? logs.join("\n") : "No logs yet — start HuRI to see output here."}
        </pre>

        <div className="modal-section-title">2. Connect client(s) (HuRI/ATP.xlsx F2-F11)</div>
        <div className="launcher-row">
          <select
            className="modal-select"
            value={selectedPreset}
            onChange={(e) => setSelectedPreset(e.target.value)}
          >
            <option value="custom">Default (rag_only-style handshake)</option>
            {Object.entries(presets).map(([name, preset]) => (
              <option key={name} value={name} disabled={!presetIsAvailable(preset, availableModules)}>
                {name}
                {!presetIsAvailable(preset, availableModules) ? " (not deployed here)" : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="modal-apply"
            onClick={() => window.open(clientUrlForPreset(selectedPreset), "_blank")}
          >
            Open client tab ↗
          </button>
        </div>
        <div className="modal-hint">
          Opens a new tab of this same site with the picked preset pre-applied — open it more
          than once for a multi-client test (F4).
        </div>

        <div className="modal-actions">
          <button type="button" className="modal-apply" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
