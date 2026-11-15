import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync("src/features/freezone/FreezoneShell.tsx", "utf8");

describe("freezone assistant launcher contract", () => {
  it("shows the Q assistant wordmark without the character animation", () => {
    expect(source).toContain("<QAssistantWordmark");
    expect(source).not.toContain('src="/images/jin-dao-launcher.mp4"');
    expect(source).not.toContain('src="/images/xia-dao-launcher.mp4"');
    expect(source).not.toContain('src="/images/avatar-claw.png"');
    expect(source).not.toContain('src="/images/avatar-motion.mp4"');
    expect(source).toContain("const CHAT_LAUNCHER_VISUAL_WIDTH = 96");
    expect(source).toContain("const CHAT_LAUNCHER_VISUAL_HEIGHT = 35");
    expect(source).toContain("const CHAT_LAUNCHER_HIT_HEIGHT = 44");
    expect(source).toContain("brightness-[1.08]");
    expect(source).toContain("group-hover/xia-dao:brightness-[1.16]");
  });

  it("does not load or play the removed icon video", () => {
    expect(source).not.toContain("const videoRef =");
    expect(source).not.toContain("onMouseEnter={playMotion}");
    expect(source).not.toContain("onMouseLeave={stopMotion}");
  });

  it("keeps the existing click and drag interactions", () => {
    expect(source).toContain("onPointerDown={handlePointerDown}");
    expect(source).toContain("onClick={handleClick}");
    expect(source).toContain("onClick();");
  });
});
