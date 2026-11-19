from __future__ import annotations

from typing import Any

PREPROCESSING_VERSION = "red-rgbd-v1"
MAX_POINTS = 128
_MIN_POINTS = 20
_DEPTH_DIMENSIONS = 2
_MIN_RED = 45
_MIN_DEPTH_M = 0.05
_MAX_DEPTH_M = 5.0


def red_point_cloud(
    rgb: Any, depth_m: Any, intrinsics: Any, camera_to_world: Any
) -> dict[str, Any]:
    """Segment the largest red RGB component, then unproject its aligned metric depth.

    Inputs use top-left image origin; camera axes x right, y down, z forward.
    No object positions, geometry IDs or simulator segmentation enter this function.
    """
    import cv2
    import numpy as np

    colors = np.asarray(rgb, dtype=float)
    depth = np.asarray(depth_m).squeeze()
    if colors.shape != (*depth.shape, 3) or depth.ndim != _DEPTH_DIMENSIONS:
        raise ValueError("RGB and depth must be aligned HxW images")
    mask = (
        (colors[..., 0] > _MIN_RED)
        & (colors[..., 0] > 1.5 * colors[..., 1])
        & (colors[..., 0] > 1.5 * colors[..., 2])
        & np.isfinite(depth)
        & (depth > _MIN_DEPTH_M)
        & (depth < _MAX_DEPTH_M)
    ).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return {
            "version": PREPROCESSING_VERSION,
            "ok": False,
            "count": 0,
            "points": [],
            "centroid": None,
        }
    label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    rows, cols = np.where(labels == label)
    pixels = np.stack([cols, rows, np.ones_like(rows)], axis=1)
    camera = (pixels @ np.linalg.inv(intrinsics).T) * depth[rows, cols, None]
    pose = np.asarray(camera_to_world)
    points = camera @ pose[:3, :3].T + pose[:3, 3]
    selected = np.linspace(0, len(points) - 1, min(MAX_POINTS, len(points)), dtype=int)
    points = points[selected]
    return {
        "version": PREPROCESSING_VERSION,
        "ok": len(points) >= _MIN_POINTS,
        "count": len(points),
        "pixel_count": len(rows),
        "points": points.round(6).tolist(),
        "centroid": points.mean(axis=0).round(6).tolist(),
        "frame": "world-z-up",
        "units": "m",
    }


def green_point_cloud(
    rgb: Any, depth_m: Any, intrinsics: Any, camera_to_world: Any
) -> dict[str, Any]:
    """Same camera-only pipeline and bounds, with green as the dominant channel."""
    import numpy as np

    result = red_point_cloud(
        np.asarray(rgb)[..., [1, 0, 2]], depth_m, intrinsics, camera_to_world
    )
    result["version"] = "green-rgbd-v1"
    return result


__all__ = [
    "PREPROCESSING_VERSION",
    "green_point_cloud",
    "metric_camera",
    "red_point_cloud",
]


def metric_camera(simulation: Any, camera: str, raw_depth: Any) -> tuple[Any, Any, Any]:
    """MuJoCo OpenGL depth -> optical Z meters and OpenCV camera-to-world pose.

    Uses camera calibration only. Equivalent to Robosuite camera_utils without
    importing its unrelated HDF5 and demonstration playback dependencies.
    """
    import numpy as np

    depth = np.asarray(raw_depth).squeeze()
    if not np.isfinite(depth).all() or np.any(depth < 0) or np.any(depth > 1):
        raise ValueError("MuJoCo depth must be finite and normalized")
    model = simulation.model
    index = model.camera_name2id(camera)
    near = model.vis.map.znear * model.stat.extent
    far = model.vis.map.zfar * model.stat.extent
    metric = near / (1 - depth * (1 - near / far))
    height, width = metric.shape
    focal = 0.5 * height / np.tan(model.cam_fovy[index] * np.pi / 360)
    intrinsic = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    pose = np.eye(4)
    pose[:3, :3] = simulation.data.cam_xmat[index].reshape(3, 3) @ np.diag([1, -1, -1])
    pose[:3, 3] = simulation.data.cam_xpos[index]
    return metric, intrinsic, pose
