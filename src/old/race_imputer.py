"""
pyethnicity_test.py

Usage:
  python pyethnicity_test.py
  python pyethnicity_test.py --model fl
  python pyethnicity_test.py --model flg --geo-type zcta --geography 27106
  python pyethnicity_test.py --names "Cangyuan Li" "Mark Luo"

Notes (from docs):
- predict_race_fl(first_name, last_name) predicts race from first+last name only.
- predict_race_flg(first_name, last_name, geography, geo_type) adds geography (zcta or tract).
- predict_race(first_name, last_name, geography, geo_type) ensembles ML + BISG + BIFSG.
Returns a pandas DataFrame with probability columns for Asian, Black, Hispanic, White. :contentReference[oaicite:0]{index=0}
"""

from __future__ import annotations

import argparse
from typing import Iterable, List, Tuple, Optional, Any, Dict

import pyethnicity  # pip install pyethnicity :contentReference[oaicite:1]{index=1}


def split_full_name(full_name: str) -> Tuple[str, str]:
    """
    Split a full name into (first_name, last_name).
    - Uses the first token as first name
    - Uses the last token as last name
    This is a simple heuristic; adjust if you have suffixes/middle names.
    """
    parts = [p for p in full_name.strip().split() if p]
    if len(parts) < 2:
        raise ValueError(f"Name must include at least 2 tokens (first last). Got: {full_name!r}")
    return parts[0], parts[-1]


def predict_for_names(
    full_names: Iterable[str],
    model: str = "fl",
    geography: Optional[int] = None,
    geo_type: Optional[str] = None,
    chunksize: int = 1028,
) -> List[Dict[str, Any]]:
    """
    full_names: iterable of strings like "First Last"
    model: one of {"fl", "flg", "race"}:
      - "fl"  -> pyethnicity.predict_race_fl(first, last)
      - "flg" -> pyethnicity.predict_race_flg(first, last, geography, geo_type)
      - "race"-> pyethnicity.predict_race(first, last, geography, geo_type)
    geography / geo_type required for flg and race.
    Returns: list of dict rows (DataFrame records).
    """
    first_names: List[str] = []
    last_names: List[str] = []

    for n in full_names:
        f, l = split_full_name(n)
        first_names.append(f)
        last_names.append(l)

    model = model.lower().strip()
    if model == "fl":
        df = pyethnicity.predict_race_fl(first_name=first_names, last_name=last_names, chunksize=chunksize)  # :contentReference[oaicite:2]{index=2}
    elif model in {"flg", "race"}:
        if geography is None or geo_type is None:
            raise ValueError("geography and geo_type are required for model='flg' or model='race'")
        geo_type = geo_type.lower().strip()
        if geo_type not in {"zcta", "tract"}:
            raise ValueError("geo_type must be 'zcta' or 'tract'")

        if model == "flg":
            df = pyethnicity.predict_race_flg(  # :contentReference[oaicite:3]{index=3}
                first_name=first_names,
                last_name=last_names,
                geography=[geography] * len(first_names),
                geo_type=geo_type,
                chunksize=chunksize,
            )
        else:
            df = pyethnicity.predict_race(  # :contentReference[oaicite:4]{index=4}
                first_name=first_names,
                last_name=last_names,
                geography=[geography] * len(first_names),
                geo_type=geo_type,
                chunksize=chunksize,
            )
    else:
        raise ValueError("model must be one of: fl, flg, race")

    def df_records(df):
        # polars DataFrame
        if hasattr(df, "to_dicts"):
            return df.to_dicts()
        # pandas DataFrame
        if hasattr(df, "to_dict"):
            try:
                return df.to_dict(orient="records")
            except TypeError:
                # fallback for non-pandas .to_dict signatures
                return [dict(r) for r in df.to_dict()]
        # pyarrow Table
        if hasattr(df, "to_pydict"):
            d = df.to_pydict()
            keys = list(d.keys())
            n = len(d[keys[0]]) if keys else 0
            return [{k: d[k][i] for k in keys} for i in range(n)]
        raise TypeError(f"Unsupported return type from pyethnicity: {type(df)}")

    return df_records(df)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="fl",
        choices=["fl", "flg", "race"],
        help="Prediction function to use: fl (name only), flg (name+geo naive bayes), race (ensemble).",
    )
    parser.add_argument(
        "--geo-type",
        default=None,
        choices=["zcta", "tract"],
        help="Geography type (required for flg/race).",
    )
    parser.add_argument(
        "--geography",
        type=int,
        default=None,
        help="Geography value (e.g., ZCTA like 27106, or tract like 72153750502). Required for flg/race.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=1028,
        help="Batch size passed to the ONNX session (default from docs). :contentReference[oaicite:5]{index=5}",
    )
    parser.add_argument(
        "--names",
        nargs="*",
        default=[
            "Cangyuan Li",
            "Mark Luo",
            "Maria Garcia",
            "Jamal Washington",
        ],
        help='Names to run, e.g. --names "First Last" "First Last"',
    )
    args = parser.parse_args()

    rows = predict_for_names(
        args.names,
        model=args.model,
        geography=args.geography,
        geo_type=args.geo_type,
        chunksize=args.chunksize,
    )

    # Pretty print results
    for r in rows:
        # The DataFrame includes input columns plus probability columns. :contentReference[oaicite:6]{index=6}
        first = r.get("first_name", "")
        last = r.get("last_name", "")
        probs = {k: r[k] for k in r.keys() if k.lower() in {"asian", "black", "hispanic", "white"}}
        print(f"{first} {last}: {probs}")


if __name__ == "__main__":
    main()