// Attach files dropped, pasted, or picked in the chat composer.
//
// An attachment is stored in the project's state directory and referenced by
// path, not smuggled into the prompt as bytes: the agent already reads files
// with its own tools, so a path is something it can act on, and the context
// block keeps its rule of carrying references only.
//
// Images are stored the same way. D[Ai]NO's provider layer sends text messages
// (`daino/schemas/core.py::Message.content` is a string), so no model here can
// *look* at a picture yet — the path is still useful ("optimise the screenshot
// at …", "move it into assets/"), and the composer says so rather than implying
// the model can see it.
import { api, ApiError } from "../api/client";
import { useAgentStore } from "../store/agentStore";

/** Beyond this the backend refuses it anyway; fail before reading the file. */
export const MAX_ATTACHMENT_BYTES = 8_000_000;
export const MAX_ATTACHMENTS = 10;

export function isImage(name: string): boolean {
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic)$/i.test(name);
}

async function toBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // Chunked, because a single spread of a few million bytes overflows the stack.
  let binary = "";
  const CHUNK = 0x8000;
  for (let index = 0; index < bytes.length; index += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(index, index + CHUNK));
  }
  return btoa(binary);
}

export interface AttachResult {
  attached: string[];
  errors: string[];
}

export async function attachFiles(files: readonly File[]): Promise<AttachResult> {
  const store = useAgentStore.getState();
  const existing = store.chips.filter((chip) => chip.kind === "attachment").length;
  const result: AttachResult = { attached: [], errors: [] };

  for (const file of files) {
    if (existing + result.attached.length >= MAX_ATTACHMENTS) {
      result.errors.push(`Only ${MAX_ATTACHMENTS} attachments per message.`);
      break;
    }
    if (file.size > MAX_ATTACHMENT_BYTES) {
      result.errors.push(
        `${file.name} is larger than ${MAX_ATTACHMENT_BYTES / 1_000_000} MB.`,
      );
      continue;
    }
    try {
      const stored = await api.attachFile(file.name, await toBase64(file));
      useAgentStore.getState().addChip({
        id: `attachment:${stored.path}`,
        kind: "attachment",
        label: `${isImage(stored.name) ? "image" : "file"}: ${stored.name}`,
        payload: { attachment: stored.path },
      });
      result.attached.push(stored.path);
    } catch (err) {
      result.errors.push(
        `${file.name}: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    }
  }
  return result;
}
