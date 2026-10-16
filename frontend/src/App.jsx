import { useEffect, useRef, useState } from "react";
import Scene from "./components/Scene.jsx";
import ChatPanel from "./components/ChatPanel.jsx";
import Composer from "./components/Composer.jsx";
import EventConfigModal from "./components/EventConfigModal.jsx";
import LauncherPanel from "./components/LauncherPanel.jsx";
import { useWebSocket } from "./hooks/useWebSocket.js";
import { useHuriStatus } from "./hooks/useHuriStatus.js";
import useStore from "./store/index.js";
import { BACKEND_URL } from "./config.js";
import { withServedTTS } from "./modules.js";

// A minimal first handshake (presets/F9/rag_only.json) so the app connects
// immediately; the Event Configuration modal offers the named presets
// (fetched from /presets) to switch afterwards.
const DEFAULT_MODULES = {
  rag: { name: "rag", args: { language: "en", tone: "formal", response_format: "short" } },
};

const STATUS_LABELS = {
  idle: "Waiting for HuRI",
  connecting: "Connecting…",
  connected: "Connected",
  error: "Connection error",
  closed: "Disconnected",
};

export default function App() {
  const [auth, setAuth] = useState(null); // null = loading

  useEffect(() => {
    fetch(`${BACKEND_URL}/auth/me`, { credentials: "include" })
      .then((r) => r.json())
      .then(setAuth)
      .catch(() => setAuth({ authenticated: false, auth_required: true }));
  }, []);

  if (auth === null) {
    return <CenteredMessage text="Checking sign-in…" />;
  }
  if (auth.auth_required && !auth.authenticated) {
    return (
      <CenteredMessage text="Sign in to test HuRI">
        <a className="modal-apply" style={{ marginTop: 16 }} href={`${BACKEND_URL}/auth/login`}>
          Sign in
        </a>
      </CenteredMessage>
    );
  }
  return <TestingApp user={auth} />;
}

function CenteredMessage({ text, children }) {
  return (
    <div
      style={{
        width: "100vw",
        height: "100dvh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
      }}
    >
      <div>{text}</div>
      {children}
    </div>
  );
}

function TestingApp({ user }) {
  // HuRI is launched on demand from the Control Panel rather than
  // auto-starting on page load (it's a real local process, not something to
  // spin up just because a tab was opened) — so the panel opens by itself on
  // first load, asking the visitor to start HuRI, and the client websocket
  // only connects once the launcher reports it's actually running.
  const { status: huriStatus } = useHuriStatus();
  const huriRunning = huriStatus?.status === "running";

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(true);

  // Auto-close the panel the moment HuRI transitions to running, whether that
  // was this visitor clicking Start or an instance already running elsewhere
  // being detected — but only once, so re-opening the panel later (to stop
  // HuRI, check logs, etc) doesn't get immediately closed out from under them.
  const autoClosedRef = useRef(false);
  useEffect(() => {
    if (huriRunning && !autoClosedRef.current) {
      autoClosedRef.current = true;
      setLauncherOpen(false);
    }
  }, [huriRunning]);

  useWebSocket(DEFAULT_MODULES, { enabled: huriRunning });

  const connectionStatus = useStore((s) => s.connectionStatus);
  const statusMessage = useStore((s) => s.statusMessage);
  const hasModule = useStore((s) => s.hasModule);
  const reconfigure = useStore((s) => s.reconfigure);
  const showAvatar = hasModule("mov");

  // A tab opened from the Control Panel's "Open client tab" (LauncherPanel.jsx)
  // carries `?preset=<name>` — apply it once `reconfigure` is wired up (after
  // the initial DEFAULT_MODULES handshake) so this client starts pre-configured
  // instead of requiring a manual Event Configuration step.
  const appliedPresetFromUrl = useRef(false);
  useEffect(() => {
    if (appliedPresetFromUrl.current || !reconfigure) return;
    const presetName = new URLSearchParams(window.location.search).get("preset");
    if (!presetName) return;
    appliedPresetFromUrl.current = true;
    const getJson = (path) =>
      fetch(`${BACKEND_URL}${path}`, { credentials: "include" }).then((r) => r.json());
    Promise.all([getJson("/presets"), getJson("/huri-modules").catch(() => ({}))])
      .then(([presets, served]) => {
        const preset = presets[presetName];
        if (preset) reconfigure(withServedTTS(preset, served.modules ?? null));
      })
      .catch(() => {});
  }, [reconfigure]);

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <div className="app-brand">
          <span className="app-brand-icon">🌼</span> HuRI testing console
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            className={`status-pill ${connectionStatus}`}
            title={statusMessage}
          >
            {STATUS_LABELS[connectionStatus] ?? connectionStatus}
          </span>
          <button
            type="button"
            className="status-pill"
            style={{ border: "none" }}
            onClick={() => setLauncherOpen(true)}
            title="Launch HuRI with a config and connect client(s) (HuRI/ATP.xlsx F1-F11)"
          >
            HuRI Control Panel
          </button>
          {user?.authenticated && (
            <a
              href={`${BACKEND_URL}/auth/logout`}
              className="status-pill"
              style={{ textDecoration: "none" }}
            >
              Sign out
            </a>
          )}
        </div>
      </div>

      <div className="app-body">
        <ChatPanel />
        {showAvatar && (
          <div className="avatar-panel">
            <Scene />
          </div>
        )}
      </div>

      <Composer onOpenSettings={() => setSettingsOpen(true)} />

      {settingsOpen && <EventConfigModal onClose={() => setSettingsOpen(false)} />}
      {launcherOpen && <LauncherPanel onClose={() => setLauncherOpen(false)} />}
    </div>
  );
}
