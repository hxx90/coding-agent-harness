# Task-agnostic geometry helper for depth-based pick/place programs.
# Pure numpy; actors pass segmented points, a measured support height and tolerances.
import numpy as np


def resting_geometry(points, support_z=None, known_height=None, grip_fraction=0.35,
                     top_fraction=0.2, min_points=20):
    """Single-view-safe geometry of one segmented point cluster.

    points: (N,3) metric points of one item in the robot/world frame (z up).
    support_z: measured height of the surface the item may rest on (optional).
    known_height: item height if known; otherwise it is estimated.
    Returns dict: ok, count, center, z_top, z_bottom_seen, height, base_z,
    clearance, elevated, span_xy, grasp, top_center.

    A depth camera only sees the faces turned towards it, so the lowest seen
    point sits well above the real base and plain centroids are biased.  The
    estimated height therefore uses the horizontal span as a lower bound and
    the base is derived from the reliable top face, not from seen bottom
    points.  clearance/elevated compare that derived base with support_z.
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[0] == 0 or pts.shape[1] < 3:
        return {"ok": False, "count": 0, "center": None, "z_top": None,
                "z_bottom_seen": None, "height": None, "base_z": None,
                "clearance": None, "elevated": False, "span_xy": None,
                "grasp": None, "top_center": None}
    pts = pts[np.isfinite(pts).all(axis=1)]
    count = int(pts.shape[0])
    if count < max(1, int(min_points)):
        return {"ok": False, "count": count, "center": None, "z_top": None,
                "z_bottom_seen": None, "height": None, "base_z": None,
                "clearance": None, "elevated": False, "span_xy": None,
                "grasp": None, "top_center": None}
    z = pts[:, 2]
    z_top = float(np.percentile(z, 97.0))
    z_bottom_seen = float(np.percentile(z, 3.0))
    frac = float(min(max(top_fraction, 0.02), 0.9))
    thr = z_top - frac * max(z_top - z_bottom_seen, 1e-4)
    top = pts[z >= thr]
    if top.shape[0] < 3:
        top = pts[z >= z_top - 1e-3] if np.any(z >= z_top - 1e-3) else pts
    top_center = np.array([float(np.median(top[:, 0])), float(np.median(top[:, 1])), z_top])
    sx = float(np.percentile(pts[:, 0], 97.0) - np.percentile(pts[:, 0], 3.0))
    sy = float(np.percentile(pts[:, 1], 97.0) - np.percentile(pts[:, 1], 3.0))
    span_xy = (sx, sy)
    seen_h = max(z_top - z_bottom_seen, 0.0)
    if known_height is not None and float(known_height) > 0.0:
        height = float(known_height)
    else:
        height = float(max(seen_h, min(sx, sy)))
    if support_z is not None and height <= 0.0:
        height = float(max(z_top - float(support_z), 1e-3))
    base_z = z_top - height
    clearance = None if support_z is None else float(base_z - float(support_z))
    elevated = bool(clearance is not None and clearance > 0.4 * height)
    gf = float(min(max(grip_fraction, 0.0), 0.9))
    grasp = np.array([top_center[0], top_center[1], z_top - gf * height])
    center = np.array([top_center[0], top_center[1], z_top - 0.5 * height])
    return {"ok": True, "count": count, "center": center, "z_top": z_top,
            "z_bottom_seen": z_bottom_seen, "height": height, "base_z": float(base_z),
            "clearance": clearance, "elevated": elevated, "span_xy": span_xy,
            "grasp": grasp, "top_center": top_center}
