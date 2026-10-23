import type { WidgetSettingField, WidgetSettingsValidationError } from "./validation";

/** The expected value when a widget names an enable topic but no value. */
export const DEFAULT_ENABLE_VALUE = "ready";

// A widget is usable only while enable_topic last reported enable_value; see resolveEnableGate.
export const ENABLE_GATE_FIELDS: readonly WidgetSettingField[] = [
  { key: "enable_topic", label: "Enable only while topic", type: "text", required: false },
  { key: "enable_value", label: `…reports (default "${DEFAULT_ENABLE_VALUE}")`, type: "text", required: false },
];

/** Settings a card can carry beyond what its kind is about: they alone leave nothing to fill in. */
export const COMMON_SETTING_KEYS: readonly string[] = ["hide_title", "enable_topic", "enable_value"];

export function validateEnableGate(settings: Record<string, unknown>): WidgetSettingsValidationError[] {
  const errors: WidgetSettingsValidationError[] = [];
  const topic = settings.enable_topic;
  if (topic !== undefined) {
    if (typeof topic !== "string") {
      errors.push({ field: "enable_topic", message: "enable_topic must be a string" });
    } else if (topic.trim() !== "" && !topic.trim().startsWith("/")) {
      errors.push({ field: "enable_topic", message: "enable_topic must start with '/'" });
    }
  }
  if (settings.enable_value !== undefined && typeof settings.enable_value !== "string") {
    errors.push({ field: "enable_value", message: "enable_value must be a string" });
  }
  return errors;
}

/**
 * The topic whose latest value decides whether the widget is usable, and the value it must report.
 * Null when the widget names no topic: it is never gated.
 */
export function resolveEnableGate(settings: Record<string, unknown>): { topic: string; value: string } | null {
  const topic = typeof settings.enable_topic === "string" ? settings.enable_topic.trim() : "";
  if (!topic.startsWith("/")) {
    return null;
  }
  const value = typeof settings.enable_value === "string" ? settings.enable_value.trim() : "";
  return { topic, value: value || DEFAULT_ENABLE_VALUE };
}

/**
 * The text a gate compares. std_msgs carry their value in `data` (String, Bool, Int*, UInt*, Float*), and
 * a display widget subscribed with field path `data` hands over the bare value: both read the same.
 */
export function readEnableGateText(value: unknown): string {
  const unwrapped = typeof value === "object" && value !== null && "data" in value ? value.data : value;
  if (typeof unwrapped === "string") {
    return unwrapped;
  }
  if (typeof unwrapped === "number" || typeof unwrapped === "boolean") {
    return String(unwrapped);
  }
  return JSON.stringify(unwrapped) ?? "";
}

/**
 * Whether a reported value opens the gate. Numbers compare as numbers, so an authored "1.0" matches the
 * `1` a Float64 1.0 arrives as; booleans ignore case, so "True" matches a Bool; anything else is exact.
 */
export function enableGateMatches(reported: string, expected: string): boolean {
  const actual = reported.trim();
  const wanted = expected.trim();
  if (actual !== "" && wanted !== "" && Number.isFinite(Number(actual)) && Number.isFinite(Number(wanted))) {
    return Number(actual) === Number(wanted);
  }
  if (/^(true|false)$/i.test(actual) && /^(true|false)$/i.test(wanted)) {
    return actual.toLowerCase() === wanted.toLowerCase();
  }
  return actual === wanted;
}
