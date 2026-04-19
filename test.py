
import os
import random
import pandas as pd
from collections import Counter
from typing import List, Set, Dict
import matplotlib.pyplot as plt


def compile_column_frequencies(column_name, records_dir="output/records"):
    """
    Compiles and prints the frequency of values in `column_name` across all CSVs
    in `records_dir`, excluding rows where Lv == "C" (if an Lv column exists).
    """
    col_counter = Counter()

    for fname in os.listdir(records_dir):
        if not fname.lower().endswith(".csv"):
            continue
        path = os.path.join(records_dir, fname)
        try:
            df = pd.read_csv(path, dtype=str)
        except Exception as e:
            print(f"Failed to read {fname}: {e}")
            continue

        if column_name not in df.columns:
            print(f"[Warning] '{column_name}' column missing in {fname}")
            continue

        # Exclude rows where Lv == "C"
        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]

        # Count non-null values in the target column
        col_counter.update(df[column_name].dropna().tolist())

    # Print frequencies in descending order
    print(f"Frequencies for column '{column_name}':")
    for value, count in col_counter.most_common():
        print(f"{value}: {count}")


def find_ev_in_files(search_value, records_dir="output/records"):
    """
    Searches all CSVs in records_dir for the given Ev value (case-insensitive),
    ignoring rows where Lv == "C".
    Returns a list of up to 10 filenames that contain search_value in their 'Ev' column.
    """
    matching_files = []
    search_value = str(search_value).lower()

    for fname in os.listdir(records_dir):
        if not fname.lower().endswith(".csv"):
            continue
        path = os.path.join(records_dir, fname)
        try:
            df = pd.read_csv(path, dtype=str)
        except Exception as e:
            print(f"[Warning] Could not read {fname}: {e}")
            continue

        if "Ev" not in df.columns:
            print(f"[Warning] 'Ev' column missing in {fname}")
            continue

        # If there's an Lv column, drop rows where Lv == "C"
        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]

        # Case-insensitive comparison
        if df["Ev"].dropna().str.lower().eq(search_value).any():
            matching_files.append(fname)
            if len(matching_files) >= 10:
                break

    return matching_files


def find_longest_record(records_dir="output/records"):
    """
    Finds the CSV in records_dir with the most data rows (excluding header).
    Prints and returns (filename, row_count).
    """
    max_rows = 0
    longest_fname = None

    for fname in os.listdir(records_dir):
        if not fname.lower().endswith(".csv"):
            continue
        path = os.path.join(records_dir, fname)
        try:
            df = pd.read_csv(path, dtype=str)
        except Exception as e:
            print(f"[Warning] Could not read {fname}: {e}")
            continue

        row_count = len(df)
        if row_count > max_rows:
            max_rows = row_count
            longest_fname = fname

    if longest_fname:
        print(f"Longest record file: {longest_fname} ({max_rows} rows)")
    else:
        print("No CSV files found in the directory.")

    return longest_fname, max_rows


def plot_real_vs_random_simulation(compiled_file="compiled_data.csv"):
    """
    Plots real sit rates vs. random simulated sit rates by elim-round experience.
    """
    # Load real data
    df = pd.read_csv(compiled_file)
    df['ElimRounds'] = df['Total Elim Rounds'].astype(int)
    df['SitRate']    = df['Sit Rate (%)'].str.rstrip('%').astype(float)
    # Only consider judges with at least one elim round
    df = df[df['ElimRounds'] > 0]

    x = df['ElimRounds'].values
    y_real = df['SitRate'].values

    # Generate random-simulation sit rates
    y_sim = []
    for rounds in x:
        sits = 0
        for _ in range(rounds):
            # random judge vote
            judge_vote = random.choice(['aff', 'neg'])
            # random panel of 3 votes
            panel_votes = [random.choice(['aff', 'neg']) for _ in range(3)]
            aff_votes = panel_votes.count('aff')
            majority = 'aff' if aff_votes >= 2 else 'neg'
            # judge sits if their vote != panel majority
            if judge_vote != majority:
                sits += 1
        y_sim.append(sits / rounds * 100)

    # Plot real vs. simulated
    plt.figure()
    plt.scatter(x, y_real, label='Real')
    plt.scatter(x, y_sim, label='Simulated')
    plt.xlabel('Total Elim Rounds')
    plt.ylabel('Sit Rate (%)')
    plt.title('Real vs Random Simulated Sit Rates by Experience')
    plt.legend()
    plt.tight_layout()
    plt.show()


