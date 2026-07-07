import { describe, expect, it } from "vitest";

import { createStore, markMessage, store, upsertMessage, type UiMessage } from "../src/chat/store";

function msg(partial: Partial<UiMessage>): UiMessage {
  return {
    id: "m1",
    sender_type: "contact",
    content: { blocks: [{ kind: "text", text: "hi" }] },
    created_at: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

describe("createStore", () => {
  it("notifies subscribers and supports functional patches", () => {
    const s = createStore({ n: 1 });
    const seen: number[] = [];
    const unsub = s.subscribe((st) => seen.push(st.n));
    s.set({ n: 2 });
    s.set((st) => ({ n: st.n + 10 }));
    unsub();
    s.set({ n: 99 });
    expect(seen).toEqual([2, 12]);
    expect(s.get().n).toBe(99);
  });
});

describe("upsertMessage", () => {
  it("keeps messages ordered by created_at", () => {
    store.set({ messages: [] });
    upsertMessage(msg({ id: "b", created_at: "2026-01-01T00:00:02Z" }));
    upsertMessage(msg({ id: "a", created_at: "2026-01-01T00:00:01Z" }));
    expect(store.get().messages.map((m) => m.id)).toEqual(["a", "b"]);
  });

  it("replaces optimistic local echo by client_msg_id and clears local_state", () => {
    store.set({ messages: [] });
    upsertMessage(msg({ id: "local-x", client_msg_id: "x", local_state: "pending" }));
    upsertMessage({
      ...msg({ id: "srv-1", client_msg_id: "x", created_at: "2026-01-01T00:00:05Z" }),
      local_state: undefined,
    });
    const list = store.get().messages;
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe("srv-1");
    expect(list[0].local_state).toBeUndefined();
  });

  it("dedupes by server id on WS + REST double delivery", () => {
    store.set({ messages: [] });
    upsertMessage(msg({ id: "srv-1" }));
    upsertMessage(msg({ id: "srv-1" }));
    expect(store.get().messages).toHaveLength(1);
  });
});

describe("markMessage", () => {
  it("patches a message in place", () => {
    store.set({ messages: [] });
    upsertMessage(msg({ id: "local-y", client_msg_id: "y", local_state: "pending" }));
    markMessage("local-y", { local_state: "failed" });
    expect(store.get().messages[0].local_state).toBe("failed");
  });
});
