import { describe, expect, it } from "vitest";
import { classifyCompactionMessage } from "./MessageItem";

describe("classifyCompactionMessage", () => {
  it("recognizes the three compactor messages", () => {
    expect(classifyCompactionMessage("Conversation to summarize:\nA long conversation")).toBe(
      "source"
    );
    expect(
      classifyCompactionMessage(
        "Now summarize the conversation above as per the format given. Remember, do NOT take any actions."
      )
    ).toBe("instruction");
    expect(classifyCompactionMessage("<summary>\n## Result\n</summary>")).toBe("summary");
  });

  it("leaves normal chat messages expanded", () => {
    expect(classifyCompactionMessage("Please summarize this file for me.")).toBeNull();
  });
});
