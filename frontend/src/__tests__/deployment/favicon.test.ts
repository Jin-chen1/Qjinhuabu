import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";

import { describe, expect, it } from "vitest";

function iconLinks() {
  const html = new DOMParser().parseFromString(
    readFileSync("index.html", "utf8"),
    "text/html",
  );
  return [...html.querySelectorAll<HTMLLinkElement>('link[rel="icon"]')];
}

function iconFile(link: HTMLLinkElement) {
  const url = new URL(link.getAttribute("href")!, "http://localhost");
  expect(url.origin).toBe("http://localhost");
  expect(url.searchParams.get("v")).toBeTruthy();
  return readFileSync(`public${url.pathname}`);
}

describe("QK favicon deployment contract", () => {
  it("declares small, versioned PNG icons instead of the full-size brand image", () => {
    const pngs = iconLinks().filter((link) => link.type === "image/png");
    expect(pngs.map((link) => link.getAttribute("sizes"))).toEqual([
      "16x16",
      "32x32",
      "48x48",
    ]);
    for (const link of pngs) {
      expect(link.getAttribute("href")).not.toContain("/brand/qk-nebula.png");
      expect(link.getAttribute("href")).toMatch(/^\/brand\/qk-favicon-/);
      iconFile(link);
    }
  });

  it.each([16, 32, 48])("ships a valid, lightweight %ipx PNG", (size) => {
    const link = iconLinks().find(
      (candidate) =>
        candidate.type === "image/png" &&
        candidate.getAttribute("sizes") === `${size}x${size}`,
    );
    expect(link).toBeDefined();
    const png = iconFile(link!);
    expect(png.length).toBeLessThan(16 * 1024);
    expect(png.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
    expect(png.subarray(12, 16).toString()).toBe("IHDR");
    expect(png.readUInt32BE(16)).toBe(size);
    expect(png.readUInt32BE(20)).toBe(size);
    expect(png[24]).toBe(8);
    expect(png[25]).toBe(2);
    expect(png[28]).toBe(0);

    const imageData: Buffer[] = [];
    let offset = 8;
    while (offset < png.length) {
      const length = png.readUInt32BE(offset);
      expect(offset + length + 12).toBeLessThanOrEqual(png.length);
      if (png.toString("ascii", offset + 4, offset + 8) === "IDAT") {
        imageData.push(png.subarray(offset + 8, offset + 8 + length));
      }
      offset += length + 12;
    }
    expect(offset).toBe(png.length);
    expect(inflateSync(Buffer.concat(imageData)).length).toBe(
      (size * 3 + 1) * size,
    );
  });

  it("provides a versioned root ICO fallback with all three tab sizes", () => {
    const link = iconLinks().find((candidate) => candidate.type === "image/x-icon");
    expect(link).toBeDefined();
    expect(link!.getAttribute("href")).toMatch(/^\/favicon\.ico\?v=/);
    const ico = iconFile(link!);
    expect(ico.length).toBeLessThan(32 * 1024);
    expect(ico.readUInt16LE(0)).toBe(0);
    expect(ico.readUInt16LE(2)).toBe(1);
    expect(ico.readUInt16LE(4)).toBe(3);
    for (const [index, size] of [16, 32, 48].entries()) {
      const entry = 6 + index * 16;
      expect(ico[entry]).toBe(size);
      expect(ico[entry + 1]).toBe(size);
      expect(ico.readUInt16LE(entry + 6)).toBe(32);
      const length = ico.readUInt32LE(entry + 8);
      const offset = ico.readUInt32LE(entry + 12);
      expect(offset).toBeGreaterThanOrEqual(54);
      expect(offset + length).toBeLessThanOrEqual(ico.length);
      expect(ico.readUInt32LE(offset)).toBe(40);
      expect(ico.readInt32LE(offset + 4)).toBe(size);
      expect(ico.readInt32LE(offset + 8)).toBe(size * 2);
    }
  });
});
