import { describe, expect, it } from "vitest";
import { formatTokenPrice, priceTag } from "./ModelTag";

describe("formatTokenPrice", () => {
  it("renders integers without a decimal", () => {
    expect(formatTokenPrice(5)).toBe("5");
    expect(formatTokenPrice(25)).toBe("25");
    expect(formatTokenPrice(0)).toBe("0");
  });

  it("trims trailing zeros on decimals", () => {
    expect(formatTokenPrice(2.5)).toBe("2.5");
    expect(formatTokenPrice(1.25)).toBe("1.25");
    expect(formatTokenPrice(0.2)).toBe("0.2");
    expect(formatTokenPrice(0.95)).toBe("0.95");
  });

  it("rounds to at most two decimals", () => {
    expect(formatTokenPrice(0.30000001)).toBe("0.3");
    expect(formatTokenPrice(1.234)).toBe("1.23");
  });

  it("returns null for missing or invalid values", () => {
    expect(formatTokenPrice(null)).toBeNull();
    expect(formatTokenPrice(undefined)).toBeNull();
    expect(formatTokenPrice(NaN)).toBeNull();
    expect(formatTokenPrice(Infinity)).toBeNull();
    expect(formatTokenPrice(-1)).toBeNull();
  });
});

describe("priceTag", () => {
  it("combines input and output as input/output", () => {
    expect(priceTag(5, 25)).toBe("5/25");
    expect(priceTag(1, 6)).toBe("1/6");
    expect(priceTag(0.2, 1)).toBe("0.2/1");
  });

  it("returns null when either price is missing", () => {
    expect(priceTag(null, 25)).toBeNull();
    expect(priceTag(5, null)).toBeNull();
    expect(priceTag(undefined, undefined)).toBeNull();
  });
});
