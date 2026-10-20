"""Shared helpers for tests. Everything here builds synthetic data only."""
import zipfile


def make_zip(path, csv_text, inner="data.csv"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(inner, csv_text)
