import { useEffect, useRef, useState } from "react";
import useStore from "../store/index.js";
import { BACKEND_URL } from "../config.js";

// Derive the websocket endpoint from the same backend origin the rest of the
// app uses (see config.js).
const WS_URL = BACKEND_URL.replace(/^http/, "ws") + "/ws";

/** Decode a base64 string of little-endian float32 PCM into a Float32Array. */
function decodeAudio(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}

/**
 * Owns the single websocket to the backend, speaking the protocol
 * HuRI/src/interfaces/web_interface.py + backend/main.py implement:
 *
 *   -> {"modules": {tag: {name, args}}}              handshake, once
 *   <- {"type": "session_config", "config": {...}}    resolved config echo
 *   -> {"topic": "question"|"token", "text": ...}     typed input (event picker)
 *   -> <binary ArrayBuffer>                            mic PCM frames
 *   <- {"type": "token"|"audio"|"segment"|"question"|"error", ...}
 *
 * "question" already carries the linked emotion (RAGQuestion.emotion, the
 * aggregated per-utterance read) — that's the only source of emotion data on
 * the wire, there's no separate raw "emotion" stream to listen for.
 *
 * `initialModules` seeds the first handshake; calling the store's
 * `reconfigure(modules)` (wired up here) tears down the connection and opens
 * a fresh one with a new module combination — this is what the Event
 * Configuration modal's Apply button drives (HuRI/ATP.xlsx F1/F2).
 *
 * `enabled` gates whether a connection is attempted at all: HuRI is started
 * on demand from the Control Panel (see App.jsx / LauncherPanel.jsx) rather
 * than automatically on page load, so this stays idle — no socket, no
 * "Connection error" noise — until the caller confirms HuRI is actually up.
 */
export function useWebSocket(initialModules, { enabled = true } = {}) {
  const [modules, setModules] = useState(initialModules);
  const wsRef = useRef(null);

  const {
    setConnectionStatus,
    setStatusMessage,
    setSessionConfig,
    setSendTopic,
    setSendAudioFrame,
    setReconfigure,
    onQuestion,
    onToken,
    onAudio,
    onMotionSegment,
  } = useStore.getState();

  useEffect(() => {
    setReconfigure((next) => setModules(next));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!enabled) {
      setConnectionStatus("idle");
      setStatusMessage("Start HuRI from the Control Panel to connect.");
      setSessionConfig(null);
      return;
    }

    setConnectionStatus("connecting");
    setStatusMessage("Connecting to HuRI...");
    setSessionConfig(null);

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ modules }));
      setSendTopic((topic, text) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ topic, text }));
      });
      setSendAudioFrame((buf) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(buf);
      });
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "session_config":
          setSessionConfig(msg.config);
          setConnectionStatus("connected");
          setStatusMessage("Connected — type a message or use the mic.");
          break;
        case "question":
          onQuestion(msg);
          break;
        case "token":
          onToken(msg);
          break;
        case "audio":
          // The wire field is snake_case (web_interface.py's WebAudioHook);
          // without this mapping createBuffer() gets an undefined rate and
          // throws, so every chunk is silently dropped.
          onAudio({
            ...msg,
            data: decodeAudio(msg.data),
            sampleRate: msg.sample_rate,
          });
          break;
        case "segment":
          // Gesture frames, already converted to this frontend's rig format
          // by the backend (main.py's _transform_outbound / pipeline.py).
          onMotionSegment(msg);
          break;
        case "error":
          setStatusMessage(`Error: ${msg.message}`);
          setConnectionStatus("error");
          break;
        default:
          break;
      }
    };

    ws.onerror = () => {
      setStatusMessage("Connection error — is the backend running?");
      setConnectionStatus("error");
    };

    ws.onclose = () => {
      setSendTopic(null);
      setSendAudioFrame(null);
      setConnectionStatus("closed");
    };

    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modules, enabled]);
}
