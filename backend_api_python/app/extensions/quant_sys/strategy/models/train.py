"""Walk-Forward Analysis model training with Gradient Boosting.

Provides WFATrainer for training GradientBoostingClassifier on financial data
using walk-forward cross-validation. Collects out-of-sample predictions,
feature importance, and per-window performance metrics.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WFAWindowResult:
    """Result from a single walk-forward window."""

    window_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    auc: float = 0.0
    feature_importance: dict[str, float] = field(default_factory=dict)


@dataclass
class WFAResult:
    """Aggregated walk-forward analysis result."""

    model_id: str
    created_at: str
    n_windows: int
    n_features: int
    total_train_samples: int
    total_oos_samples: int
    windows: list[WFAWindowResult] = field(default_factory=list)
    aggregate_metrics: dict[str, float] = field(default_factory=dict)
    aggregate_importance: dict[str, float] = field(default_factory=dict)
    oos_predictions: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------


class WFATrainer:
    """Walk-Forward Analysis trainer using GradientBoostingClassifier.

    Trains on rolling windows, predicts out-of-sample, and collects
    performance metrics plus feature importance per window.

    Parameters
    ----------
    n_estimators : int
        Number of boosting stages (default 100).
    max_depth : int
        Maximum depth of individual estimators (default 3).
    learning_rate : float
        Learning rate shrinks contribution of each tree (default 0.05).
    subsample : float
        Fraction of samples used for fitting each tree (default 0.8).
    random_state : int
        Random seed for reproducibility (default 42).
    """

    def __init__(
        self,
        n_estimators: int = 100,
        max_depth: int = 3,
        learning_rate: float = 0.05,
        subsample: float = 0.8,
        random_state: int = 42,
    ):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.random_state = random_state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def train_wfa(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        target_col: str,
        train_windows: int = 5,
        test_months: int = 1,
        date_col: str = "trade_date",
        min_train_samples: int = 252,
    ) -> WFAResult:
        """Run walk-forward analysis over a time-series DataFrame.

        The data is split into sequential windows. For each window, the
        model is trained on ``train_windows`` worth of past data and tested
        on the next ``test_months`` of data. The window then advances by
        one test period and the process repeats.

        Parameters
        ----------
        df : pd.DataFrame
            Input data sorted by date, containing features + target.
        feature_cols : list[str]
            Column names to use as predictors.
        target_col : str
            Column name of the binary target (0/1).
        train_windows : int
            Number of consecutive test periods used for training.
        test_months : int
            Number of months in each test (OOS) window.
        date_col : str
            Name of the date column for window splitting.
        min_train_samples : int
            Minimum number of training samples required per window.

        Returns
        -------
        WFAResult
            Aggregated results with per-window metrics, feature importance,
            and OOS predictions.
        """
        model_id = str(uuid.uuid4())[:8]

        # Ensure sorted by date
        if date_col in df.columns:
            df = df.sort_values(date_col).reset_index(drop=True)

        if date_col in df.columns:
            dates = pd.to_datetime(df[date_col])
        else:
            dates = pd.date_range("2000-01-01", periods=len(df), freq="B")
            df[date_col] = dates

        # Validate feature columns
        missing = [c for c in feature_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Feature columns not in DataFrame: {missing}")
        if target_col not in df.columns:
            raise ValueError(f"Target column '{target_col}' not in DataFrame")

        # Build rolling windows
        windows = self._build_windows(dates, train_windows, test_months)

        if not windows:
            raise ValueError(
                f"Not enough data for WFA with train_windows={train_windows}, "
                f"test_months={test_months}. Got {len(dates)} dates."
            )

        all_window_results: list[WFAWindowResult] = []
        all_importance: dict[str, list[float]] = {f: [] for f in feature_cols}
        all_oos: list[dict] = []

        for idx, (train_start, train_end, test_start, test_end) in enumerate(windows):
            # Build train / test masks
            train_mask = (dates >= train_start) & (dates <= train_end)
            test_mask = (dates >= test_start) & (dates <= test_end)

            X_train = df.loc[train_mask, feature_cols].copy()
            y_train = df.loc[train_mask, target_col].copy()
            X_test = df.loc[test_mask, feature_cols].copy()
            y_test = df.loc[test_mask, target_col].copy()

            # Drop rows with NaN in features or target
            X_train = X_train.dropna()
            y_train = y_train.loc[X_train.index]
            X_test = X_test.dropna()
            y_test = y_test.loc[X_test.index]

            if len(X_train) < min_train_samples or len(X_test) < 5:
                logger.warning(
                    "Window %d: insufficient samples (train=%d, test=%d), skipping",
                    idx, len(X_train), len(X_test),
                )
                continue

            # Train and evaluate
            try:
                window_result = self._train_and_evaluate(
                    X_train, y_train, X_test, y_test,
                    window_idx=idx,
                    train_start=str(train_start.date()),
                    train_end=str(train_end.date()),
                    test_start=str(test_start.date()),
                    test_end=str(test_end.date()),
                )
                all_window_results.append(window_result)

                # Collect feature importance
                for feat, imp in window_result.feature_importance.items():
                    all_importance[feat].append(imp)

                # Collect OOS predictions
                pred_probs = getattr(window_result, "pred_probs", [0.0] * len(X_test))
                for i, (_, row) in enumerate(X_test.iterrows()):
                    all_oos.append({
                        "window": idx,
                        "date": str(dates.iloc[X_test.index.get_loc(i)].date())
                        if date_col in df.columns
                        else str(dates.iloc[test_mask.values].iloc[i].date()),
                        "predicted": float(pred_probs[i]) if i < len(pred_probs) else 0.0,
                        "actual": int(y_test.iloc[i]),
                    })
            except Exception:
                logger.exception("Window %d training failed", idx)
                continue

        if not all_window_results:
            raise RuntimeError("No WFA windows produced valid results")

        # Aggregate metrics
        agg_metrics = self._aggregate_metrics(all_window_results)

        # Aggregate feature importance (mean across windows)
        agg_importance = {
            f: float(np.mean(vals)) if vals else 0.0
            for f, vals in all_importance.items()
        }
        # Sort by importance descending
        agg_importance = dict(
            sorted(agg_importance.items(), key=lambda x: x[1], reverse=True)
        )

        # Total samples
        total_train = sum(w.n_train for w in all_window_results)
        total_oos = sum(w.n_test for w in all_window_results)

        return WFAResult(
            model_id=model_id,
            created_at=datetime.utcnow().isoformat(),
            n_windows=len(all_window_results),
            n_features=len(feature_cols),
            total_train_samples=total_train,
            total_oos_samples=total_oos,
            windows=all_window_results,
            aggregate_metrics=agg_metrics,
            aggregate_importance=agg_importance,
            oos_predictions=all_oos,
        )

    def train_single(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_test: pd.DataFrame,
        y_test: pd.Series,
    ) -> dict:
        """Single train/test split. Returns predictions + metrics.

        Parameters
        ----------
        X_train, y_train : Training data.
        X_test, y_test : Test data.

        Returns
        -------
        dict with keys: accuracy, precision, recall, f1, auc, feature_importance,
        predictions (list of predicted probabilities).
        """
        model = self._build_model()
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)[:, 1]

        importance = dict(
            zip(X_train.columns, model.feature_importances_.tolist())
        )

        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "auc": float(roc_auc_score(y_test, y_proba)),
            "feature_importance": dict(
                sorted(importance.items(), key=lambda x: x[1], reverse=True)
            ),
            "predictions": [
                {"index": int(idx), "predicted": float(p), "actual": int(y)}
                for idx, p, y in zip(X_test.index, y_proba, y_test)
            ],
        }
        return metrics

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_model(self) -> GradientBoostingClassifier:
        """Create a new GradientBoostingClassifier instance."""
        return GradientBoostingClassifier(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            subsample=self.subsample,
            random_state=self.random_state,
        )

    def _build_windows(
        self,
        dates: pd.DatetimeIndex,
        train_months: int,
        test_months: int,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
        """Build rolling (train_start, train_end, test_start, test_end) windows."""
        unique_dates = sorted(dates.unique())
        if len(unique_dates) < 10:
            return []

        start_date = unique_dates[0]
        end_date = unique_dates[-1]

        # Generate month boundaries
        month_starts = pd.date_range(start_date, end_date, freq="MS")
        if len(month_starts) < 2:
            return []

        windows = []
        # Starting from the first train window
        for i in range(len(month_starts) - test_months):
            test_start = month_starts[i + train_months]
            test_end = test_start + pd.DateOffset(months=test_months) - pd.DateOffset(days=1)

            if test_end > end_date:
                break

            train_start = month_starts[i]
            train_end = test_start - pd.DateOffset(days=1)

            # Ensure train range has at least some data
            train_dates = [d for d in unique_dates if train_start <= d <= train_end]
            test_dates = [d for d in unique_dates if test_start <= d <= test_end]

            if len(train_dates) >= 5 and len(test_dates) >= 5:
                windows.append((
                    min(train_dates), max(train_dates),
                    min(test_dates), max(test_dates),
                ))

            # Advance by test_months
            # (the next iteration will pick up the next train window)

        return windows

    def _train_and_evaluate(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_test: pd.DataFrame,
        y_test: pd.Series,
        window_idx: int,
        train_start: str,
        train_end: str,
        test_start: str,
        test_end: str,
    ) -> WFAWindowResult:
        """Train a single window and return its result."""
        model = self._build_model()
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)[:, 1]

        importance = dict(
            zip(X_train.columns, model.feature_importances_.tolist())
        )
        importance = dict(
            sorted(importance.items(), key=lambda x: x[1], reverse=True)
        )

        result = WFAWindowResult(
            window_idx=window_idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            n_train=len(X_train),
            n_test=len(X_test),
            accuracy=float(accuracy_score(y_test, y_pred)),
            precision=float(precision_score(y_test, y_pred, zero_division=0)),
            recall=float(recall_score(y_test, y_pred, zero_division=0)),
            f1=float(f1_score(y_test, y_pred, zero_division=0)),
            auc=float(roc_auc_score(y_test, y_proba)),
            feature_importance=importance,
        )
        # Store pred_probs on the dataclass dynamically for OOS collection
        result.pred_probs = y_proba.tolist()  # type: ignore[attr-defined]
        return result

    @staticmethod
    def _aggregate_metrics(windows: list[WFAWindowResult]) -> dict[str, float]:
        """Compute mean and std of metrics across all windows."""
        if not windows:
            return {}

        keys = ["accuracy", "precision", "recall", "f1", "auc"]
        agg: dict[str, float] = {}
        for k in keys:
            vals = [getattr(w, k) for w in windows]
            agg[f"{k}_mean"] = float(np.mean(vals))
            agg[f"{k}_std"] = float(np.std(vals))
            agg[f"{k}_min"] = float(np.min(vals))
            agg[f"{k}_max"] = float(np.max(vals))
        agg["window_count"] = len(windows)
        return agg
