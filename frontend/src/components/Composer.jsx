import { useMemo, useState } from "react";
import useStore from "../store/index.js";
import { ensureContext } from "../audio/playback.js";
import { startMic, stopMic } from "../audio/microphone.js";

const TOPIC_LABELS = {
  "audio.in": "audio.in",
  question: "rag.in",
  token: "rag.out",
};

function micLevelFromFrame(buf) {
  const int16 = new Int16Array(buf);
  let sum = 0;
  for (let i = 0; i < int16.length; i++) sum += Math.abs(int16[i]);
  return Math.min(1, sum / int16.length / 8000);
}

export default function Composer({ onOpenSettings }) {
  const [text, setText] = useState("");
  const [topic, setTopic] = useState("question");
  const [level, setLevel] = useState(0);

  const hasModule = useStore((s) => s.hasModule);
  const hasTTS = useStore((s) => s.hasTTS);
  const sendTopic = useStore((s) => s.sendTopic);
  const sendAudioFrame = useStore((s) => s.sendAudioFrame);
  const pushMicFrame = useStore((s) => s.pushMicFrame);
  const submitText = useStore((s) => s.submitText);
  const beginListening = useStore((s) => s.beginListening);
  const stopListening = useStore((s) => s.stopListening);
  const setStatusMessage = useStore((s) => s.setStatusMessage);
  const recording = useStore((s) => s.recording);
  const connected = useStore((s) => s.connectionStatus === "connected");

  const canAudio = hasModule("vad");
  const canQuestion = hasModule("rag");
  const canToken = hasTTS();

  const options = useMemo(() => {
    const opts = [];
    if (canAudio) opts.push("audio.in");
    if (canQuestion) opts.push("question");
    if (canToken) opts.push("token");
    return opts;
  }, [canAudio, canQuestion, canToken]);

  const activeTopic = options.includes(topic) ? topic : options[0];
  const isAudioTopic = activeTopic === "audio.in";

  const submit = () => {
    if (isAudioTopic || !text.trim() || !connected) return;
    submitText(activeTopic, text);
    setText("");
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const toggleMic = async () => {
    if (recording) {
      stopMic();
      stopListening();
      setLevel(0);
      return;
    }
    ensureContext();
    beginListening();
    try {
      await startMic((buf) => {
        setLevel(micLevelFromFrame(buf));
        pushMicFrame(buf);
        sendAudioFrame?.(buf);
      });
    } catch (err) {
      console.error("Microphone error", err);
      stopMic();
      stopListening();
      setStatusMessage("Microphone unavailable — check browser permissions.");
    }
  };

  return (
    <div className="composer">
      {options.length > 0 && (
        <select
          className="composer-select"
          value={activeTopic}
          onChange={(e) => setTopic(e.target.value)}
        >
          {options.map((t) => (
            <option key={t} value={t}>
              {TOPIC_LABELS[t]}
            </option>
          ))}
        </select>
      )}

      <input
        type="text"
        className="composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        disabled={isAudioTopic || !connected}
        placeholder={
          isAudioTopic
            ? "Use the mic to speak…"
            : connected
              ? "Enter your text"
              : "Connecting to HuRI…"
        }
      />

      {canAudio && (
        <div className="composer-mic-level" aria-hidden="true">
          {[0, 1, 2, 3, 4].map((i) => (
            <span
              key={i}
              style={{
                height: `${recording ? 4 + level * 16 * (1 - Math.abs(i - 2) / 3) : 4}px`,
              }}
            />
          ))}
        </div>
      )}

      {canAudio && (
        <button
          type="button"
          className={`icon-button mic-button ${recording ? "recording" : ""}`}
          onClick={toggleMic}
          title={recording ? "Stop recording" : "Record (audio.in)"}
        >
          {recording ? "⏹" : "🎤"}
        </button>
      )}

      <button
        type="button"
        className="icon-button"
        onClick={onOpenSettings}
        title="Event Configuration"
      >
        ⚙
      </button>

      <button
        type="button"
        className="icon-button send-button"
        onClick={submit}
        disabled={isAudioTopic || !text.trim() || !connected}
        title="Send"
      >
        ➤
      </button>
    </div>
  );
}
