export interface IntegrationSettings {
  devinUsage: boolean;
  codexUsage: boolean;
  chatgpt: boolean;
}

export const DEFAULT_INTEGRATIONS: IntegrationSettings = { devinUsage: false, codexUsage: false, chatgpt: false };
const STORAGE_KEY = "devin-gui-integrations-v1";

export function readIntegrationSettings(): IntegrationSettings {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    if (typeof value === "object" && value !== null) {
      return {
        devinUsage: "devinUsage" in value && value.devinUsage === true,
        codexUsage: "codexUsage" in value && value.codexUsage === true,
        chatgpt: "chatgpt" in value && value.chatgpt === true,
      };
    }
  } catch {
    return { ...DEFAULT_INTEGRATIONS };
  }
  return { ...DEFAULT_INTEGRATIONS };
}

export function saveIntegrationSettings(settings: IntegrationSettings): boolean {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    return true;
  } catch {
    return false;
  }
}
