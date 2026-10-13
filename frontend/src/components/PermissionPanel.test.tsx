import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { PermissionRequest } from "../chatSessionState";
import type { ToolCallContent, UIMessage } from "../types";
import { PermissionPanel } from "./PermissionPanel";

afterEach(cleanup);

function mkRequest(
  data: Record<string, unknown> = {},
  overrides: Partial<PermissionRequest> = {}
): PermissionRequest {
  return {
    sessionId: "s1",
    toolCallId: "tc-1",
    data,
    ...overrides,
  };
}

function carrierMessage(
  toolCallId: string,
  overrides: Partial<{ title: string; content: ToolCallContent[] }> = {}
): UIMessage {
  return {
    id: `tool-${toolCallId}`,
    role: "assistant",
    content: "",
    timestamp: null,
    isStreaming: true,
    toolCalls: [
      {
        id: toolCallId,
        title: overrides.title ?? "carrier title",
        kind: "execute",
        status: "pending",
        ...(overrides.content ? { content: overrides.content } : {}),
      },
    ],
  };
}

function renderPanel(
  request: PermissionRequest | null,
  messages: UIMessage[] = [],
  connected = true
) {
  const onRespond = vi.fn();
  const onDiscard = vi.fn();
  const utils = render(
    <PermissionPanel
      request={request}
      messages={messages}
      connected={connected}
      onRespond={onRespond}
      onDiscard={onDiscard}
    />
  );
  return { ...utils, onRespond, onDiscard };
}

describe("PermissionPanel", () => {
  it("renders nothing when request is null", () => {
    const { container } = renderPanel(null);
    expect(container.firstChild).toBeNull();
  });

  it("prefers permissionData.detail over the carrier tool call content", () => {
    renderPanel(
      mkRequest({ detail: "rm -rf /tmp/x" }),
      [carrierMessage("tc-1", { content: ["carrier content"] })]
    );
    expect(screen.getByText("rm -rf /tmp/x")).toBeTruthy();
    expect(screen.queryByText("carrier content")).toBeNull();
  });

  it("prefers the carrier tool call title over the nested request title", () => {
    renderPanel(
      mkRequest({ toolCall: { title: "Nested title" } }),
      [carrierMessage("tc-1", { title: "Carrier title" })]
    );
    expect(screen.getByText("Approval required for Carrier title")).toBeTruthy();
  });

  it("joins only string and text items from mixed carrier content with newlines", () => {
    renderPanel(mkRequest(), [
      carrierMessage("tc-1", {
        content: ["line one", { text: "line two" }, { other: "ignored" }],
      }),
    ]);
    const pre = document.querySelector("pre");
    expect(pre?.textContent).toBe("line one\nline two");
  });

  it("shows the fallback detail when neither detail nor carrier content is present", () => {
    renderPanel(mkRequest(), [carrierMessage("tc-1")]);
    expect(
      screen.getByText(
        "Devin did not include the exact command or arguments in this permission request."
      )
    ).toBeTruthy();
  });

  it("filters invalid options and falls back to Deny/Allow when none remain", () => {
    renderPanel(
      mkRequest({ detail: "cmd", options: [{ name: "no id" }, null, "str", 42] })
    );
    const deny = screen.getByRole("button", { name: "Deny" }) as HTMLButtonElement;
    const allow = screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement;
    expect(deny.disabled).toBe(false);
    expect(allow.disabled).toBe(false);
    expect(screen.queryByRole("button", { name: "no id" })).toBeNull();
  });

  it("invokes onRespond with ('deny', optionId) for reject-kind options", () => {
    const { onRespond } = renderPanel(
      mkRequest({
        detail: "cmd",
        options: [{ optionId: "opt-rej", name: "Reject once", kind: "reject_once" }],
      })
    );
    fireEvent.click(screen.getByRole("button", { name: "Reject once" }));
    expect(onRespond).toHaveBeenCalledWith("deny", "opt-rej");
  });

  it("invokes onRespond with ('allow', optionId) for allow-kind options", () => {
    const { onRespond } = renderPanel(
      mkRequest({
        detail: "cmd",
        options: [{ optionId: "opt-alw", name: "Allow always", kind: "allow_always" }],
      })
    );
    fireEvent.click(screen.getByRole("button", { name: "Allow always" }));
    expect(onRespond).toHaveBeenCalledWith("allow", "opt-alw");
  });

  it("disables option buttons while disconnected", () => {
    renderPanel(mkRequest({ detail: "cmd" }), [], false);
    const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) expect(button.disabled).toBe(true);
  });

  it("disables option buttons and shows the submitted decision while submitting", () => {
    renderPanel(
      mkRequest({ detail: "cmd" }, { submitting: true, submittedDecision: "allow" })
    );
    expect(
      screen.getByText("Decision (allow) submitted — waiting for backend…")
    ).toBeTruthy();
    const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
    for (const button of buttons) expect(button.disabled).toBe(true);
  });

  it("shows the interrupt error and still allows retry when connected", () => {
    const { onRespond, onDiscard } = renderPanel(
      mkRequest(
        { detail: "cmd" },
        { interrupted: true, interruptError: "socket went away", submitting: false }
      )
    );
    expect(screen.getByText("socket went away")).toBeTruthy();
    const allow = screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement;
    expect(allow.disabled).toBe(false);
    fireEvent.click(allow);
    expect(onRespond).toHaveBeenCalledWith("allow", "allow");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });
});
