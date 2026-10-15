import { create } from "zustand";
import { resetUtterance, scheduleChunk } from "../audio/playback.js";
import { TTS_ENGINES } from "../modules.js";

let nextId = 1;
const newId = () => `m${nextId++}`;

/** Concatenate Int16 mic frames captured during a recording into one Float32
 * buffer, for attaching to the resulting user message (waveform + replay). */
function int16FramesToFloat32(frames) {
  const total = frames.reduce((n, f) => n + f.byteLength / 2, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const frame of frames) {
    const int16 = new Int16Array(frame);
    for (let i = 0; i < int16.length; i++) out[offset++] = int16[i] / 0x8000;
  }
  return out;
}

/**
 * Chat + session state.
 *
 * One entry in `messages` per conversational turn (not per raw wire event):
 *   {id, role: "user"|"assistant", text, createdAt, completedAt, pending,
 *    emotion?: {label, confidence, scores}, audio?: {samples: Float32Array,
 *    sampleRate}, expanded}
 *
 * A turn's audio/motion arrive as a stream of chunks tagged with a shared
 * `pts` clock (see audio/playback.js); `frames` mirrors that for the avatar,
 * reset at the start of every new turn via `_resetTurn`.
 */
const useStore = create((set, get) => ({
  // --- connection / session -------------------------------------------
  connectionStatus: "idle", // "idle" | "connecting" | "connected" | "error" | "closed"
  statusMessage: "",
  sessionConfig: null, // {modules}, echoed back by the backend
  sendTopic: null, // (topic, text) => void, installed by useWebSocket
  sendAudioFrame: null, // (ArrayBuffer) => void
  reconfigure: null, // (modules) => void — re-handshakes with a new module set

  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setStatusMessage: (statusMessage) => set({ statusMessage }),
  setSessionConfig: (sessionConfig) => set({ sessionConfig, _sentQuestions: [] }),
  setSendTopic: (fn) => set({ sendTopic: fn }),
  setSendAudioFrame: (fn) => set({ sendAudioFrame: fn }),
  setReconfigure: (fn) => set({ reconfigure: fn }),

  /** Whether a module (by its HuRI name) is part of the active session —
   * drives conditional rendering (no mic button without "vad", no avatar
   * panel without "mov", etc). */
  hasModule: (name) => {
    const modules = get().sessionConfig?.modules || {};
    return Object.values(modules).some((m) => m.name === name);
  },

  /** Whether the session speaks, with either TTS engine. */
  hasTTS: () => TTS_ENGINES.some((name) => get().hasModule(name)),

  // --- avatar ------------------------------------------------------------
  frames: [],
  eyeHeight: null,
  setEyeHeight: (h) => set({ eyeHeight: h }),

  // --- mic -----------------------------------------------------------
  recording: false,
  micArmed: false, // half-duplex: reopen the mic once the current reply finishes
  _micBuffer: [], // Int16 ArrayBuffers captured during the current utterance

  pushMicFrame: (buf) => set((s) => ({ _micBuffer: [...s._micBuffer, buf] })),

  // --- turn lifecycle ------------------------------------------------
  /** Text questions this client published on "question" itself and therefore
   * expects to see come back on the "question" hook.
   *
   * HuRI fans a published event out to *every* subscriber of its topic, and
   * with qag in the session the bridge subscribes to huri.question, the
   * topic a typed question is sent on (backend/huri_bridge.py). So a typed
   * message is echoed straight back to us alongside being handed to RAG — without this, `onQuestion` would open a second turn for a message
   * `submitText` already rendered, duplicating the user bubble and leaving
   * the first assistant bubble pending forever. Entries are {id, text}, FIFO. */
  _sentQuestions: [],

  /** Reset the avatar/audio clock for a fresh turn. */
  _resetTurn: () => {
    resetUtterance();
    set({ frames: [] });
  },

  /** Typed input submitted from the Composer, targeting whichever topic the
   * event dropdown picked ("question" = rag.in, "token" = rag.out). */
  submitText: (topic, text) => {
    const trimmed = text.trim();
    if (!trimmed || !get().sendTopic) return;
    get()._resetTurn();
    const now = Date.now();
    const userId = newId();
    set((s) => ({
      _sentQuestions:
        topic === "question" && get().hasModule("qag")
          ? [...s._sentQuestions, { id: userId, text: trimmed }]
          : s._sentQuestions,
      messages: [
        ...s.messages,
        {
          id: userId,
          role: "user",
          text: trimmed,
          topic,
          createdAt: now,
          completedAt: now,
        },
        {
          id: newId(),
          role: "assistant",
          text: "",
          createdAt: now,
          completedAt: null,
          pending: true,
          audio: null,
        },
      ],
    }));
    get().sendTopic(topic, trimmed);
  },

  /** Mic toggle (HuRI/ATP.xlsx F5/F6). */
  beginListening: () => {
    get()._resetTurn();
    set({ recording: true, micArmed: false, _micBuffer: [] });
  },
  stopListening: () => set({ recording: false, micArmed: false }),

  /** "question" hook message: a voice utterance was fully transcribed and
   * (if the emotion modules are active) linked to its prosody read — closes
   * the user's turn and opens the assistant's (HuRI/ATP.xlsx F9/F10/F11). */
  onQuestion: ({ text, emotion }) => {
    // Our own typed question coming back (see `_sentQuestions`): the turn is
    // already on screen, so only fold in anything the echo adds — never a
    // second bubble pair, and no turn reset, since audio/frames for the reply
    // may already be streaming in.
    const echoed = get()._sentQuestions.find((q) => q.text === text);
    if (echoed) {
      set((s) => ({
        _sentQuestions: s._sentQuestions.filter((q) => q.id !== echoed.id),
        messages: emotion
          ? s.messages.map((m) => (m.id === echoed.id ? { ...m, emotion } : m))
          : s.messages,
      }));
      return;
    }

    const now = Date.now();
    const micBuffer = get()._micBuffer;
    get()._resetTurn();
    const audio = micBuffer.length
      ? { samples: int16FramesToFloat32(micBuffer), sampleRate: 16000 }
      : null;
    set((s) => ({
      _micBuffer: [],
      messages: [
        ...s.messages,
        {
          id: newId(),
          role: "user",
          text,
          emotion: emotion || null,
          audio,
          createdAt: now,
          completedAt: now,
        },
        {
          id: newId(),
          role: "assistant",
          text: "",
          createdAt: now,
          completedAt: null,
          pending: true,
          audio: null,
        },
      ],
    }));
  },

  /** "token" hook message: streamed RAG answer text. */
  onToken: ({ text, end }) => {
    set((s) => {
      const messages = [...s.messages];
      const last = messages[messages.length - 1];
      if (!last || last.role !== "assistant") return {};
      messages[messages.length - 1] = { ...last, text: last.text + text };
      return { messages };
    });
    // No TTS in this session: the token stream is the whole reply, so its end
    // marker is what closes the turn (otherwise the "audio" end marker does).
    if (end && !get().hasTTS()) get()._completeLastAssistant();
  },

  /** "audio" hook message: one TTS chunk. */
  onAudio: ({ data, sampleRate, pts, end }) => {
    scheduleChunk(data, sampleRate, pts);
    if (data.length) {
      set((s) => {
        const messages = [...s.messages];
        const last = messages[messages.length - 1];
        if (!last || last.role !== "assistant") return {};
        const prev = last.audio;
        const samples = prev
          ? concatFloat32(prev.samples, data)
          : Float32Array.from(data);
        messages[messages.length - 1] = { ...last, audio: { samples, sampleRate } };
        return { messages };
      });
    }
    if (end) get()._completeLastAssistant();
  },

  /** "segment" message: gesture frames, already converted to this frontend's
   * rig format by the backend (pipeline.py). */
  onMotionSegment: ({ frames }) => {
    if (!frames?.length) return;
    set((s) => ({ frames: [...s.frames, ...frames].sort((a, b) => a.t - b.t) }));
  },

  _completeLastAssistant: () => {
    set((s) => {
      const messages = [...s.messages];
      const last = messages[messages.length - 1];
      if (!last || last.role !== "assistant" || !last.pending) return {};
      messages[messages.length - 1] = {
        ...last,
        pending: false,
        completedAt: Date.now(),
      };
      return { messages };
    });
    if (get().recording) set({ micArmed: true }); // reopen the mic once idle
  },

  // --- chat ------------------------------------------------------------
  messages: [],
  toggleExpanded: (id) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === id ? { ...m, expanded: !m.expanded } : m,
      ),
    })),
}));

function concatFloat32(a, b) {
  const out = new Float32Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

export default useStore;
