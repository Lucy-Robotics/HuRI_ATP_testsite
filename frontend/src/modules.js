/** HuRI module names as the server knows them (huri-modules' curated
 * modules, listed by GET /huri-modules), and what this site needs to know
 * about them. */

/** A default args block per curated module, so ticking one on in the Event
 * Configuration modal gives a working starting point. A module the server
 * serves but that is not listed here starts with `{}`. */
export const DEFAULT_ARGS = {
  vad: { vad_agressiveness: 2, silence_duration: 1.0, block_duration: 0.03 },
  stt: { language: "en", block_duration: 0.03 },
  tag: {},
  emo: { block_duration: 0.03 },
  eag: {},
  qag: {},
  rag: { language: "en", tone: "formal", response_format: "paragraph" },
  tts: {},
  tts_cosyvoice: {},
  mov: {},
};

/** The two speech engines: Piper (`tts`, CPU, huri_cpu.yaml) and CosyVoice3
 * (`tts_cosyvoice`, GPU, huri.yaml). A server runs one of them. */
export const TTS_ENGINES = ["tts", "tts_cosyvoice"];

/** `modules` ({tag: {name, args}}) with its TTS engine swapped for the one
 * the server serves, when it names the other one and only that one is
 * missing. Presets name CosyVoice (huri.yaml, the ATP's config); this keeps
 * them usable against a Piper server. `available === null` (server not
 * reachable) leaves them as written. */
export function withServedTTS(modules, available) {
  if (!available) return modules;
  const served = TTS_ENGINES.find((name) => available.includes(name));
  if (!served) return modules;
  const out = {};
  for (const [tag, entry] of Object.entries(modules)) {
    out[tag] =
      TTS_ENGINES.includes(entry.name) && !available.includes(entry.name)
        ? { ...entry, name: served }
        : entry;
  }
  return out;
}

/** Whether every module a preset needs is served by this HuRI instance
 * (unknown availability = don't block anything). */
export function presetIsAvailable(preset, available) {
  if (!available) return true;
  return Object.values(withServedTTS(preset, available)).every((m) =>
    available.includes(m.name),
  );
}
