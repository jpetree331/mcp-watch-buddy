"""Test for bundler.py — runs standalone from watch_buddy/ directory."""
import sys, time
sys.path.insert(0, ".")

from PIL import Image
from bundler import Bundler

bundles_received = []

def on_bundle(bundle):
    bundles_received.append(bundle)
    print(f"Bundle received: {len(bundle['frames'])} frames, "
          f"{len(bundle['metadata'])} metadata entries")

bundler = Bundler(on_bundle_ready=on_bundle)
bundler.start()

# Feed 25 synthetic frames over 2.5 seconds
for i in range(25):
    frame = Image.new("RGB", (1280, 720), color=(i * 10 % 255, 50, 50))
    bundler.feed(frame)
    time.sleep(0.1)

bundler.stop()
time.sleep(0.5)

assert len(bundles_received) >= 2, \
    f"FAIL: expected >= 2 bundles, got {len(bundles_received)}"
for bundle in bundles_received:
    assert "frames" in bundle, "FAIL: bundle missing frames key"
    assert "metadata" in bundle, "FAIL: bundle missing metadata key"
    assert "timestamp" in bundle, "FAIL: bundle missing timestamp key"
    assert len(bundle["frames"]) <= 10, \
        f"FAIL: bundle has > 10 frames: {len(bundle['frames'])}"

print(f"PASS: {len(bundles_received)} bundles produced correctly")
print(f"Average frames per bundle: {sum(len(b['frames']) for b in bundles_received) / len(bundles_received):.1f}")
print("\nAll bundler tests PASSED")
