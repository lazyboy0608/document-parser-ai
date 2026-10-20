import sqlite3
import json
import sys
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

report_path = sys.argv[1] if len(sys.argv) > 1 else "data/reports/tong_hop_tai_lieu.xlsx"
print(f"=== EXCEL REPORT DATA ({report_path}) ===")
df = pd.read_excel(report_path)
for idx, col in enumerate(df.columns):
    print(f"Col {idx}: {col}")

for idx, row in df.iterrows():
    print("-" * 50)
    for col in df.columns:
        print(f"  {col}: {row[col]}")
