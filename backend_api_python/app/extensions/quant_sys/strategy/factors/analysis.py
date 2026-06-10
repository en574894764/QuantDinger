"""
Factor analysis — IC (Information Coefficient), IR (Information Ratio),
and factor return analysis.

IC = rank correlation between factor values at time T and forward returns.
IR = mean(IC) / std(IC).  Measures factor stability.
"""

import logging

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


def forward_returns(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """
    Compute forward returns: close[+period] / close - 1.
    Aligned so forward_returns[t] = return from t to t+period.
    """
    return df["close"].shift(-period) / df["close"] - 1.0


def compute_ic(
    factor_df: pd.DataFrame,
    forward_ret: pd.Series,
    method: str = "spearman",
) -> pd.Series:
    """
    Compute cross-sectional IC per date.

    Parameters
    ----------
    factor_df : pd.DataFrame
        Shape (T x N) — rows=dates, columns=symbols. Each cell is a factor value.
    forward_ret : pd.Series
        Forward returns indexed by date, aligned to symbols (column-wise broadcast).
    method : str
        'spearman' or 'pearson'.

    Returns
    -------
    pd.Series of IC values per date.
    """
    ic_series = pd.Series(np.nan, index=factor_df.index)
    for date in factor_df.index:
        fv = factor_df.loc[date]
        if date not in forward_ret.index:
            continue
        fwd = forward_ret.loc[date]
        # Align on symbols
        common = fv.dropna().index.intersection(fwd.dropna().index)
        if len(common) < 5:
            continue
        fv_aligned = fv.loc[common].values.astype(float)
        fwd_aligned = fwd.loc[common].values.astype(float)
        try:
            if method == "spearman":
                ic, _ = stats.spearmanr(fv_aligned, fwd_aligned)
            else:
                ic, _ = stats.pearsonr(fv_aligned, fwd_aligned)
            ic_series.loc[date] = ic
        except Exception:
            logger.debug("IC computation failed for date %s", date)
    return ic_series


def compute_ic_for_factors(
    factors: dict[str, pd.DataFrame],
    forward_ret: pd.Series,
    method: str = "spearman",
) -> dict[str, dict]:
    """
    Compute IC & IR for a dict of factor DataFrames.

    Parameters
    ----------
    factors : dict[str, pd.DataFrame]
        key=factor_name, value=DataFrame(T x N).
    forward_ret : pd.Series
        Forward returns per date.
    method : str
        'spearman' or 'pearson'.

    Returns
    -------
    dict: factor_name -> {ic_mean, ic_std, ir, ic_series, rank_ic_mean, ...}
    """
    results = {}
    for name, fdf in factors.items():
        ic = compute_ic(fdf, forward_ret, method=method)
        ic_clean = ic.dropna()
        if len(ic_clean) == 0:
            results[name] = {
                "ic_mean": None,
                "ic_std": None,
                "ir": None,
                "ic_series": [],
                "rank_ic_mean": None,
                "n_observations": 0,
            }
            continue
        ic_mean = float(ic_clean.mean())
        ic_std = float(ic_clean.std())
        ir = ic_mean / ic_std if ic_std > 0 else 0.0
        # Rank IC = Spearman correlation between factor and ranked returns
        rank_ic_val = ic_clean.corr(ic_clean.rank())
        results[name] = {
            "ic_mean": round(ic_mean, 6),
            "ic_std": round(ic_std, 6),
            "ir": round(float(ir), 6),
            "n_observations": len(ic_clean),
            "rank_ic_mean": round(float(rank_ic_val), 6),
            "ic_series": [
                {"date": str(d), "ic": round(float(v), 6) if not np.isnan(v) else None}
                for d, v in ic.items()
            ],
        }
    return results


def factor_returns(
    factor_df: pd.DataFrame,
    forward_ret: pd.Series,
    n_bins: int = 5,
) -> dict:
    """
    Compute factor returns by binning stocks into quintiles at each date,
    then tracking the long-short portfolio (top quintile long, bottom short).

    Returns
    -------
    dict with keys: long_ret, short_ret, ls_ret (each a list of {date, value})
    """
    long_rets = []
    short_rets = []
    ls_rets = []

    for date in factor_df.index:
        fv = factor_df.loc[date].dropna()
        if date not in forward_ret.index or len(fv) < n_bins * 2:
            continue
        fwd = forward_ret.loc[date].dropna()
        common = fv.index.intersection(fwd.index)
        if len(common) < n_bins * 2:
            continue

        fv = fv.loc[common]
        fwd = fwd.loc[common]

        try:
            bins = pd.qcut(fv, n_bins, labels=False, duplicates="drop")
        except ValueError:
            continue

        top_mask = bins == (bins.max())
        bottom_mask = bins == (bins.min())

        long_ret = fwd[top_mask].mean()
        short_ret = fwd[bottom_mask].mean()

        long_rets.append({"date": str(date), "value": round(float(long_ret), 6)})
        short_rets.append({"date": str(date), "value": round(float(short_ret), 6)})
        ls_rets.append(
            {"date": str(date), "value": round(float(long_ret - short_ret), 6)}
        )

    return {
        "long_ret": long_rets,
        "short_ret": short_rets,
        "ls_ret": ls_rets,
    }


def factor_correlation(factors: dict[str, pd.DataFrame]) -> dict:
    """
    Compute pairwise correlation matrix of factor values (cross-sectionally averaged).

    Returns {factor_names: [...], correlation_matrix: [[...], ...]}
    """
    names = list(factors.keys())
    if len(names) < 2:
        return {"factor_names": names, "correlation_matrix": []}

    # Stack cross-section averages per date
    avg_series = {}
    for name, fdf in factors.items():
        avg_series[name] = fdf.mean(axis=1)  # mean across symbols per date

    corr_df = pd.DataFrame(avg_series).corr()
    matrix = []
    for row_name in names:
        row = [round(float(corr_df.loc[row_name, col]), 4) for col in names]
        matrix.append(row)

    return {"factor_names": names, "correlation_matrix": matrix}
