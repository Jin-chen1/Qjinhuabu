import { describe, expect, it } from "vitest";
import { buildComposePayload, type ComposeTimelineState } from "@/features/canvas/compose/timelineModel";

describe("composition crop payload", () => {
  const timeline: ComposeTimelineState = { tracks: [], resolution: "720p" };
  it("leaves other timelines unchanged by default", () => {
    expect(buildComposePayload(timeline)).not.toHaveProperty("compositionCrop");
  });
  it("preserves crop configuration in draft exports without changing time or fps", () => {
    const crop = { bottom_fraction: 0.04, aspect_ratio: "9:16" as const };
    const payload = buildComposePayload({ ...timeline, compositionCrop: crop }, { fps: 24 });
    expect(payload.compositionCrop).toEqual(crop);
    expect(payload.fps).toBe(24);
    expect(payload.tracks).toEqual([]);
  });
});
