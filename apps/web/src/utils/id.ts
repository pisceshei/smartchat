/** Client-side idempotency ids (client_msg_id). */
export function newClientId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `cid-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
