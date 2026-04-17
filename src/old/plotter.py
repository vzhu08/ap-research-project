# src/plotter.py

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import linregress

def plot_all_model_fits(
    compiled_file: str = "compiled_data.csv",
    x_col: str = "Total Rounds (HS only)",
    y_col: str = "Sit Rate (%)",
    min_rounds: int = 25,
    min_elims: int = 5,
    bin_count: int = 10
):
    """
    Plot data with exponential & power-law fits plus binned median trend.

    Parameters:
    - compiled_file: path to CSV file
    - x_col: column name for independent variable
    - y_col: column name for dependent variable
    - min_rounds: filter threshold for Total Rounds (HS only)
    - min_elims: filter threshold for Total Elim Rounds
    - bin_count: number of bins for median trend
    """
    # Load data
    df = pd.read_csv(compiled_file)

    # Filter by minimum rounds and eliminations
    df['TotalRounds'] = pd.to_numeric(df['Total Rounds (HS only)'], errors='coerce').astype(int)
    df['TotalElims']  = pd.to_numeric(df['Total Elim Rounds'], errors='coerce').astype(int)
    df = df[df['TotalRounds'] > min_rounds]
    df = df[df['TotalElims']  > min_elims]

    # Helper to convert columns:
    def convert_series_to_float(s: pd.Series) -> pd.Series:
        if s.dtype == object:
            if s.str.contains("/").any():
                # Aff/Neg Split (%) like '39.1/60.9' -> take first part
                first = s.str.split('/', expand=True)[0]
                return pd.to_numeric(first.str.rstrip('%'), errors='coerce')
            if s.str.endswith('%').all():
                # Percentage series like '30.0%' -> 30.0
                return pd.to_numeric(s.str.rstrip('%'), errors='coerce')
        return pd.to_numeric(s, errors='coerce')

    # Prepare x and y
    x = convert_series_to_float(df[x_col]).astype(float)
    y = convert_series_to_float(df[y_col]).astype(float)

    # Drop NaNs
    mask = (~np.isnan(x)) & (~np.isnan(y))
    x = x[mask]
    y = y[mask]

    # Prepare line for plotting fits
    x_line = np.linspace(x.min(), x.max(), 200)

    plt.figure()
    plt.scatter(x, y, label='Data', alpha=0.7)

    # Exponential fit (y>0)
    mask_exp = y > 0
    if mask_exp.sum() > 1:
        exp = linregress(x[mask_exp], np.log(y[mask_exp]))
        a = np.exp(exp.intercept)
        y_exp = a * np.exp(exp.slope * x_line)
        plt.plot(x_line, y_exp, label=f"Exp (p={exp.pvalue:.2e})")

    # Power-law fit (x>0, y>0)
    mask_pow = (x > 0) & (y > 0)
    if mask_pow.sum() > 1:
        pow_fit = linregress(np.log(x[mask_pow]), np.log(y[mask_pow]))
        c = np.exp(pow_fit.intercept)
        y_pow = c * x_line**pow_fit.slope
        plt.plot(x_line, y_pow, label=f"Power (p={pow_fit.pvalue:.2e})")

    # Binned median trend
    bins = np.linspace(x.min(), x.max(), bin_count + 1)
    df_bins = pd.DataFrame({'x': x, 'y': y})
    df_bins['bin'] = pd.cut(df_bins['x'], bins, include_lowest=True)
    med = df_bins.groupby('bin').agg({'x': 'mean', 'y': 'median'}).dropna()
    plt.plot(med['x'], med['y'], marker='o', linestyle='-', label='Binned median')

    # Finalize plot
    plt.xlabel(x_col)
    plt.ylabel(y_col)
    plt.title(f'Model Fits & Binned Median Trend: {y_col} vs {x_col}')
    plt.legend()
    plt.tight_layout()
    plt.show()
