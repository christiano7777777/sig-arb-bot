"""Senate-control model for strategy D (stat arb): pure functions, no API.

Republicans control the Senate with >= R_NEEDED seats (50: the Vice President breaks a 50-50 tie).
31 Republican seats are not up in 2026, so they need >= 19 of the 35 seats up (verified 2026-10-04:
Wikipedia "2026 United States Senate elections": 35 up = 33 Class 2 (20 R, 13 D) + OH and FL specials;
the Senate has 53 R, 47 D incl. 2 independents -> holdovers 31 R, 34 D).

National-swing model (one-factor Gaussian copula): race i goes Republican if
    sqrt(rho) Z + sqrt(1 - rho) e_i < Phi^-1(p_i),   Z, e_i ~ N(0, 1) independent,
so each race keeps its own probability p_i (from Kalshi) and rho sets how much they move together.
P(control) = E_Z[ P(#R wins >= needed | Z) ], the inner term a Poisson-binomial tail (exact DP),
the outer expectation by Gauss-Hermite quadrature. rho is calibrated so the model reproduces Kalshi's
own control price. Delta of race i = dP(control)/dp_i (central difference) = how often race i is pivotal.
"""
import math

import numpy as np
from scipy.stats import norm

R_HOLDOVER = 31
R_CONTROL = 50
NODES, WEIGHTS = np.polynomial.hermite_e.hermegauss(64)      # for E over Z ~ N(0, 1)
WEIGHTS = WEIGHTS / WEIGHTS.sum()


def p_control(p, rho):
    """P(Republican control) given each race's P(R wins) and the swing correlation rho.
    Vectorised over the quadrature nodes: one Poisson-binomial DP per node, all nodes at once."""
    need = R_CONTROL - R_HOLDOVER
    thr = norm.ppf(np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6))
    s, c = math.sqrt(max(rho, 0.0)), math.sqrt(max(1.0 - rho, 1e-12))
    q = norm.cdf((thr[None, :] - s * NODES[:, None]) / c)   # P(R wins race i | Z = node), shape (nodes, races)
    dist = np.zeros((len(NODES), q.shape[1] + 1))
    dist[:, 0] = 1.0
    for i in range(q.shape[1]):                              # Poisson-binomial by DP, every node at once
        qi = q[:, i:i + 1]
        dist[:, 1:] = dist[:, 1:] * (1 - qi) + dist[:, :-1] * qi
        dist[:, 0] *= (1 - q[:, i])
    return float(WEIGHTS @ dist[:, need:].sum(axis=1))


def calibrate(p, target, lo=0.0, hi=0.95, tol=1e-5):
    """rho in [lo, hi] with p_control(p, rho) == target, or None if no rho reaches it."""
    f_lo, f_hi = p_control(p, lo) - target, p_control(p, hi) - target
    if f_lo * f_hi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = p_control(p, mid) - target
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def deltas(p, rho, h=0.01):
    """dP(control)/dp_i for every race (central difference, clipped to (0, 1))."""
    out = []
    for i in range(len(p)):
        up, dn = list(p), list(p)
        up[i], dn[i] = min(p[i] + h, 0.999), max(p[i] - h, 0.001)
        out.append((p_control(up, rho) - p_control(dn, rho)) / (up[i] - dn[i]))
    return out
