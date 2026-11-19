# Capability notes

Mechanism -> check/repair -> when it applies

- Single-view depth sees only camera-facing faces, so a cluster's lowest
  seen point lies above its true base and its centroid is biased upward.
  Judging "resting on", "lifted" or "stacked" from seen bottom points gives
  wrong answers and aborts correct plans. Repair: derive the base from the
  top face minus a height taken as the larger of the seen vertical extent
  and the smaller horizontal span, and compare it with a measured support
  height. Applies to any depth-based verification.
- Centroid grasps dip too low or high. Repair: aim at the top-face median
  in x,y, a fraction of the height below the top; recompute after motions.
- Check ok/count first; few points mean occlusion.

Helper

- resting_geometry(points, support_z=None, known_height=None,
  grip_fraction=0.35, top_fraction=0.2, min_points=20) -> dict: ok, count,
  center, z_top, z_bottom_seen, height, base_z, clearance, elevated, grasp.
