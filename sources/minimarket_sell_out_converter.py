from pathlib import Path
import locale
import re
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# Set Indonesian locale for number parsing (comma as decimal separator)
# ----------------------------------------------------------------------
try:
    locale.setlocale(locale.LC_NUMERIC, "id_ID.UTF-8")
except:
    try:
        locale.setlocale(locale.LC_NUMERIC, "Indonesian_Indonesia.1252")  # Windows
    except:
        print("Warning: Indonesian locale not available. Falling back to default.")
        pass


def parse_idr(value):
    """Parse Indonesian currency number exactly like M code:
    - Convert to string
    - Replace '.' with ','
    - Parse using Indonesian locale (or manual fallback)
    """
    if pd.isna(value) or value == "":
        return 0.0
    s = str(value).strip()
    if s == "":
        return 0.0
    # Replace dot with comma (the M code does this before parsing)
    s = s.replace(".", ",")
    try:
        # Try using locale (comma as decimal)
        return locale.atof(s)
    except:
        # Fallback: manually convert comma to dot and remove any thousand separators
        s = s.replace(",", ".")
        s = re.sub(r"[^0-9.-]", "", s)
        try:
            return float(s)
        except:
            return 0.0


# ----------------------------------------------------------------------
# Process a single folder for a given account
# ----------------------------------------------------------------------
def process_files(folder_path, account_name):
    folder = Path(folder_path)
    if not folder.exists():
        print(f"Folder not found: {folder_path}")
        return pd.DataFrame(
            columns=[
                "Date",
                "Branch",
                "Brand",
                "Category",
                "Product",
                "Value IDR",
                "Value Qty",
                "Account",
            ]
        )

    # Determine file extensions based on account
    if account_name == "INDOMARET":
        extensions = [".csv"]
    else:  # ALFAMART or ALFAMIDI
        extensions = [".csv", ".xlsx", ".xls"]

    all_files = []
    for ext in extensions:
        all_files.extend(folder.glob(f"*{ext}"))

    if not all_files:
        print(f"No matching files found in {folder_path}")
        return pd.DataFrame(
            columns=[
                "Date",
                "Branch",
                "Brand",
                "Category",
                "Product",
                "Value IDR",
                "Value Qty",
                "Account",
            ]
        )

    dfs = []
    for file_path in all_files:
        try:
            # ---------- Read raw data ----------
            if account_name == "INDOMARET":
                # Pipe-delimited CSV, no header, 7 columns expected
                df_raw = pd.read_csv(
                    file_path,
                    delimiter="|",
                    encoding="utf-8",
                    header=None,
                    dtype=str,
                )
                if df_raw.shape[1] < 7:
                    raise ValueError(f"Indomaret CSV has less than 7 columns: {file_path}")
                df_raw = df_raw.iloc[:, :7]
                df_raw.columns = [
                    "Date",
                    "Branch",
                    "Category",
                    "Brand",
                    "Product",
                    "Value IDR",
                    "Value Qty",
                ]
                # Remove header row if it contains "MONTH" in Date column
                df_raw = df_raw[df_raw["Date"] != "MONTH"]

            else:  # ALFAMART / ALFAMIDI
                if file_path.suffix.lower() == ".csv":
                    # Comma-delimited CSV, detect header
                    with open(file_path, "r", encoding="utf-8") as f:
                        first_line = f.readline()
                    has_header = "DATE" in first_line.upper()
                    if has_header:
                        df_raw = pd.read_csv(file_path, delimiter=",", encoding="utf-8", dtype=str)
                    else:
                        df_raw = pd.read_csv(
                            file_path, delimiter=",", encoding="utf-8", header=None, dtype=str
                        )
                        if df_raw.shape[1] >= 5:
                            df_raw = df_raw.iloc[:, :5]
                            df_raw.columns = [
                                "Date",
                                "Branch",
                                "Product",
                                "Value IDR",
                                "Value Qty",
                            ]
                        else:
                            raise ValueError(f"CSV has less than 5 columns: {file_path}")
                else:  # Excel file
                    excel_file = pd.ExcelFile(file_path)
                    sheet_name = (
                        "Sell Out" if "Sell Out" in excel_file.sheet_names else excel_file.sheet_names[0]
                    )
                    df_raw = pd.read_excel(file_path, sheet_name=sheet_name, dtype=str)

            # ---------- Standardise transformations ----------
            if account_name != "INDOMARET":
                # Normalise column names (case-insensitive)
                col_mapping = {}
                for expected in ["Date", "Branch", "Product", "Value IDR", "Value Qty"]:
                    matches = [c for c in df_raw.columns if c.lower() == expected.lower()]
                    if matches:
                        col_mapping[matches[0]] = expected
                if col_mapping:
                    df_raw = df_raw.rename(columns=col_mapping)
                for col in ["Date", "Branch", "Product", "Value IDR", "Value Qty"]:
                    if col not in df_raw.columns:
                        raise ValueError(f"Missing column '{col}' in {file_path}")

            # Convert Date column
            df_raw["Date"] = pd.to_datetime(df_raw["Date"], errors="coerce").dt.date

            # Trim and uppercase text columns
            for col in ["Branch", "Product"]:
                if col in df_raw.columns:
                    df_raw[col] = df_raw[col].astype(str).str.strip().str.upper()

            # Convert Value IDR and Value Qty using Indonesian number parser
            df_raw["Value IDR"] = df_raw["Value IDR"].apply(parse_idr)
            df_raw["Value Qty"] = df_raw["Value Qty"].apply(parse_idr)

            # ---------- Brand and Category logic ----------
            if account_name == "INDOMARET":
                # Already present, just clean
                df_raw["Brand"] = df_raw["Brand"].astype(str).str.strip().str.upper()
                df_raw["Category"] = df_raw["Category"].astype(str).str.strip().str.upper()
            else:
                # Add Brand column based on Product
                def get_brand(product):
                    p = str(product).upper()
                    if "SCARLETT" in p:
                        return "SCARLETT"
                    elif "WHITE INC" in p:
                        return "WHITE INC"
                    else:
                        return None

                df_raw["Brand"] = df_raw["Product"].apply(get_brand)

                # Add Category column based on Product
                def get_category(product):
                    p = str(product).upper()
                    if "B.LOT" in p or "HBL" in p:
                        return "BODY LOTION"
                    elif "B.SRM" in p:
                        return "BODY SERUM"
                    elif re.search(r"\bMEN\b", p) or "BIRU SAMUDRA" in p or "BIRU SMDR" in p or "SOUL" in p:
                        # Men's perfume is its own portal category (3239 MEN PARFUME EDT & EXTRAIT), same name as
                        # in the market share; Alfamidi sold Biru Samudra as "SCARLETT EDP BIRU SAMUDRA 30ML" before
                        # the "EXT MEN" names. \bMEN\b so WOMEN never matches.
                        return "MEN PARFUME EDT & EXTRAIT"
                    elif "EDP" in p or "EXT" in p or "XDP" in p:
                        # XDP (SCARLETT XDP SAFA ATHER 30ML, Alfamidi since 2026-03)
                        # has neither EDP nor EXT; it was silently dropped as uncategorised.
                        return "WOMEN PARFUME & EDT"
                    elif "SUNSCR" in p:
                        return "SUNSCREEN"
                    elif "FW" in p:
                        return "FACIAL WASH SOAP"
                    elif "MOIST" in p:
                        return "MOISTURIZER"
                    else:
                        return None

                df_raw["Category"] = df_raw["Product"].apply(get_category)

            # Add Account column
            df_raw["Account"] = account_name

            # Select and reorder final columns
            final_cols = [
                "Date",
                "Branch",
                "Brand",
                "Category",
                "Product",
                "Value IDR",
                "Value Qty",
                "Account",
            ]
            df_final = df_raw[final_cols].copy()

            dfs.append(df_final)

        except Exception as e:
            print(f"Error processing file {file_path}: {e}")
            empty_df = pd.DataFrame(
                columns=[
                    "Date",
                    "Branch",
                    "Brand",
                    "Category",
                    "Product",
                    "Value IDR",
                    "Value Qty",
                    "Account",
                ]
            )
            dfs.append(empty_df)

    if not dfs:
        return pd.DataFrame(
            columns=[
                "Date",
                "Branch",
                "Brand",
                "Category",
                "Product",
                "Value IDR",
                "Value Qty",
                "Account",
            ]
        )

    result = pd.concat(dfs, ignore_index=True)
    return result


