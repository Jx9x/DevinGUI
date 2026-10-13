import { describe, expect, it } from "vitest";
import { safeImageSource } from "./imageSources";

describe("safeImageSource", () => {
  it.each([
    "/api/message-nodes/5/images/1",
    "/api/message-nodes/1/images/0",
    "/api/message-nodes/123456/images/42",
    "data:image/png;base64,cG5n",
    "data:image/jpeg;base64,/9j/4AAQ",
    "data:image/gif;base64,R0lG",
    "data:image/webp;base64,UklG",
    "data:image/bmp;base64,Qk0=",
    "data:image/avif;base64,AAAA",
    "data:image/png;base64,QQ==",
    "data:image/png;base64,QUJD",
    "data:image/png;base64,QUJDQQ==",
    "data:image/png;base64,cG5=",
  ])("accepts %s", (source) => {
    expect(safeImageSource(source)).toBe(source);
  });

  it.each([
    "/api/message-nodes/0/images/1",
    "/api/message-nodes/-5/images/1",
    "/api/message-nodes/05/images/1",
    "/api/message-nodes/5/images/01",
    "/api/message-nodes/5/images/",
    "/api/message-nodes//images/1",
    "/api/message-nodes/5/images/1?x=1",
    "/api/message-nodes/5/images/1#frag",
    "/api/message-nodes/5/images/1/",
    "/api/message-nodes/5/images/1/../2",
    "/api/message-nodes/5/../images/1",
    "/api/message-nodes/5%2fimages/1",
    "/API/message-nodes/5/images/1",
    "//api/message-nodes/5/images/1",
    "/api/message-nodes/5/images/1 ",
    " /api/message-nodes/5/images/1",
    "/api/message-nodes/5/images/1\n",
    "/api/message-nodes/5/images/1\r\n",
    "/api/message-nodes/5/images/1\t",
    "/api/message-nodes/5\n/images/1",
    "/missing",
    "/api/browse",
    "/api/browse?path=C:/",
    "images/1.png",
    "./x.png",
    "../x.png",
    "http://evil.test/x.png",
    "https://evil.test/x.png",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "blob:https://evil.test/id",
    "filesystem:https://evil.test/x",
    "device://x",
    "DATA:image/png;base64,cG5n",
    "data:Image/PNG;base64,cG5n",
    "data:image/png;BASE64,cG5n",
    "data:image/png;charset=utf-8;base64,cG5n",
    "data:image/png;base64;x,cG5n",
    "data:image/png;cG5n",
    "data:image/svg+xml;base64,PHN2Zz4=",
    "data:text/html;base64,PGI+",
    "data:image/png;base64,",
    "data:image/png;base64,cG5n ",
    "data:image/png;base64, cG5n",
    "data:image/png;base64,cG5n\n",
    "data:image/png;base64,cG5n\r\n",
    "data:image/png;base64,cG5n\t",
    "data:image/png;base64,cG5\nn",
    "data:image/\tpng;base64,cG5n",
    "data:image/png;base64,cG5n=",
    "data:image/png;base64,cG5n====",
    "data:image/png;base64,c=A=",
    "data:image/png;base64,====",
    "data:image/png;base64,AAA",
    "data:image/png;base64,cG5n,x",
    " data:image/png;base64,cG5n",
    "data:image/png;base64,cG5n#x",
    "data:image/png;base64,cG5n?x",
  ])("rejects %s", (source) => {
    expect(safeImageSource(source)).toBeNull();
  });

  it.each([null, undefined, 5, {}, [], true])("rejects non-string %o", (value) => {
    expect(safeImageSource(value)).toBeNull();
  });
});
