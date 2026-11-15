import { describe, expect, it } from "vitest";
import { enforceSingleShotReferenceTimeline, type ComposeTimelineState } from "@/features/canvas/compose/timelineModel";

function timeline(videoDuration = 6000): ComposeTimelineState {
  const clip = {
    id: "clip", nodeId: "video", kind: "video" as const, sourceUrl: "/generated.mp4",
    displayName: null, thumbUrl: null, durationMs: videoDuration, timelineStartMs: 0,
    trimStartMs: 0, trimEndMs: videoDuration, volume: 1, muted: false, speed: 1,
  };
  return { resolution: "720p", tracks: [
    { id: "track_video", kind: "video", clips: [clip] },
    { id: "track_audio", kind: "audio", clips: [{
      ...clip, id: "audio", nodeId: "source-audio", kind: "audio",
      sourceUrl: "/original.m4a", durationMs: 5062, trimEndMs: 5062,
    }] },
  ] };
}

describe("single shot reference timeline", () => {
  it("keeps reference length and source audio after generated URL reconciliation", () => {
    const original = timeline();
    const result = enforceSingleShotReferenceTimeline(original, 5057, "source-audio");
    const [video, audio] = result.tracks.flatMap(track => track.clips);
    expect(video.trimEndMs).toBe(5057);
    expect(video.muted).toBe(true);
    expect(audio.trimEndMs).toBe(5057);
    expect(audio.muted).toBe(false);
    expect(audio.sourceUrl).toBe("/original.m4a");
    expect(original.tracks[0].clips[0].trimEndMs).toBe(6000);
  });

  it("rejects a generated video shorter than the frozen range", () => {
    expect(() => enforceSingleShotReferenceTimeline(timeline(5000), 5057, "source-audio"))
      .toThrow("短于");
  });

  it("accepts 1ms metadata rounding without changing the frozen range or speed", () => {
    const result = enforceSingleShotReferenceTimeline(timeline(5056), 5057, "source-audio");
    for (const clip of result.tracks.flatMap(track => track.clips)) {
      expect(clip.trimEndMs).toBe(5057);
      expect(clip.speed).toBe(1);
    }
  });

  it("accepts at most one video frame only with explicit fps", () => {
    expect(() => enforceSingleShotReferenceTimeline(timeline(5041.667), 5057, "source-audio"))
      .toThrow("短于");
    expect(enforceSingleShotReferenceTimeline(timeline(5041.667), 5057, "source-audio", 24)
      .tracks[0].clips[0].trimEndMs).toBe(5057);
    expect(() => enforceSingleShotReferenceTimeline(timeline(5015), 5057, "source-audio", 24))
      .toThrow("短于");
  });

  it("does not apply video frame tolerance to original audio or unknown durations", () => {
    const state = timeline(5056);
    state.tracks[1].clips[0].durationMs = 5055;
    expect(() => enforceSingleShotReferenceTimeline(state, 5057, "source-audio", 24))
      .toThrow("短于");
    state.tracks[1].clips[0].durationMs = 5062;
    state.tracks[0].clips[0].durationMs = Number.NaN;
    expect(() => enforceSingleShotReferenceTimeline(state, 5057, "source-audio", 24))
      .toThrow("未知");
  });

  it("rejects missing original audio instead of silently replacing it", () => {
    expect(() => enforceSingleShotReferenceTimeline(timeline(), 5057, "missing"))
      .toThrow("原声音轨");
  });
});
