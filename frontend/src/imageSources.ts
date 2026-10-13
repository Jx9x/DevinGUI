const ATTACHMENT_SOURCE = /^\/api\/message-nodes\/[1-9]\d*\/images\/(?:0|[1-9]\d*)$/;

const DATA_SOURCE = /^data:image\/(?:png|jpeg|gif|webp|bmp|avif);base64,(.+)$/;

const BASE64_BODY = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)?$/;

export function safeImageSource(value: unknown): string | null {
  if (typeof value !== "string" || /\s/.test(value)) return null;
  if (ATTACHMENT_SOURCE.test(value)) return value;
  const data = DATA_SOURCE.exec(value);
  if (data && BASE64_BODY.test(data[1])) return value;
  return null;
}

export function externalImageUrl(value: unknown): boolean {
  if (typeof value !== "string") return false;
  try {
    const protocol = new URL(value).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}
