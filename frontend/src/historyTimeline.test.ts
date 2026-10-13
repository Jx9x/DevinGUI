import { describe, expect, it } from "vitest";
import { buildChatTimeline } from "./historyTimeline";
import type { ReasoningStep, UIMessage } from "./types";

describe("buildChatTimeline", () => {
  it("interleaves reasoning and tool calls between persisted chat messages", () => {
    const messages: UIMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "Inspect the file",
        timestamp: "2026-07-14T10:00:00Z",
        isStreaming: false,
      },
      {
        id: "tool-output-1",
        role: "tool",
        content: "file contents",
        timestamp: "2026-07-14T10:00:02Z",
        isStreaming: false,
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "The file is correct.",
        timestamp: "2026-07-14T10:00:04Z",
        isStreaming: false,
      },
    ];
    const reasoning: ReasoningStep[] = [
      {
        step_id: 8,
        reasoning_content: "I should read the file.",
        timestamp: "2026-07-14T10:00:01Z",
        tool_calls: [
          {
            tool_call_id: "read-1",
            function_name: "read",
            arguments: { file_path: "src/App.tsx" },
          },
        ],
        message: "",
        metrics: {},
      },
      {
        step_id: 9,
        reasoning_content: "The file looks correct.",
        timestamp: "2026-07-14T10:00:03Z",
        tool_calls: [],
        message: "The file is correct.",
        metrics: {},
      },
    ];

    const timeline = buildChatTimeline(messages, reasoning);

    expect(timeline.map((item) => item.kind)).toEqual([
      "message",
      "reasoning",
      "message",
      "reasoning",
      "message",
    ]);
    const firstReasoning = timeline[1];
    expect(firstReasoning.kind).toBe("reasoning");
    if (firstReasoning.kind === "reasoning") {
      expect(firstReasoning.toolCalls[0]).toMatchObject({
        id: "read-1",
        title: "read",
        status: "completed",
      });
      expect(firstReasoning.toolCalls[0].content).toEqual([
        { text: '{\n  "file_path": "src/App.tsx"\n}' },
      ]);
    }
  });

  it("supplements final agent output from the transcript when DB history omitted it", () => {
    const timeline = buildChatTimeline(
      [],
      [
        {
          step_id: 1,
          reasoning_content: "Finished.",
          timestamp: "2026-07-14T10:00:00Z",
          tool_calls: [],
          message: "Here is the result.",
          metrics: {},
        },
      ]
    );

    expect(timeline).toHaveLength(2);
    expect(timeline[1].kind).toBe("message");
    if (timeline[1].kind === "message") {
      expect(timeline[1].message.content).toBe("Here is the result.");
    }
  });

  it("keeps a partial streamed final message at the end of the timeline", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "user-1",
          role: "user",
          content: "Inspect the file",
          timestamp: "2026-07-14T10:00:00Z",
          isStreaming: false,
        },
        {
          id: "stream-1",
          role: "assistant",
          content: "Here is the ",
          timestamp: "2026-07-14T10:00:04Z",
          isStreaming: true,
        },
      ],
      [
        {
          step_id: 1,
          reasoning_content: "Finished.",
          timestamp: "2026-07-14T10:00:01Z",
          tool_calls: [],
          message: "Here is the completed final summary.",
          metrics: {},
        },
      ]
    );

    expect(timeline.map((item) => item.kind)).toEqual(["message", "reasoning", "message"]);
    expect(timeline[2].kind).toBe("message");
    if (timeline[2].kind === "message") {
      expect(timeline[2].message).toMatchObject({ id: "stream-1", content: "Here is the " });
    }
  });

  it("does not synthesize transcript assistant messages while a turn is live", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "user-1",
          role: "user",
          content: "Fix the typo",
          timestamp: "2026-08-10T03:30:00Z",
          isStreaming: false,
        },
      ],
      [
        {
          step_id: 12,
          reasoning_content: "I found another typo.",
          timestamp: "2026-08-10T03:30:01Z",
          tool_calls: [],
          message: "Also I noticed a typo in the plan summary.",
          metrics: {},
        },
      ],
      { liveTurn: true }
    );

    expect(timeline.filter((item) => item.kind === "message")).toHaveLength(1);
    expect(
      timeline.some(
        (item) =>
          item.kind === "message" &&
          item.message.content === "Also I noticed a typo in the plan summary."
      )
    ).toBe(false);
  });

  it("dedupes transcript messages across markdown and whitespace normalization", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "assistant-1",
          role: "assistant",
          content: "Also I noticed a typo: 9x{num} should be x{num}.",
          timestamp: "2026-08-10T03:30:02Z",
          isStreaming: false,
        },
      ],
      [
        {
          step_id: 13,
          reasoning_content: "Correct the plan summary too.",
          timestamp: "2026-08-10T03:30:03Z",
          tool_calls: [],
          message: "Also I noticed a typo: `9x{num}`\nshould be `x{num}`.",
          metrics: {},
        },
      ]
    );

    const assistantMessages = timeline.filter(
      (item) => item.kind === "message" && item.message.role === "assistant"
    );
    expect(assistantMessages).toHaveLength(1);
  });

  it("anchors a reasoning step before the assistant message it produced", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "assistant-summary",
          role: "assistant",
          content: "Everything is fixed.",
          timestamp: "2026-08-10T03:30:05Z",
          isStreaming: false,
        },
      ],
      [
        {
          step_id: 14,
          reasoning_content: "Now give the final summary.",
          // Transcript completion time is later than the DB message's timestamp.
          timestamp: "2026-08-10T03:30:08Z",
          tool_calls: [],
          message: "Everything is fixed.",
          metrics: {},
        },
      ]
    );

    expect(timeline.map((item) => item.kind)).toEqual(["reasoning", "message"]);
    expect(timeline[0].kind).toBe("reasoning");
    expect(timeline[1].kind).toBe("message");
  });

  it("never synthesizes transcript assistant fallbacks while any turn is live", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "user-old",
          role: "user",
          content: "Old prompt",
          timestamp: "2026-08-10T03:00:00Z",
          isStreaming: false,
        },
        {
          id: "user-current",
          role: "user",
          content: "Current prompt",
          timestamp: "2026-08-10T03:30:00Z",
          isStreaming: false,
        },
      ],
      [
        {
          step_id: 10,
          reasoning_content: "Old reasoning.",
          timestamp: "2026-08-10T03:00:01Z",
          tool_calls: [],
          message: "Historical transcript-only answer.",
          metrics: {},
        },
        {
          step_id: 11,
          reasoning_content: "Current reasoning.",
          timestamp: "2026-08-10T03:30:01Z",
          tool_calls: [],
          message: "Current transcript answer must wait for ACP.",
          metrics: {},
        },
      ],
      { liveTurn: true }
    );

    const renderedText = timeline
      .filter((item) => item.kind === "message")
      .map((item) => item.message.content);
    // During a live turn, DB/ACP are the only visible assistant-output
    // channels. Trying to infer which transcript messages are historical from
    // user-role timestamps is unsafe because Devin inserts internal user rows
    // mid-turn.
    expect(renderedText).not.toContain("Historical transcript-only answer.");
    expect(renderedText).not.toContain("Current transcript answer must wait for ACP.");
  });

  it("keeps skewed precursor reasoning and tools before the final summary", () => {
    const timeline = buildChatTimeline(
      [
        {
          id: "assistant-summary",
          role: "assistant",
          content: "All requested changes are complete.",
          // SQLite created_at can reflect when the assistant node was opened,
          // while transcript reasoning timestamps reflect later completion.
          timestamp: "2026-08-13T01:26:00Z",
          isStreaming: false,
        },
      ],
      [
        {
          step_id: 20,
          reasoning_content: "I should update the todo list first.",
          timestamp: "2026-08-13T01:26:02Z",
          tool_calls: [
            {
              tool_call_id: "todo-1",
              function_name: "update_todo",
              arguments: { status: "complete" },
            },
          ],
          message: "",
          metrics: {},
        },
        {
          step_id: 21,
          reasoning_content: "Now provide the final summary.",
          timestamp: "2026-08-13T01:26:03Z",
          tool_calls: [],
          message: "All requested changes are complete.",
          metrics: {},
        },
      ]
    );

    expect(timeline.map((item) => item.kind)).toEqual([
      "reasoning",
      "reasoning",
      "message",
    ]);
    const first = timeline[0];
    expect(first.kind).toBe("reasoning");
    if (first.kind === "reasoning") {
      expect(first.toolCalls[0]?.id).toBe("todo-1");
    }
    expect(timeline[2].kind).toBe("message");
  });

});
