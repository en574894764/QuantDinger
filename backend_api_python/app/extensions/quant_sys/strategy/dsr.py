"""DSR/FDR statistical analysis — Deflated Sharpe Ratio + False Discovery Rate.

Lopez de Prado (2018) methodology for multiple testing correction in
quantitative strategy research.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


# ── Benjamini-Hochberg FDR ─────────────────────────────────────────────

def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg FDR correction.

    Returns True for factors that pass FDR at significance alpha.
    """
    n = len(p_values)
    if n == 0:
        return []

    sorted_idx = np.argsort(p_values)
    sorted_p = np.array(p_values)[sorted_idx]
    rejected = np.zeros(n, dtype=bool)

    for i, p in enumerate(sorted_p):
        threshold = (i + 1) / n * alpha
        if p <= threshold:
            rejected[sorted_idx[i]] = True
        else:
            break

    return rejected.tolist()


# ── FDR Factor Filter ──────────────────────────────────────────────────

def fdr_filter_factors(ic_results: list[dict], alpha: float = 0.05) -> list[dict]:
    """Filter factors using FDR correction on IC t-test p-values.

    Args:
        ic_results: list of dicts with {factor_name, ic_mean, ir, p_value}
        alpha: FDR significance level

    Returns:
        list of dicts sorted by |IR| descending, with fdr_significant flag
    """
    if not ic_results:
        return []

    p_values = [r.get("p_value", 1.0) for r in ic_results]
    rejected = benjamini_hochberg(p_values, alpha)

    result = []
    for r, rej in zip(ic_results, rejected):
        result.append({
            "factor": r.get("factor_name", r.get("factor", "")),
            "ic_mean": r.get("ic_mean", 0),
            "ir": r.get("ir", 0),
            "p_value": r.get("p_value", 1.0),
            "fdr_significant": bool(rej),
        })

    result.sort(key=lambda x: abs(x["ir"]), reverse=True)
    return result


# ── Deflated Sharpe Ratio ───────────────────────────────────────────────

def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    n_observations: int,
    skewness: float = 0,
    kurtosis: float = 3,
    sharpe_variance: float | None = None,
) -> float:
    """Compute Deflated Sharpe Ratio (Lopez de Prado).

    DSR accounts for the fact that the maximum Sharpe across N trials
    is higher than any single trial's Sharpe.

    Args:
        observed_sharpe: Sharpe ratio of the selected strategy
        n_trials: total number of strategy variants tested
        n_observations: number of returns used to compute the Sharpe
        skewness: skewness of returns
        kurtosis: kurtosis of returns
        sharpe_variance: variance of the Sharpe ratio estimate,
            or None to estimate from n_observations, skewness, kurtosis

    Returns:
        DSR p-value: probability that the observed Sharpe is
        statistically significant after accounting for multiple testing.
        p < 0.05 suggests the strategy has genuine predictive power.
    """
    if sharpe_variance is None:
        # Asymptotic variance of Sharpe ratio (Lo, 2002)
        sharpe_variance = (
            1 + observed_sharpe ** 2 * (kurtosis - 1) / 4
            - observed_sharpe * skewness
        ) / (n_observations - 1)

    from scipy.stats import norm

    euler = 0.5772156649

    # E[max(Z)] for N i.i.d. standard normals
    if n_trials > 1:
        expected_max_z = (
            (1 - euler) * norm.ppf(1 - 1 / n_trials)
            + euler * norm.ppf(1 - 1 / (n_trials * np.e))
        )
    else:
        expected_max_z = 0

    expected_max_sharpe = np.sqrt(sharpe_variance) * expected_max_z
    test_stat = (observed_sharpe - expected_max_sharpe) / np.sqrt(sharpe_variance)
    p_value = 1 - norm.cdf(test_stat)

    return float(np.clip(p_value, 0, 1))


# ── Minimum Backtest Length ────────────────────────────────────────────

def minimum_backtest_length(
    target_sharpe: float,
    n_trials: int,
    skewness: float = 0,
    kurtosis: float = 3,
    significance: float = 0.05,
) -> int:
    """Compute minimum backtest length required (Bailey & Lopez de Prado).

    Given a target Sharpe and number of trials attempted, returns the
    minimum number of observations needed to achieve statistical significance.

    Args:
        target_sharpe: desired Sharpe ratio
        n_trials: number of variants tested
        skewness: return skewness
        kurtosis: return kurtosis
        significance: significance level (default 0.05)

    Returns:
        Minimum number of observations required
    """
    from scipy.stats import norm

    euler = 0.5772156649
    z_alpha = norm.ppf(1 - significance)

    if n_trials > 1:
        expected_max_z = (
            (1 - euler) * norm.ppf(1 - 1 / n_trials)
            + euler * norm.ppf(1 - 1 / (n_trials * np.e))
        )
    else:
        expected_max_z = 0

    numerator = (
        z_alpha * np.sqrt(
            1 + target_sharpe ** 2 * (kurtosis - 1) / 4
            - target_sharpe * skewness
        )
        + expected_max_z
    ) ** 2

    n = numerator / target_sharpe ** 2 + 1
    return int(np.ceil(n))


# ── DSR Report (convenience) ───────────────────────────────────────────

def compute_dsr_report(
    observed_sharpe: float,
    n_trials: int,
    n_observations: int,
    returns: list[float] | None = None,
    skewness: float = 0,
    kurtosis: float = 3,
) -> dict:
    """Compute a full DSR report for a given strategy.

    Args:
        observed_sharpe: the Sharpe ratio of the strategy
        n_trials: total experiments attempted (for multiple testing correction)
        n_observations: number of return observations
        returns: optional list of period returns for skewness/kurtosis estimation
        skewness: override skewness
        kurtosis: override kurtosis

    Returns:
        dict with dsr_pvalue, min_obs, interpreted_result
    """
    if returns and len(returns) > 3:
        from scipy.stats import skew, kurtosis as kurt
        skewness = float(skew(returns))
        kurtosis = float(kurt(returns, fisher=False))  # Pearson kurtosis

    dsr_p = deflated_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        n_trials=n_trials,
        n_observations=n_observations,
        skewness=skewness,
        kurtosis=kurtosis,
    )

    min_obs = minimum_backtest_length(
        target_sharpe=observed_sharpe,
        n_trials=n_trials,
        skewness=skewness,
        kurtosis=kurtosis,
    )

    # Interpretation
    if dsr_p < 0.01:
        interpretation = "strongly_significant"
        desc = "Strong evidence of genuine predictive power (DSR p < 0.01)"
    elif dsr_p < 0.05:
        interpretation = "significant"
        desc = "Likely genuine predictive power (DSR p < 0.05)"
    elif dsr_p < 0.10:
        interpretation = "borderline"
        desc = "Borderline significance — needs more testing (DSR p < 0.10)"
    else:
        interpretation = "not_significant"
        desc = "Not statistically significant — likely overfit (DSR p ≥ 0.10)"

    return {
        "observed_sharpe": observed_sharpe,
        "n_trials": n_trials,
        "n_observations": n_observations,
        "skewness": round(skewness, 4),
        "kurtosis": round(kurtosis, 4),
        "dsr_pvalue": round(dsr_p, 6),
        "min_backtest_observations": min_obs,
        "min_backtest_days": min_obs,  # assuming daily returns
        "interpretation": interpretation,
        "description": desc,
        "is_significant": dsr_p < 0.05,
        "has_sufficient_data": n_observations >= min_obs,
    }
