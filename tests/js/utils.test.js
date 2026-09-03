import { describe, it, expect } from "vitest";
import { formatUptime, formatSessionTime, parseMountPoint, ordinal } from "../../static/utils.js";

// ── formatUptime ──────────────────────────────────────────────────────────────

describe("formatUptime", () => {
  it("shows hours and minutes when under 1 day", () =>
    expect(formatUptime(3 * 3600000 + 45 * 60000)).toBe("3h 45m"));

  it("shows 0h Xm when under 1 hour", () =>
    expect(formatUptime(20 * 60000)).toBe("0h 20m"));

  it("shows days when >= 1 day", () =>
    expect(formatUptime(2 * 86400000 + 3 * 3600000)).toBe("2d 3h"));

  it("shows 1d 0h at exact day boundary", () =>
    expect(formatUptime(86400000)).toBe("1d 0h"));

  it("returns 0h 0m for zero", () =>
    expect(formatUptime(0)).toBe("0h 0m"));

  it("floors sub-minute precision", () =>
    expect(formatUptime(3661000)).toBe("1h 1m"));
});

// ── formatSessionTime ─────────────────────────────────────────────────────────

describe("formatSessionTime", () => {
  it("formats m:ss when under 1 hour", () =>
    expect(formatSessionTime(247)).toBe("4:07 listening"));

  it("zero-pads seconds", () =>
    expect(formatSessionTime(60)).toBe("1:00 listening"));

  it("shows Xh Ym when >= 1 hour", () =>
    expect(formatSessionTime(5400)).toBe("1h 30m listening"));

  it("returns 0:00 listening for zero", () =>
    expect(formatSessionTime(0)).toBe("0:00 listening"));

  it("shows 0m on exact hour", () =>
    expect(formatSessionTime(7200)).toBe("2h 0m listening"));
});

// ── parseMountPoint ───────────────────────────────────────────────────────────

describe("parseMountPoint", () => {
  it("uppercases the MIME subtype", () =>
    expect(parseMountPoint("audio/mpeg")).toBe("MPEG"));

  it("returns — for null", () =>
    expect(parseMountPoint(null)).toBe("—"));

  it("returns — for empty string", () =>
    expect(parseMountPoint("")).toBe("—"));

  it("returns — with no slash", () =>
    expect(parseMountPoint("audiompeg")).toBe("—"));

  it("returns — for trailing slash (empty subtype)", () =>
    expect(parseMountPoint("audio/")).toBe("—"));

  it("handles application/ogg", () =>
    expect(parseMountPoint("application/ogg")).toBe("OGG"));
});

// ── ordinal ───────────────────────────────────────────────────────────────────
// Powers the "heard this before" badge — "3rd play · first heard 21 Aug on …".

describe("ordinal", () => {
  it("uses st/nd/rd for 1, 2, 3", () => {
    expect(ordinal(1)).toBe("1st");
    expect(ordinal(2)).toBe("2nd");
    expect(ordinal(3)).toBe("3rd");
  });

  it("uses th for 4 through 10", () => {
    expect(ordinal(4)).toBe("4th");
    expect(ordinal(9)).toBe("9th");
    expect(ordinal(10)).toBe("10th");
  });

  it("uses th for the 11-13 exception, not st/nd/rd", () => {
    // The reason this isn't a plain last-digit lookup: 13 is "13th", not "13rd".
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(12)).toBe("12th");
    expect(ordinal(13)).toBe("13th");
  });

  it("resumes st/nd/rd past the exception", () => {
    expect(ordinal(21)).toBe("21st");
    expect(ordinal(22)).toBe("22nd");
    expect(ordinal(23)).toBe("23rd");
    expect(ordinal(24)).toBe("24th");
  });

  it("handles the 111-113 exception in the next hundred", () => {
    expect(ordinal(111)).toBe("111th");
    expect(ordinal(112)).toBe("112th");
    expect(ordinal(101)).toBe("101st");
  });
});
