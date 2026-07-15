"""Efficiency fixes — 2026-07-15 live-field-test findings.

Three regressions pinned:
  1. Zone scaling: zones authored in a design space must scale to the real
     window size (a 1734x1392 window silently broke caching before).
  2. No zone eviction: an idle period must never permanently destroy the
     zone registry.
  3. Bundle dedup + thumbnail cap: near-identical images never ship twice
     in one bundle; the sub-threshold fallback thumbnail ships at most once.
"""
import time

from PIL import Image, ImageDraw

from delta import DeltaEngine
from bundler import Bundler


ZONES = {
    "gameplay": {"x": 0, "y": 0, "width": 1000, "height": 680},
    "chat": {"x": 1000, "y": 0, "width": 280, "height": 720},
    "hud": {"x": 0, "y": 680, "width": 1000, "height": 40},
}


def test_zone_scaling():
    engine = DeltaEngine(screen_zones=ZONES, frame_size=(1734, 1392))
    # Design space bounding box is 1280x720; zones must scale to 1734x1392.
    chat = engine.zones["chat"]
    assert chat.x == round(1000 * 1734 / 1280), f"chat.x={chat.x}"
    assert chat.width == round(280 * 1734 / 1280), f"chat.width={chat.width}"
    assert chat.height == round(720 * 1392 / 720), f"chat.height={chat.height}"
    # Zones collectively cover the full scaled frame width
    max_extent = max(z.x + z.width for z in engine.zones.values())
    assert abs(max_extent - 1734) <= 2, f"zones cover {max_extent}, frame is 1734"
    print("PASS: zones scale from design space to actual frame size")


def test_no_eviction_after_idle():
    engine = DeltaEngine(screen_zones=ZONES, frame_size=(1280, 720))
    a = Image.new("RGB", (1280, 720), "black")
    b = a.copy()
    engine.process(a, b)
    assert len(engine.zones) == 3
    # Simulate a long idle: backdate last_seen far beyond the old 30s window
    for z in engine.zones.values():
        z.last_seen = time.monotonic() - 3600
    engine.process(a, b)
    assert len(engine.zones) == 3, "zones must survive idle periods"
    print("PASS: zones are never evicted by idle")


def test_bundle_dedup_and_thumb_cap():
    bundles = []
    bundler = Bundler(on_bundle_ready=bundles.append, region={"x": 0, "y": 0, "width": 640, "height": 360})
    # Use an engine with NO zones -> full-frame diff mode
    bundler._delta = DeltaEngine(screen_zones={"z": {"x": 0, "y": 0, "width": 640, "height": 360}},
                                 frame_size=(640, 360), min_contour_area=200)

    base = Image.new("RGB", (640, 360), "black")
    frames = [base.copy()]
    # Frames with sub-threshold noise: single differing pixel triggers the
    # hash gate but produces no contour -> the uniform-thumb fallback path.
    for i in range(6):
        f = base.copy()
        f.putpixel((10 + i, 10), (30 + i, 0, 0))
        frames.append(f)
    result = bundler._build_bundle(frames)

    # baseline (1) + AT MOST one fallback thumbnail = <= 2 images total,
    # where the old code shipped baseline + one thumb per noisy frame.
    assert len(result["frames"]) <= 2, f"expected <=2 images, got {len(result['frames'])}"
    print(f"PASS: noisy bundle shipped {len(result['frames'])} images (baseline + <=1 thumb)")

    # Dedup: two LARGE identical changes in different frames -> one crop.
    bundler2 = Bundler(on_bundle_ready=bundles.append, region={"x": 0, "y": 0, "width": 640, "height": 360})
    bundler2._delta = DeltaEngine(screen_zones={"z": {"x": 0, "y": 0, "width": 640, "height": 360}},
                                  frame_size=(640, 360), min_contour_area=200)
    bundler2._baseline_sent = True  # skip baseline for this check
    box = Image.new("RGB", (640, 360), "black")
    d = ImageDraw.Draw(box)
    d.rectangle([100, 100, 300, 300], fill="white")
    seq = [Image.new("RGB", (640, 360), "black"), box.copy(),
           Image.new("RGB", (640, 360), "black"), box.copy()]
    result2 = bundler2._build_bundle(seq)
    # The white box appears twice (identical) -> its crop ships once; the
    # reversion to black also produces one crop. Old code shipped 4+.
    assert len(result2["frames"]) <= 2, f"expected <=2 deduped crops, got {len(result2['frames'])}"
    print(f"PASS: identical repeated change deduped ({len(result2['frames'])} images)")


if __name__ == "__main__":
    test_zone_scaling()
    test_no_eviction_after_idle()
    test_bundle_dedup_and_thumb_cap()
    print("\nAll efficiency v3 tests passed")
