"""oc-alive - keep an OCI Always Free instance out of the "idle" reclamation list.

Oracle reclaims Always Free compute instances when, over a 7 day window, *all*
of the following are true:

  * CPU utilisation 95th percentile < 20%
  * Network utilisation           < 20%
  * Memory utilisation            < 20%   (A1 / Arm shapes)

This package keeps CPU (and, by default, memory and network) comfortably above
2x those thresholds, using *idle-class, low priority* work so that real
workloads on the host are never disturbed.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
