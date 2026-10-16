import { useEffect, useState } from "react";
import useStore from "../store/index.js";
import { BACKEND_URL } from "../config.js";
import { useAvailableModules } from "../hooks/useAvailableModules.js";
import { DEFAULT_ARGS, presetIsAvailable, withServedTTS } from "../modules.js";

/** The modules the modal lists: those the connected HuRI serves (GET
 * /huri-modules), or, when it cannot be asked, the curated ones this site
 * has defaults for plus whatever the current session uses. */
function moduleNames(availableModules, sessionModules) {
  if (availableModules) return availableModules;
  const names = new Set(Object.keys(DEFAULT_ARGS));
  for (const entry of Object.values(sessionModules || {})) {
    if (entry.name) names.add(entry.name);
  }
  return [...names];
}

/** Selection state ({name: {enabled, argsText}}) for a `modules` block. */
function selectionFromModules(names, modules, availableModules) {
  const selection = {};
  for (const name of names) {
    selection[name] = {
      enabled: false,
      argsText: JSON.stringify(DEFAULT_ARGS[name] || {}),
    };
  }
  for (const entry of Object.values(withServedTTS(modules || {}, availableModules))) {
    if (!selection[entry.name]) continue;
    selection[entry.name] = {
      enabled: true,
      argsText: JSON.stringify(entry.args || {}, null, 0),
    };
  }
  return selection;
}

export default function EventConfigModal({ onClose }) {
  const sessionConfig = useStore((s) => s.sessionConfig);
  const reconfigure = useStore((s) => s.reconfigure);

  const [presets, setPresets] = useState({});
  const availableModules = useAvailableModules();
  const [presetName, setPresetName] = useState("custom");
  const names = moduleNames(availableModules, sessionConfig?.modules);
  const [selection, setSelection] = useState({});
  // Rebuilt once the server's module list arrives (it decides the rows).
  useEffect(() => {
    setSelection(selectionFromModules(names, sessionConfig?.modules, availableModules));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableModules]);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${BACKEND_URL}/presets`)
      .then((r) => r.json())
      .then(setPresets)
      .catch(() => setPresets({}));
  }, []);


  const applyPreset = (name) => {
    setPresetName(name);
    if (name === "custom" || !presets[name]) return;
    setSelection(selectionFromModules(names, presets[name], availableModules));
  };

  const toggleModule = (name) => {
    setPresetName("custom");
    setSelection((s) => ({ ...s, [name]: { ...s[name], enabled: !s[name].enabled } }));
  };

  const setArgsText = (name, value) => {
    setPresetName("custom");
    setSelection((s) => ({ ...s, [name]: { ...s[name], argsText: value } }));
  };

  const apply = () => {
    const modules = {};
    for (const name of names) {
      const entry = selection[name];
      if (!entry?.enabled) continue;
      let args;
      try {
        args = JSON.parse(entry.argsText || "{}");
      } catch {
        setError(`Invalid JSON in "${name}" args`);
        return;
      }
      modules[name] = { name, args };
    }
    if (Object.keys(modules).length === 0) {
      setError("Select at least one module.");
      return;
    }
    setError("");
    reconfigure?.(modules);
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">Event Configuration</div>

        <select
          className="modal-select"
          value={presetName}
          onChange={(e) => applyPreset(e.target.value)}
        >
          <option value="custom">Custom combination</option>
          {Object.entries(presets).map(([name, preset]) => (
            <option key={name} value={name} disabled={!presetIsAvailable(preset, availableModules)}>
              {name}
              {!presetIsAvailable(preset, availableModules) ? " (not deployed here)" : ""}
            </option>
          ))}
        </select>

        <div className="modal-section-title">Event Structure</div>
        {names.map((name) => {
          const entry = selection[name] || { enabled: false, argsText: "{}" };
          return (
            <div className="module-row" key={name}>
              <label className="module-name">
                <input
                  type="checkbox"
                  checked={entry.enabled}
                  onChange={() => toggleModule(name)}
                />{" "}
                {name}
              </label>
              <input
                type="text"
                value={entry.argsText}
                disabled={!entry.enabled}
                onChange={(e) => setArgsText(name, e.target.value)}
                style={{ flex: 1, marginLeft: 12 }}
              />
            </div>
          );
        })}

        {error && <div className="modal-hint" style={{ color: "var(--danger)" }}>{error}</div>}
        {availableModules === null ? (
          <div className="modal-hint">
            Could not reach HuRI to list its modules: showing the curated ones, any of which
            may be missing on the server (it then refuses the session).
          </div>
        ) : (
          <div className="modal-hint">
            Applying reconnects the session with this module combination (HuRI/ATP.xlsx F1/F2).
          </div>
        )}

        <div className="modal-actions">
          <button type="button" className="modal-apply" onClick={apply}>
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
