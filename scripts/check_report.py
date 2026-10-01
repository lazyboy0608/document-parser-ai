import sqlite3
import json
import sys
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

print("=== EXCEL REPORT DATA ===")
df = pd.read_excel("data/reports/tong_hop_tai_lieu.xlsx")
for idx, col in enumerate(df.columns):
    print(f"Col {idx}: {col}")

for idx, row in df.iterrows():
    print("-" * 50)
    for col in df.columns:
        print(f"  {col}: {row[col]}")