# ----------------------------------------------------------------------
# Main execution
# ----------------------------------------------------------------------
if __name__ == "__main__":
    base_path = Path(r"D:\SCARLETT_512\SCARLETT-329\SOM\Data\Report\Sales\Minimarket")

    # Process each account folder
    indomaret_data = process_files(base_path / "Indomaret" / "Sell Out", "INDOMARET")
    alfamart_data = process_files(base_path / "Alfamart" / "Sell Out", "ALFAMART")
    alfamidi_data = process_files(base_path / "Alfamidi" / "Sell Out", "ALFAMIDI")

    # Combine all
    combined = pd.concat([indomaret_data, alfamart_data, alfamidi_data], ignore_index=True)

    # Remove rows where Date, Branch, or Category is null
    combined = combined.dropna(subset=["Date", "Branch", "Category"])

    # Ensure proper types
    combined["Date"] = pd.to_datetime(combined["Date"]).dt.date
    combined["Value Qty"] = combined["Value Qty"].astype(float)
    combined["Value IDR"] = combined["Value IDR"].astype(float)

    # ------------------------------------------------------------------
    # Verification: print total Value IDR (for debugging)
    # ------------------------------------------------------------------
    total_idr = combined["Value IDR"].sum()
    print(f"\nTotal Value IDR (all accounts): {total_idr:,.2f}")
    print(f"Expected (if known): 18,853,415,408.00")
    print(f"Difference: {total_idr - 18853415408:,.2f}\n")

    # Output paths
    output_parquet = base_path / "Minimarket_Sell_Out.parquet"
    output_csv = base_path / "Minimarket_Sell_Out.csv"

    # Write files
    combined.to_parquet(output_parquet, index=False, engine="pyarrow")
    combined.to_csv(output_csv, index=False, encoding="utf-8-sig")

    print(f"Successfully written:\n{output_parquet}\n{output_csv}")
    print(f"Total rows: {len(combined)}")