def collect_unique_events_from_csvs(folder_path: str) -> Dict[str, int]:
    event_counts: Dict[str, int] = {}

    for filename in os.listdir(folder_path):
        if not filename.lower().endswith('.csv'):
            continue

        full_path = os.path.join(folder_path, filename)
        try:
            df = pd.read_csv(full_path, dtype=str)
        except Exception as e:
            print(f"[Warning] Skipping {filename}: {e}")
            continue

        if "Ev" not in df.columns:
            continue

        # Filter for HS level if 'Lv' exists
        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]

        for val in df['Ev'].dropna():
            cleaned = val.lower().strip()
            event_counts[cleaned] = event_counts.get(cleaned, 0) + 1

    return event_counts


def classify_events_by_type(event_counts: Dict[str, int]):
    cx_exact = {"shir", "pel"}
    cx_fuzzy = {"cx", "pd", "pol"}
    pf_exact = set()
    pf_fuzzy = {"pf"}
    ld_exact = set()
    ld_fuzzy = {"ld"}

    def filter_events(events: Dict[str, int], exact_set: Set[str], fuzzy_set: Set[str], label: str) -> Dict[str, int]:
        exact_matches = {}
        fuzzy_matches = {}
        unmatched = {}

        for ev, count in events.items():
            if ev in exact_set:
                exact_matches[ev] = count
            else:
                unmatched[ev] = count

        truly_unmatched = {}
        for ev, count in unmatched.items():
            if any(fuzzy in ev for fuzzy in fuzzy_set):
                fuzzy_matches[ev] = count
            else:
                truly_unmatched[ev] = count

        def sort_dict(d): return sorted(d.items(), key=lambda x: x[1], reverse=True)

        print(f"\n--- {label.upper()} ---")
        print("Exact matches:")
        for ev, cnt in sort_dict(exact_matches):
            print(f"  {ev}: {cnt}")
        print("Fuzzy matches:")
        for ev, cnt in sort_dict(fuzzy_matches):
            print(f"  {ev}: {cnt}")
        print("Remaining:")
        for ev, cnt in sort_dict(truly_unmatched):
            print(f"  {ev}: {cnt}")

        return truly_unmatched

    remaining = event_counts
    remaining = filter_events(remaining, cx_exact, cx_fuzzy, "policy")
    remaining = filter_events(remaining, pf_exact, pf_fuzzy, "pf")
    remaining = filter_events(remaining, ld_exact, ld_fuzzy, "ld")

def count_tournaments_for_event(folder_path: str, event_value: str) -> Dict[str, int]:
    event_value = event_value.strip().lower()
    tournament_counts: Dict[str, int] = {}

    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(".csv"):
            continue

        full_path = os.path.join(folder_path, filename)
        try:
            df = pd.read_csv(full_path, dtype=str)
        except Exception as e:
            print(f"[Warning] Skipping {filename}: {e}")
            continue

        if "Ev" not in df.columns or "Tournament" not in df.columns:
            continue

        # Filter for HS level if 'Lv' exists
        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]

        # Drop missing values in columns of interest
        df = df.dropna(subset=["Ev", "Tournament"])

        # Normalize Ev column and filter rows
        df["Ev_norm"] = df["Ev"].str.lower().str.strip()
        filtered = df[df["Ev_norm"] == event_value]

        # Count tournaments
        for val in filtered["Tournament"]:
            name = val.lower().strip()
            tournament_counts[name] = tournament_counts.get(name, 0) + 1

    # Return sorted by count descending
    return dict(sorted(tournament_counts.items(), key=lambda x: x[1], reverse=True))

# Execute
#plot_real_vs_random_simulation("output/compiled_data.csv")

#compile_column_frequencies("Ev", records_dir="output/records")
#find_longest_record(records_dir="output/records")


value_to_search = "DCI"

hits = find_ev_in_files(value_to_search, "output/records")
if hits:
    print(f"Found '{value_to_search}' in:")
    for f in hits:
        print("  -", f)
else:
    print(f"No files contain '{value_to_search}'.")


#print(count_tournaments_for_event("output/records", "open"))
#event_counts = collect_unique_events_from_csvs("output/records")
#classify_events_by_type(event_counts)
