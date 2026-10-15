import { useEffect, useState } from "react";
import { BACKEND_URL } from "../config.js";

/** The modules the connected HuRI instance serves (backend/main.py's
 * /huri-modules, a proxy of HuRI's GET /modules), in the server's names.
 * `null` means "couldn't ask" (don't block on it), as opposed to an empty
 * array (asked, HuRI serves nothing). */
export function useAvailableModules() {
  const [availableModules, setAvailableModules] = useState(null);

  useEffect(() => {
    fetch(`${BACKEND_URL}/huri-modules`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => setAvailableModules(data.modules ?? null))
      .catch(() => setAvailableModules(null));
  }, []);

  return availableModules;
}
