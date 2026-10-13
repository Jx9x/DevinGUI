import { describe, expect, it } from "vitest";
import { assertWSInbound } from "./types";

describe("history_loaded runtime validation", () => {
  it("accepts the normalized backend history shape including transcript tool ids", () => {
    const message = {
      type: "history_loaded",
      sessionId: "session-1",
      requestId: "req-load-1",
      messages: [
        {
          row_id: 1,
          node_id: "node-1",
          parent_node_id: null,
          created_at: "2026-07-14T10:00:00.123456700Z",
          role: "user",
          content: "Inspect the project",
          message_id: null,
          metrics: null,
          finish_reason: null,
          metadata: null,
        },
      ],
      reasoning: [
        {
          step_id: 8,
          reasoning_content: "I should inspect the file.",
          timestamp: "2026-07-14T10:00:01Z",
          tool_calls: [
            {
              tool_call_id: "functions.read:1",
              function_name: "read",
              arguments: { file_path: "src/App.tsx" },
            },
          ],
          message: "",
          metrics: { prompt_tokens: 100, completion_tokens: 20, cached_tokens: 80 },
        },
      ],
    } as const;

    expect(assertWSInbound(message)).toEqual(message);
  });
});
