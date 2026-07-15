"""Delta Atlas + content-aware downscale — v1.1 tests (2026-07-15)."""
from PIL import Image, ImageDraw

from bundler import Bundler, _pack_atlas
from delta import DeltaEngine


def _tile(w, h, color, x=0, y=0, idx=0, name="t"):
    return {"frame_index": idx, "name": name, "x": x, "y": y, "w": w, "h": h,
            "img": Image.new("RGB", (w, h), color)}


def test_pack_geometry():
    tiles = [_tile(100, 80, "red", x=10, y=20), _tile(200, 60, "green", x=500, y=40),
             _tile(50, 120, "blue", x=900, y=600), _tile(70, 70, "white", x=0, y=680)]
    atlas, legend = _pack_atlas(tiles, gutter=8, max_tiles=12, max_width=400)
    assert len(legend["tiles"]) == 4
    assert legend["dropped_tiles"] == 0
    # No two tiles overlap, and every pair is separated by >= gutter on some axis
    rects = [(e["atlas"]["x"], e["atlas"]["y"], e["atlas"]["w"], e["atlas"]["h"])
             for e in legend["tiles"]]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax, ay, aw, ah = rects[i]
            bx, by, bw, bh = rects[j]
            h_sep = ax + aw + 8 <= bx or bx + bw + 8 <= ax
            v_sep = ay + ah + 8 <= by or by + bh + 8 <= ay
            assert h_sep or v_sep, f"tiles {i},{j} touch or overlap"
    # Legend src preserves original screen coordinates
    assert any(e["src"] == {"x": 500, "y": 40, "w": 200, "h": 60} for e in legend["tiles"])
    # Gutter pixel between content is background gray
    assert atlas.getpixel((2, 2)) == (96, 96, 96)
    print("PASS: atlas geometry — no touching tiles, src coords preserved, gutters gray")


def test_max_tiles_recency():
    tiles = [_tile(20, 20, "red", idx=i, name=f"t{i}") for i in range(15)]
    _, legend = _pack_atlas(tiles, gutter=4, max_tiles=12)
    assert legend["dropped_tiles"] == 3
    kept = {e["name"] for e in legend["tiles"]}
    assert "t14" in kept and "t0" not in kept, "recency cap must keep newest tiles"
    print("PASS: tile cap drops oldest, keeps newest")


def test_bundle_atlases_multiple_crops():
    got = []
    b = Bundler(on_bundle_ready=got.append, region={"x": 0, "y": 0, "width": 640, "height": 360})
    b._delta = DeltaEngine(screen_zones={"z": {"x": 0, "y": 0, "width": 640, "height": 360}},
                           frame_size=(640, 360), min_contour_area=200)
    b._baseline_sent = True
    black = Image.new("RGB", (640, 360), "black")
    two_boxes = black.copy()
    d = ImageDraw.Draw(two_boxes)
    d.rectangle([50, 50, 150, 150], fill="red")
    d.rectangle([400, 200, 550, 330], fill="cyan")
    result = b._build_bundle([black, two_boxes])
    # Two distinct changed regions -> ONE atlas image, legend present
    assert len(result["frames"]) == 1, f"expected 1 atlas image, got {len(result['frames'])}"
    assert result["atlas"] and len(result["atlas"]["tiles"]) == 2
    print("PASS: multiple crops ship as one atlas with a 2-tile legend")


def test_single_crop_skips_atlas():
    got = []
    b = Bundler(on_bundle_ready=got.append, region={"x": 0, "y": 0, "width": 640, "height": 360})
    b._delta = DeltaEngine(screen_zones={"z": {"x": 0, "y": 0, "width": 640, "height": 360}},
                           frame_size=(640, 360), min_contour_area=200)
    b._baseline_sent = True
    black = Image.new("RGB", (640, 360), "black")
    one_box = black.copy()
    ImageDraw.Draw(one_box).rectangle([50, 50, 200, 200], fill="white")
    result = b._build_bundle([black, one_box])
    assert len(result["frames"]) == 1 and result["atlas"] is None
    print("PASS: single crop ships plain — no atlas overhead")


def test_downscale_default_off_and_zone_exclusion():
    b = Bundler(on_bundle_ready=lambda x: None, region={"x": 0, "y": 0, "width": 1280, "height": 720})
    b._delta = DeltaEngine(screen_zones={"chat": {"x": 1000, "y": 0, "width": 280, "height": 720}},
                           frame_size=(1280, 720))
    big_cam = _tile(500, 500, "red", x=100, y=100)
    big_chat = _tile(500, 500, "green", x=1010, y=100)  # centered in chat zone
    # Default: OFF — nothing changes
    out = b._maybe_downscale([dict(big_cam), dict(big_chat)])
    assert out[0]["img"].size == (500, 500) and out[1]["img"].size == (500, 500)
    # Enabled: big non-chat tile shrinks; chat tile is protected
    b.downscale_cfg = {"enabled": True, "min_dimension": 400, "scale": 0.5,
                       "exclude_zones": ["chat"]}
    out = b._maybe_downscale([dict(big_cam), dict(big_chat)])
    assert out[0]["img"].size == (250, 250), f"cam tile not downscaled: {out[0]['img'].size}"
    assert out[1]["img"].size == (500, 500), "chat tile must never downscale"
    print("PASS: downscale is opt-in and never touches the chat zone")


if __name__ == "__main__":
    test_pack_geometry()
    test_max_tiles_recency()
    test_bundle_atlases_multiple_crops()
    test_single_crop_skips_atlas()
    test_downscale_default_off_and_zone_exclusion()
    print("\nAll atlas v4 tests passed")
