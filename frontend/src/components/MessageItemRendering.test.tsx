import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MessageItem } from "./MessageItem";
import type { UIMessage } from "../types";

afterEach(cleanup);

function message(overrides: Partial<UIMessage> = {}): UIMessage {
  return {
    id: "message-1",
    role: "user",
    content: "",
    timestamp: null,
    isStreaming: false,
    ...overrides,
  };
}

describe("MessageItem formatting", () => {
  it("marks the measured output rate with a tilde", () => {
    render(<MessageItem message={message({
      role: "assistant", content: "A completed response",
      metrics: { output_tokens: 282, total_time_ms: 6622, tokens_per_sec: 42.5853 },
    })} />);
    expect(screen.getByText("~43 tok/s")).toBeTruthy();
  });

  it.each(["user", "tool", "assistant"] as const)(
    "shows image-only %s messages before expansion",
    (role) => {
      render(
        <MessageItem
          message={message({
            role,
            attachments: [{ src: "/api/message-nodes/5/images/1", mimeType: "image/png" }],
          })}
        />
      );
      const image = screen.getByRole("img");
      expect(image.getAttribute("src")).toBe("/api/message-nodes/5/images/1");
      expect(image.getAttribute("loading")).toBe("lazy");
      expect(screen.getByRole("button", { name: /Enlarge/ })).toBeTruthy();
    }
  );

  it("shows a pasted image and opens a dismissible full-size preview", () => {
    // JSDOM has a dialog element but does not implement its browser methods.
    const originalShowModal = HTMLDialogElement.prototype.showModal;
    const originalClose = HTMLDialogElement.prototype.close;
    HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    HTMLDialogElement.prototype.close = function () {
      this.open = false;
      this.dispatchEvent(new Event("close"));
    };
    render(
      <MessageItem message={message({ attachments: [{ mimeType: "image/png", data: "cG5n" }] })} />
    );
    expect(screen.getByRole("img").getAttribute("src")).toBe("data:image/png;base64,cG5n");
    fireEvent.click(screen.getByRole("button", { name: "Enlarge image 1" }));
    expect(screen.getByRole("dialog", { name: "Image 1" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Actual size" }));
    expect(screen.getByRole("button", { name: "Fit to window" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close image preview" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Enlarge image 1" }));
    HTMLDialogElement.prototype.showModal = originalShowModal;
    HTMLDialogElement.prototype.close = originalClose;
  });

  it("renders live ACP images without dumping base64 or hiding them in collapsed text", () => {
    render(
      <MessageItem
        message={message({
          role: "assistant",
          toolCalls: [
            {
              id: "read-1",
              title: "Read screenshot",
              kind: "read",
              status: "completed",
              content: [
                { text: "tool details" },
                { type: "image", mimeType: "image/png", data: "cG5n" },
              ],
            },
          ],
        })}
      />
    );
    expect(screen.getByRole("img", { name: "Viewed image 1" }).getAttribute("src")).toBe(
      "data:image/png;base64,cG5n"
    );
    expect(screen.queryByText("tool details")).toBeNull();
    expect(screen.queryByText(/cG5n/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Read screenshot/ }));
    expect(screen.getByText("tool details")).toBeTruthy();
    expect(screen.getAllByRole("img")).toHaveLength(1);
  });

  it("shows an unavailable label for a missing image", () => {
    render(
      <MessageItem
        message={message({
          attachments: [{ src: "/api/message-nodes/5/images/9", mimeType: "image/png" }],
        })}
      />
    );
    fireEvent.error(screen.getByRole("img"));
    expect(screen.getByRole("status").textContent).toBe("Image 1 unavailable");
  });

  it("renders no image for an invalid attachment source", () => {
    render(
      <MessageItem
        message={message({ attachments: [{ src: "/missing", mimeType: "image/png" }] })}
      />
    );
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Image 1 unavailable");
  });

  it.each([
    "/api/message-nodes/0/images/1",
    "/api/message-nodes/5/images/01",
    "/api/browse?path=C:/",
    "//evil.test/x.png",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:image/svg+xml;base64,PHN2Zz4=",
    "data:image/png;base64,cG5n=",
  ])("renders no image for unsafe attachment source %s", (src) => {
    render(
      <MessageItem message={message({ attachments: [{ src, mimeType: "image/png" }] })} />
    );
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Image 1 unavailable");
  });

  it("clears the thumbnail when a rerender replaces the source with an invalid one", () => {
    const { rerender } = render(
      <MessageItem
        message={message({
          attachments: [{ src: "/api/message-nodes/5/images/1", mimeType: "image/png" }],
        })}
      />
    );
    expect(screen.getByRole("img")).toBeTruthy();
    rerender(
      <MessageItem
        message={message({ attachments: [{ src: "/missing", mimeType: "image/png" }] })}
      />
    );
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Image 1 unavailable");
    rerender(
      <MessageItem
        message={message({
          attachments: [{ src: "/api/message-nodes/5/images/2", mimeType: "image/png" }],
        })}
      />
    );
    expect(screen.getByRole("img").getAttribute("src")).toBe("/api/message-nodes/5/images/2");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("never renders Markdown browse endpoint payloads as images", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content: "![browse](/api/browse?path=C:/Users)\n![missing](/missing)",
        })}
      />
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("renders accepted Markdown image sources", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content:
            "![stored](/api/message-nodes/5/images/1)\n\n![pasted](data:image/png;base64,cG5n)",
        })}
      />
    );
    const images = container.querySelectorAll("img");
    expect([...images].map((img) => img.getAttribute("src"))).toEqual([
      "/api/message-nodes/5/images/1",
      "data:image/png;base64,cG5n",
    ]);
  });

  it("replaces Markdown external images with an explicit open link", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content: "![tracker](https://evil.test/x.png)",
        })}
      />
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
    const link = screen.getByRole("link", { name: "Open external image" });
    expect(link.getAttribute("href")).toBe("https://evil.test/x.png");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("referrerPolicy")).toBe("no-referrer");
  });

  it("renders rejected Markdown image sources as inert text only", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content:
            "![svg](data:image/svg+xml;base64,PHN2Zz4=)\n\n![rel](images/x.png)\n\n![relroot](/etc/passwd)",
        })}
      />
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("svg")).toBeTruthy();
    expect(screen.getByText("relroot")).toBeTruthy();
  });

  it("keeps raw HTML and javascript links inert", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content:
            '<img src="https://evil.test/x.png">\n\n[click](javascript:alert(1))\n\n[site](https://example.com)',
        })}
      />
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
    const anchors = [...container.querySelectorAll("a")];
    expect(
      anchors.some((anchor) => (anchor.getAttribute("href") ?? "").includes("javascript"))
    ).toBe(false);
    expect(screen.getByRole("link", { name: "site" }).getAttribute("href")).toBe(
      "https://example.com"
    );
  });

  it("preserves user-authored line breaks in rendered conversation text", () => {
    const { container } = render(
      <MessageItem message={message({ content: "First line\nSecond line\n\nFourth line" })} />
    );

    const markdown = container.querySelector(".message-markdown");
    const paragraphs = markdown?.querySelectorAll("p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs?.[0]).toHaveProperty("textContent", "First line\nSecond line");
    expect(paragraphs?.[0].querySelectorAll("br")).toHaveLength(1);
    expect(paragraphs?.[1]).toHaveProperty("textContent", "Fourth line");
    expect(markdown?.classList.contains("whitespace-pre-wrap")).toBe(false);
  });

  it("does not expose Markdown block-separator whitespace around headings and lists", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "assistant",
          content: "## Guild rewards\n\n- New helper\n\n## Arena\n\n- Added a delay",
        })}
      />
    );

    const markdown = container.querySelector(".message-markdown");
    expect(markdown?.classList.contains("whitespace-pre-wrap")).toBe(false);
    expect(markdown?.querySelectorAll(":scope > h2")).toHaveLength(2);
    expect(markdown?.querySelectorAll(":scope > ul")).toHaveLength(2);
  });

  it("renders Devin file references as subtle filename labels instead of HTML", () => {
    render(
      <MessageItem
        message={message({
          role: "assistant",
          content:
            'Updated the renderer. <ref_snippet file="C:\\repo\\src\\MessageItem.tsx" lines="37-43" />',
        })}
      />
    );

    expect(screen.queryByText(/<ref_snippet/)).toBeNull();
    const reference = screen.getByText("MessageItem.tsx:37-43");
    expect(reference.tagName).toBe("CODE");
    expect(reference.classList.contains("file-reference")).toBe(true);
    expect(reference).toHaveProperty("title", "C:\\repo\\src\\MessageItem.tsx");
  });

  it("preserves line breaks when expanded tool file output is rendered", () => {
    const { container } = render(
      <MessageItem
        message={message({
          role: "tool",
          content: "line 1\n    indented line 2\nline 3",
        })}
      />
    );

    fireEvent.click(screen.getByRole("button"));
    const output = container.querySelector(".tool-output");
    expect(output).toHaveProperty("textContent", "line 1\n    indented line 2\nline 3");
    expect(output?.classList.contains("whitespace-pre-wrap")).toBe(true);
  });
});
