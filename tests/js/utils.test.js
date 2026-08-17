import { describe, it, expect } from "vitest";
import { formatUptime, formatSessionTime, parseMountPoint } from "../../static/utils.js";

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
