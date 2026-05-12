"""
clustering.py - Automatic clustering and gate creation from clusters.

Supports KMeans and DBSCAN algorithms from scikit-learn.
Creates polygon gates from cluster convex hulls.
"""

from __future__ import annotations

import numpy as np


def cluster_dbscan(
    x: np.ndarray,
    y: np.ndarray,
    eps: float = 0.5,
    min_samples: int = 5,
) -> np.ndarray:
    """
    Cluster points using DBSCAN.

    Returns array of cluster labels (-1 for noise).

    Args:
        x: X coordinates
        y: Y coordinates
        eps: Maximum distance between samples in same neighborhood
        min_samples: Minimum samples in neighborhood to form core point
    """
    from sklearn.cluster import DBSCAN

    points = np.column_stack([x, y])
    dbscan = DBSCAN(eps=eps, min_samples=min_samples)
    labels = dbscan.fit_predict(points)
    return labels


def cluster_kmeans(
    x: np.ndarray,
    y: np.ndarray,
    n_clusters: int = 3,
) -> np.ndarray:
    """
    Cluster points using KMeans.

    Returns array of cluster labels.

    Args:
        x: X coordinates
        y: Y coordinates
        n_clusters: Number of clusters to find
    """
    from sklearn.cluster import KMeans

    points = np.column_stack([x, y])
    kmeans = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    labels = kmeans.fit_predict(points)
    return labels


def convex_hull_from_cluster(
    x: np.ndarray,
    y: np.ndarray,
    indices: np.ndarray,
) -> list[tuple[float, float]]:
    """
    Create convex hull vertices for a cluster.

    Uses scipy.spatial.ConvexHull.

    Args:
        x: All X coordinates
        y: All Y coordinates
        indices: Indices of points in this cluster

    Returns:
        List of (x, y) vertices forming convex hull
    """
    from scipy.spatial import ConvexHull

    if len(indices) < 3:
        return []

    points = np.column_stack([x[indices], y[indices]])
    try:
        hull = ConvexHull(points)
        verts = [(points[i, 0], points[i, 1]) for i in hull.vertices]
        return verts
    except Exception:
        # ConvexHull can fail for degenerate cases
        return []


def create_gate_polygons(
    x: np.ndarray,
    y: np.ndarray,
    labels: np.ndarray,
    x_channel: str,
    y_channel: str,
) -> list[dict]:
    """
    Create gate definitions from cluster labels.

    Returns list of gate dicts ready for GateManager.

    Args:
        x: X coordinates
        y: Y coordinates
        labels: Cluster labels for each point
        x_channel: Name of X channel
        y_channel: Name of Y channel

    Returns:
        List of dicts with keys: name, x_channel, y_channel, vertices
    """
    gates = []
    unique_labels = set(labels)

    for label in sorted(unique_labels):
        if label == -1:  # Skip noise points in DBSCAN
            continue

        mask = labels == label
        indices = np.where(mask)[0]

        verts = convex_hull_from_cluster(x, y, indices)

        if len(verts) >= 3:
            gates.append(
                {
                    "name": f"C{label}",
                    "x_channel": x_channel,
                    "y_channel": y_channel,
                    "vertices": verts,
                }
            )

    return gates
